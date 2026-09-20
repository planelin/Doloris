"""
afk_supervisor.engine — 无头监管主循环与动作分派引擎 (afk / afk2)
==============================================================
驱动无头 Worker 进程生命周期；
实现类型化 next_action.type 分派：
  switch_to_repair -> REPAIR 修复 -> 重新采证 -> 发起新 REVIEW;
  terminate_blocked -> L2 有依据地停止;
  worker_fix / worker_instruction -> 指令喂回 Worker;
  terminate_success -> 终态提交前版本二次复核;
精准同步 SupervisorState 会话 ID 与计数器，原子记录已分派动作。
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from afk_supervisor.models import ActionType, DeadlineBudget, L2Result
from afk_supervisor.platform.process import log, safe_kill
from afk_supervisor.platform.windows import net_enable, net_disable, find_connected_adapter
from afk_supervisor.baseline import TaskBaseline
from afk_supervisor.coordinator import SupervisorCoordinator
from afk_supervisor.evidence import collect_evidence
from afk_supervisor.l2.bridge import AntigravityManager
from afk_supervisor.sessions.rollout import is_codex_working
from afk_supervisor.l2.protocol import extract_protocol_json, normalize_next_action
from afk_supervisor.l2.transport import clean_l2_decision_text, l2_dispatch
from afk_supervisor.reporting import generate_final_report, send_terminal_notification
from afk_supervisor.sessions.discovery import register_thread_for_codex_ui
from afk_supervisor.state import SupervisorState
from afk_supervisor.compat import get_sym
from afk_supervisor.actions import repair_action, WORKER_ACTIONS, UnattendedL2, DecisionStopped
from afk_supervisor.observations import file_size, observe_worker

BACKOFFS = [15, 45, 90, 120, 120, 120, 120, 120]
FAILS_BEFORE_SWITCH = 2
STALE_LIMITS = [600, 1200, 1800]


def run_headless_supervisor(
    driver: Any,
    args: Any,
    run_dir: Path,
    ws_path: Path,
    adopt_mode: Optional[str],
    task_baseline: TaskBaseline,
    coordinator: SupervisorCoordinator,
    state_mgr: SupervisorState,
    budget: DeadlineBudget,
    ivl: Callable[..., None],
    verify_fn: Callable[..., Tuple[bool, str]],
    agy_mgr: Optional[AntigravityManager] = None,
    proxy: Optional[str] = None,
    chaos: Optional[object] = None,
    rollout: Optional[Path] = None,
    title: str = "",
    task_md: Optional[Path] = None,
    ws_lock: Optional[object] = None,
) -> int:
    """无头长程挂机核心监管循环 (经典 resume 与 Fork 派生模式)。"""
    ts = run_dir.name
    ivl_path = run_dir / "interventions.jsonl"
    prompt_path = run_dir / "prompt.txt"
    resume_path = run_dir / "resume-prompt.txt"
    resume_path.write_text("继续\n", encoding="utf-8")
    l2_cmd = args.l2_cmd
    session_cwd = str(ws_path)

    ivl(
        "LAUNCH",
        session=driver.session_id or "(运行时发现)",
        provider=driver.provider_name,
        chaos=str(chaos) if chaos else "off",
    )

    launched_at = time.time()
    worker_runtime = 0.0
    last_runtime_tick = time.monotonic()
    chaos_fired = False
    resumes = 0
    busy_waits = 0
    l2_calls = 0
    interactions = 0
    early_exits = 0
    total_kills = 0
    net_off_adapter = None
    fails_on_provider = 0
    providers_tried = [driver.provider_name]
    last_hb_log = 0.0
    last_error_note = ""
    last_activity = ""
    outcome, outcome_detail = None, ""
    headless_l2_retries = 0
    recovery_limit = args.max_resumes
    coordinator.state_mgr = state_mgr
    state_mgr.max_interactions = args.max_interactions
    state_mgr.max_resumes = args.max_resumes

    def start_worker(kind, prompt_file, payload=None, action_type=ActionType.WORKER_INSTRUCTION, prepared=False):
        nonlocal launched_at
        request_id = (payload or {}).get("request_id", "")
        if not prepared:
            if request_id and not state_mgr.should_dispatch(request_id):
                raise DecisionStopped("failed", f"请求 {request_id} 已发送且处理状态不明确；保留检查点，禁止重复发送")
            state_mgr.record_dispatched(
                request_id, action_type, prompt_file.read_text(encoding="utf-8"),
                dispatch_offset=file_size(getattr(driver, "jsonl", None)),
                dispatch_stdout_offset=file_size(run_dir / "worker-stdout.log"),
                dispatch_target=driver.session_id or "", dispatch_at=time.time(),
            )
        state_mgr.launch_kind = kind
        state_mgr.launch_at = launched_at = time.time()
        state_mgr.event_offset = state_mgr.dispatch_offset
        state_mgr.event_path = str(Path(driver.jsonl).resolve()) if getattr(driver, "jsonl", None) else ""
        state_mgr.worker_pid = 0
        state_mgr.mark_delivery("SENDING")
        try:
            getattr(driver, kind)(prompt_file)
        except Exception:
            state_mgr.mark_delivery("UNCERTAIN")
            raise
        state_mgr.worker_pid = driver.proc.pid
        state_mgr.worker_rollout = str(driver.jsonl or "")
        if driver.session_id:
            state_mgr.set_worker_session_id(driver.session_id)
        elif kind == "fork":
            state_mgr.worker_session_id = state_mgr.sid = ""
        state_mgr.mark_delivery("SENT")
        state_mgr.transition("RUNNING", detail="Worker started; awaiting task events")

    def resume_with_action(prompt_file, payload=None, action_type=ActionType.WORKER_INSTRUCTION):
        start_worker("resume", prompt_file, payload, action_type)

    l2_gate = UnattendedL2(coordinator, driver, session_cwd, budget, state_mgr, ivl)
    decide = l2_gate.decide
    consult_repair = l2_gate.consult_repair

    try:
        # 1. 启动或接管逻辑
        if args.driver == "codex" and args.adopt:
            if adopt_mode == "fork":
                # CLI 已确认原任务暂停；不能让父任务与无头子任务并发运行。
                is_working_fn = get_sym("is_codex_working", is_codex_working)
                parent_working, parent_reason, parent_last_msg = is_working_fn(rollout) if rollout else (True, "缺少父任务轨迹", "")
                if parent_working:
                    raise DecisionStopped("failed", "原任务仍在运行，拒绝Fork以避免并发消耗token: " + parent_reason)

                from afk_supervisor.acceptance import is_interaction_request
                fork_action = None
                if is_interaction_request(parent_last_msg) and l2_cmd and l2_cmd.lower() not in ("off", "none"):
                    # 真正停在待决策处才咨询 L2；切换导致的 turn_aborted 不是新问题。
                    interactions += 1
                    instruction, payload, action = decide(parent_last_msg, interactions)
                    prompt_path.write_text(instruction.rstrip() + "\n", encoding="utf-8")
                    fork_action = ((payload or {}).get("request_id", ""), action, instruction)
                else:
                    prompt_path.write_text("继续\n", encoding="utf-8")
                    log("ADOPT    已为 Fork 准备续跑指令: 继续（原任务保持暂停）")

                # L2 决策可能耗时较长；若原任务已恢复，不能再启动子任务。
                if rollout:
                    parent_working, parent_reason, _ = is_working_fn(rollout)
                    if parent_working:
                        raise DecisionStopped("failed", "原任务在Fork前恢复运行，拒绝并发启动: " + parent_reason)
                start_worker("fork", prompt_path, {"request_id": fork_action[0]} if fork_action else None,
                             fork_action[1] if fork_action else ActionType.WORKER_INSTRUCTION)
                ivl("ADOPTED_FORK", parent_session=state_mgr.parent_session_id, pid=driver.proc.pid)
                log(f"ADOPTED FORK — 基于父会话派生无头进程 (pid={driver.proc.pid})")
            else:
                start_worker("resume", prompt_path)
                ivl("ADOPTED_RESUME", pid=driver.proc.pid)
        else:
            start_worker("launch", prompt_path)
            ivl("SPAWNED", pid=driver.proc.pid)

        state_mgr.transition("RUNNING", detail="Headless supervision loop running")

        # 2. 业务状态推进循环
        while True:
            time.sleep(budget.bound_timeout(5.0))
            now = time.time()

            if budget.is_expired():
                ivl("TERMINAL", state="TIMEOUT", detail=f"已达总时长上限 {args.max_run_sec}s")
                log("TERMINAL TIMEOUT — 达到总时长上限")
                outcome, outcome_detail = "timeout", f"已达总时长上限 {args.max_run_sec}s"
                break

            # 重新发现并同步 session_id
            if hasattr(driver, "discover_session"):
                driver.discover_session(launched_at)
                if driver.session_id:
                    state_mgr.set_worker_session_id(driver.session_id)
                    if agy_mgr and agy_mgr.codex_session_id != driver.session_id:
                        agy_mgr.set_codex_session_id(driver.session_id)

            alive = driver.proc.poll() is None
            observe_worker(state_mgr, driver)
            now_mono = time.monotonic()
            dt = now_mono - last_runtime_tick
            last_runtime_tick = now_mono
            if alive:
                worker_runtime += dt

            # Chaos 注入
            if chaos and not chaos_fired and alive:
                if chaos[0] == "kill" and worker_runtime >= chaos[1]:
                    chaos_fired = True
                    method = safe_kill(driver)
                    ivl("CHAOS_KILL", at_sec=worker_runtime, method=method)
                    total_kills += 1
                    alive = False
                elif chaos[0] == "net" and worker_runtime >= chaos[1]:
                    chaos_fired = True
                    adapter = find_connected_adapter()
                    if adapter and net_disable(adapter):
                        net_off_adapter = adapter
                        net_on_mono = time.monotonic() + chaos[2]
                        ivl("CHAOS_NET_OFF", at_sec=worker_runtime, adapter=adapter, duration_sec=chaos[2])
                    else:
                        ivl("CHAOS_NET_FAIL", reason="断网失败: 未找到已连接网卡或需管理员权限")
            if net_off_adapter and time.monotonic() >= net_on_mono:
                net_enable(net_off_adapter)
                ivl("CHAOS_NET_ON", at_sec=worker_runtime)
                net_off_adapter = None

            # 心跳检查
            if alive:
                from afk_supervisor.sessions.rollout import peek_rollout_activity
                activity = peek_rollout_activity(driver.jsonl) if driver.jsonl else ""
                if activity and activity != last_activity:
                    last_activity = activity
                    if hasattr(driver, "note_activity"):
                        driver.note_activity(now)
                    ivl("ACTIVITY", activity=activity)

                age = driver.heartbeat_age(launched_at)

                stale_limits = get_sym("STALE_LIMITS", STALE_LIMITS)
                threshold = stale_limits[min(total_kills, len(stale_limits) - 1)]
                if age > threshold:
                    ivl("DETECT_HANG", stale_sec=round(age), threshold_sec=round(threshold))
                    log(f"HANG     {age:.0f}s 无心跳 (阈值={threshold:.0f}s), 击杀并续跑")
                    safe_kill(driver)
                    total_kills += 1
                    fails_on_provider += 1
                    outcome, outcome_detail = "hang", f"{age:.0f}s 无心跳"
                else:
                    if now - last_hb_log >= 30.0:
                        ivl("HEARTBEAT", alive=True, stale_sec=round(age))
                        last_hb_log = now
                    continue
            else:
                rc = driver.proc.poll()
                if outcome is None:
                    # 检查 Worker 最后留言是否发起提问或完工审查
                    from afk_supervisor.l2.transport import worker_last_message
                    from afk_supervisor.acceptance import is_interaction_request
                    last_agent_msg = worker_last_message(run_dir)
                    if is_interaction_request(last_agent_msg):
                        interactions += 1
                        outcome, outcome_detail = "interaction", last_agent_msg
                    elif rc == 0:
                        interactions += 1
                        outcome, outcome_detail = "review", last_agent_msg or "任务完成"
                    else:
                        fails_on_provider += 1
                        ivl("EXIT_CRASH", rc=rc, provider=driver.provider_name)
                        outcome, outcome_detail = "crash", f"exit_code={rc}"

            # --- 交互决策分派 (DECIDE) ---
            if outcome == "interaction":
                if interactions > args.max_interactions:
                    ivl("TERMINAL", state="FAILED", detail=f"交互请求超过上限({interactions}次), 已达本次托管预算上限")
                    outcome, outcome_detail = "failed", f"交互请求超过上限({interactions}次)"
                    break

                instruction, payload, action = decide(outcome_detail, interactions)
                answer_path = run_dir / f"answer-{interactions}.txt"
                answer_path.write_text(instruction, encoding="utf-8")
                resume_with_action(answer_path, payload, action)
                ivl("RESUMED_WITH_DECISION", n=interactions, pid=driver.proc.pid)
                launched_at = time.time()
                outcome, outcome_detail = None, ""
                continue

            # --- 客观审查与动作分派 (REVIEW) ---
            if outcome == "review":
                if interactions > args.max_interactions:
                    ivl("TERMINAL", state="FAILED", detail=f"审查轮次超过上限({interactions}次), 需人工介入")
                    outcome, outcome_detail = "failed", f"审查轮次超过上限({interactions}次)"
                    break

                ivl("L2_CONSULT", n=interactions, kind="REVIEW", question=outcome_detail[:150])
                verdict, answer, l2_log, payload = coordinator.handle_turn_review(
                    outcome_detail, interactions, driver=driver, session_cwd=session_cwd, verify_fn=verify_fn
                )
                ivl("L2_ANSWER", verdict=verdict, answer=answer[:150], log=str(l2_log))

                norm_next = payload.get("next_action") if (payload and isinstance(payload.get("next_action"), dict)) else {}
                act_type = norm_next.get("type", "")

                if verdict == "STOP" and act_type == ActionType.TERMINATE_BLOCKED:
                    outcome, outcome_detail = "blocked", "L2决定停止: " + norm_next.get("instructions", "") + "；证据: " + str((payload or {}).get("blockers", []))
                    break

                # Legacy responses cannot authorize a worker action.
                if verdict == "DEFER" or act_type == ActionType.REQUEST_USER:
                    outcome, outcome_detail = "interaction", "审查返回了旧式转人工结果，请重新决定后续工作。\n" + answer
                    continue

                # 分支 A: switch_to_repair -> REPAIR 修复 -> 重新采证与 REVIEW
                if act_type == ActionType.SWITCH_TO_REPAIR:
                    log("ACTION   L2 审查指示切换至 REPAIR 模式，启动自动化环境/配置修复闭环...")
                    state_mgr.transition("REPAIRING", detail=norm_next.get("instructions", "转入修复模式"))
                    rep_verdict, rep_answer, rep_log, rep_payload = consult_repair(
                        norm_next.get("instructions", "审查建议修复"), outcome_detail, coordinator.repair_count + 1,
                    )
                    ivl("REPAIR_ACTION_RESULT", verdict=rep_verdict, answer=rep_answer[:150])

                    route, text = repair_action(rep_verdict, rep_answer, rep_payload)
                    if route == "blocked":
                        outcome, outcome_detail = "blocked", "L2决定停止修复: " + text
                        break
                    if route == "review":
                        interactions += 1
                        outcome, outcome_detail = "review", f"修复后重新验证: {text}"
                        continue
                    if route == "worker":
                        answer_path = run_dir / f"repair-alternative-{coordinator.repair_count}.txt"
                        answer_path.write_text(clean_l2_decision_text(text), encoding="utf-8")
                        resume_with_action(answer_path, rep_payload, rep_payload["next_action"]["type"])
                        outcome, outcome_detail = None, ""
                        continue
                    # Transport failures retry REPAIR, not REVIEW or an absent user.
                    if route == "retry":
                        outcome, outcome_detail = "repair", norm_next.get("instructions", "审查建议修复")
                    else:
                        outcome, outcome_detail = "interaction", text
                    continue

                # 分支 C: 审查通过 PASS -> 终态前版本复核
                if verdict in ("PASS", "COMPLETED"):
                    ok_acc, acc_detail, review_again = coordinator.check_completion(outcome_detail, payload, verify_fn)
                    if budget.is_expired():
                        outcome, outcome_detail = "timeout", "最终验收期间达到总时长上限"
                        break
                    if review_again:
                        ivl("REVISION_MUTATED_BEFORE_EXIT", detail=acc_detail)
                        interactions += 1
                        outcome, outcome_detail = "review", outcome_detail
                        continue
                    if ok_acc:
                        ivl("EXIT_OK", acceptance=acc_detail)
                        outcome, outcome_detail = "success", acc_detail
                        break
                    else:
                        early_exits += 1
                        cleaned_answer = f"显式验收未通过: {acc_detail}。请继续完成未达标项。"
                        answer_path = run_dir / f"answer-{interactions}.txt"
                        answer_path.write_text(cleaned_answer, encoding="utf-8")
                        resume_with_action(answer_path, payload, act_type or ActionType.WORKER_INSTRUCTION)
                        ivl("RESUMED_WITH_DECISION", n=interactions, pid=driver.proc.pid)
                        launched_at = time.time()
                        outcome, outcome_detail = None, ""
                        continue

                # 分支 D: L2 通道异常
                if verdict in ("NO-VERDICT", "NO-BRIDGE", "PROTOCOL_ERROR") or not answer.strip():
                    headless_l2_retries += 1
                    state_mgr.retries += 1
                    state_mgr.save()
                    if headless_l2_retries >= 3:
                        ivl("TERMINAL", state="FAILED", detail=f"L2审查通道异常 ({verdict})，已达重试上限")
                        outcome, outcome_detail = "failed", f"L2通道异常 ({verdict})"
                        break
                    ivl("L2_UNAVAILABLE", verdict=verdict, action=f"等待重试 ({headless_l2_retries}/3)")
                    time.sleep(budget.bound_timeout(5.0))
                    continue

                # 分支 E: 普通 FAIL 或 INCONCLUSIVE (worker_fix / gather_evidence)
                headless_l2_retries = 0
                early_exits += 1
                cleaned_answer = clean_l2_decision_text(answer) or "请根据需求清单继续推进项目并交付目标成果。"
                answer_path = run_dir / f"answer-{interactions}.txt"
                answer_path.write_text(cleaned_answer, encoding="utf-8")
                resume_with_action(answer_path, payload, act_type or ActionType.WORKER_INSTRUCTION)
                ivl("RESUMED_WITH_DECISION", n=interactions, pid=driver.proc.pid)
                launched_at = time.time()
                outcome, outcome_detail = None, ""
                continue

            # --- 异常续跑与供应商轮换 ---
            if outcome in ("crash", "hang", "early_exit"):
                if resumes >= recovery_limit:
                    l2_enabled = l2_cmd and l2_cmd.lower() not in ("off", "none")
                    if l2_enabled and l2_calls < getattr(args, "l2_max", 2):
                        l2_calls += 1
                        ivl("L2_ESCALATE", agent=l2_cmd, call=l2_calls, kind="repair")
                        err_log = run_dir / "worker-stderr.log"
                        err_tail = err_log.read_text(encoding="utf-8", errors="replace")[-800:] if err_log.exists() else ""
                        rep_v, rep_answer, rep_log, rep_payload = consult_repair(outcome_detail, err_tail, l2_calls)
                        ivl("L2_RESULT", verdict=rep_v, log=str(rep_log))
                        route, text = repair_action(rep_v, rep_answer, rep_payload)
                        if route == "blocked":
                            outcome, outcome_detail = "blocked", "L2决定停止修复: " + text
                            break
                        if route == "worker":
                            answer_path = run_dir / f"repair-alternative-{coordinator.repair_count}.txt"
                            answer_path.write_text(clean_l2_decision_text(text), encoding="utf-8")
                            resume_with_action(answer_path, rep_payload, rep_payload["next_action"]["type"])
                            outcome, outcome_detail = None, ""
                            continue
                        if route == "decide":
                            outcome, outcome_detail = "interaction", text
                            continue
                        # Keep historical retry counts; extend the limit, never reset counters.
                        recovery_limit += 4
                        state_mgr.max_resumes = recovery_limit
                        state_mgr.save()
                        ivl("L2_BUDGET_GRANTED", extra=4)
                    else:
                        ivl("TERMINAL", state="FAILED", detail=f"续跑预算耗尽({resumes}次)")
                        outcome, outcome_detail = "failed", f"续跑预算耗尽({resumes}次)"
                        break

                backoffs = get_sym("BACKOFFS", BACKOFFS)
                wait_sec = backoffs[min(resumes, len(backoffs) - 1)]
                switch_after = get_sym("FAILS_BEFORE_SWITCH", FAILS_BEFORE_SWITCH)
                if (fails_on_provider >= switch_after and hasattr(driver, "has_next")
                        and driver.has_next() and hasattr(driver, "switch_provider")):
                    provider = driver.switch_provider()
                    if provider:
                        providers_tried.append(provider)
                        fails_on_provider = 0
                        wait_sec = 10
                        ivl("PROVIDER_SWITCH", to=provider, after_failures=switch_after, wait_sec=wait_sec)
                ivl("RESUME_WAIT", backoff_sec=wait_sec, attempt=resumes + 1, reason=outcome)
                time.sleep(budget.bound_timeout(wait_sec))
                if budget.is_expired():
                    outcome, outcome_detail = "timeout", "故障续跑前达到总时长上限"
                    break
                resumes += 1
                state_mgr.resumes = resumes
                state_mgr.save()
                resume_with_action(resume_path)
                ivl("RESUMED", attempt=resumes, pid=driver.proc.pid)
                launched_at = time.time()
                outcome, outcome_detail = None, ""
                continue

    except DecisionStopped as stopped:
        outcome, outcome_detail = stopped.outcome, stopped.detail
    except KeyboardInterrupt:
        outcome, outcome_detail = "waiting_user", "用户中断监管"
        ivl("TERMINAL", state="WAITING_USER", detail=outcome_detail)
    except Exception as error:
        outcome, outcome_detail = "failed", f"监管异常: {type(error).__name__}: {error}"
        ivl("SUPERVISOR_ERROR", detail=outcome_detail)
    finally:
        if ws_lock:
            try:
                ws_lock.release()
            except Exception:
                pass
        if net_off_adapter:
            try:
                net_enable(net_off_adapter)
            except Exception:
                pass
        if driver and hasattr(driver, "kill_tree"):
            try:
                if driver.proc and driver.proc.poll() is None:
                    driver.kill_tree()
            except Exception:
                pass
        if agy_mgr:
            try:
                agy_mgr.teardown()
            except Exception:
                pass

    # 3. 终态登记与报告生成
    if adopt_mode == "fork" and driver.session_id and driver.session_id != state_mgr.parent_session_id:
        register_thread_for_codex_ui(driver.session_id, parent_id=getattr(driver, "_fork_parent_id", None))
        log(f"UI_SYNC  派生会话 {driver.session_id[:8]} 已确认同步至 Codex 侧边栏")

    # Reuse the final gate result, not a second context-free acceptance call.
    detail = outcome_detail
    if outcome == "success":
        state = "SUCCESS"
    elif outcome == "waiting_user":
        state = "WAITING_USER"
    elif outcome == "blocked":
        state = "BLOCKED"
    elif outcome == "timeout":
        state = "TIMEOUT"
    else:
        state = "FAILED"

    state_mgr.transition(state, detail=outcome_detail or detail)
    ivl("TERMINAL", state=state, detail=outcome_detail or detail)

    generate_final_report(
        run_dir=run_dir,
        state=state,
        detail=outcome_detail or detail,
        ts=ts,
        session_id=driver.session_id or "(未知)",
        jsonl_path=getattr(driver, "jsonl", None),
        providers_tried=providers_tried,
        resumes=resumes,
        chaos=chaos,
        ivl_path=ivl_path,
        title=title,
        baseline=task_baseline,
    )
    log(f"TERMINAL {state} — SHUTDOWN WOULD HAPPEN HERE")
    return 0 if state == "SUCCESS" else (2 if state == "WAITING_USER" else 1)
