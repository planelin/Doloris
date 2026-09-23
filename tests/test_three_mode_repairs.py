"""Three-mode repair regressions. Real state/loops; no live UI, network or workers."""
import contextlib
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import supervise
from afk_supervisor import cli
from afk_supervisor.models import DeliveryResult, L2Result
from afk_supervisor.observations import command_accepted, read_events
from afk_supervisor.sessions.rollout import codex_session_state, is_codex_working
from tests.test_supervisor_loops import LoopFixture


def event(kind, **fields):
    return json.dumps({"type": "event_msg", "payload": {"type": kind, **fields}}, ensure_ascii=False) + "\n"


class ThreeModeRepairs(LoopFixture):
    def setUp(self):
        super().setUp()
        self.rollout = self.root / "parent.jsonl"
        self.rollout.write_text(event("task_complete", last_agent_message="Neutral report"), encoding="utf-8")

    def append(self, data):
        with self.rollout.open("a", encoding="utf-8") as stream:
            stream.write(data)

    def instruction(self, *args, **kwargs):
        payload = self.payload(kwargs["evidence_packet"], "FAIL", "worker_fix")
        payload["next_action"]["instructions"] = "修复交付物并运行测试"
        return L2Result("FAIL", json.dumps(payload), self.run / "l2.log", payload=payload)

    def accepted_send(self, text, **kwargs):
        self.assertEqual(kwargs["target_sid"], "worker")
        self.assertEqual(kwargs["rollout_path"], self.rollout)
        self.append(event("user_message", message=text) + event("task_started") +
                    event("task_complete", last_agent_message="Updated result"))
        return DeliveryResult("SENT", "simulated UI")

    def run_observed_gui(self, dispatch=None, inject_fn=None):
        return self.run_gui(dispatch=dispatch or self.instruction,
                            inject_fn=inject_fn, rollout=self.rollout, working=is_codex_working)

    def test_unknown_evidence_never_means_idle(self):
        stopped = event("task_complete", last_agent_message="done")
        for content in (None, "", "{}\n", "broken\n", stopped + '{"type":',
                        stopped.rstrip(), stopped + event("user_message", message="new request"),
                        stopped + event("future_activity"), stopped + event("agent_message", message="new output")):
            with self.subTest(content=content):
                if content is None:
                    self.rollout.unlink(missing_ok=True)
                else:
                    self.rollout.write_text(content, encoding="utf-8")
                self.assertTrue(is_codex_working(self.rollout)[0])
        self.rollout.write_text(stopped + event("token_count"), encoding="utf-8")
        self.assertEqual(codex_session_state(self.rollout)["status"], "stopped")
        self.assertEqual(is_codex_working(self.rollout)[2], "done")

    def test_unknown_rollout_never_calls_l2_or_gui(self):
        self.append('{"type":')
        dispatch = Mock()
        rc, inject = self.run_observed_gui(dispatch=dispatch)
        self.assertEqual(rc, 1)
        self.assertEqual(self.state.state, "TIMEOUT")
        dispatch.assert_not_called()
        inject.assert_not_called()

    def test_missing_parent_cannot_launch_fork(self):
        rc, driver = self.run_headless(mode="fork")
        self.assertEqual(rc, 1)
        self.assertFalse(driver.calls)

    def test_uncertain_delivery_never_resends_and_retains_checkpoint(self):
        rc, inject = self.run_observed_gui(inject_fn=lambda *a, **kw: DeliveryResult("UNCERTAIN", "timeout after click"))
        self.assertEqual(rc, 1)
        inject.assert_called_once()
        self.assertEqual(self.state.state, "FAILED")
        self.assertEqual(self.state.dispatch_status, "UNCERTAIN")
        self.assertTrue(self.state.pending_command)
        saved = json.loads((self.run / "supervisor_state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["dispatch_attempts"], 1)
        self.assertEqual(saved["dispatch_status"], "UNCERTAIN")

    def test_gui_exception_after_invocation_is_uncertain_and_not_retried(self):
        def send(*args, **kwargs):
            raise OSError("lost response after potential send")
        rc, inject = self.run_observed_gui(inject_fn=send)
        self.assertEqual(rc, 1)
        inject.assert_called_once()
        self.assertEqual(self.state.dispatch_status, "UNCERTAIN")
        self.assertTrue(self.state.pending_command)

    def test_accepted_ui_receipt_without_event_is_not_ack(self):
        rc, inject = self.run_observed_gui(inject_fn=lambda *a, **kw: DeliveryResult("ACCEPTED"))
        self.assertEqual(rc, 1)
        inject.assert_called_once()
        self.assertEqual(self.state.dispatch_status, "SENT")
        self.assertFalse(any(e == "GUI_INJECT_ACK" for e, _ in self.events))

    def test_not_sent_is_not_retried_if_parent_restarts(self):
        def send(*args, **kwargs):
            self.append(event("task_started"))
            return DeliveryResult("NOT_SENT")
        rc, inject = self.run_observed_gui(inject_fn=send)
        self.assertEqual(rc, 1)
        inject.assert_called_once()
        self.assertEqual(self.state.dispatch_status, "NOT_SENT")

    def test_sent_mtime_and_unrelated_events_are_not_ack(self):
        def send(text, **kwargs):
            os.utime(self.rollout, (self.clock.now, self.clock.now))
            self.append(event("token_count") + event("task_started") + event("user_message", message="unrelated"))
            return DeliveryResult("SENT")
        rc, inject = self.run_observed_gui(inject_fn=send)
        self.assertEqual(rc, 1)
        inject.assert_called_once()
        self.assertEqual(self.state.dispatch_status, "SENT")
        self.assertFalse(any(e == "GUI_INJECT_ACK" for e, _ in self.events))

    def test_injection_latency_does_not_consume_confirmation_window(self):
        """注入脚本自身耗时(最长约18s)不得挤占事件确认窗口，否则慢速注入必然误判失败。"""
        sent, appended = [], []
        def send(text, **kwargs):
            sent.append(text)
            self.clock.sleep(25)                       # 模拟 UI Automation 注入耗时
            return DeliveryResult("SENT", "simulated slow UI")
        start = self.clock.now
        original_sleep = self.clock.sleep
        def scheduled_sleep(seconds):
            original_sleep(seconds)
            if sent and not appended and self.clock.now >= start + 36:
                self.append(event("user_message", message=sent[0]) + event("task_started") +
                            event("task_complete", last_agent_message="Updated result"))
                appended.append(True)
        self.clock.sleep = scheduled_sleep
        self.addCleanup(lambda: setattr(self.clock, "sleep", original_sleep))
        reviews = []
        def dispatch(*args, **kwargs):
            reviews.append(kwargs["mode"])
            return self.instruction(*args, **kwargs) if len(reviews) == 1 else self.pass_dispatch(*args, **kwargs)

        rc, inject = self.run_observed_gui(dispatch=dispatch, inject_fn=send)

        self.assertEqual(rc, 0, "慢速注入后仍须等待新事件确认，不得按发送前计时提前判失败")
        self.assertEqual(inject.call_count, 1)
        self.assertEqual(sum(e == "GUI_INJECT_ACK" for e, _ in self.events), 1)

    def test_matching_historical_command_cannot_ack_new_send(self):
        self.rollout.write_text(event("user_message", message="修复交付物并运行测试") +
                                event("task_complete", last_agent_message="Neutral report"), encoding="utf-8")
        rc, inject = self.run_observed_gui(inject_fn=lambda *a, **kw: DeliveryResult("SENT"))
        self.assertEqual(rc, 1)
        inject.assert_called_once()
        self.assertTrue(self.state.pending_command)
        self.assertFalse(any(e == "GUI_INJECT_ACK" for e, _ in self.events))

    def test_matching_new_command_ack_then_fresh_review_success(self):
        reviews = []
        def dispatch(*args, **kwargs):
            reviews.append(kwargs["mode"])
            return self.instruction(*args, **kwargs) if len(reviews) == 1 else self.pass_dispatch(*args, **kwargs)
        rc, inject = self.run_observed_gui(dispatch=dispatch, inject_fn=self.accepted_send)
        self.assertEqual(rc, 0)
        inject.assert_called_once()
        self.assertEqual(reviews, ["REVIEW", "REVIEW"])
        self.assertEqual(self.state.dispatch_status, "ACCEPTED")
        self.assertFalse(self.state.pending_command)
        self.assertEqual(sum(e == "GUI_INJECT_ACK" for e, _ in self.events), 1)

    def test_only_explicit_not_sent_allows_bounded_retry(self):
        rc, inject = self.run_observed_gui(inject_fn=lambda *a, **kw: DeliveryResult("NOT_SENT", "identity unavailable"))
        self.assertEqual(rc, 1)
        self.assertEqual(inject.call_count, 3)
        self.assertEqual(self.state.dispatch_status, "NOT_SENT")
        self.assertEqual(self.state.dispatch_attempts, 3)

    def test_blocked_delivery_reason_reaches_heartbeat_and_terminal_detail(self):
        """身份探针的环境性失败必须落在心跳与终局结论里，否则用户只会看到"接管后无反应"。"""
        reason = "GUI 身份探针判定不可送达 [UIA_ACCESS_DENIED]: 目标桌面端以更高权限运行"
        rc, inject = self.run_observed_gui(inject_fn=lambda *a, **kw: DeliveryResult("NOT_SENT", reason))

        self.assertEqual(rc, 1)
        deliveries = [detail for event_name, detail in self.events if event_name == "GUI_DELIVERY"]
        self.assertTrue(deliveries, "每次注入尝试都必须留下回执事件")
        self.assertTrue(all(item["detail"] == reason for item in deliveries))
        heartbeats = [detail for event_name, detail in self.events if event_name == "GUI_WAIT"]
        self.assertTrue(heartbeats, "长等待期间必须持续输出可观测心跳")
        self.assertTrue(all(item["delivery_status"] == "NOT_SENT" for item in heartbeats))
        self.assertTrue(all(item["delivery_detail"] == reason for item in heartbeats))
        self.assertIn("UIA_ACCESS_DENIED", self.state.detail, "终局结论必须带上基础设施失败原因")

    def test_not_sent_then_accepted_executes_only_once(self):
        attempts, reviews = [], []
        def send(text, **kwargs):
            attempts.append(text)
            return DeliveryResult("NOT_SENT") if len(attempts) == 1 else self.accepted_send(text, **kwargs)
        def dispatch(*args, **kwargs):
            reviews.append(kwargs["mode"])
            return self.instruction(*args, **kwargs) if len(reviews) == 1 else self.pass_dispatch(*args, **kwargs)
        rc, inject = self.run_observed_gui(dispatch=dispatch, inject_fn=send)
        self.assertEqual(rc, 0)
        self.assertEqual(inject.call_count, 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(sum(e == "GUI_INJECT_ACK" for e, _ in self.events), 1)

    def test_partial_event_requires_newline_before_ack(self):
        offset = self.rollout.stat().st_size
        self.append(event("user_message", message="command").rstrip())
        events, next_offset = read_events(self.rollout, offset)
        self.assertFalse(command_accepted(events, "command"))
        self.assertEqual(offset, next_offset)
        self.append("\n")
        events, next_offset = read_events(self.rollout, offset)
        self.assertTrue(command_accepted(events, "command"))
        self.assertGreater(next_offset, offset)

    def test_gui_repair_worker_alternative_continues_to_acceptance(self):
        modes = []
        def dispatch(*args, **kwargs):
            modes.append(kwargs["mode"])
            if len(modes) == 1:
                payload = self.payload(kwargs["evidence_packet"], "FAIL", "switch_to_repair")
                return L2Result("FAIL", "repair environment", self.run / "l2.log", payload=payload)
            if kwargs["mode"] == "REPAIR":
                return L2Result("UNRESOLVED", "Use an authorized alternative", self.run / "l2.log",
                    payload={"request_id": kwargs["request_id"], "next_action": {"type": "worker_fix", "instructions": "使用本地替代工具完成交付"}})
            return self.pass_dispatch(*args, **kwargs)
        rc, inject = self.run_observed_gui(dispatch=dispatch, inject_fn=self.accepted_send)
        self.assertEqual(rc, 0)
        self.assertEqual(modes, ["REVIEW", "REPAIR", "REVIEW"])
        self.assertEqual(inject.call_args.args[0].strip(), "使用本地替代工具完成交付")
        self.assertEqual(self.state.repairs, 1)

    def test_gui_legacy_human_request_gets_decision_and_continues(self):
        modes = []
        def dispatch(*args, **kwargs):
            modes.append(kwargs["mode"])
            if len(modes) == 1:
                return L2Result("DEFER", "Old human handoff", self.run / "l2.log")
            if kwargs["mode"] == "DECIDE":
                return L2Result("PROCEED", "采用默认方案并完成验证", self.run / "l2.log")
            return self.pass_dispatch(*args, **kwargs)
        rc, inject = self.run_observed_gui(dispatch=dispatch, inject_fn=self.accepted_send)
        self.assertEqual(rc, 0)
        self.assertEqual(modes, ["REVIEW", "DECIDE", "REVIEW"])
        inject.assert_called_once()

    def test_gui_repair_channel_failure_is_bounded_not_waiting_user(self):
        modes = []
        def dispatch(*args, **kwargs):
            modes.append(kwargs["mode"])
            if kwargs["mode"] == "REVIEW":
                payload = self.payload(kwargs["evidence_packet"], "FAIL", "switch_to_repair")
                return L2Result("FAIL", "repair environment", self.run / "l2.log", payload=payload)
            return L2Result("NO-BRIDGE", "channel down", self.run / "l2.log")
        rc, inject = self.run_observed_gui(dispatch=dispatch)
        self.assertEqual(rc, 1)
        self.assertEqual(modes, ["REVIEW", "REPAIR", "REPAIR", "REPAIR"])
        self.assertEqual(self.state.state, "FAILED")
        self.assertEqual(self.state.retries, 3)
        inject.assert_not_called()

    def test_evidence_bound_stop_is_blocked_not_waiting_user(self):
        self.rollout.write_text(event("task_complete", last_agent_message="请确认使用哪种方案？"), encoding="utf-8")
        dispatch = Mock(return_value=L2Result("STOP", "No authorized alternative", self.run / "l2.log",
            payload={"blockers": ["Required credential unavailable; local alternative tested and failed"],
                     "next_action": {"type": "terminate_blocked", "instructions": "停止，缺少必要凭据"}}))
        rc, inject = self.run_observed_gui(dispatch=dispatch)
        self.assertEqual(rc, 1)
        self.assertEqual(self.state.state, "BLOCKED")
        self.assertEqual(dispatch.call_args.kwargs["mode"], "DECIDE")
        inject.assert_not_called()

    def test_task_restart_during_l2_prevents_injection(self):
        def dispatch(*args, **kwargs):
            result = self.instruction(*args, **kwargs)
            self.append(event("task_started"))
            return result
        rc, inject = self.run_observed_gui(dispatch=dispatch)
        self.assertEqual(rc, 1)
        inject.assert_not_called()
        self.assertEqual(self.state.state, "TIMEOUT")

    def test_aborted_turn_does_not_revive_old_question(self):
        self.rollout.write_text(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "请确认旧方案？"}]}}) + "\n" + event("turn_aborted"), encoding="utf-8")
        modes = []
        def dispatch(*args, **kwargs):
            modes.append(kwargs["mode"])
            return self.pass_dispatch(*args, **kwargs)
        rc, inject = self.run_observed_gui(dispatch=dispatch)
        self.assertEqual(rc, 0)
        self.assertEqual(modes, ["REVIEW"])
        inject.assert_not_called()

    def check_cli_handoff_failure(self, *, competing=False, error=None, pause_result=False):
        self.lock.acquire.return_value = (not competing, "other supervisor")
        def pause(path, **kwargs):
            self.lock.acquire.assert_called_once()
            run = next((self.root / "runs").iterdir())
            saved = json.loads((run / "supervisor_state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "HANDOFF_WAIT")
            self.assertEqual(kwargs["max_wait"], None)
            if error:
                raise error
            return pause_result
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(cli, "__file__", str(self.root / "afk_supervisor" / "cli.py")))
            stack.enter_context(patch.object(cli, "backup_workspace"))
            stack.enter_context(patch.object(cli, "load_codex_thread_titles", return_value={}))
            stack.enter_context(patch.object(supervise, "find_codex_session_by_id", return_value=("parent", self.rollout, str(self.ws))))
            stack.enter_context(patch.object(supervise, "WorkspaceSupervisorLock", return_value=self.lock))
            pause_mock = stack.enter_context(patch.object(supervise, "pause_codex_gui_session", side_effect=pause))
            close = stack.enter_context(patch.object(supervise, "close_codex_app"))
            worker = stack.enter_context(patch.object(cli, "run_headless_supervisor"))
            notify = stack.enter_context(patch("afk_supervisor.reporting.send_terminal_notification"))
            rc = cli.main(["--adopt", "parent", "--fork", "--quick", "--yes"])
        self.assertEqual(rc, 1)
        worker.assert_not_called()
        close.assert_not_called()
        if competing:
            pause_mock.assert_not_called()
        else:
            pause_mock.assert_called_once()
        self.lock.release.assert_called_once()
        notify.assert_called_once()
        run = next((self.root / "runs").iterdir())
        self.assertTrue((run / "report.md").is_file())
        saved = json.loads((run / "supervisor_state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], "CANCELLED" if isinstance(error, KeyboardInterrupt) else "FAILED")

    def test_competing_fork_never_pauses_parent(self):
        self.check_cli_handoff_failure(competing=True)

    def test_fork_cancel_during_wait_reports_and_releases(self):
        self.check_cli_handoff_failure(error=KeyboardInterrupt())

    def test_fork_pause_exception_reports_and_releases(self):
        self.check_cli_handoff_failure(error=OSError("simulated read failure"))

    def test_fork_pause_failure_reports_and_releases(self):
        self.check_cli_handoff_failure(pause_result=False)


class GuiIdentityTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows C# compiler")
    def test_compiled_identity_predicate_without_live_ui(self):
        script = Path(__file__).with_name("check_gui_identity.ps1").resolve()
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("IDENTITY_TESTS_OK", result.stdout)
