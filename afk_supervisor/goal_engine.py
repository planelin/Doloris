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
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from afk_supervisor.platform.gui import find_best_codex_window, inject_into_codex_gui
from afk_supervisor.platform.process import WorkspaceSupervisorLock, log
from afk_supervisor.platform.windows import set_keep_awake
from afk_supervisor.reporting import generate_final_report, send_terminal_notification
from afk_supervisor.sessions.rollout import (
    codex_session_state,
    is_codex_working,
    peek_rollout_activity,
)
from afk_supervisor.state import SupervisorState
from afk_supervisor.l2.bridge import (
    bind_agy_conversation_for_codex,
    discover_antigravity_bridge,
    discover_antigravity_project_id,
    get_agy_brain_dir,
    get_agy_conversation_for_codex,
)
from afk_supervisor.sessions.discovery import find_codex_session_by_id

LONG_HORIZON_SYSTEM_PROMPT = (
    "该目标用于数小时无人值守托管，必须具备持续执行价值与可实现、可测试、可验收的闭环。"
    "只允许提炼业务主线级目标，必须覆盖实现、验证和验收；严禁把提交 Git/commit、"
    "查看日志或状态、运行某项测试等秒级单步动作当作目标，"
    "严禁把助手对当前工作区状态的陈述（例如“改动尚未提交 Git”）当作用户行动目标。\n"
    "状态分支：若上一阶段刚完成并附有“后续建议/待办事项/实施计划”，目标应聚焦落实这些建议"
    "并完成深度健壮性改进；若用户只输入“继续/请继续/继续线程工作”，必须回溯业务主线和已验证的"
    "fork 父会话的原始业务需求，不得把“继续”本身当作目标。\n"
)

GOAL_GUARDRAIL_FALLBACK = "在业务主线上落实后续建议与深度健壮性改进，完成实现、测试与验收闭环"


class GoalExtractionError(RuntimeError):
    """AGY 未交付目标；界面不得把上下文摘录伪装成提炼结果。"""


def build_long_horizon_directive(goal: str) -> str:
    """构造"目标已设立"时可下发的长程托管续跑指令 (普通消息，不重设 /goal)。

    该分支过去不下发任何提示词，导致重构后的长程托管认知根本到不了 LLM：
    模型只看到自己上一轮的结论，于是把"提交 Git / 查看日志 / 跑单项测试"这类
    秒级动作当成阶段目标，一条命令执行完自动退出，托管形同秒级早退。
    """
    clean = _strip_goal_prefixes(goal) or GOAL_GUARDRAIL_FALLBACK
    return "\n".join([
        "【长程托管续跑指令】本会话已被 Doloris 无人值守接管，将连续运行数小时，",
        "期间没有人会回答问题或替你做选择，请自行推进到可验收的终态。",
        f"当前已设立目标（保持不变，不要重新设定）：{clean}",
        "执行要求：",
        "1. 只做业务主线级工作：实现、重构、修复、验证、验收；每个动作都要推进目标并可核查。",
        "2. 禁止把秒级过程动作当成阶段目标：提交/推送 Git、查看日志或状态、运行某一个单项测试，都不是目标。",
        "3. 禁止把“当前工作区状态”的陈述（例如“改动尚未提交 Git”）当成待办目标。",
        "4. 先复核上下文中已有的审查结论、后续建议、待办事项与实施计划，把尚未落实的部分排成计划逐项执行。",
        "5. 每阶段收尾都要自测并给出验收证据；无法自动验证的事项显式标注为需人工确认，不要谎报完成。",
        "6. 只有外部输入才能解除的阻塞不算完成：写明阻塞原因、已尝试方案与可选路径后停止回合。",
        "现在直接开始执行，不要输出与推进目标无关的寒暄。",
    ])

KEY_CONTEXT_MARKERS = (
    "审查结论",
    "审查结果",
    "改进建议",
    "后续建议",
    "待办事项",
    "实施计划",
    "验收标准",
    "下一步",
)

_INTERNAL_CONTEXT_MARKERS = (
    "<environment_context>",
    "<codex_internal_context",
    "<turn_context>",
    "<collaboration_mode",
    "<turn_aborted",
    "<app-context>",
    "<skills_instructions>",
    "<permissions instructions>",
)


def _clean_user_text(raw_text: str) -> Optional[str]:
    """过滤环境注入，并把结构化用户选择还原为可读文本。"""
    txt = (raw_text or "").strip()
    if not txt:
        return None
    if any((marker in txt or txt.startswith(marker)) for marker in _INTERNAL_CONTEXT_MARKERS):
        return None
    if "<send_user_message_question_reply>" in txt:
        try:
            match = re.search(
                r"<send_user_message_question_reply>\s*(\[.*?\])\s*</send_user_message_question_reply>",
                txt,
                re.DOTALL,
            )
            if match:
                answers = []
                for item in json.loads(match.group(1)):
                    question = item.get("question", "")
                    answer = item.get("answer", "")
                    if answer:
                        answers.append(f"{question}: {answer}" if question else str(answer))
                if answers:
                    return "; ".join(answers)
        except Exception:
            pass
    return txt


def _is_short_continuation(text: str) -> bool:
    normalized = re.sub(r"[\s，。！？!?、,.：:；;]+", "", (text or "").lower())
    return normalized in {
        "继续",
        "请继续",
        "继续吧",
        "继续工作",
        "继续任务",
        "继续线程工作",
        "继续推进",
        "接着继续",
        "接着做",
        "可以继续",
        "continue",
        "pleasecontinue",
        "resume",
        "goon",
    }


def _truncate_context_text(text: str) -> str:
    """关键结论保留近千字符，普通上下文保留更宽裕的摘要窗口。"""
    clean = (text or "").strip()
    limit = 900 if any(marker in clean for marker in KEY_CONTEXT_MARKERS) else 400
    if len(clean) <= limit:
        return clean
    head = clean[:limit]
    cut = max(head.rfind("\n"), head.rfind("。"), head.rfind("；"))
    if cut >= int(limit * 0.65):
        head = head[: cut + 1]
    return head.rstrip() + "..."


def _strip_goal_prefixes(text: str) -> str:
    clean = (text or "").strip().strip('`"\'“” \n\r\t')
    prefixes = ("/goal ", "/goal", "目标：", "目标:", "推进并完成：", "推进并完成:", "完成：", "完成:")
    for prefix in prefixes:
        if clean.startswith(prefix):
            clean = clean[len(prefix):].strip()
    return clean.strip()


def _read_rollout_events(path: Optional[Path]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    if not path or not Path(path).exists():
        return events
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
    except Exception:
        pass
    return events


def _rollout_user_texts(rollout_path: Optional[Path]) -> List[str]:
    texts: List[str] = []
    for obj in _read_rollout_events(rollout_path):
        payload = obj.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type", "")
        if ptype == "user_message":
            content = payload.get("message") or payload.get("content") or ""
            if isinstance(content, str):
                cleaned = _clean_user_text(content)
                if cleaned:
                    texts.append(cleaned)
        elif ptype == "message" and payload.get("role") == "user":
            for item in payload.get("content") or []:
                if isinstance(item, dict) and item.get("text"):
                    cleaned = _clean_user_text(str(item["text"]))
                    if cleaned:
                        texts.append(cleaned)
    return texts


def _first_business_request(rollout_path: Optional[Path]) -> str:
    for text in _rollout_user_texts(rollout_path):
        if _is_short_continuation(text):
            continue
        compact = " ".join(text.split())
        if compact:
            return compact[:900]
    return ""


def _fork_parent_id(rollout_path: Optional[Path]) -> str:
    """读取 fork 子会话的父会话 ID (session_meta.payload.forked_from_id)。"""
    for obj in _read_rollout_events(rollout_path):
        if obj.get("type") != "session_meta":
            continue
        payload = obj.get("payload") or {}
        if isinstance(payload, dict) and payload.get("forked_from_id"):
            return str(payload["forked_from_id"]).strip()
    return ""


def _origin_rollout_path(session_id: str) -> Optional[Path]:
    """定位某个会话自身的原始 rollout。

    优先选择文件名以该会话 ID 结尾的文件（root 会话原始文件），避免被同名的
    后代 fork 文件按修改时间抢占。
    """
    try:
        from afk_supervisor.sessions.discovery import find_codex_session_rollouts

        matches = find_codex_session_rollouts(session_id)
    except Exception:
        return None
    own = [item for item in matches if item[1].stem.endswith(session_id)]
    pool = own or matches
    return pool[0][1] if pool else None


def _fork_parent_business_context(rollout_path: Optional[Path], max_depth: int = 4) -> str:
    """沿 fork 链向上回溯，返回最近一个具备业务语义的祖先原始需求。"""
    seen = set()
    cursor = rollout_path
    for _ in range(max(1, max_depth)):
        parent_id = _fork_parent_id(cursor)
        if not parent_id or parent_id in seen:
            return ""
        seen.add(parent_id)
        parent_path = _origin_rollout_path(parent_id)
        if not parent_path:
            return ""
        request = _first_business_request(parent_path)
        if request:
            return _truncate_context_text(request)
        cursor = parent_path
    return ""


def _latest_followup_context(rollout_path: Optional[Path]) -> str:
    """提取最近一次阶段汇报里的后续建议，供提炼和安全回退使用。"""
    for event in reversed(_read_rollout_events(rollout_path)):
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "user_message":
            user_text = _clean_user_text(str(payload.get("message") or payload.get("content") or ""))
            if user_text and not _is_short_continuation(user_text):
                return ""
        if payload.get("type") == "message" and payload.get("role") == "user":
            user_text = " ".join(str(item.get("text") or "") for item in payload.get("content") or []
                                 if isinstance(item, dict))
            if user_text and not _is_short_continuation(user_text):
                return ""
        texts = []
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            texts = [str(item.get("text")) for item in payload.get("content") or []
                     if isinstance(item, dict) and item.get("text")]
        elif payload.get("last_agent_message"):
            texts = [str(payload["last_agent_message"])]
        for message in reversed(texts):
            markers = ("后续建议", "待办事项", "实施计划", "改进建议", "下一步")
            positions = [message.find(marker) for marker in markers if marker in message]
            if positions:
                section = message[min(positions):].strip()
                return " ".join(_truncate_context_text(section)[:500].split())
    return ""


def _contextual_fallback(rollout_path: Optional[Path], title: str = "") -> str:
    business = next(
        (text for text in reversed(_rollout_user_texts(rollout_path))
         if not _is_short_continuation(text) and not detect_goal_guardrail(text)),
        "",
    )
    business = business or _fork_parent_business_context(rollout_path)
    if business and detect_goal_guardrail(business):
        business = ""
    if not business:
        candidate = _strip_goal_prefixes(title).rstrip("….").strip()
        if candidate and candidate != "无标题任务" and not detect_goal_guardrail(candidate):
            business = candidate
    followup = _latest_followup_context(rollout_path)
    if not business and not followup:
        return GOAL_GUARDRAIL_FALLBACK
    scope = f"围绕{business[:120]}，" if business else ""
    detail = f"落实{followup[:260]}，" if followup else "落实后续建议与深度健壮性改进，"
    return f"{scope}{detail}完成实现、测试和验收闭环"


def detect_goal_guardrail(goal: str) -> str:
    """Return a guardrail reason when a candidate is a micro step or a state statement."""
    clean = _strip_goal_prefixes(goal)
    if not clean:
        return ""
    lowered = clean.lower()
    if _is_short_continuation(clean):
        return "目标仅为“继续”类延续指令，未溯及业务主线"
    macro_verbs = (
        "实现",
        "重构",
        "修复",
        "优化",
        "完善",
        "审查",
        "设计",
        "补充",
        "验证",
        "验收",
        "推进",
        "开发",
        "改造",
        "落实",
        "完成",
        "review",
        "implement",
        "refactor",
        "fix",
        "improve",
    )
    commit_pattern = re.compile(
        r"^(?:请)?(?:把|将)?(?:当前)?(?:改动|代码|变更)?\s*(?:提交|commit|push|推送).{0,30}(?:git|github|远端|仓库)",
        re.IGNORECASE,
    )
    git_command_pattern = re.compile(r"^(?:运行|执行|查看)?\s*git\s+(?:commit|push|status|log|diff)\b", re.IGNORECASE)
    view_pattern = re.compile(
        r"^(?:查看|检查|读取|打印|看一下|查看一下).{0,16}(?:日志|log|状态|status|工作区|改动|git)",
        re.IGNORECASE,
    )
    run_pattern = re.compile(
        r"^(?:运行|执行|跑|重跑|再跑)\s*(?:一下)?\s*(?:(?:python[0-9.]*|py)\s+-m\s+)?"
        r"(?:pytest|测试|单元测试|单项测试|某个测试|指定测试|指定用例)",
        re.IGNORECASE,
    )
    status_misread = re.search(
        r"(?:尚未|还未|还没|未|没有)(?:提交|推送).{0,12}(?:git|github|远端|仓库)"
        r"|当前(?:工作区|仓库|代码|改动).{0,16}(?:状态|未提交|尚未提交|已修改)",
        clean,
        re.IGNORECASE,
    )
    if status_misread and not any(verb in lowered for verb in macro_verbs):
        return "将工作区现状陈述误判为行动目标"
    if commit_pattern.search(clean) or git_command_pattern.search(clean):
        return "目标仅为 Git 提交/推送等微观动作"
    micro_match = view_pattern.search(clean) or run_pattern.search(clean)
    if micro_match and len(clean) <= 60 and not any(verb in lowered for verb in macro_verbs):
        return "目标仅为查看状态、日志或运行单项测试等微观动作"
    return ""


def sanitize_goal(goal: str, rollout_path: Optional[Path] = None, title: str = "") -> Tuple[str, str]:
    clean = _strip_goal_prefixes(goal)
    reason = detect_goal_guardrail(clean)
    if reason:
        return _contextual_fallback(rollout_path, title), reason
    return clean, ""


def extract_recent_dialogue_summary(rollout_path: Optional[Path], max_turns: int = 6) -> str:
    """从 rollout 中提取最近几轮真实对话内容用于目标归纳，过滤环境注入与内部提示。"""
    if not rollout_path or not Path(rollout_path).exists():
        return ""
    messages: List[str] = []

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
                        clean_u = _clean_user_text(content)
                        if clean_u:
                            messages.append(f"User: {clean_u}")
                elif ptype == "message" and payload.get("role") == "user":
                    content_list = payload.get("content") or []
                    for c in content_list:
                        if isinstance(c, dict) and c.get("text"):
                            clean_u = _clean_user_text(c["text"])
                            if clean_u:
                                messages.append(f"User: {clean_u}")
                elif ptype == "message" and payload.get("role") == "assistant":
                    content_list = payload.get("content") or []
                    for c in content_list:
                        if isinstance(c, dict) and c.get("text"):
                            snip = c["text"].strip()
                            if snip.startswith("<") or "<codex_internal_context" in snip or "<environment_context" in snip:
                                continue
                            messages.append(f"Assistant: {_truncate_context_text(snip)}")
    except Exception:
        pass

    user_texts = [message[6:] for message in messages if message.startswith("User:")]
    has_business_text = any(not _is_short_continuation(text) for text in user_texts)
    business_line = ""
    if not has_business_text and (user_texts or _fork_parent_id(rollout_path)):
        parent_context = _fork_parent_business_context(rollout_path)
        if parent_context:
            business_line = f"Business: {parent_context}"

    recent = messages[-max_turns:] if len(messages) >= max_turns else messages
    followup = _latest_followup_context(rollout_path)
    if followup and not any(followup in message for message in recent):
        recent = [f"Follow-up: {followup}"] + list(recent)
    if business_line:
        # 业务主线必须位于截断之外，否则会被最近几轮对话挤出上下文窗口。
        recent = [business_line] + list(recent)
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
    timeout_sec: Optional[float] = 15.0,
    codex_session_id: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
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
        f"{LONG_HORIZON_SYSTEM_PROMPT}\n"
        "请根据以下 Codex 任务上下文，用一句话总结最终需要交付的纯净目标。"
        "目标应能在无人值守下持续推进，并包含实现、验证与验收闭环；不要任何“推进并完成”等前缀，"
        "不要输出 /goal，仅输出目标本身：\n"
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
            res = subprocess.run(cmd, capture_output=True, timeout=min(timeout_sec, 12) if timeout_sec is not None else None,
                                 env=env, creationflags=no_win)
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
        deadline = time.monotonic() + timeout_sec if timeout_sec is not None else None
        while deadline is None or time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise GoalExtractionError("目标提炼已取消")
            if t_path.exists():
                try:
                    all_lines = t_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    lines = all_lines[initial_line_count:]
                    for line in reversed(lines):
                        if '"PLANNER_RESPONSE"' in line and 'DONE' in line:
                            obj = json.loads(line)
                            if (obj.get("type") != "PLANNER_RESPONSE" or obj.get("status") != "DONE"
                                    or obj.get("tool_calls")):
                                continue
                            content = obj.get("content", "").strip()
                            if content:
                                for pfx in ("/goal ", "/goal", "目标：", "目标:", "推进并完成：", "推进并完成:", "完成：", "完成:"):
                                    if content.startswith(pfx):
                                        content = content[len(pfx):].strip()
                                content = content.strip('`"\'“” \n\r\t')
                                lines_c = [line.strip() for line in content.splitlines() if line.strip()]
                                if lines_c:
                                    cand = lines_c[0].strip('`"\'“” \n\r\t')
                                    for pfx in ("/goal ", "/goal", "目标：", "目标:", "推进并完成：", "推进并完成:", "完成：", "完成:"):
                                        if cand.startswith(pfx):
                                            cand = cand[len(pfx):].strip()
                                    if len(cand) > 3:
                                        log(f"GOAL_AGY  AGY 提炼目标成功: [{cand}]")
                                        return cand[:300]
                except Exception:
                    pass
            if cancel_event is not None:
                cancel_event.wait(0.5)
            else:
                time.sleep(0.5)
    except GoalExtractionError:
        raise
    except Exception as exc:
        log(f"GOAL_AGY  AGY 提炼异常，未取得有效目标: {exc}")
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
                                lines_c = [line.strip() for line in content.splitlines() if line.strip()]
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


def extract_clean_goal_with_reason(
    rollout_path: Optional[Path],
    title: str = "",
    agy_mgr: Optional[Any] = None,
    run_dir: Optional[Path] = None,
    codex_session_id: Optional[str] = None,
    require_agy: bool = False,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[str, str]:
    """在懒人模式下，由 AGY 智能提炼纯净交付目标，或启发式优雅回退。

    绝不添加“推进并完成：”等多余前缀，下发提示词严格约束为 /goal [目标]。
    优先级：
    1. rollout 中已有的 thread_goal_updated 目标；
    2. fork 子会话仅输入“继续”或无用户回合时，回溯父会话原始业务需求；
    3. 核心：通过 Antigravity AGY 深度阅读任务上下文并智能提炼纯净目标；
    4. “继续”类输入无法回溯父任务时，回退到宏观长程目标；
    5. 会话原生 title (若具备业务语义)；
    6. 最近一轮用户输入的有效文本（去除环境上下文和系统指令）。
    """
    # 1. 尝试从 rollout 中读取已有 goal 的 objective
    existing = get_existing_thread_goal(rollout_path)
    if existing:
        return sanitize_goal(existing, rollout_path, title)

    # 2. fork 子会话的“继续”类输入必须优先穿透父任务意图，避免就地停顿
    summary = extract_recent_dialogue_summary(rollout_path)
    user_texts = _rollout_user_texts(rollout_path)
    clean_title = _strip_goal_prefixes(title or "")
    title_is_continuation = _is_short_continuation(clean_title)
    has_business_text = any(not _is_short_continuation(text) for text in user_texts)
    continuation_only = not has_business_text and (
        bool(user_texts) or title_is_continuation or bool(_fork_parent_id(rollout_path))
    )

    # 3. 核心：调用 AGY 语言服务提炼目标
    agy_res = extract_goal_via_agy_agent(
        rollout_path,
        title=title,
        agy_mgr=agy_mgr,
        run_dir=run_dir,
        timeout_sec=None if require_agy else 25.0,
        codex_session_id=codex_session_id,
        cancel_event=cancel_event,
    )
    if agy_res:
        return sanitize_goal(agy_res, rollout_path, title)
    if require_agy:
        raise GoalExtractionError("AGY 未返回有效目标，请重试或手动输入")

    # 4. 属于“继续”类但无法回溯父任务时，回退到宏观长程目标
    if continuation_only:
        parent_context = _fork_parent_business_context(rollout_path)
        if parent_context and not _latest_followup_context(rollout_path):
            return sanitize_goal(parent_context, rollout_path, title)
        return _contextual_fallback(rollout_path, title), ""

    if _latest_followup_context(rollout_path):
        return _contextual_fallback(rollout_path, title), ""

    # 5. 检查会话 title
    if clean_title and clean_title != "无标题任务" and len(clean_title) > 3:
        clean_title = clean_title.rstrip("….").strip()
        if clean_title and not clean_title.startswith("读取codex://"):
            return sanitize_goal(clean_title, rollout_path, title)

    # 6. 提取最近一轮用户诉求（严密过滤系统标记、错误标签与无意义串）
    if summary:
        lines = [line for line in summary.splitlines() if line.startswith("User:")]
        if lines:
            last_user = lines[-1].replace("User:", "").strip()
            last_user = _strip_goal_prefixes(last_user)
            first_para = last_user.split("\n\n")[0].strip()
            if (
                len(first_para) > 4
                and not first_para.startswith("<")
                and not any(k in first_para for k in ("turn_aborted", "interrupted", "unified exec", "runs文件夹中有调试日志"))
            ):
                return sanitize_goal(first_para[:300], rollout_path, title)

    fallback_title = clean_title if (clean_title and clean_title != "无标题任务") else "当前任务"
    return sanitize_goal(f"完成{fallback_title}所有要求，补齐测试并完成验收", rollout_path, title)


def extract_clean_goal(
    rollout_path: Optional[Path],
    title: str = "",
    agy_mgr: Optional[Any] = None,
    run_dir: Optional[Path] = None,
    codex_session_id: Optional[str] = None,
    require_agy: bool = False,
    cancel_event: Optional[threading.Event] = None,
) -> str:
    return extract_clean_goal_with_reason(
        rollout_path,
        title=title,
        agy_mgr=agy_mgr,
        run_dir=run_dir,
        codex_session_id=codex_session_id,
        require_agy=require_agy,
        cancel_event=cancel_event,
    )[0]


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
            "choice": "继续",
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

    def guarded_goal(candidate: str) -> str:
        clean, guard_reason = sanitize_goal(candidate, rollout, title)
        if guard_reason:
            ivl(
                "GOAL_GUARDRAIL",
                original=str(candidate or "")[:300],
                reason=guard_reason,
                fallback=clean,
            )
            log(f"GOAL_GUARDRAIL 已拦截微观或现状误读目标: {guard_reason}")
        return clean

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
    goal_already_set = bool(goal_target == "__ALREADY_SET__" or (not goal_target and existing_goal))
    # 目标已设立时不再重复下发 /goal(会覆盖既有目标)，但仍必须注入长程托管续跑指令：
    # 否则重构后的提示词策略永远到不了 LLM，模型会把秒级动作当阶段目标后自动退出。
    reuse_existing_prompt = False

    if goal_already_set:
        clean_goal = guarded_goal(existing_goal or GOAL_GUARDRAIL_FALLBACK)
        final_prompt = build_long_horizon_directive(clean_goal)
        reuse_existing_prompt = True
        log("=" * 60)
        log("GOAL     目标模式已启动 (三阶段自主流转)")
        log(f"GOAL     目标会话: [{sid[:8]}] {title or '(无标题)'}")
        log(f"GOAL_PHASE 1 [目标已设立] 检测到已有活跃目标: [{clean_goal}]，下发长程托管续跑指令")
        log("=" * 60)
        ivl("GOAL_PHASE", phase=1, status="ALREADY_SET", goal=clean_goal)
        prompt_file = run_dir / "goal_prompt.txt"
        prompt_file.write_text(final_prompt + "\n", encoding="utf-8")
        state_mgr.transition("RUNNING", detail=f"Goal mode active (existing): {clean_goal}")
        ivl("GOAL_STARTED", session=sid, prompt=f"existing:{clean_goal}", title=title, strategy="long_horizon_resume")
    else:
        clean_goal = str(goal_target or "").strip()
        if not clean_goal:
            return finish_goal("FAILED", "尚未设定 Goal 目标；请手动输入，或明确选择懒人模式")
        if clean_goal == "__LAZY__":
            ivl("GOAL_LAZY_EXTRACT", session=sid)
            try:
                clean_goal, guard_reason = extract_clean_goal_with_reason(
                    rollout,
                    title=title,
                    agy_mgr=agy_mgr,
                    run_dir=run_dir,
                    codex_session_id=sid,
                    require_agy=True,
                )
            except GoalExtractionError as exc:
                return finish_goal("FAILED", f"AGY 目标提炼未完成: {exc}")
            if guard_reason:
                ivl(
                    "GOAL_GUARDRAIL",
                    original="AGY/启发式提炼结果",
                    reason=guard_reason,
                    fallback=clean_goal,
                )
                log(f"GOAL_GUARDRAIL 已拦截微观或现状误读目标: {guard_reason}")
        else:
            clean_goal = guarded_goal(clean_goal)

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

    # 3. 目标指令注入 (既有目标时下发长程续跑指令，新目标时下发 /goal)
    target_hwnd = find_best_codex_window()
    if target_hwnd > 0:
        kind = "长程续跑指令" if reuse_existing_prompt else "Goal 目标指令"
        log(f"GOAL_INJECT 检测到 Codex 桌面主窗口 (HWND: {target_hwnd})，正在注入{kind}...")
        inject_res = inject_into_codex_gui(
            target_hwnd=target_hwnd,
            target_sid=sid,
            target_title=title,
            text=final_prompt,
            rollout_path=rollout,
        )
        ivl("GOAL_INJECT", status=inject_res.status, detail=inject_res.detail,
            kind="resume_directive" if reuse_existing_prompt else "goal_command")
        log(f"GOAL_INJECT 注入回执: {inject_res.status} ({inject_res.detail})")
        if inject_res.status == "NOT_SENT":
            reason = "长程续跑指令" if reuse_existing_prompt else "Goal 目标指令"
            return finish_goal("FAILED", f"GUI {reason}注入失败: {inject_res.detail} (请确保目标会话在 Codex 桌面端展开)")
    else:
        log("GOAL_INJECT 未检测到 Codex 前台活动主窗口，指令已记录到 prompt_file")
        ivl("GOAL_INJECT", status="RECORDED", detail="桌面端窗口未激活")
        if reuse_existing_prompt:
            # 桌面端未开时无法下发续跑指令，但既有目标仍在走，不因此中断守护。
            log("GOAL_PHASE 1 目标已设立且无法注入，直接进入计划与长程执行阶段")

    # 4. 看门狗三阶段流转主循环
    # 桌面端未开且目标已设立时无需等待新回合；其余情况都必须确认注入真的引发了新回合。
    has_started = reuse_existing_prompt and target_hwnd <= 0
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
    last_heartbeat_log = time.monotonic()
    last_heartbeat_ivl = time.monotonic()

    try:
        while True:
            time.sleep(check_interval)

            if max_run_sec > 0 and (time.monotonic() - start_time) > max_run_sec:
                return finish_goal("TIMEOUT", f"达到设定的最大运行时间上限 ({max_run_sec}s)")

            refreshed = find_codex_session_by_id(sid)
            if refreshed and refreshed[1] != rollout and refreshed[1].exists():
                old_rollout = rollout
                rollout = refreshed[1]
                log(f"GOAL_ROLLOUT 检测到 Codex 会话分页轮转: {old_rollout.name} -> {rollout.name}")
                ivl("ROLLOUT_ROTATED", old=old_rollout.name, new=rollout.name)
                last_change_time = time.monotonic()

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

            # 心跳日志：长跑期间必须让外部看得见"还活着、在看什么"，避免观感上"无任何反应"。
            now_mono = time.monotonic()
            if now_mono - last_heartbeat_log >= 60.0:
                last_heartbeat_log = now_mono
                log(
                    f"GOAL_WAIT phase={current_phase or 'init'} size={curr_size} "
                    f"activity={last_activity or 'idle'} idle={int(idle_elapsed)}s "
                    f"elapsed={int(now_mono - start_time)}s"
                )
                if now_mono - last_heartbeat_ivl >= 300.0:
                    last_heartbeat_ivl = now_mono
                    ivl("GOAL_WAIT", phase=current_phase, size=curr_size,
                        activity=last_activity, idle_sec=int(idle_elapsed))

            # 阶段 D：生命周期判定与暂停分析
            is_working, reason, last_msg = is_codex_working(rollout)

            if not is_working:
                snapshot = codex_session_state(rollout)
                if snapshot.get("is_rate_limited"):
                    err_msg = snapshot.get("turn_error_message") or "Codex API 速率受限或网络偶发异常"
                    retry_wait = max(5.0, snapshot.get("retry_delay_sec") or 25.0)
                    log(f"[WARN] GOAL RATE_LIMIT 检测到 Codex API 速率受限/偶发网络故障: {err_msg}")
                    log(f"[WAIT] 触发防雪崩熔断避让，冷却等待 {retry_wait:.0f}s 后自动注入恢复指令...")
                    ivl("RATE_LIMIT_BACKOFF", error=err_msg[:200], retry_wait_sec=retry_wait)
                    time.sleep(retry_wait)
                    target_hwnd = target_hwnd or find_best_codex_window()
                    if target_hwnd > 0:
                        inject_res = inject_into_codex_gui(
                            target_hwnd=target_hwnd,
                            target_sid=sid,
                            target_title=title,
                            text="继续",
                            rollout_path=rollout,
                        )
                        ivl("GOAL_INJECT_DECISION", status=inject_res.status, detail=inject_res.detail, choice="继续")
                    time.sleep(check_interval)
                    continue

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

                if last_ev == "task_complete" and not snapshot.get("is_rate_limited"):
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
