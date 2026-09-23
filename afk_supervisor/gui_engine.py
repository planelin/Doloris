"""
afk_supervisor.gui_engine — 双有头原生 GUI 监管主循环 (afk3)
=============================================================
守望 Codex 桌面端 App，监听生命周期契约；
通过 Windows UI Automation 动态注入 L2 指令；
与无头模式共享无人值守 DECIDE / REPAIR 动作分派
与客观证据验收标准。
"""

import hashlib
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from afk_supervisor.models import ActionType, DeadlineBudget, delivery_result
from afk_supervisor.platform.gui import (
    DummyDriver,
    ensure_codex_window_restored,
    inject_into_codex_gui,
)
from afk_supervisor.platform.process import log
from afk_supervisor.baseline import extract_task_baseline
from afk_supervisor.coordinator import SupervisorCoordinator
from afk_supervisor.l2.bridge import AntigravityManager
from afk_supervisor.l2.transport import clean_l2_decision_text
from afk_supervisor.reporting import generate_final_report
from afk_supervisor.sessions.rollout import is_codex_working
from afk_supervisor.state import SupervisorState
from afk_supervisor.compat import get_sym
from afk_supervisor.actions import UnattendedL2, DecisionStopped, repair_action, WORKER_ACTIONS, CHANNEL_ERRORS
from afk_supervisor.observations import read_events, command_accepted, file_size

# 注入回执窗口：确认窗口从"最后一次发送返回"起算，注入脚本自身耗时不得挤占它；
# 明确未送达 (NOT_SENT) 时只按有界重试预算判定，否则慢速发送会提前掐断重试。
GUI_ACK_WINDOW_SEC = 30.0
GUI_RETRY_INTERVAL_SEC = 6.0
GUI_RETRY_TOTAL_SEC = 90.0
GUI_MAX_ATTEMPTS = 3
# 心跳间隔：GUI 模式过去只在状态发生变化时写日志，长时间等待期间用户在界面上
# 看不到任何输出，会误判为"接管后无反应/卡死"。这里按固定间隔输出可观测心跳。
GUI_HEARTBEAT_INTERVAL_SEC = 30.0


def run_gui_supervisor(
    sid: str,
    rollout: Path,
    scwd: str,
    title: str,
    args: Any,
    run_dir: Path,
    agy_mgr: Optional[AntigravityManager],
    proxy: Optional[str],
    verify_fn: Callable[..., Tuple[bool, str]],
    ivl: Optional[Callable[..., None]] = None,
    budget: Optional[DeadlineBudget] = None,
    ws_lock: Optional[object] = None,
    state_mgr: Optional[SupervisorState] = None,
    coordinator: Optional[SupervisorCoordinator] = None,
) -> int:
    """双有头交互监管守护循环。"""
    if ivl is None:
        def ivl(event, **kw):
            return None
    if budget is None:
        budget = DeadlineBudget(args.max_run_sec)

    ivl_path = run_dir / "interventions.jsonl"
    l2_cmd = None if args.l2_cmd.strip().lower() in ("off", "none") else args.l2_cmd
    max_interactions = args.max_interactions
    interactions = 0
    injections = 0
    l2_channel_failures = 0
    last_handled_msg_hash = ""
    pending_injection = None
    last_activity = ""
    payload = None

    driver = DummyDriver(sid, Path(scwd), title=title)

    log(f"LAUNCH_GUI session={sid[:8]} title='{title}' cwd={scwd}")
    log("GUI ==========================================================")
    log("GUI [双有头模式守则] 请保持 Codex 桌面窗口显示，切勿最小化到任务栏！")
    log("GUI 提示: 可以被IDE或其他窗口遮挡覆盖，但严禁点击最小化按钮")
    log("GUI ==========================================================")

    if state_mgr is None:
        state_mgr = SupervisorState(run_dir, sid=sid, mode="gui", ws=Path(scwd))
        state_mgr.transition("RUNNING", detail=f"GUI supervision started for {sid[:8]}")

    if coordinator is None:
        task_bl = extract_task_baseline(
            rollout_path=Path(rollout) if rollout else None,
            session_cwd=Path(scwd),
            title=title,
            work_dir=args.work_dir,
        )
        coordinator = SupervisorCoordinator(
            run_dir=run_dir,
            workspace_root=Path(scwd),
            delivery_dir=Path(task_bl.delivery_dir),
            task_baseline=task_bl,
            agy_mgr=agy_mgr,
            l2_cmd=l2_cmd,
            args=args,
            proxy=proxy,
            budget=budget,
            ivl_fn=ivl,
        )

    coordinator.state_mgr = state_mgr
    state_mgr.max_interactions = max_interactions
    state_mgr.transition("RUNNING", detail="GUI supervision loop running")

    heartbeat_path = Path(rollout) if rollout else None
    loop_start = time.monotonic()
    last_heartbeat = loop_start
    # 最近一次注入回执 (含身份探针结论)：心跳与终局报告都要带上它，
    # 否则权限/可访问性树这类基础设施失败只能表现为"接管后无反应"。
    last_delivery = {"status": "-", "detail": "-"}

    def beat(phase: str, force: bool = False) -> None:
        """周期性写入 GUI_WAIT 心跳，证明守护循环仍在推进而非卡死。"""
        nonlocal last_heartbeat
        now = time.monotonic()
        if not force and now - last_heartbeat < GUI_HEARTBEAT_INTERVAL_SEC:
            return
        last_heartbeat = now
        try:
            size_now = file_size(heartbeat_path) if heartbeat_path else 0
        except OSError:
            size_now = -1
        elapsed_sec = int(now - loop_start)
        delivery_note = last_delivery["detail"] or "-"
        if last_delivery["status"] not in ("", "-"):
            delivery_note = f"[{last_delivery['status']}] {delivery_note}"
        log(f"GUI_WAIT phase={phase} size={size_now} "
            f"activity={last_activity or '-'} delivery={delivery_note} elapsed={elapsed_sec}s")
        ivl("GUI_WAIT", phase=phase, size=size_now,
            activity=last_activity or "-", delivery_status=last_delivery["status"],
            delivery_detail=last_delivery["detail"] or "-", elapsed_sec=elapsed_sec)

    def finish(state: str, detail: str) -> int:
        state_mgr.transition(state, detail=detail)
        if ws_lock:
            try:
                ws_lock.release()
            except Exception:
                pass
        if agy_mgr:
            try:
                agy_mgr.teardown()
            except Exception:
                pass

        generate_final_report(
            run_dir=run_dir,
            state=state,
            detail=detail,
            ts=run_dir.name,
            session_id=sid,
            jsonl_path=Path(rollout) if rollout else None,
            providers_tried=["codex-desktop(GUI)"],
            resumes=injections,
            chaos=None,
            ivl_path=ivl_path,
            title=title,
            baseline=coordinator.task_baseline,
        )
        ivl("TERMINAL", state=state, detail=detail)
        log(f"TERMINAL {state} — SHUTDOWN WOULD HAPPEN HERE")
        return 0 if state == "SUCCESS" else (2 if state == "WAITING_USER" else 1)

    l2_gate = UnattendedL2(coordinator, driver, scwd, budget, state_mgr, ivl)
    is_working_fn = get_sym("is_codex_working", is_codex_working)

    def send_pending():
        # Persist before invoking the UI. An exception after invocation is uncertain.
        state_mgr.dispatch_attempts += 1
        state_mgr.mark_delivery("SENDING")
        try:
            result = delivery_result(get_sym("inject_into_codex_gui", inject_into_codex_gui)(
                pending_injection["text"], target_sid=sid, target_title=title, rollout_path=Path(rollout)))
            status, detail = result.status, result.detail
        except Exception as error:
            status, detail = "UNCERTAIN", f"{type(error).__name__}: {error}"
        # Even an ACCEPTED receipt must be corroborated by the exact new event.
        if status == "ACCEPTED":
            status = "SENT"
        if status not in {"NOT_SENT", "SENT", "UNCERTAIN"}:
            status = "UNCERTAIN"
        pending_injection["status"] = status
        pending_injection["last_try"] = time.monotonic()
        state_mgr.mark_delivery(status)
        last_delivery["status"] = status
        last_delivery["detail"] = str(detail)
        ivl("GUI_DELIVERY", status=status, attempt=state_mgr.dispatch_attempts, detail=detail)
        # 立即强制一条心跳：注入失败的原因不能等到下一个 30s 周期才可见。
        beat(f"delivery_{status.lower()}", force=True)

    try:
        while True:
            time.sleep(budget.bound_timeout(3.0))
            if budget.is_expired():
                return finish("TIMEOUT", f"已达总时长上限 {args.max_run_sec}s")
            get_sym("ensure_codex_window_restored", ensure_codex_window_restored)()
            p_roll = Path(rollout)

            if pending_injection:
                beat("awaiting_ack")
                events, offset = read_events(p_roll, state_mgr.event_offset)
                state_mgr.event_offset = offset
                if command_accepted(events, pending_injection["text"]):
                    ivl("GUI_INJECT_ACK", kind=pending_injection["kind"], evidence="new_matching_user_event")
                    state_mgr.acknowledge_command()
                    pending_injection = None
                    continue
                state_mgr.save()
                now = time.monotonic()
                if pending_injection["status"] == "NOT_SENT":
                    # 明确未送达：没有在途指令可等待确认，只按重试总预算退出。
                    if now - pending_injection["t0"] >= GUI_RETRY_TOTAL_SEC:
                        return finish("FAILED", "GUI 指令始终未送达；已超出有界重试总预算，禁止盲目重发。"
                                                f"最后回执: {last_delivery['detail']}")
                    if now - pending_injection["last_try"] >= GUI_RETRY_INTERVAL_SEC:
                        if state_mgr.dispatch_attempts >= GUI_MAX_ATTEMPTS:
                            return finish("FAILED", "GUI 连续三次明确未发送；基础设施失败，未重复执行任务。"
                                                    f"最后回执: {last_delivery['detail']}")
                        # Only an explicit NOT_SENT receipt allows a retry, and only
                        # while the same stopped turn still owns the composer.
                        if not is_working_fn(p_roll)[0] and file_size(p_roll) == state_mgr.dispatch_offset:
                            send_pending()
                elif now - pending_injection["last_try"] >= GUI_ACK_WINDOW_SEC:
                    return finish("FAILED", "GUI 指令接收未确认；保留在途指令和送达状态，禁止盲目重发")
                continue

            if not p_roll.exists():
                beat("rollout_missing")
                continue
            working, reason, task_agent_msg = is_working_fn(p_roll)
            if working:  # Includes unknown/partial/unreadable evidence.
                if reason != last_activity:
                    ivl("ACTIVITY", activity=reason)
                    last_activity = reason
                beat("worker_active")
                continue
            observed_offset = file_size(p_roll)
            last_msg = task_agent_msg
            msg_hash = hashlib.sha256(f"{observed_offset}:{last_msg}".encode("utf-8")).hexdigest()
            if msg_hash == last_handled_msg_hash:
                beat("no_new_turn")
                continue

            payload = None
            inject_kind = "Worker continuation"
            action_type = ActionType.WORKER_INSTRUCTION
            if l2_cmd:
                interactions += 1
                if interactions > max_interactions:
                    return finish("FAILED", f"交互请示超过上限({interactions}次)")
                from afk_supervisor.acceptance import is_interaction_request
                if is_interaction_request(last_msg):
                    inject_text, payload, action_type = l2_gate.decide(last_msg, interactions)
                    inject_kind = "AGY主管决策"
                else:
                    verdict, answer, l2_log, payload = coordinator.handle_turn_review(
                        last_msg, interactions, driver=driver, session_cwd=scwd, verify_fn=verify_fn)
                    if budget.is_expired():
                        return finish("TIMEOUT", "L2审查期间达到总时长上限")
                    ivl("L2_ANSWER", verdict=verdict, answer=answer[:150], log=str(l2_log))
                    action = (payload or {}).get("next_action") or {}
                    action_type = action.get("type", "")
                    if verdict == "STOP" and action_type == ActionType.TERMINATE_BLOCKED and (payload or {}).get("blockers"):
                        return finish("BLOCKED", action.get("instructions", answer) + "；证据: " + str(payload["blockers"]))
                    if verdict in CHANNEL_ERRORS or not answer.strip():
                        l2_channel_failures += 1
                        state_mgr.retries += 1
                        state_mgr.save()
                        if l2_channel_failures >= 3:
                            return finish("FAILED", f"AGY主管通道连续三次异常 ({verdict})；不是等待人工决策")
                        continue
                    l2_channel_failures = 0
                    if action_type == ActionType.SWITCH_TO_REPAIR:
                        result = l2_gate.consult_repair(action.get("instructions", answer), last_msg, coordinator.repair_count + 1)
                        route, text = repair_action(result[0], result[1], result[3])
                        if route == "blocked":
                            return finish("BLOCKED", text)
                        if route == "review":
                            continue  # Always collect fresh evidence and REVIEW again.
                        if route == "worker":
                            payload = result[3]
                            inject_text = clean_l2_decision_text(text)
                            action_type = payload["next_action"]["type"]
                        else:
                            inject_text, payload, action_type = l2_gate.decide(text, interactions)
                        inject_kind = "AGY修复替代方案"
                    elif verdict == "DEFER" or action_type == ActionType.REQUEST_USER:
                        inject_text, payload, action_type = l2_gate.decide(
                            "审查返回旧式转人工结果，请决定可执行步骤或有依据地停止。\n" + answer, interactions)
                        inject_kind = "AGY重新决策"
                    elif verdict in ("PASS", "COMPLETED"):
                        ok, detail, review_again = coordinator.check_completion(last_msg, payload, verify_fn)
                        if budget.is_expired():
                            return finish("TIMEOUT", "最终验收期间达到总时长上限")
                        if review_again:
                            ivl("REVISION_MUTATED_BEFORE_EXIT", detail=detail)
                            continue
                        # A user may have resumed the desktop task while L2 ran.
                        if is_working_fn(p_roll)[0] or file_size(p_roll) != observed_offset:
                            continue
                        if ok:
                            return finish("SUCCESS", f"AGY主管确认完成且验收达标: {detail}")
                        inject_text = f"显式验收未达标（{detail}）。请完成未达标项。"
                        action_type = ActionType.WORKER_FIX
                    elif action_type in WORKER_ACTIONS:
                        inject_text = clean_l2_decision_text(action.get("instructions") or answer)
                    else:
                        inject_text, payload, action_type = l2_gate.decide(
                            "审查未提供有效工作指令，请决定后续步骤。\n" + answer, interactions)
            else:
                ok, detail = verify_fn(custom_last_msg=last_msg, min_mtime=p_roll.stat().st_ctime - 120)
                if is_working_fn(p_roll)[0] or file_size(p_roll) != observed_offset:
                    continue
                if ok:
                    return finish("SUCCESS", detail)
                inject_text = "继续推进项目，完成尚未达标的要求。"

            # L2 may have taken minutes: never inject into a restarted/unknown turn.
            if is_working_fn(p_roll)[0] or file_size(p_roll) != observed_offset:
                ivl("GUI_DISPATCH_DEFERRED", detail="目标任务在咨询期间发生变化，重新观察")
                continue
            if not inject_text.strip():
                return finish("FAILED", "L2 指令为空，未向 Worker 注入")
            request_id = (payload or {}).get("request_id", "")
            if request_id and not state_mgr.should_dispatch(request_id):
                return finish("FAILED", f"请求 {request_id} 已发送；保留检查点，禁止重复发送")
            state_mgr.record_dispatched(request_id, action_type, inject_text,
                dispatch_offset=observed_offset, event_offset=observed_offset,
                dispatch_target=sid, dispatch_at=time.time(), event_path=str(p_roll.resolve()))
            injections += 1
            state_mgr.resumes = injections
            state_mgr.save()
            # t0 只用于 NOT_SENT 重试总预算；确认窗口从每次 send_pending 返回后起算。
            pending_injection = {"text": inject_text, "kind": inject_kind, "t0": time.monotonic()}
            last_handled_msg_hash = msg_hash
            send_pending()
    except DecisionStopped as error:
        return finish(error.outcome.upper(), error.detail)
    except KeyboardInterrupt:
        return finish("CANCELLED", "用户中断 GUI 监管")
    except Exception as error:
        ivl("SUPERVISOR_ERROR", detail=f"{type(error).__name__}: {error}")
        return finish("FAILED", f"GUI 监管异常: {type(error).__name__}: {error}")
