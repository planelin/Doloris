"""
afk_supervisor.sessions.rollout — Rollout 轨迹事件感知与生命周期契约
===================================================================
解析完整 Codex rollout JSONL 事件状态机；
判断当前回合是否结束 (task_complete / turn_aborted)；
提取实时思维链、工具调用状态以及最后的 agent 文本。
"""

import json
import re
import time
from pathlib import Path
from typing import Tuple


def codex_handoff_state(path) -> dict:
    """Read typed lifecycle and tool events to assess a kill handoff boundary.

    High-risk boundary: unreturned tool calls in flight (pending_calls).
    All other states (idle, reasoning, message generation, turn complete, turn aborted, etc.)
    are safe to directly kill because model-side generation can be cleanly resumed by codex resume.
    """
    pending = set()
    anonymous = 0
    known = False
    turn_ended = False
    last_event = ""
    invalid = False
    last_agent_message = ""
    fork_parent_id = ""
    saw_thread_settings = False
    only_fork_metadata = True
    try:
        p = Path(path)
        before = p.stat()
        with p.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                if not line.endswith("\n"):
                    invalid = True
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        raise ValueError("event must be an object")
                    payload = obj.get("payload") or {}
                    if not isinstance(payload, dict):
                        raise ValueError("payload must be an object")
                except (ValueError, TypeError):
                    invalid = True
                    continue
                kind = obj.get("type")
                event = payload.get("type", "")
                if kind == "session_meta":
                    parent_id = payload.get("forked_from_id")
                    session_id = payload.get("session_id") or payload.get("id")
                    if isinstance(parent_id, str) and parent_id.strip() and isinstance(session_id, str) and session_id.strip():
                        fork_parent_id = parent_id.strip()
                    continue
                if event == "thread_settings_applied":
                    saw_thread_settings = True
                    continue
                if event in ("token_count", "item_completed") or kind in (
                    "token_usage_record", "world_state", "turn_context", "compacted"
                ):
                    only_fork_metadata = False
                    continue
                only_fork_metadata = False
                last_event = event or kind or ""
                if kind == "event_msg" and event in ("task_complete", "turn_aborted"):
                    known = True
                    turn_ended = True
                    last_agent_message = payload.get("last_agent_message", "") if event == "task_complete" else ""
                elif kind == "event_msg" and event in ("task_started", "turn_started", "user_message", "agent_message", "agent_reasoning", "agent_reasoning_raw_content"):
                    known = True
                    turn_ended = False
                elif kind == "response_item" and event in ("reasoning", "message", "compaction"):
                    known = True
                    turn_ended = False
                elif kind == "response_item" and (
                    event.endswith("_call") or event in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call", "tool_search_call")
                ):
                    known = True
                    turn_ended = False
                    if payload.get("status") != "completed":
                        call_id = payload.get("call_id") or payload.get("id")
                        if call_id:
                            pending.add(str(call_id))
                        else:
                            anonymous += 1
                elif kind == "response_item" and (
                    event.endswith("_output") or event in ("function_call_output", "custom_tool_call_output", "local_shell_output", "tool_search_output")
                ):
                    known = True
                    turn_ended = False
                    call_id = payload.get("call_id") or payload.get("id")
                    if call_id:
                        pending.discard(str(call_id))
                    # An uncorrelated output cannot clear another outstanding call.
                else:
                    turn_ended = False
        after = p.stat()
        age = round(max(0, time.time() - after.st_mtime), 1)
        pending_ids = sorted(pending) + ["<missing-call-id>"] * anonymous
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            state, reason = "unknown", "读取期间轨迹发生变化，重新采样"
        elif invalid:
            state, reason = "unknown", "轨迹包含损坏/尚未写完的 JSON 事件"
        elif pending_ids:
            state, reason = "unsafe", f"工具调用尚未返回: {', '.join(pending_ids)}"
        elif fork_parent_id and saw_thread_settings and only_fork_metadata and not known:
            state = "safe"
            turn_ended = True
            reason = "新建 fork 会话尚无回合，可安全接管"
        elif not known:
            state, reason = "unknown", "没有可确认生命周期或工具边界的事件"
        else:
            state, reason = "safe", "已记录的工具调用均已返回，可中断模型侧生成（不代表任务完成）"
        return dict(state=state, reason=reason, last_event=last_event,
                    pending_calls=pending_ids, file_age_sec=age, turn_ended=turn_ended,
                    last_agent_message=last_agent_message, event_offset=before.st_size,
                    never_started=(state == "safe" and not known))
    except (OSError, TypeError, UnicodeError) as exc:
        return dict(state="unknown", reason=f"无法读取会话轨迹: {exc}",
                    last_event=last_event, pending_calls=sorted(pending), file_age_sec=None,
                    turn_ended=False, never_started=False)


def rollout_tail_state(path, tail=16384) -> str:
    """Compatibility wrapper; never infer safety from a truncated tail or mtime."""
    return codex_handoff_state(path)["state"]


def peek_rollout_activity(rollout_path, max_bytes=65536) -> str:
    """提取 Codex rollout 尾部的实时动态 (思考链/工具调用/阶段产出)，消除黑盒盲等感。"""
    if not rollout_path:
        return ""
    p = Path(rollout_path)
    if not p.exists():
        return ""
    try:
        size = p.stat().st_size
        if size == 0:
            return ""
        with open(p, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        if size > max_bytes and lines:
            lines.pop(0)

        for raw in reversed(lines):
            raw = raw.strip()
            if not raw or not raw.startswith("{"):
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue

            t = d.get("type")
            payload = d.get("payload") or {}
            ptype = payload.get("type") if isinstance(payload, dict) else ""

            if ptype == "reasoning":
                content = payload.get("content") or []
                texts = [c["text"].strip() for c in content if isinstance(c, dict) and c.get("text")]
                if texts:
                    sublines = [s.strip() for s in " ".join(texts).splitlines() if s.strip()]
                    snip = sublines[-1] if sublines else ""
                    if len(snip) > 80:
                        snip = snip[:77] + "..."
                    return f"[思维] {snip}"
            elif ptype in ("function_call", "custom_tool_call"):
                name = payload.get("name", "tool")
                raw_args = payload.get("arguments") or payload.get("input") or ""
                detail = ""
                if isinstance(raw_args, str) and raw_args:
                    try:
                        args_obj = json.loads(raw_args)
                        if isinstance(args_obj, dict):
                            for k in ("cmd", "command", "path", "file_path", "code"):
                                if k in args_obj:
                                    val = str(args_obj[k]).strip()
                                    detail = val.splitlines()[0] if val else ""
                                    break
                    except Exception:
                        m = re.search(r'(?:cmd|command|path|file_path)\s*:\s*["\']([^"\']+)["\']', raw_args)
                        if m:
                            detail = m.group(1).strip().splitlines()[0]
                        else:
                            detail = raw_args.strip().splitlines()[0] if raw_args.strip() else ""
                elif isinstance(raw_args, dict):
                    for k in ("cmd", "command", "path", "file_path", "code"):
                        if k in raw_args:
                            val = str(raw_args[k]).strip()
                            detail = val.splitlines()[0] if val else ""
                            break
                if not detail and raw_args:
                    detail = str(raw_args).strip().splitlines()[0]
                if len(detail) > 60:
                    detail = detail[:57] + "..."
                return f"[工具: {name}] {detail}" if detail else f"[工具: {name}]"
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                return "[工具执行完毕，模型分析中]"
            elif ptype == "message":
                role = payload.get("role")
                if role == "assistant":
                    content = payload.get("content") or []
                    texts = [c["text"].strip() for c in content if isinstance(c, dict) and c.get("text")]
                    if texts:
                        joined = " ".join(texts).replace("\n", " ").strip()
                        if len(joined) > 80:
                            joined = joined[:77] + "..."
                        return f"[输出] {joined}"
            elif ptype == "task_complete" or (t == "event_msg" and ptype == "task_complete"):
                return "[任务完成]"
            elif ptype == "turn_aborted":
                return "[回合打断]"
    except Exception:
        pass
    return ""


def codex_session_state(rollout_path) -> dict:
    """Single lifecycle contract: running / stopped / unknown, never silence."""
    snapshot = codex_handoff_state(rollout_path)
    if snapshot["state"] == "unknown":
        status = "unknown"
    elif snapshot["state"] == "safe" and snapshot["turn_ended"]:
        status = "stopped"
    else:
        status = "running"
    return {**snapshot, "status": status}


def is_codex_working(rollout_path: Path) -> Tuple[bool, str, str]:
    """Legacy boolean gate: only confirmed stopped returns False.

    Unknown is deliberately blocking. New consumers may inspect the three-state
    codex_session_state contract; errors/partial writes never authorize work.
    """
    snapshot = codex_session_state(rollout_path)
    stopped = snapshot["status"] == "stopped"
    reason = (f"已确认回合停止 ({snapshot['last_event']})" if stopped else
              f"{snapshot['status']}: {snapshot['reason']}")
    return not stopped, reason, snapshot.get("last_agent_message", "") if stopped else ""


def wait_for_codex_idle(rollout_path: Path, timeout_sec: float = 60.0) -> bool:
    """阻塞等待 Codex 退出活跃工作状态进入空闲。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        is_working, reason, _ = is_codex_working(rollout_path)
        if not is_working:
            return True
        time.sleep(2)
    return False


def codex_rollout_is_turn_complete(rollout_path: Path) -> Tuple[bool, str]:
    """向后兼容接口：判断当前回合是否真正结束。"""
    is_working, reason, last_msg = is_codex_working(rollout_path)
    return (not is_working), last_msg


def read_rollout_last_message(rollout_path: Path, window_bytes: int = 262144) -> str:
    """从 rollout JSONL 中提取最新的 assistant / agent_message 文本。"""
    if not rollout_path or not Path(rollout_path).exists():
        return ""
    p = Path(rollout_path)
    try:
        sz = p.stat().st_size
    except OSError:
        return ""
    if sz <= 0:
        return ""

    def _parse_lines(lines):
        last_m = ""
        for line in lines:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            pyld = obj.get("payload")
            if (obj.get("type") == "response_item" and isinstance(pyld, dict)
               and pyld.get("type") == "message" and pyld.get("role") == "assistant"):
                txt = " ".join(c.get("text", "") for c in pyld.get("content", []) if isinstance(c, dict))
                if txt.strip():
                    last_m = txt.strip()
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                t = item.get("text", "")
                if t.strip():
                    last_m = t.strip()
        return last_m

    try:
        with open(p, "rb") as f:
            if sz > window_bytes:
                f.seek(sz - window_bytes)
                raw = f.read(window_bytes)
                text = raw.decode("utf-8", errors="replace")
                lines = text.splitlines()
                if len(lines) > 1:
                    lines = lines[1:]
                res = _parse_lines(lines)
                if res:
                    return res
                f.seek(0)
            text = f.read().decode("utf-8", errors="replace")
            return _parse_lines(text.splitlines())
    except Exception:
        return ""
