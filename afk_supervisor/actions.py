"""Shared unattended interpretation; UNRESOLVED is not an instruction to give up."""
import time

from afk_supervisor.models import ActionType

CHANNEL_ERRORS = {"NO-BRIDGE", "NO-VERDICT", "PROTOCOL_ERROR"}
WORKER_ACTIONS = {ActionType.WORKER_INSTRUCTION, ActionType.WORKER_FIX, ActionType.GATHER_EVIDENCE}


def repair_action(verdict, answer, payload):
    action = (payload or {}).get("next_action") or {}
    kind = action.get("type", "")
    text = action.get("instructions") or answer
    if verdict == "STOP" and kind == ActionType.TERMINATE_BLOCKED and (payload or {}).get("blockers") and text.strip():
        return "blocked", text + "；证据: " + str(payload["blockers"])
    if verdict in CHANNEL_ERRORS:
        return "retry", answer
    if verdict == "REPAIRED":
        return "review", text
    if verdict == "UNRESOLVED" and kind in WORKER_ACTIONS and text.strip():
        return "worker", text
    # Legacy DEFER and non-executable replies get a new L2 decision, never a
    # fabricated human approval or an unconditional 'continue'.
    return "decide", "修复未建立成功证据，请决定可执行替代步骤或有依据地停止。\n" + text


class DecisionStopped(Exception):
    def __init__(self, outcome, detail):
        self.outcome, self.detail = outcome, detail


class UnattendedL2:
    """Shared bounded DECIDE/REPAIR gates for headless and GUI transports."""
    def __init__(self, coordinator, driver, session_cwd, budget, state_mgr, ivl):
        self.coordinator, self.driver, self.session_cwd = coordinator, driver, session_cwd
        self.budget, self.state_mgr, self.ivl = budget, state_mgr, ivl

    def decide(self, question, number):
        """Shared bounded decision gate before fork and during execution."""
        coordinator, driver, session_cwd = self.coordinator, self.driver, self.session_cwd
        budget, state_mgr, ivl = self.budget, self.state_mgr, self.ivl
        from afk_supervisor.l2.transport import clean_l2_decision_text
        context = question
        for attempt in range(3):
            if budget.is_expired():
                raise DecisionStopped("timeout", "L2决策期间达到总时长上限")
            ivl("L2_CONSULT", n=number, kind="DECIDE", attempt=attempt + 1, question=context[:150])
            verdict, answer, log_path, payload = coordinator.handle_interaction(context, number, driver=driver, session_cwd=session_cwd)
            ivl("L2_ANSWER", verdict=verdict, answer=answer[:150], log=str(log_path))
            if budget.is_expired():
                raise DecisionStopped("timeout", "L2决策期间达到总时长上限")
            action = (payload or {}).get("next_action", {})
            kind = action.get("type", "")
            if verdict == "STOP" and kind == ActionType.TERMINATE_BLOCKED and (payload or {}).get("blockers") and action.get("instructions", "").strip():
                raise DecisionStopped("blocked", "L2决定停止: " + action["instructions"] + "；证据: " + str(payload["blockers"]))
            instruction = clean_l2_decision_text(action.get("instructions") or answer)
            if verdict == "PROCEED" and kind in ("", ActionType.WORKER_INSTRUCTION, ActionType.WORKER_FIX, ActionType.GATHER_EVIDENCE) and instruction.strip():
                return instruction, payload, kind or ActionType.WORKER_INSTRUCTION
            state_mgr.retries += 1
            state_mgr.save()
            context = question if verdict in ("NO-VERDICT", "NO-BRIDGE") else question + "\n上次回复未能执行（" + verdict + "）。当前无人值守，请自行决定后续工作或采证步骤；确实无法继续则返回 STOP/terminate_blocked 并提供 blockers 和停止依据。不得请求人工回复。"
            ivl("L2_UNAVAILABLE", verdict=verdict, attempt=attempt + 1)
            if attempt < 2:
                time.sleep(budget.bound_timeout(5.0))
        raise DecisionStopped("failed", "L2决策通道连续三次未返回有效可执行决议；详见 L2 审计")

    def consult_repair(self, failure, message, number):
        coordinator, driver, session_cwd = self.coordinator, self.driver, self.session_cwd
        budget, state_mgr = self.budget, self.state_mgr
        for attempt in range(3):
            if budget.is_expired():
                raise DecisionStopped("timeout", "REPAIR期间达到总时长上限")
            result = coordinator.handle_repair(failure, message, number, driver=driver, session_cwd=session_cwd)
            if budget.is_expired():
                raise DecisionStopped("timeout", "REPAIR期间达到总时长上限")
            if repair_action(result[0], result[1], result[3])[0] != "retry":
                return result
            state_mgr.retries += 1
            state_mgr.save()
            if attempt < 2:
                time.sleep(budget.bound_timeout(5.0))
        raise DecisionStopped("failed", "L2修复通道连续三次不可用；基础设施失败，并非L2放弃或等待人工")

