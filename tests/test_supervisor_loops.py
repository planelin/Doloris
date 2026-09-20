"""Exercise real loops and coordinator; mock only process/L2/GUI/clock boundaries."""

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from afk_supervisor.engine import run_headless_supervisor
from afk_supervisor.gui_engine import run_gui_supervisor
from afk_supervisor.l2.protocol import validate_protocol_payload
from afk_supervisor.models import DeadlineBudget, L2Result
from afk_supervisor.state import SupervisorState
from tests.test_debug_regressions import DebugFixture


class FakeClock:
    def __init__(self):
        self.now = 1000000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.1, seconds)


class FakeDriver:
    def __init__(self, run, exit_codes=(0,), providers=("one",), messages=("Neutral report",)):
        self.run = run
        self.exit_codes = exit_codes
        self.providers = providers
        self.messages = messages
        self.session_id = "worker"
        self.jsonl = None
        self.title = "Debug task"
        self.proc = None
        self.provider_index = 0
        self.calls = []
        self.switches = []
        self.starts = 0

    @property
    def provider_name(self):
        return self.providers[self.provider_index]

    def has_next(self):
        return self.provider_index + 1 < len(self.providers)

    def switch_provider(self):
        self.provider_index += 1
        self.switches.append(self.provider_name)
        return self.provider_name

    def _start(self, mode, prompt):
        self.calls.append((mode, prompt.read_text(encoding="utf-8")))
        rc = self.exit_codes[min(self.starts, len(self.exit_codes) - 1)]
        message = self.messages[min(self.starts, len(self.messages) - 1)]
        (self.run / "codex-last-message.txt").write_text(message, encoding="utf-8")
        self.starts += 1
        self.proc = SimpleNamespace(pid=123, poll=lambda: rc)

    def launch(self, prompt):
        self._start("launch", prompt)

    def resume(self, prompt):
        self._start("resume", prompt)

    def fork(self, prompt):
        self.session_id = "child"
        self._start("fork", prompt)

    def discover_session(self, launched_at):
        pass

    def kill_tree(self):
        raise AssertionError("A fake exited worker must never be killed")


class LoopFixture(DebugFixture):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.args = SimpleNamespace(
            driver="codex", adopt="parent", max_interactions=6, max_resumes=2,
            max_run_sec=120, l2_cmd="agy", l2_max=1, work_dir="work",
        )
        (self.ws / "result.txt").write_text("result", encoding="utf-8")
        (self.run / "prompt.txt").write_text("Continue the task", encoding="utf-8")
        self.state = SupervisorState(self.run, sid="parent", ws=self.ws)
        self.events = []
        self.lock = Mock()
        self.report = Mock()
        self.coordinator_instance = self.coordinator()
        self.dispatch_count = 0

    def ivl(self, event, **details):
        self.events.append((event, details))

    def pass_dispatch(self, *args, **kwargs):
        self.dispatch_count += 1
        evidence = kwargs["evidence_packet"]
        payload = self.payload(evidence)
        valid, reason = validate_protocol_payload(
            payload, expected_request_id=evidence.request_id, expected_task_id=self.baseline.task_id,
            expected_mode="REVIEW", expected_revision=evidence.reviewed_revision,
            task_baseline=self.baseline, evidence_packet=evidence,
        )
        self.assertTrue(valid, reason)
        return L2Result("PASS", json.dumps(payload), self.run / "l2.log", payload=payload)

    def run_headless(self, mode="resume", dispatch=None, driver=None, verify=None, task_md=None, chaos=None, rollout=None):
        driver = driver or FakeDriver(self.run)
        with patch("supervise.l2_dispatch", side_effect=dispatch or self.pass_dispatch), \
             patch("afk_supervisor.engine.time.sleep", side_effect=self.clock.sleep), \
             patch("afk_supervisor.engine.time.monotonic", side_effect=self.clock.monotonic), \
             patch("afk_supervisor.engine.time.time", side_effect=self.clock.time), \
             patch("afk_supervisor.engine.register_thread_for_codex_ui"), \
             patch("afk_supervisor.engine.generate_final_report", self.report):
            rc = run_headless_supervisor(
                driver, self.args, self.run, self.ws, mode, self.baseline,
                self.coordinator_instance, self.state, DeadlineBudget(120, self.clock),
                self.ivl, verify or (lambda custom_last_msg=None: (True, "Verified")),
                task_md=task_md, ws_lock=self.lock, chaos=chaos, rollout=rollout,
            )
        return rc, driver

    def run_gui(self, dispatch=None, verify=None, inject_fn=None, rollout=None, working=None):
        if rollout is None:
            rollout = self.root / "rollout.jsonl"
            rollout.write_text("{}\n", encoding="utf-8")
        if working is None:
            working = lambda path: (False, "idle", "Neutral report")
        with patch("supervise.l2_dispatch", side_effect=dispatch or self.pass_dispatch), \
             patch("supervise.ensure_codex_window_restored"), \
             patch("supervise.inject_into_codex_gui", side_effect=inject_fn) as inject, \
             patch("supervise.is_codex_working", side_effect=working), \
             patch("afk_supervisor.gui_engine.time.sleep", side_effect=self.clock.sleep), \
             patch("afk_supervisor.gui_engine.time.monotonic", side_effect=self.clock.monotonic), \
             patch("afk_supervisor.gui_engine.time.time", side_effect=self.clock.time), \
             patch("afk_supervisor.gui_engine.generate_final_report", self.report):
            rc = run_gui_supervisor(
                "worker", rollout, str(self.ws), "Debug task", self.args, self.run, None, None,
                verify or (lambda custom_last_msg=None, min_mtime=0.0, title_str="": (True, "Verified")),
                ivl=self.ivl, budget=DeadlineBudget(120, self.clock), ws_lock=self.lock,
                state_mgr=self.state, coordinator=self.coordinator_instance,
            )
        return rc, inject

class SupervisorLoopRegressions(LoopFixture):
    def test_cli_pauses_active_parent_then_forks_continue_through_real_loop(self):
        self.check_cli_pause_then_fork(0)

    def test_cli_waits_past_old_timeout_and_run_budget_before_fork(self):
        self.check_cli_pause_then_fork(180)

    def check_cli_pause_then_fork(self, delay):
        from contextlib import ExitStack
        from afk_supervisor.cli import main
        from afk_supervisor.platform.gui import pause_codex_gui_session
        from afk_supervisor.sessions.rollout import is_codex_working

        parent = self.root / "parent.jsonl"
        parent.write_text(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n", encoding="utf-8")
        os.utime(parent, (self.clock.time() - 10, self.clock.time() - 10))
        (self.root / "gui_inject.ps1").write_text("# Simulated pause boundary; never executed.\n", encoding="utf-8")
        self.lock.acquire.return_value = (True, "")
        order, drivers = [], []

        pause_requested_at = None
        def confirm_pause():
            with parent.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted"}}) + "\n")
            # Keep a fresh mtime: explicit termination requires no silence buffer.
            os.utime(parent, (self.clock.time(), self.clock.time()))
            order.append("pause")

        def send_pause(command, **kwargs):
            nonlocal pause_requested_at
            self.lock.acquire.assert_called_once()
            checkpoint = json.loads((drivers[0].run / "supervisor_state.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["state"], "HANDOFF_WAIT")
            self.assertEqual(checkpoint["parent_rollout"], str(parent))
            self.assertIn("-PauseOnly", command)
            self.assertEqual(command[command.index("-TargetHwnd") + 1], "123")
            self.assertTrue(is_codex_working(parent)[0])
            pause_requested_at = self.clock.time()
            if not delay:
                confirm_pause()
            return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

        def advance_and_confirm(seconds):
            if pause_requested_at is not None and not order:
                self.assertTrue(all(not driver.calls for driver in drivers), "No child may start before pause confirmation")
            self.clock.sleep(seconds)
            if pause_requested_at is not None and not order and self.clock.time() - pause_requested_at >= delay:
                confirm_pause()

        def create_driver(work_dir, run_dir):
            driver = FakeDriver(run_dir)
            original_fork = driver.fork

            def fork(prompt):
                self.assertEqual(order, ["pause"])
                self.assertFalse(is_codex_working(parent)[0])
                self.assertEqual(prompt.read_text(encoding="utf-8"), "继续\n")
                order.append("fork")
                driver.jsonl = None
                original_fork(prompt)

            driver.fork = fork
            drivers.append(driver)
            return driver

        with ExitStack() as stack:
            stack.enter_context(patch("afk_supervisor.cli.__file__", str(self.root / "supervisor" / "afk_supervisor" / "cli.py")))
            stack.enter_context(patch("afk_supervisor.cli.CodexDriver", side_effect=create_driver))
            stack.enter_context(patch("afk_supervisor.cli.AntigravityManager"))
            stack.enter_context(patch("afk_supervisor.cli.atexit"))
            stack.enter_context(patch("afk_supervisor.cli.keep_awake"))
            stack.enter_context(patch("afk_supervisor.cli.detect_system_proxy", return_value=None))
            stack.enter_context(patch("afk_supervisor.cli.backup_workspace", return_value=None))
            stack.enter_context(patch("afk_supervisor.cli.extract_task_baseline", return_value=self.baseline))
            stack.enter_context(patch("afk_supervisor.cli.load_codex_thread_titles", return_value={}))
            stack.enter_context(patch("afk_supervisor.cli.read_session_title", return_value="Handoff integration"))
            stack.enter_context(patch("afk_supervisor.cli.get_codex_locks_dir", return_value=self.root / "locks"))
            stack.enter_context(patch("supervise.find_codex_session_by_id", return_value=("parent", parent, str(self.ws))))
            stack.enter_context(patch("supervise.WorkspaceSupervisorLock", return_value=self.lock))
            pause = stack.enter_context(patch("supervise.pause_codex_gui_session", side_effect=pause_codex_gui_session))
            close_app = stack.enter_context(patch("supervise.close_codex_app"))
            stack.enter_context(patch("afk_supervisor.platform.gui.find_best_codex_window", return_value=123))
            stack.enter_context(patch("afk_supervisor.platform.gui.get_workspace_root", return_value=self.root))
            pause_signal = stack.enter_context(patch("afk_supervisor.platform.gui.subprocess.run", side_effect=send_pause))
            stack.enter_context(patch("supervise.l2_dispatch", side_effect=self.pass_dispatch))
            stack.enter_context(patch("afk_supervisor.engine.time.sleep", side_effect=advance_and_confirm))
            stack.enter_context(patch("afk_supervisor.engine.time.monotonic", side_effect=self.clock.monotonic))
            stack.enter_context(patch("afk_supervisor.engine.time.time", side_effect=self.clock.time))
            stack.enter_context(patch("afk_supervisor.engine.register_thread_for_codex_ui"))
            stack.enter_context(patch("afk_supervisor.engine.generate_final_report", self.report))
            rc = main(["--adopt", "parent", "--fork", "--quick", "--yes", "--max-run-sec", "120"])
        self.assertEqual(rc, 0)
        self.assertEqual(order, ["pause", "fork"])
        self.assertEqual(drivers[0].calls, [("fork", "继续\n")])
        self.assertEqual(self.dispatch_count, 1)  # REVIEW, no handoff-only DECIDE.
        pause.assert_called_once_with(parent, max_wait=None)
        pause_signal.assert_called_once()
        close_app.assert_not_called()
        self.lock.release.assert_called_once()
        saved_state = json.loads((drivers[0].run / "supervisor_state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_state["state"], "SUCCESS")
        self.assertEqual(saved_state["worker_session_id"], "child")
        self.assertEqual(saved_state["parent_session_id"], "parent")

    def test_fork_confirmation_reaches_l2_and_completes(self):
        self.baseline.human_confirmation_required = ["向用户确认主视觉方向"]
        modes = []
        def dispatch(*args, **kwargs):
            modes.append(kwargs["mode"])
            if kwargs["mode"] == "DECIDE":
                return L2Result("PROCEED", "使用深色主题并完成交互", self.run / "l2.log")
            return self.pass_dispatch(*args, **kwargs)
        with patch("supervise.is_codex_working", return_value=(False, "idle", "请确认主视觉方向？")):
            rc, driver = self.run_headless(mode="fork", dispatch=dispatch, rollout=self.root / "parent.jsonl")
        self.assertEqual(rc, 0)
        self.assertEqual(modes, ["DECIDE", "REVIEW"])
        self.assertIn("深色主题", driver.calls[0][1])
        self.report.assert_called_once()

    def test_fork_resumes_interrupted_parent_with_continue_without_l2_decision(self):
        parent = self.root / "parent.jsonl"
        parent.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in (
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "请确认旧的方案？"}]}},
            {"type": "event_msg", "payload": {"type": "turn_aborted"}},
        )) + "\n", encoding="utf-8")
        os.utime(parent, (self.clock.time() - 10, self.clock.time() - 10))
        # The abort is the current lifecycle state; do not revive an older question.
        with patch("afk_supervisor.sessions.rollout.read_rollout_last_message") as old_message:
            rc, driver = self.run_headless(mode="fork", rollout=parent)
        self.assertEqual(rc, 0)
        self.assertEqual(driver.calls, [("fork", "继续\n")])
        self.assertEqual(self.state.interactions, 0)
        self.assertEqual(self.state.retries, 0)
        self.assertEqual(self.dispatch_count, 1)  # Only the post-worker REVIEW.
        old_message.assert_not_called()
        self.assertFalse(any(event == "PARENT_RUNNING" for event, _ in self.events))
        self.assertFalse(any(event == "L2_CONSULT" and data.get("kind") == "DECIDE"
                             for event, data in self.events))

    def test_fork_idle_parent_uses_continue_without_mandatory_l2_decision(self):
        with patch("supervise.is_codex_working", return_value=(False, "task_complete", "已完成当前页面步骤")):
            rc, driver = self.run_headless(mode="fork", rollout=self.root / "parent.jsonl")
        self.assertEqual(rc, 0)
        self.assertEqual(driver.calls, [("fork", "继续\n")])
        self.assertEqual(self.state.interactions, 0)
        self.assertEqual(self.dispatch_count, 1)

    def test_fork_refuses_still_active_parent_without_starting_child(self):
        # CLI owns the pause operation; the engine must not bypass its result.
        with patch("supervise.is_codex_working", return_value=(True, "working", "")), \
             patch("supervise.pause_codex_gui_session") as pause:
            rc, driver = self.run_headless(mode="fork", rollout=self.root / "parent.jsonl")
        self.assertEqual(rc, 1)
        self.assertEqual(self.state.state, "FAILED")
        self.assertEqual(driver.calls, [])
        self.assertEqual(self.dispatch_count, 0)
        self.report.assert_called_once()
        self.lock.release.assert_called_once()
        pause.assert_not_called()

    def test_fork_rechecks_parent_after_l2_decision_before_starting_child(self):
        def dispatch(*args, **kwargs):
            self.assertEqual(kwargs["mode"], "DECIDE")
            return L2Result("PROCEED", "使用深色主题并完成交互", None)

        with patch("supervise.is_codex_working", side_effect=[
            (False, "task_complete", "请确认主视觉方向？"),
            (True, "parent restarted while L2 was deciding", ""),
        ]):
            rc, driver = self.run_headless(mode="fork", dispatch=dispatch, rollout=self.root / "parent.jsonl")
        self.assertEqual(rc, 1)
        self.assertEqual(self.state.state, "FAILED")
        self.assertEqual(driver.calls, [])
        self.report.assert_called_once()

    def test_review_timeout_reuses_request_id_and_evidence(self):
        ids = []
        def dispatch(*args, **kwargs):
            ids.append(kwargs["request_id"])
            if len(ids) == 1:
                return L2Result("NO-VERDICT", "Still working", None)
            return self.pass_dispatch(*args, **kwargs)
        rc, _ = self.run_headless(dispatch=dispatch)
        self.assertEqual(rc, 0)
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1])

    def test_l2_authorized_continue_is_accepted_before_fork(self):
        modes = []

        def dispatch(*args, **kwargs):
            modes.append(kwargs["mode"])
            if kwargs["mode"] == "DECIDE":
                return L2Result("PROCEED", "继续", None)
            return self.pass_dispatch(*args, **kwargs)

        with patch("supervise.is_codex_working", return_value=(False, "task_complete", "是否继续？")):
            rc, driver = self.run_headless(mode="fork", dispatch=dispatch, rollout=self.root / "parent.jsonl")
        self.assertEqual(rc, 0)
        self.assertEqual(modes, ["DECIDE", "REVIEW"])
        self.assertEqual(self.state.retries, 0)
        self.assertEqual(driver.calls, [("fork", "继续\n")])

    def test_fork_bridge_failure_retries_and_reports_without_starting(self):
        dispatch = Mock(return_value=L2Result("NO-BRIDGE", "Disconnected", self.run / "l2.log"))
        with patch("supervise.is_codex_working", return_value=(False, "idle", "请确认主视觉方向？")):
            rc, driver = self.run_headless(mode="fork", dispatch=dispatch, rollout=self.root / "parent.jsonl")
        self.assertEqual(rc, 1)
        self.assertEqual(dispatch.call_count, 3)
        self.assertEqual(driver.calls, [])
        self.assertEqual(self.state.state, "FAILED")
        self.report.assert_called_once()
        self.lock.release.assert_called_once()

    def test_fork_legacy_defer_is_returned_to_l2_then_proceeds(self):
        replies = [L2Result("DEFER", "Ask user", None), L2Result("PROCEED", "继续实现深色主题", None)]
        def dispatch(*args, **kwargs):
            if kwargs["mode"] == "DECIDE":
                return replies.pop(0)
            return self.pass_dispatch(*args, **kwargs)
        with patch("supervise.is_codex_working", return_value=(False, "idle", "请确认主视觉方向？")):
            rc, driver = self.run_headless(mode="fork", dispatch=dispatch, rollout=self.root / "parent.jsonl")
        self.assertEqual(rc, 0)
        self.assertEqual(self.state.retries, 1)
        self.assertEqual(len(driver.calls), 1)

    def test_fork_failure_writes_real_report_and_notifies(self):
        from afk_supervisor.reporting import generate_final_report
        self.report = generate_final_report
        dispatch = Mock(return_value=L2Result("NO-BRIDGE", "offline", None))
        with patch("supervise.is_codex_working", return_value=(False, "idle", "请确认主视觉方向？")), patch("afk_supervisor.reporting.send_terminal_notification") as notify:
            rc, _ = self.run_headless(mode="fork", dispatch=dispatch, rollout=self.root / "parent.jsonl")
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", (self.run / "report.md").read_text(encoding="utf-8"))
        notify.assert_called_once()

    def test_fork_l2_stop_records_blocked_and_report(self):
        payload = {"blockers": ["Upstream quota exhausted"], "next_action": {"type": "terminate_blocked", "instructions": "No configured alternative provider; stop."}}
        dispatch = Mock(return_value=L2Result("STOP", "Cannot continue", None, payload=payload))
        with patch("supervise.is_codex_working", return_value=(False, "idle", "请确认如何继续？")):
            rc, driver = self.run_headless(mode="fork", dispatch=dispatch, rollout=self.root / "parent.jsonl")
        self.assertEqual(rc, 1)
        self.assertEqual(self.state.state, "BLOCKED")
        self.assertEqual(driver.calls, [])
        self.report.assert_called_once()

    def test_resume_unchanged_structured_pass_exits_successfully(self):
        rc, driver = self.run_headless()
        self.assertEqual(rc, 0)
        self.assertEqual(self.dispatch_count, 1)
        self.assertEqual(self.state.state, "SUCCESS")
        self.assertEqual(self.state.round, 1)
        self.assertEqual(self.state.reviews, 1)
        self.assertEqual(self.state.interactions, 0)
        self.assertEqual(len(driver.calls), 1)
        self.lock.release.assert_called_once()

    def test_fork_unchanged_structured_pass_exits_successfully(self):
        parent = self.root / "parent.jsonl"
        parent.write_text(json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted"}}) + "\n", encoding="utf-8")
        rc, driver = self.run_headless(mode="fork", rollout=parent)
        self.assertEqual(rc, 0)
        self.assertEqual(self.dispatch_count, 1)
        self.assertEqual(driver.calls[0][0], "fork")
        self.assertEqual(self.state.worker_session_id, "child")
        self.assertEqual(self.state.parent_session_id, "parent")

    def test_gui_pass_uses_strict_cli_callback_signature(self):
        rc, inject = self.run_gui()
        self.assertEqual(rc, 0)
        self.assertEqual(self.dispatch_count, 1)
        inject.assert_not_called()
        self.assertEqual(self.state.reviews, 1)
        self.assertEqual(self.state.interactions, 0)
        self.lock.release.assert_called_once()

    def test_explicit_acceptance_context_survives_pre_exit_collection(self):
        task_dir = self.root / "task"
        task_dir.mkdir()
        task = task_dir / "task.md"
        task.write_text("Deliver result.txt", encoding="utf-8")
        (task_dir / "acceptance.md").write_text("result.txt:1", encoding="utf-8")
        self.coordinator_instance.task_dir = task_dir
        rc, _ = self.run_headless(task_md=task)
        self.assertEqual(rc, 0)
        self.assertEqual(self.dispatch_count, 1)

    def test_mutation_requires_a_fresh_review_not_a_worker_resume(self):
        revisions = []
        def dispatch(*args, **kwargs):
            revisions.append(kwargs["evidence_packet"].artifact_revision)
            result = self.pass_dispatch(*args, **kwargs)
            if len(revisions) == 1:
                (self.ws / "result.txt").write_text("updated result", encoding="utf-8")
            return result
        rc, driver = self.run_headless(dispatch=dispatch)
        self.assertEqual(rc, 0)
        self.assertEqual(len(revisions), 2)
        self.assertNotEqual(revisions[0], revisions[1])
        self.assertEqual(len(driver.calls), 1)

    def test_repair_then_fresh_review_not_immediate_success(self):
        modes = []
        def dispatch(*args, **kwargs):
            modes.append(kwargs["mode"])
            if len(modes) == 1:
                payload = self.payload(kwargs["evidence_packet"], "FAIL", "switch_to_repair")
                return L2Result("FAIL", json.dumps(payload), self.run / "l2.log", payload=payload)
            if kwargs["mode"] == "REPAIR":
                (self.ws / "result.txt").write_text("repaired result", encoding="utf-8")
                return L2Result("REPAIRED", "Configuration repaired", self.run / "l2.log")
            return self.pass_dispatch(*args, **kwargs)
        rc, driver = self.run_headless(dispatch=dispatch)
        self.assertEqual(rc, 0)
        self.assertEqual(modes, ["REVIEW", "REPAIR", "REVIEW"])
        self.assertEqual(self.state.repairs, 1)
        self.assertEqual(self.state.reviews, 2)
        self.assertEqual(len(driver.calls), 1)

    def test_review_legacy_defer_escalates_to_decision_without_blind_resume(self):
        def dispatch(*args, **kwargs):
            if kwargs["mode"] == "DECIDE":
                return L2Result("DEFER", "Human required", self.run / "l2.log")
            payload = self.payload(kwargs["evidence_packet"], "DEFER", "switch_to_repair")
            return L2Result("DEFER", "Human required", self.run / "l2.log", payload=payload)
        rc, driver = self.run_headless(dispatch=dispatch)
        self.assertEqual(rc, 1)
        self.assertEqual(self.state.state, "FAILED")
        self.assertEqual(self.state.interactions, 3)
        self.assertEqual(len(driver.calls), 1)

    def test_payloadless_channel_failure_retries_without_resuming_worker(self):
        dispatch = Mock(return_value=L2Result("NO-BRIDGE", "Bridge not connected", self.run / "l2.log"))
        rc, driver = self.run_headless(dispatch=dispatch)
        self.assertEqual(rc, 1)
        self.assertEqual(dispatch.call_count, 3)
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(self.state.retries, 3)

    def test_crash_rotates_existing_provider_pool(self):
        driver = FakeDriver(self.run, exit_codes=(1,), providers=("one", "two"))
        self.args.l2_max = 0
        rc, driver = self.run_headless(driver=driver)
        self.assertEqual(rc, 1)
        self.assertEqual(driver.switches, ["two"])
        self.assertEqual(self.state.resumes, 2)

    def test_crash_budget_exhaustion_escalates_to_repair_and_resumes(self):
        driver = FakeDriver(self.run, exit_codes=(1, 0))
        self.args.max_resumes = 0
        modes = []
        def dispatch(*args, **kwargs):
            modes.append(kwargs["mode"])
            if kwargs["mode"] == "REPAIR":
                return L2Result("REPAIRED", "Fixed infrastructure", self.run / "l2.log")
            return self.pass_dispatch(*args, **kwargs)
        rc, driver = self.run_headless(driver=driver, dispatch=dispatch)
        self.assertEqual(rc, 0)
        self.assertEqual(modes, ["REPAIR", "REVIEW"])
        self.assertEqual(len(driver.calls), 2)
        self.assertEqual(self.state.resumes, 1)

    def test_gui_mutation_gets_new_review_without_injecting_worker_input(self):
        revisions = []
        def dispatch(*args, **kwargs):
            revisions.append(kwargs["evidence_packet"].artifact_revision)
            result = self.pass_dispatch(*args, **kwargs)
            if len(revisions) == 1:
                (self.ws / "result.txt").write_text("updated GUI result", encoding="utf-8")
            return result
        rc, inject = self.run_gui(dispatch=dispatch)
        self.assertEqual(rc, 0)
        self.assertEqual(len(revisions), 2)
        self.assertNotEqual(revisions[0], revisions[1])
        inject.assert_not_called()

    def test_gui_repair_requires_fresh_review_not_immediate_success(self):
        modes = []
        def dispatch(*args, **kwargs):
            modes.append(kwargs["mode"])
            if len(modes) == 1:
                payload = self.payload(kwargs["evidence_packet"], "FAIL", "switch_to_repair")
                return L2Result("FAIL", json.dumps(payload), self.run / "l2.log", payload=payload)
            if kwargs["mode"] == "REPAIR":
                return L2Result("REPAIRED", "Simulated environment repair", self.run / "l2.log")
            return self.pass_dispatch(*args, **kwargs)
        rc, inject = self.run_gui(dispatch=dispatch)
        self.assertEqual(rc, 0)
        self.assertEqual(modes, ["REVIEW", "REPAIR", "REVIEW"])
        self.assertEqual((self.state.repairs, self.state.reviews), (1, 2))
        inject.assert_not_called()

    def test_gui_legacy_request_user_redecides_then_fails_if_still_invalid(self):
        dispatch = Mock(return_value=L2Result(
            "INCONCLUSIVE", "Human approval is needed", self.run / "l2.log",
            payload={"next_action": {"type": "request_user", "instructions": "Need human approval"}},
        ))
        rc, inject = self.run_gui(dispatch=dispatch)
        self.assertEqual(rc, 1)
        self.assertEqual([call.kwargs["mode"] for call in dispatch.call_args_list],
                         ["REVIEW", "DECIDE", "DECIDE", "DECIDE"])
        self.assertEqual(self.state.state, "FAILED")
        inject.assert_not_called()

    def test_gui_payloadless_channel_errors_have_bounded_retries(self):
        dispatch = Mock(return_value=L2Result("NO-BRIDGE", "Bridge not connected", self.run / "l2.log"))
        rc, inject = self.run_gui(dispatch=dispatch)
        self.assertEqual(rc, 1)
        self.assertEqual(self.state.state, "FAILED")
        self.assertEqual(dispatch.call_count, 3)
        self.assertEqual(self.state.retries, 3)
        inject.assert_not_called()

    def test_headless_callback_exception_reports_failure_and_releases_resources(self):
        def dispatch(*args, **kwargs):
            raise OSError("Simulated dispatch exception")
        rc, driver = self.run_headless(dispatch=dispatch)
        self.assertEqual(rc, 1)
        self.assertEqual(self.state.state, "FAILED")
        self.lock.release.assert_called_once()
        self.report.assert_called_once()
        self.assertEqual(len(driver.calls), 1)

    def test_headless_uses_legacy_backoff_settings(self):
        self.args.max_resumes = 1
        self.args.l2_max = 0
        driver = FakeDriver(self.run, exit_codes=(1,))
        with patch("supervise.BACKOFFS", [17, 29]):
            self.run_headless(driver=driver)
        waits = [data["backoff_sec"] for event, data in self.events if event == "RESUME_WAIT"]
        self.assertEqual(waits, [17])

    def test_adaptive_hang_thresholds_do_not_kill_a_healthy_long_turn(self):
        driver = FakeDriver(self.run)
        previous_start = driver._start
        def start(mode, prompt):
            previous_start(mode, prompt)
            driver.proc.poll = lambda: None
        driver._start = start
        driver.heartbeat_age = Mock(return_value=400)
        driver.kill_tree = Mock()
        rc, _ = self.run_headless(driver=driver)
        self.assertEqual(rc, 1)
        self.assertEqual(self.state.state, "TIMEOUT")
        self.assertFalse(any(event == "DETECT_HANG" for event, _ in self.events))
        driver.kill_tree.assert_called_once()  # Only terminal cleanup, never a hang kill.

    def test_opt_in_network_chaos_restores_adapter_on_exception_with_fake_adapter(self):
        driver = FakeDriver(self.run)
        previous_start = driver._start
        def start(mode, prompt):
            previous_start(mode, prompt)
            driver.proc.poll = lambda: None
        driver._start = start
        driver.heartbeat_age = Mock(side_effect=OSError("Simulated worker failure"))
        driver.kill_tree = Mock()
        # No network command runs. These are the exact engine platform boundaries.
        with patch("afk_supervisor.engine.find_connected_adapter", return_value="FAKE-ADAPTER"), \
             patch("afk_supervisor.engine.net_disable", return_value=True) as disable, \
             patch("afk_supervisor.engine.net_enable", return_value=True) as enable:
            rc, _ = self.run_headless(driver=driver, chaos=("net", 0, 60))
        self.assertEqual(rc, 1)
        disable.assert_called_once_with("FAKE-ADAPTER")
        enable.assert_called_once_with("FAKE-ADAPTER")
        self.lock.release.assert_called_once()

    def test_gui_exception_records_failure_and_releases_resources(self):
        def dispatch(*args, **kwargs):
            raise OSError("Unexpected channel failure")
        rc, inject = self.run_gui(dispatch=dispatch)
        self.assertEqual(rc, 1)
        self.assertEqual(self.state.state, "FAILED")
        self.lock.release.assert_called_once()
        self.report.assert_called_once()
        inject.assert_not_called()


if __name__ == "__main__":
    unittest.main()
