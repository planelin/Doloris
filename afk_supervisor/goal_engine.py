"""
afk_supervisor.goal_engine — 独立 Goal 目标模式监管引擎
======================================================
专注于中途离席场景的自主 Goal 接管，分三阶段流水线全自动推进：
1. 阶段 1 (目标设立)：自动探测会话是否已有活跃目标；已设立则跳过目标指令注入，未设立则由 60s 倒计时或懒人模式注入纯净 `/goal [目标]`；
2. 阶段 2 (制定计划)：精准识别方案草案与交互选择题，自动选择推荐项（(Recommended) / (推荐) / 首项）并自动批准计划草案；
3. 阶段 3 (长程执行)：计划制定完成后自动切入长程任务守护，保持防熄屏，等待任务终态完工。
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from afk_supervisor.models import DeadlineBudget
from afk_supervisor.platform.gui import _navigate_target, ensure_codex_window_restored, find_best_codex_window, inject_into_codex_gui
from afk_supervisor.platform.process import WorkspaceSupervisorLock, log
from afk_supervisor.platform.windows import set_keep_awake
from afk_supervisor.reporting import generate_final_report, send_terminal_notification
from afk_supervisor.sessions.discovery import (
    clean_session_id,
    find_codex_session_by_id,
    list_recent_codex_sessions,
    load_codex_thread_titles,
    read_session_title,
)
from afk_supervisor.sessions.rollout import (
    codex_session_state,
    is_codex_working,
    peek_rollout_activity,
    read_rollout_last_message,
)
from afk_supervisor.state import SupervisorState
from afk_supervisor.l2.bridge import (
    bind_agy_conversation_for_codex,
    discover_antigravity_bridge,
    discover_antigravity_project_id,
    get_agy_brain_dir,
    get_agy_conversation_for_codex,
)


def extract_recent_dialogue_summary(rollout_path: Optional[Path], max_turns: int = 6) -> str:
    """从 rollout 中提取最近几轮真实对话内容用于目标归纳，过滤环境注入与内部提示。"""
    if not rollout_path or not Path(rollout_path).exists():
        return ""
    messages: List[str] = []

    def clean_user_text(raw_text: str) -> Optional[str]:
        txt = raw_text.strip()
        if not txt:
            return None
        # 严格过滤系统及内部提示注入
        if any((pfx in txt or txt.startswith(pfx)) for pfx in (
            "<environment_context>", "<codex_internal_context", "<turn_context>", "<collaboration_mode",
            "<turn_aborted", "<app-context>", "<skills_instructions>", "<permissions instructions>"
        )):
            return None
        # 解析提问问答组件中的用户答案
        if "<send_user_message_question_reply>" in txt:
            try:
                m = re.search(r"<send_user_message_question_reply>\s*(\[.*?\])\s*</send_user_message_question_reply>", txt, re.DOTALL)
                if m:
                    arr = json.loads(m.group(1))
                    ans_parts = []
                    for item in arr:
                        q = item.get("question", "")
                        a = item.get("answer", "")
                        if a:
                            ans_parts.append(f"{q}: {a}" if q else str(a))
                    if ans_parts:
                        return "; ".join(ans_parts)
            except Exception:
                pass
        return txt

    try:
        with open(rollout_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                payload = obj.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                ptype = payload.get("type", "")
                if ptype == "user_message":
                    content = payload.get("message") or payload.get("content") or ""
                    if content and isinstance(content, str):
                        clean_u = clean_user_text(content)
                        if clean_u:
                            messages.append(f"User: {clean_u}")
                elif ptype == "message" and payload.get("role") == "user":
                    content_list = payload.get("content") or []
                    for c in content_list:
                        if isinstance(c, dict) and c.get("text"):
                            clean_u = clean_user_text(c["text"])
                            if clean_u:
                                messages.append(f"User: {clean_u}")
                elif ptype == "message" and payload.get("role") == "assistant":
                    content_list = payload.get("content") or []
                    for c in content_list:
                        if isinstance(c, dict) and c.get("text"):
                            snip = c["text"].strip()
                            if snip.startswith("<") or "<codex_internal_context" in snip or "<environment_context" in snip:
                                continue
                            if len(snip) > 120:
                                snip = snip[:117] + "..."
                            messages.append(f"Assistant: {snip}")
    except Exception:
        pass

    recent = messages[-max_turns:] if len(messages) >= max_turns else messages
    return "\n".join(recent)


DUMMY_GOAL_MARKERS = {
    "完成当前任务所有未尽要求与测试",
    "推进当前任务",
    "自主推进计划",
    "完成当前任务",
    "推进并完成",
    "继续推进目标",
    "__LAZY__",
    "__ALREADY_SET__",
}


def get_existing_thread_goal(rollout_path: Optional[Path]) -> Optional[str]:
    """检测会话中是否已由用户或系统设立了活跃 Goal 目标。过滤占位符与内部标记。"""
    if not rollout_path or not Path(rollout_path).exists():
        return None
    try:
        with open(rollout_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            for line in reversed(lines):
                if "thread_goal_updated" in line:
                    try:
                        obj = json.loads(line)
                        p = obj.get("payload") or {}
                        g = p.get("goal") or {}
                        objective = g.get("objective") or ""
                        status = (g.get("status") or "").lower()
                        if objective and isinstance(objective, str) and (not status or status in ("active", "paused", "blocked")):
                            clean = objective.strip()
                            for prefix in ("/goal ", "推进并完成：", "推进并完成:", "完成：", "完成:"):
                                if clean.startswith(prefix):
                                    clean = clean[len(prefix):].strip()
                            if len(clean) > 2 and clean not in DUMMY_GOAL_MARKERS:
                                return clean
                    except Exception:
                        pass
                if "update_goal" in line:
                    try:
                        obj = json.loads(line)
                        p = obj.get("payload") or {}
                        if p.get("type") in ("function_call_output", "custom_tool_call_output"):
                            out_str = p.get("output") or ""
                            if isinstance(out_str, str) and '"objective"' in out_str:
                                out_data = json.loads(out_str)
                                g = out_data.get("goal") or {}
                                objective = g.get("objective") or ""
                                status = (g.get("status") or "").lower()
                                if objective and isinstance(objective, str) and (not status or status in ("active", "paused", "blocked")):
                                    clean = objective.strip()
                                    for prefix in ("/goal ", "推进并完成：", "推进并完成:", "完成：", "完成:"):
                                        if clean.startswith(prefix):
                                            clean = clean[len(prefix):].strip()
                                    if len(clean) > 2 and clean not in DUMMY_GOAL_MARKERS:
                                        return clean
                    except Exception:
                        pass
    except Exception:
        pass
    return None


def check_plan_status(rollout_path: Optional[Path]) -> str:
    """识别当前 Goal 模式所处阶段：
    - 'not_set': 目标尚未设立 (阶段 1)
    - 'planning': 计划制定与提问访谈中 (阶段 2)
    - 'executing': 计划已制定完成，进入长程任务执行 (阶段 3)
    """
    if not rollout_path or not Path(rollout_path).exists():
        return "not_set"

    events: List[Dict[str, Any]] = []
    try:
        with open(rollout_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return "not_set"

    if not events:
        return "not_set"

    # 找到最近一次设立目标的事件索引（忽略占位目标）
    goal_idx = -1
    for idx in range(len(events) - 1, -1, -1):
        p = events[idx].get("payload") or {}
        if isinstance(p, dict) and p.get("type") == "thread_goal_updated":
            g = p.get("goal") or {}
            obj = (g.get("objective") or "").strip()
            for prefix in ("/goal ", "推进并完成：", "推进并完成:", "完成：", "完成:"):
                if obj.startswith(prefix):
                    obj = obj[len(prefix):].strip()
            if obj and obj not in DUMMY_GOAL_MARKERS:
                goal_idx = idx
                break

    if goal_idx < 0:
        return "not_set"

    # 检查目标设立之后的事件序列
    has_proposed_plan = False
    plan_proposed_idx = -1
    has_plan_approval = False
    has_execution_tools = False

    EXEC_TOOL_NAMES = {
        "exec_command", "apply_diff", "write_to_file", "replace_file_content",
        "modify_file", "edit_file", "bash", "shell", "run_shell_command", "run_command"
    }

    for idx in range(goal_idx, len(events)):
        ev = events[idx]
        p = ev.get("payload") or {}
        if not isinstance(p, dict):
            continue

        ptype = p.get("type", "")
        name = p.get("name", "")

        # 检查是否提出了计划草案
        if ptype == "message" and p.get("role") == "assistant":
            content = p.get("content") or []
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("text")]
            full_t = " ".join(texts)
            if "<proposed_plan>" in full_t or "## 实施计划" in full_t or "### 实施计划" in full_t or "## 方案" in full_t:
                has_proposed_plan = True
                plan_proposed_idx = idx

        # 检查是否有对计划的确认与批准
        if has_proposed_plan and idx > plan_proposed_idx:
            if (ptype == "message" and p.get("role") == "user") or (ev.get("type") == "event_msg" and ptype == "user_message"):
                content = p.get("content") or p.get("message") or ""
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                txt = str(content)
                if any(k in txt for k in ("PLEASE IMPLEMENT THIS PLAN", "请按计划执行", "同意并继续", "按此计划", "同意执行")):
                    has_plan_approval = True

        # 检查是否有执行类工具派发
        if ptype in ("function_call", "custom_tool_call") and name in EXEC_TOOL_NAMES:
            has_execution_tools = True

    # 判定阶段：
    # 如果提出了方案草案：必须满足“已批准”或“已开始执行工具”，才算 executing，否则仍是 planning
    if has_proposed_plan:
        if has_plan_approval or has_execution_tools:
            return "executing"
        return "planning"

    # 如果未提出专门的 proposed_plan，一旦执行工具开始工作，即为 executing
    if has_execution_tools:
        return "executing"

    # 刚确立目标，尚未开始执行工具，处于阶段 2 planning
    return "planning"


def get_codex_session_id_from_rollout(rollout_path: Optional[Path]) -> Optional[str]:
    """从 rollout 路径或内容中提取 Codex 任务 Session ID。"""
    if not rollout_path:
        return None
    p = Path(rollout_path)
    try:
        from afk_supervisor.sessions.discovery import _read_meta, clean_session_id
        if p.exists():
            meta = _read_meta(p)
            if meta and meta[0]:
                return clean_session_id(meta[0])
    except Exception:
        pass
    m = re.search(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', str(p))
    if m:
        return m.group(1).lower()
    return None


def extract_goal_via_agy_agent(
    rollout_path: Optional[Path],
    title: str = "",
    agy_mgr: Optional[Any] = None,
    run_dir: Optional[Path] = None,
    timeout_sec: float = 15.0,
    codex_session_id: Optional[str] = None,
) -> Optional[str]:
    """尝试通过 Antigravity 语言服务提炼简洁目标。
    严格复用与 Codex 任务 1:1 绑定的专属 AGY 会话。
    """
    try:
        if agy_mgr is not None and hasattr(agy_mgr, "ensure_bridge"):
            csrf, ports, agexe = agy_mgr.ensure_bridge(timeout=5)
        else:
            csrf, ports, agexe = discover_antigravity_bridge()
    except Exception:
        csrf, ports, agexe = None, [], None

    if not csrf or not ports or not agexe or not Path(agexe).exists():
        return None

    summary = extract_recent_dialogue_summary(rollout_path, max_turns=6)
    clean_t = (title or "").strip()
    if not summary and not clean_t:
        return None

    prompt_text = (
        "请根据以下 Codex 任务上下文，用一句话总结最终需要交付的纯净目标（30字以内，不要任何'推进并完成'等前缀，不要输出/goal，仅输出目标本身）：\n"
        f"任务标题：{clean_t}\n"
        f"最近对话：\n{summary}\n"
    )

    project_id = None
    try:
        project_id = discover_antigravity_project_id(Path(agexe), csrf, ports)
    except Exception:
        pass

    codex_sid = codex_session_id or (agy_mgr.codex_session_id if agy_mgr and agy_mgr.codex_session_id != "unknown" else None) or get_codex_session_id_from_rollout(rollout_path)
    existing_cid = get_agy_conversation_for_codex(codex_sid, run_dir=run_dir) if codex_sid else None
    if not existing_cid and agy_mgr and agy_mgr.cid:
        existing_cid = agy_mgr.cid

    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    initial_line_count = 0
    brain_dir = get_agy_brain_dir()

    if existing_cid:
        cid = existing_cid
        t_path = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if t_path.exists():
            try:
                initial_line_count = len(t_path.read_text(encoding="utf-8", errors="replace").splitlines())
            except Exception:
                initial_line_count = 0
        cmd = [str(agexe), "agentapi", "send-message", existing_cid, prompt_text]
        log(f"GOAL_AGY  复用 Codex 专属 AGY 会话 ({existing_cid[:8]}) 提炼目标...")
    else:
        cid = None
        round_title = f"[Doloris] Codex: {(clean_t or (codex_sid[:8] if codex_sid else '目标提炼'))[:20]}"
        cmd = [str(agexe), "agentapi", "new-conversation", "--model=flash", f"--title={round_title}", prompt_text]
        log(f"GOAL_AGY  未找到已绑定会话，为 Codex [{codex_sid[:8] if codex_sid else '任务'}] 创建专属 AGY 会话...")

    for port in ports:
        try:
            env = dict(os.environ)
            env["ANTIGRAVITY_CSRF_TOKEN"] = csrf
            env["ANTIGRAVITY_LS_ADDRESS"] = f"127.0.0.1:{port}"
            if project_id:
                env["ANTIGRAVITY_PROJECT_ID"] = project_id
            res = subprocess.run(cmd, capture_output=True, timeout=min(timeout_sec, 12), env=env, creationflags=no_win)
            out = (res.stdout or b"").decode("utf-8", errors="replace")
            if not existing_cid:
                m = re.search(r'"conversationId"\s*:\s*"([^"]+)"', out)
                if m:
                    cid = m.group(1)
                    if codex_sid:
                        bind_agy_conversation_for_codex(codex_sid, cid, run_dir=run_dir)
                    if agy_mgr is not None and hasattr(agy_mgr, "persist_cid"):
                        agy_mgr.persist_cid(cid)
                    break
            else:
                if res.returncode == 0 and '"error"' not in out:
                    break
        except Exception:
            continue

    if not cid:
        return None

    try:
        t_path = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if t_path.exists():
                try:
                    all_lines = t_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    lines = all_lines[initial_line_count:]
                    for line in reversed(lines):
                        if '"PLANNER_RESPONSE"' in line and 'DONE' in line:
                            obj = json.loads(line)
                            if obj.get("type") != "PLANNER_RESPONSE" or obj.get("status") != "DONE":
                                continue
                            content = obj.get("content", "").strip()
                            if content:
                                for pfx in ("/goal ", "/goal", "目标：", "目标:", "推进并完成：", "推进并完成:", "完成：", "完成:"):
                                    if content.startswith(pfx):
                                        content = content[len(pfx):].strip()
                                content = content.strip('`"\'“” \n\r\t')
                                lines_c = [l.strip() for l in content.splitlines() if l.strip()]
                                if lines_c:
                                    cand = lines_c[0].strip('`"\'“” \n\r\t')
                                    for pfx in ("/goal ", "/goal", "目标：", "目标:", "推进并完成：", "推进并完成:", "完成：", "完成:"):
                                        if cand.startswith(pfx):
                                            cand = cand[len(pfx):].strip()
                                    if len(cand) > 3:
                                        log(f"GOAL_AGY  AGY 提炼目标成功: [{cand}]")
                                        return cand[:60]
                except Exception:
                    pass
            time.sleep(0.5)
    except Exception as exc:
        log(f"GOAL_AGY  AGY 提炼异常，转入启发式回退: {exc}")
    return None


def resolve_stalled_goal_via_agy(
    rollout_path: Optional[Path],
    title: str = "",
    last_msg: str = "",
    agy_mgr: Optional[Any] = None,
    run_dir: Optional[Path] = None,
    timeout_sec: float = 15.0,
    codex_session_id: Optional[str] = None,
) -> Optional[str]:
    """在目标停滞 (Goal Stalled / Blocked) 状态下，委托专属 AGY 进行技术拍板并破局。"""
    try:
        if agy_mgr is not None and hasattr(agy_mgr, "ensure_bridge"):
            csrf, ports, agexe = agy_mgr.ensure_bridge(timeout=5)
        else:
            csrf, ports, agexe = discover_antigravity_bridge()
    except Exception:
        csrf, ports, agexe = None, [], None

    if not csrf or not ports or not agexe or not Path(agexe).exists():
        return None

    clean_t = (title or "").strip()
    prompt_text = (
        "Codex 目标模式长任务在执行中遇到阻塞/停滞，模型正在等待确认或选择以继续推进：\n"
        f"任务标题：{clean_t}\n"
        f"Codex 最后一轮消息：\n{last_msg}\n\n"
        "请作为技术主管代表用户做出明确决断以破除阻塞（例如在提供的选项中选出最合理的推荐项，或给出具体明确的执行指令）。\n"
        "请直接输出你要发送给 Codex 的简短回复内容（10字以内为佳，例如选项编号 '1' 或 '1. 深色科技感'，严禁输出代码块或多余解释）："
    )

    project_id = None
    try:
        project_id = discover_antigravity_project_id(Path(agexe), csrf, ports)
    except Exception:
        pass

    codex_sid = codex_session_id or (agy_mgr.codex_session_id if agy_mgr and agy_mgr.codex_session_id != "unknown" else None) or get_codex_session_id_from_rollout(rollout_path)
    existing_cid = get_agy_conversation_for_codex(codex_sid, run_dir=run_dir) if codex_sid else None
    if not existing_cid and agy_mgr and agy_mgr.cid:
        existing_cid = agy_mgr.cid

    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    initial_line_count = 0
    brain_dir = get_agy_brain_dir()

    if existing_cid:
        cid = existing_cid
        t_path = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if t_path.exists():
            try:
                initial_line_count = len(t_path.read_text(encoding="utf-8", errors="replace").splitlines())
            except Exception:
                initial_line_count = 0
        cmd = [str(agexe), "agentapi", "send-message", existing_cid, prompt_text]
        log(f"GOAL_AGY  复用 Codex 专属 AGY 会话 ({existing_cid[:8]}) 破局拍板...")
    else:
        cid = None
        round_title = f"[Doloris] Codex: {(clean_t or (codex_sid[:8] if codex_sid else '目标破局'))[:20]}"
        cmd = [str(agexe), "agentapi", "new-conversation", "--model=flash", f"--title={round_title}", prompt_text]
        log(f"GOAL_AGY  未找到已绑定会话，为 Codex [{codex_sid[:8] if codex_sid else '任务'}] 创建专属 AGY 会话...")

    for port in ports:
        try:
            env = dict(os.environ)
            env["ANTIGRAVITY_CSRF_TOKEN"] = csrf
            env["ANTIGRAVITY_LS_ADDRESS"] = f"127.0.0.1:{port}"
            if project_id:
                env["ANTIGRAVITY_PROJECT_ID"] = project_id
            res = subprocess.run(cmd, capture_output=True, timeout=min(timeout_sec, 12), env=env, creationflags=no_win)
            out = (res.stdout or b"").decode("utf-8", errors="replace")
            if not existing_cid:
                m = re.search(r'"conversationId"\s*:\s*"([^"]+)"', out)
                if m:
                    cid = m.group(1)
                    if codex_sid:
                        bind_agy_conversation_for_codex(codex_sid, cid, run_dir=run_dir)
                    if agy_mgr is not None and hasattr(agy_mgr, "persist_cid"):
                        agy_mgr.persist_cid(cid)
                    break
            else:
                if res.returncode == 0 and '"error"' not in out:
                    break
        except Exception:
            continue

    if not cid:
        return None

    try:
        t_path = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if t_path.exists():
                try:
                    all_lines = t_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    lines = all_lines[initial_line_count:]
                    for line in reversed(lines):
                        if '"PLANNER_RESPONSE"' in line and 'DONE' in line:
                            obj = json.loads(line)
                            if obj.get("type") != "PLANNER_RESPONSE" or obj.get("status") != "DONE":
                                continue
                            content = obj.get("content", "").strip()
                            if content:
                                content = content.strip('`"\'“” \n\r\t')
                                lines_c = [l.strip() for l in content.splitlines() if l.strip()]
                                if lines_c:
                                    cand = lines_c[0].strip('`"\'“” \n\r\t')
                                    if cand.startswith("回复"):
                                        cand = cand.replace("回复", "").strip("：: ")
                                    if cand:
                                        log(f"GOAL_AGY  AGY 破局拍板成功: [{cand}]")
                                        return cand[:50]
                except Exception:
                    pass
            time.sleep(0.5)
    except Exception as exc:
        log(f"GOAL_AGY  AGY 破局拍板异常: {exc}")
    return None


def extract_clean_goal(
    rollout_path: Optional[Path],
    title: str = "",
    agy_mgr: Optional[Any] = None,
    run_dir: Optional[Path] = None,
    codex_session_id: Optional[str] = None,
) -> str:
    """在懒人模式下，由 AGY 智能提炼纯净交付目标，或启发式优雅回退。

    绝不添加“推进并完成：”等多余前缀，下发提示词严格约束为 /goal [目标]。
    优先级：
    1. rollout 中已有的 thread_goal_updated 目标；
    2. 核心：通过 Antigravity AGY 深度阅读任务上下文并智能提炼纯净目标；
    3. 会话原生 title (若具备业务语义)；
    4. 最近一轮用户输入的有效文本（去除环境上下文和系统指令）。
    """
    # 1. 尝试从 rollout 中读取已有 goal 的 objective
    existing = get_existing_thread_goal(rollout_path)
    if existing:
        return existing

    # 2. 核心：无条件优先调用 AGY 语言服务提炼目标
    agy_res = extract_goal_via_agy_agent(
        rollout_path,
        title=title,
        agy_mgr=agy_mgr,
        run_dir=run_dir,
        timeout_sec=25.0,
        codex_session_id=codex_session_id,
    )
    if agy_res:
        return agy_res

    # 3. 检查会话 title
    clean_title = (title or "").strip()
    for prefix in ("/goal ", "推进并完成：", "推进并完成:", "完成：", "完成:"):
        if clean_title.startswith(prefix):
            clean_title = clean_title[len(prefix):].strip()
    if clean_title and clean_title != "无标题任务" and len(clean_title) > 3:
        clean_title = clean_title.rstrip("….").strip()
        if clean_title and not clean_title.startswith("读取codex://"):
            return clean_title

    # 4. 提取最近一轮用户诉求（严密过滤系统标记、错误标签与无意义串）
    summary = extract_recent_dialogue_summary(rollout_path)
    if summary:
        lines = [l for l in summary.splitlines() if l.startswith("User:")]
        if lines:
            last_user = lines[-1].replace("User:", "").strip()
            for prefix in ("/goal ", "推进并完成：", "推进并完成:", "完成：", "完成:"):
                if last_user.startswith(prefix):
                    last_user = last_user[len(prefix):].strip()
            first_sent = last_user.split("\n")[0].split("。")[0].split(".")[0].strip()
            if (
                len(first_sent) > 4
                and not first_sent.startswith("<")
                and not any(k in first_sent for k in ("turn_aborted", "interrupted", "unified exec", "runs文件夹中有调试日志"))
            ):
                return first_sent[:50]

    fallback_title = clean_title if (clean_title and clean_title != "无标题任务") else "当前任务"
    return f"完成{fallback_title}所有要求并通过测试"


# 向后兼容别名
extract_goal_via_agy = extract_clean_goal


def analyze_goal_pause(rollout_path: Optional[Path]) -> Dict[str, Any]:
    """分析当前 Codex 会话暂停的原因，并自主提取推荐决策/动作。

    支持场景：
    1. 方案草案等待确认 (<proposed_plan> / 实施计划) -> 动作: approve_plan ("请按计划执行")
    2. request_user_input 工具交互请求 -> 提取 (Recommended) / (推荐) 选项，若无则取首项
    3. 文本中包含的多选提问与推荐标记 (1. ... (Recommended) / A. ... (推荐)) -> 提取该选项
    4. thread_goal_updated 变为 paused 状态 -> 动作: resume_goal ("继续推进目标")
    5. 无待办交互且无新事件 -> 动作: check_done
    """
    default_res: Dict[str, Any] = {
        "is_paused": False,
        "pause_type": None,
        "action": "check_done",
        "choice": "",
        "detail": "未检测到待处理交互",
    }
    if not rollout_path or not rollout_path.exists():
        return default_res

    events: List[Dict[str, Any]] = []
    try:
        with open(rollout_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return default_res

    if not events:
        return default_res

    # 1. 检查最新的 goal status (thread_goal_updated 或 update_goal 工具调用)
    last_goal_status = None
    for e in reversed(events):
        p = e.get("payload") or {}
        if not isinstance(p, dict):
            continue
        if p.get("type") == "thread_goal_updated":
            g = p.get("goal") or {}
            if isinstance(g, dict) and g.get("status"):
                last_goal_status = str(g.get("status")).lower()
                break
        if p.get("type") in ("function_call_output", "custom_tool_call_output"):
            out_str = p.get("output") or ""
            if isinstance(out_str, str) and '"status"' in out_str:
                try:
                    out_d = json.loads(out_str)
                    g = out_d.get("goal") or {}
                    if isinstance(g, dict) and g.get("status"):
                        last_goal_status = str(g.get("status")).lower()
                        break
                except Exception:
                    pass
        if p.get("type") == "function_call" and p.get("name") == "update_goal":
            args_str = p.get("arguments") or ""
            if '"blocked"' in args_str:
                last_goal_status = "blocked"
                break
            elif '"paused"' in args_str:
                last_goal_status = "paused"
                break

    # 2. 检查是否存在尚未响应的 request_user_input 工具调用
    for idx in range(len(events) - 1, -1, -1):
        e = events[idx]
        p = e.get("payload") or {}
        if not isinstance(p, dict):
            continue
        if p.get("name") == "request_user_input":
            call_id = p.get("call_id") or p.get("id")
            has_output = any(
                isinstance(ev.get("payload"), dict)
                and (
                    ev["payload"].get("call_id") == call_id
                    or ev["payload"].get("id") == call_id
                )
                for ev in events[idx + 1:]
                if (ev.get("payload") or {}).get("type") in ("function_call_output", "custom_tool_call_output")
            )
            if not has_output:
                args_raw = p.get("arguments") or p.get("input") or "{}"
                args: Dict[str, Any] = {}
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw)
                    except Exception:
                        pass
                elif isinstance(args_raw, dict):
                    args = args_raw

                questions = args.get("questions", [])
                answers: List[str] = []
                for q in questions:
                    if not isinstance(q, dict):
                        continue
                    opts = q.get("options", [])
                    rec_opt = None
                    for o in opts:
                        if not isinstance(o, dict):
                            continue
                        lbl = str(o.get("label", ""))
                        desc = str(o.get("description", ""))
                        if re.search(r"(?i)\(recommended\)|（推荐）|\[推荐\]|【推荐】", lbl + " " + desc):
                            rec_opt = lbl
                            break
                    if not rec_opt and opts and isinstance(opts[0], dict):
                        rec_opt = opts[0].get("label", "")
                    if rec_opt:
                        answers.append(rec_opt)

                if answers:
                    choice = "; ".join(answers)
                    return {
                        "is_paused": True,
                        "pause_type": "request_user_input",
                        "action": "inject_choice",
                        "choice": choice,
                        "detail": f"自动选择推荐选项: {choice}",
                    }
            break

    # 3. 检查最新 assistant 消息内容
    last_msg = ""
    for e in reversed(events):
        p = e.get("payload") or {}
        if not isinstance(p, dict):
            continue
        if p.get("last_agent_message"):
            last_msg = str(p.get("last_agent_message")).strip()
            break
        if p.get("type") == "message" and p.get("role") == "assistant":
            content = p.get("content") or []
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("text")]
            if texts:
                last_msg = " ".join(texts).strip()
                break
        item = e.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            t = item.get("text", "")
            if t.strip():
                last_msg = t.strip()
                break
        if p.get("type") == "item_completed":
            it = p.get("item") or {}
            if it.get("type") in ("AgentMessage", "agent_message"):
                ct = it.get("content") or []
                texts = [c.get("text", "") for c in ct if isinstance(c, dict) and c.get("text")]
                if texts:
                    last_msg = " ".join(texts).strip()
                    break

    # 3a. 检测计划草案
    if "<proposed_plan>" in last_msg or "</proposed_plan>" in last_msg or "## 实施计划" in last_msg or "### 实施计划" in last_msg or "## 方案" in last_msg:
        return {
            "is_paused": True,
            "pause_type": "proposed_plan",
            "action": "approve_plan",
            "choice": "请按计划执行",
            "detail": "自动批准并推进计划草案",
        }

    # 3b. 匹配行内带反引号/括号的选项: `1`（深色科技感） 或 (1) 选项一
    inline_opts = re.findall(r"[`'\"\s]?([1-9]|[A-Za-z])[`'\"\s]?\s*[（\(]([^）\)]+)[）\)]", last_msg)

    # 3c. 匹配按行编号选项: 1. ... 或 1、...
    line_opts = re.findall(r"(?:^|\n)\s*([1-9]|[A-Da-d])[\.\、\)\:]\s*([^\n]+)", last_msg)

    # 3d. 正则匹配正文中带有推荐标记的选项
    rec_match = re.search(
        r"(?:^|\n)\s*([1-9]|[A-Da-d])[\.\、\)\:]\s*([^\n]+?(?:\(Recommended\)|\(推荐\)|（推荐）)[^\n]*)",
        last_msg,
        re.I,
    )
    if rec_match:
        opt_idx = rec_match.group(1)
        opt_text = rec_match.group(2).strip()
        return {
            "is_paused": True,
            "pause_type": "text_options",
            "action": "inject_choice",
            "choice": f"{opt_idx}. {opt_text}",
            "detail": f"自动跟进正文推荐选项: {opt_idx}",
        }

    # 3e. 行内选项中若包含明确推荐标记
    if inline_opts:
        rec_inline = next((o for o in inline_opts if any(rk in o[1] for rk in ("推荐", "Recommended", "recommended"))), None)
        if rec_inline:
            return {
                "is_paused": True,
                "pause_type": "text_options",
                "action": "inject_choice",
                "choice": f"{rec_inline[0]}. {rec_inline[1].strip()}",
                "detail": f"自动跟进行内推荐选项: {rec_inline[0]}",
            }

    # 4. 检查目标停滞 (Goal Stalled / Blocked) 状态
    from afk_supervisor.acceptance import DONE_SIGNALS
    is_done_msg = any(sig in (last_msg or "") for sig in DONE_SIGNALS)

    is_stalled = not is_done_msg and ((last_goal_status == "blocked") or (
        any(
            k in last_msg for k in (
                "当前任务被阻塞",
                "任务被阻塞",
                "任务阻塞",
                "目标停滞",
                "处于阻塞状态",
                "尚未收到",
                "只差主视觉确认",
                "等待确认",
                "请回复",
            )
        ) and any(k in last_msg for k in ("回复", "确认", "选择", "决定", "1", "2", "3", "A", "B", "C", "`1`"))
    ))

    if is_stalled or (last_goal_status == "blocked" and not is_done_msg):
        cand_choice = "1"
        if inline_opts:
            cand_choice = f"{inline_opts[0][0]}. {inline_opts[0][1].strip()}"
        elif line_opts:
            cand_choice = f"{line_opts[0][0]}. {line_opts[0][1].strip()}"
        return {
            "is_paused": True,
            "pause_type": "goal_stalled",
            "action": "inject_choice",
            "choice": cand_choice,
            "detail": f"检测到目标停滞 (Goal Stalled / Blocked: {last_msg[:50]})",
            "stalled_msg": last_msg,
        }

    # 5. 正则匹配正文中的多选选项（至少 2 项且含提问意图），默认第 1 项
    if (len(line_opts) >= 2 or len(inline_opts) >= 2) and any(k in last_msg for k in ("选择", "请问", "方案", "哪种", "?", "？", "偏好", "决定")):
        if line_opts:
            first_idx = line_opts[0][0]
            first_txt = line_opts[0][1].strip()
        else:
            first_idx = inline_opts[0][0]
            first_txt = inline_opts[0][1].strip()
        return {
            "is_paused": True,
            "pause_type": "text_options",
            "action": "inject_choice",
            "choice": f"{first_idx}. {first_txt}",
            "detail": f"未显式标明推荐，默认选择第 {first_idx} 项",
        }

    # 6. 检查是否为内部 goal paused
    if last_goal_status == "paused":
        return {
            "is_paused": True,
            "pause_type": "goal_paused",
            "action": "resume_goal",
            "choice": "继续推进目标",
            "detail": "Codex 内部目标暂停，自动发送继续指令",
        }

    return default_res


def run_goal_supervisor(
    sid: str,
    rollout: Path,
    scwd: str,
    title: str,
    args: Any,
    run_dir: Path,
    goal_target: str = "",
    agy_mgr: Optional[Any] = None,
    proxy: Optional[str] = None,
    ws_lock: Optional[WorkspaceSupervisorLock] = None,
    state_mgr: Optional[SupervisorState] = None,
) -> int:
    """独立 Goal 目标模式执行主循环（三阶段全自动流转）。"""
    ws_path = Path(scwd).resolve()
    ivl_path = run_dir / "interventions.jsonl"

    def ivl(event: str, **kw: Any) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kw}
        with open(ivl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        act = kw.pop("activity", None)
        parts = [event]
        if kw:
            parts.append(str(kw))
        if act:
            parts.append(f"| {act}")

    if state_mgr is None:
        state_mgr = SupervisorState(run_dir, sid=sid, mode="goal", ws=ws_path)

    def finish_goal(status: str, detail: str) -> int:
        set_keep_awake(enable=False)
        ivl("TERMINAL", state=status, detail=detail)
        state_mgr.transition(status, detail=detail)
        report_path = generate_final_report(
            run_dir=run_dir,
            state=status,
            detail=detail,
            ts=run_dir.name,
            session_id=sid,
            jsonl_path=rollout,
            providers_tried=["codex-goal"],
            resumes=0,
            chaos=None,
            ivl_path=ivl_path,
            title=title,
        )
        send_terminal_notification(status, detail, report_path, title=title)
        if ws_lock:
            try:
                ws_lock.release()
            except Exception:
                pass
        log(f"TERMINAL {status} — {detail}")
        return 0 if status == "SUCCESS" else 1

    # 1. 确保系统与显示器常亮
    set_keep_awake(enable=True, keep_display=True)

    # 2. 阶段 1：目标设立检查 (识别目标是否设立)
    existing_goal = get_existing_thread_goal(rollout)
    skip_goal_injection = False

    if goal_target == "__ALREADY_SET__" or (not goal_target and existing_goal):
        skip_goal_injection = True
        clean_goal = existing_goal or "推进当前任务"
        final_prompt = ""
        log("=" * 60)
        log("GOAL     目标模式已启动 (三阶段自主流转)")
        log(f"GOAL     目标会话: [{sid[:8]}] {title or '(无标题)'}")
        log(f"GOAL_PHASE 1 [目标已设立] 检测到已有活跃目标: [{clean_goal}]，跳过目标设定指令")
        log("=" * 60)
        ivl("GOAL_PHASE", phase=1, status="ALREADY_SET", goal=clean_goal)
        prompt_file = run_dir / "goal_prompt.txt"
        prompt_file.write_text(f"Existing goal: {clean_goal}\n", encoding="utf-8")
        state_mgr.transition("RUNNING", detail=f"Goal mode active (existing): {clean_goal}")
        ivl("GOAL_STARTED", session=sid, prompt=f"existing:{clean_goal}", title=title)
    else:
        clean_goal = str(goal_target or "").strip()
        if clean_goal == "__LAZY__" or not clean_goal:
            ivl("GOAL_LAZY_EXTRACT", session=sid)
            clean_goal = extract_clean_goal(rollout, title=title, agy_mgr=agy_mgr, run_dir=run_dir, codex_session_id=sid)

        if clean_goal.startswith("/goal "):
            final_prompt = clean_goal
        else:
            final_prompt = f"/goal {clean_goal}"

        log("=" * 60)
        log("GOAL     目标模式已启动 (三阶段自主流转)")
        log(f"GOAL     目标会话: [{sid[:8]}] {title or '(无标题)'}")
        log(f"GOAL_PHASE 1 [设定目标] 注入指令: {final_prompt}")
        log("=" * 60)

        prompt_file = run_dir / "goal_prompt.txt"
        prompt_file.write_text(final_prompt + "\n", encoding="utf-8")
        state_mgr.transition("RUNNING", detail=f"Goal mode active: {final_prompt}")
        ivl("GOAL_STARTED", session=sid, prompt=final_prompt, title=title)

    initial_size = rollout.stat().st_size if (rollout and rollout.exists()) else 0

    # 3. 目标指令注入 (若已设立则仅聚焦并恢复桌面窗口)
    target_hwnd = find_best_codex_window()
    if not skip_goal_injection:
        if target_hwnd > 0:
            log(f"GOAL_INJECT 检测到 Codex 桌面主窗口 (HWND: {target_hwnd})，正在注入...")
            inject_res = inject_into_codex_gui(
                target_hwnd=target_hwnd,
                target_sid=sid,
                target_title=title,
                text=final_prompt,
                rollout_path=rollout,
            )
            ivl("GOAL_INJECT", status=inject_res.status, detail=inject_res.detail)
            log(f"GOAL_INJECT 注入回执: {inject_res.status} ({inject_res.detail})")
            if inject_res.status == "NOT_SENT":
                return finish_goal("FAILED", f"GUI 指令注入失败: {inject_res.detail} (请确保目标会话在 Codex 桌面端展开)")
        else:
            log("GOAL_INJECT 未检测到 Codex 前台活动主窗口，指令已记录到 prompt_file")
            ivl("GOAL_INJECT", status="RECORDED", detail="桌面端窗口未激活")
    else:
        if sid:
            _navigate_target(sid)
        if target_hwnd > 0:
            ensure_codex_window_restored()
        log("GOAL_PHASE 1 目标已设立，直接进入计划与长程执行阶段")

    # 4. 看门狗三阶段流转主循环
    has_started = skip_goal_injection
    wait_start_deadline = time.monotonic() + 30.0
    last_activity = ""
    last_change_time = time.monotonic()
    check_interval = 3.0
    stale_warn_sec = 600
    raw_mrs = getattr(args, "max_run_sec", 0)
    try:
        max_run_sec = float(raw_mrs)
    except (TypeError, ValueError):
        max_run_sec = 0.0
    start_time = time.monotonic()

    last_autopilot_choice = ""
    last_autopilot_time = 0.0
    autopilot_count = 0
    raw_mi = getattr(args, "max_interactions", 30)
    try:
        max_autopilot = int(raw_mi)
    except (TypeError, ValueError):
        max_autopilot = 30
    settle_logged = False
    last_phase = ""

    try:
        while True:
            time.sleep(check_interval)

            if max_run_sec > 0 and (time.monotonic() - start_time) > max_run_sec:
                return finish_goal("TIMEOUT", f"达到设定的最大运行时间上限 ({max_run_sec}s)")

            curr_size = rollout.stat().st_size if (rollout and rollout.exists()) else 0

            # 阶段 A：确认新 Goal 回合已启动（若跳过注入则直接处于启动状态）
            if not has_started:
                is_working, reason, _ = is_codex_working(rollout)
                if curr_size > initial_size or is_working:
                    has_started = True
                    settle_logged = False
                    log("GOAL_RUNNING 确认新 Goal 回合已启动，进入持续守护...")
                    ivl("GOAL_RUNNING", session=sid)
                else:
                    if time.monotonic() > wait_start_deadline:
                        return finish_goal("FAILED", "等待新 Goal 回合启动超时 (Codex 会话未产生新执行事件)")
                    continue

            # 阶段 B：识别当前是阶段 2 (制定计划) 还是阶段 3 (执行计划)
            current_phase = check_plan_status(rollout)
            if current_phase != last_phase:
                last_phase = current_phase
                if current_phase == "planning":
                    log("GOAL_PHASE 2 [制定计划中] 轮询交互提问与方案草案，准备自动选择推荐项...")
                    ivl("GOAL_PHASE", phase=2, status="PLANNING")
                elif current_phase == "executing":
                    log("GOAL_PHASE 3 [计划已制定，长程执行中] 持续守护直至任务完工...")
                    ivl("GOAL_PHASE", phase=3, status="EXECUTING")

            # 阶段 C：活动监测与进度日志
            activity = peek_rollout_activity(rollout)
            if activity and activity != last_activity:
                last_activity = activity
                last_change_time = time.monotonic()
                settle_logged = False
                log(f"GOAL_PROGRESS {activity}")
                ivl("GOAL_PROGRESS", activity=activity)

            idle_elapsed = time.monotonic() - last_change_time
            if idle_elapsed > stale_warn_sec and idle_elapsed % 120 < check_interval:
                log(f"[WARN] GOAL 会话已静默 {int(idle_elapsed)} 秒，持续监测中...")

            # 阶段 D：生命周期判定与暂停分析
            is_working, reason, last_msg = is_codex_working(rollout)

            if not is_working:
                pause_info = analyze_goal_pause(rollout)

                # 分支 1：模型暂停需要审批计划或选择推荐项 -> 自动注入决策推进
                if pause_info["action"] in ("approve_plan", "inject_choice", "resume_goal"):
                    choice = pause_info["choice"]
                    pause_type = pause_info.get("pause_type", "")
                    now_mono = time.monotonic()

                    # 若为目标停滞 (goal_stalled)，优先委托 AGY 破局提炼决断
                    if pause_type == "goal_stalled":
                        log(f"GOAL_STALLED [{current_phase}] 检测到目标停滞 (Goal Stalled)，呼叫 AGY 破局推进中...")
                        ivl("GOAL_STALLED", phase=current_phase, last_message=(last_msg[:100] if last_msg else ""))
                        agy_choice = resolve_stalled_goal_via_agy(
                            rollout_path=rollout,
                            title=title,
                            last_msg=last_msg,
                            agy_mgr=agy_mgr,
                            run_dir=run_dir,
                            timeout_sec=15.0,
                            codex_session_id=sid,
                        )
                        if agy_choice:
                            choice = agy_choice
                            log(f"GOAL_AGY  使用 AGY 决断破局: {choice}")
                        else:
                            log(f"GOAL_AGY  AGY 未响应，使用启发式推断破局: {choice}")

                    if choice == last_autopilot_choice and (now_mono - last_autopilot_time) < 15.0:
                        log(f"[WAIT] GOAL_AUTOPILOT 相同决策已下发，等待桌面端处理响应: {choice}")
                        time.sleep(check_interval)
                        continue

                    if autopilot_count >= max_autopilot:
                        return finish_goal("FAILED", f"自主决策跟进已达上限 ({max_autopilot} 次)，需人工介入")

                    log(f"GOAL_AUTOPILOT [{current_phase}] 检测到模型暂停 ({pause_type})，自主跟进决策: {choice}")
                    ivl(
                        "GOAL_AUTOPILOT",
                        phase=current_phase,
                        pause_type=pause_type,
                        choice=choice,
                        detail=pause_info["detail"],
                    )

                    target_hwnd = target_hwnd or find_best_codex_window()
                    if target_hwnd > 0:
                        inject_res = inject_into_codex_gui(
                            target_hwnd=target_hwnd,
                            target_sid=sid,
                            target_title=title,
                            text=choice,
                            rollout_path=rollout,
                        )
                        ivl("GOAL_INJECT_DECISION", status=inject_res.status, detail=inject_res.detail, choice=choice)
                        log(f"GOAL_INJECT_DECISION 注入回执: {inject_res.status} ({inject_res.detail})")
                    else:
                        log(f"GOAL_AUTOPILOT [模拟/未激活窗口] 记录推荐决策: {choice}")
                        ivl("GOAL_INJECT_DECISION", status="RECORDED", detail="桌面端窗口未激活", choice=choice)

                    last_autopilot_choice = choice
                    last_autopilot_time = now_mono
                    autopilot_count += 1
                    has_started = False
                    wait_start_deadline = time.monotonic() + 25.0
                    last_change_time = time.monotonic()
                    settle_logged = False
                    time.sleep(check_interval)
                    continue

                # 分支 2：无待处理交互，检查终态完成
                snapshot = codex_session_state(rollout)
                last_ev = snapshot.get("last_event", "")

                if last_ev == "task_complete":
                    # 严密闸门：若任务处于停滞/阻塞状态或有未决交互，绝不允许误判为完工早退！
                    is_stalled = (
                        pause_info.get("pause_type") == "goal_stalled"
                        or (pause_info.get("is_paused") and pause_info.get("action") in ("inject_choice", "approve_plan", "resume_goal"))
                        or any(k in (last_msg or "") for k in ("当前任务被阻塞", "任务被阻塞", "任务阻塞", "目标停滞", "处于阻塞状态", "尚未收到主视觉"))
                    )
                    if is_stalled:
                        time.sleep(check_interval)
                        continue

                    from afk_supervisor.acceptance import DONE_SIGNALS
                    is_done_signal = any(sig in (last_msg or "") for sig in DONE_SIGNALS)
                    settle_quiet = (time.monotonic() - last_change_time) >= 8.0

                    # 在 executing 阶段或有明确完工信号时，判定 Goal 顺利达成
                    if is_done_signal or (current_phase == "executing" and settle_quiet) or settle_quiet:
                        log("GOAL_COMPLETED 确认 Goal 目标任务已顺利完成！")
                        ivl("GOAL_COMPLETED", reason=reason, phase=current_phase, last_message=(last_msg[:100] if last_msg else ""))
                        return finish_goal("SUCCESS", f"Goal 目标任务已圆满完成 ({reason})")
                    else:
                        if not settle_logged:
                            log(f"GOAL_SETTLE [{current_phase}] 回合已停止，等待静默确认以杜绝中间状态误判...")
                            settle_logged = True
                        time.sleep(check_interval)
                        continue
                elif last_ev == "turn_aborted":
                    log(f"[WARN] GOAL_ABORTED 任务回合被中断或暂停: {reason}")
                    ivl("GOAL_ABORTED", reason=reason)
                    return finish_goal("CANCELLED", f"任务回合被外部中断或暂停 ({reason})")
                else:
                    log(f"GOAL_STOPPED 任务已停止: {reason}")
                    ivl("GOAL_STOPPED", reason=reason)
                    return finish_goal("STOPPED", f"任务已停止 ({reason})")

    except KeyboardInterrupt:
        return finish_goal("CANCELLED", "用户主动中断 Goal 模式运行")
    except Exception as e:
        return finish_goal("FAILED", f"Goal 模式运行异常: {e}")
