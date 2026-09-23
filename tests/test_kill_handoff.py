"""Kill takeover regression tests; all process/GUI boundaries are simulated."""
import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import supervise
from afk_supervisor import cli
from afk_supervisor.platform import process
from afk_supervisor.sessions.rollout import codex_handoff_state, codex_session_state
from tests.test_supervisor_loops import FakeClock


class KillFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="afk-kill-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rollout = self.root / "rollout.jsonl"
        self.clock = FakeClock()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("time.sleep", side_effect=self.clock.sleep))
        self.stack.enter_context(patch("time.monotonic", side_effect=self.clock.monotonic))
        self.write(self.event("task_complete"))

    @staticmethod
    def event(kind, **kwargs):
        outer = "event_msg" if kind in ("task_started", "task_complete", "turn_aborted", "token_count") else "response_item"
        return {"type": outer, "payload": {"type": kind, **kwargs}}

    def write(self, *events):
        self.rollout.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    def simulated_desktop(self, survives=False):
        alive = {111, 222}
        self.stack.enter_context(patch.object(supervise, "get_codex_desktop_pids", side_effect=lambda: sorted(alive)))
        self.stack.enter_context(patch.object(supervise, "pid_is_running", side_effect=lambda pid: pid in alive))
        def terminate(command, **kwargs):
            self.assertEqual(command[0], "taskkill")
            self.assertEqual(command[1], "/PID")
            self.assertNotIn("/T", command)
            self.assertNotIn("/IM", command)
            if not survives:
                alive.discard(int(command[2]))
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        return self.stack.enter_context(patch.object(process.subprocess, "run", side_effect=terminate))


class TestHandoffEvents(KillFixture):
    def test_long_silent_tool_remains_unsafe(self):
        self.write(self.event("task_started"), self.event("function_call", call_id="slow"))
        os.utime(self.rollout, (1, 1))
        state = codex_handoff_state(self.rollout)
        self.assertEqual(state["state"], "unsafe")
        self.assertEqual(state["pending_calls"], ["slow"])
        self.assertFalse(cli.wait_session_quiet(self.rollout, max_wait=0))

    def test_parallel_calls_are_matched_by_id_even_beyond_old_tail(self):
        self.write(self.event("function_call", call_id="a"), self.event("custom_tool_call", call_id="b"),
                   self.event("custom_tool_call_output", call_id="b", output="x" * 100000))
        self.assertEqual(codex_handoff_state(self.rollout)["pending_calls"], ["a"])

    def test_custom_tool_output_is_recognised(self):
        self.write(self.event("custom_tool_call", call_id="a"), self.event("custom_tool_call_output", call_id="a"))
        self.assertEqual(codex_handoff_state(self.rollout)["state"], "safe")
        self.assertFalse(codex_handoff_state(self.rollout)["turn_ended"])

    def test_message_text_cannot_forge_tool_events(self):
        self.write(self.event("function_call", call_id="a"), self.event("message", role="assistant", content='"function_call_output"'))
        self.assertEqual(codex_handoff_state(self.rollout)["state"], "unsafe")

    def test_missing_empty_partial_metadata_are_unknown(self):
        for text in ("", "{}\n", '{"type":"session_meta"}\n', '{"type":'):
            with self.subTest(text=text):
                self.rollout.write_text(text, encoding="utf-8")
                self.assertEqual(codex_handoff_state(self.rollout)["state"], "unknown")
        self.assertEqual(codex_handoff_state(self.root / "missing")["state"], "unknown")

    def test_new_fork_metadata_is_a_safe_stopped_boundary(self):
        self.write(
            {
                "type": "session_meta",
                "payload": {
                    "session_id": "01a0cc3a-2f4f-7791-be89-f5f59e005336",
                    "forked_from_id": "01a0cc11-580a-7ca0-97bf-8d44b2ae978f",
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "thread_settings_applied"},
            },
        )
        snapshot = codex_session_state(self.rollout)
        self.assertEqual(snapshot["state"], "safe")
        self.assertEqual(snapshot["status"], "stopped")
        self.assertTrue(snapshot["turn_ended"])
        self.assertTrue(snapshot["never_started"])
        self.assertIn("新建 fork", snapshot["reason"])

    def test_incomplete_fork_metadata_remains_unknown(self):
        cases = (
            [{"type": "session_meta", "payload": {"session_id": "child"}}],
            [{"type": "session_meta", "payload": {"forked_from_id": "parent"}}],
            [{"type": "event_msg", "payload": {"type": "thread_settings_applied"}}],
            [
                {"type": "session_meta", "payload": {"session_id": "child", "forked_from_id": "parent"}},
                {"type": "event_msg", "payload": {"type": "token_count"}},
            ],
        )
        for events in cases:
            with self.subTest(events=events):
                self.write(*events)
                snapshot = codex_handoff_state(self.rollout)
                self.assertEqual(snapshot["state"], "unknown")
                self.assertFalse(snapshot["never_started"])

    def test_aborted_turn_does_not_hide_unreturned_tool(self):
        self.write(self.event("function_call", call_id="a"), self.event("turn_aborted"))
        self.assertEqual(codex_handoff_state(self.rollout)["state"], "unsafe")

    def test_recent_terminal_event_needs_no_fifteen_second_silence(self):
        self.write(self.event("task_complete"), self.event("token_count"))
        self.assertTrue(cli.wait_session_quiet(self.rollout, max_wait=0))

    def test_new_input_reopens_turn(self):
        self.write(self.event("task_complete"), self.event("message", role="user"))
        self.assertFalse(codex_handoff_state(self.rollout)["turn_ended"])


class TestAutomaticClose(KillFixture):
    def test_idle_closes_immediately_with_audit(self):
        terminate = self.simulated_desktop()
        audit = Mock()
        with patch.object(supervise, "pause_codex_gui_session") as pause:
            self.assertEqual(process.close_codex_app(self.rollout, on_event=audit), ["111", "222"])
        pause.assert_not_called()
        self.assertEqual(terminate.call_count, 2)
        self.assertEqual(audit.call_args.args[0], "HANDOFF_BOUNDARY")

    def test_waits_for_tool_result_then_closes_directly(self):
        terminate = self.simulated_desktop()
        self.write(self.event("function_call", call_id="a"))
        def complete_tool(seconds):
            self.clock.sleep(seconds)
            self.write(self.event("function_call", call_id="a"), self.event("function_call_output", call_id="a"))
        with patch("time.sleep", side_effect=complete_tool), patch.object(supervise, "pause_codex_gui_session") as pause:
            self.assertEqual(process.close_codex_app(self.rollout), ["111", "222"])
        pause.assert_not_called()
        self.assertEqual(terminate.call_count, 2)

    def test_active_model_generation_closes_directly_without_gui_pause(self):
        terminate = self.simulated_desktop()
        self.write(self.event("task_started"))
        with patch.object(supervise, "pause_codex_gui_session") as pause:
            self.assertEqual(process.close_codex_app(self.rollout), ["111", "222"])
        pause.assert_not_called()
        self.assertEqual(terminate.call_count, 2)

    def test_restarted_app_prevents_success(self):
        self.simulated_desktop()
        with patch.object(supervise, "get_codex_desktop_pids", side_effect=[[111, 222], [333]]):
            with self.assertRaisesRegex(RuntimeError, "333"):
                process.close_codex_app(self.rollout)

    def test_surviving_desktop_is_not_success(self):
        self.simulated_desktop(survives=True)
        with self.assertRaisesRegex(RuntimeError, "未完全退出"):
            process.close_codex_app(self.rollout)

    def test_already_closed_needs_no_idle_wait(self):
        with patch.object(supervise, "get_codex_desktop_pids", return_value=[]), patch.object(process.subprocess, "run") as run:
            self.assertEqual(process.close_codex_app(self.root / "missing"), [])
        run.assert_not_called()


class TestKillCLI(KillFixture):
    def run_cli(self, options=(), failure=None, held_lock=False, tty=False, lock_exists=True, real_close=False, competing_supervisor=False):
        locks = self.root / "thread-writer-locks"
        locks.mkdir(exist_ok=True)
        lock = locks / "target.lock"
        if lock_exists:
            lock.write_bytes(b"")
        order = []
        if real_close:
            self.simulated_desktop()
        def close(*args, **kwargs):
            order.append("close")
            if failure:
                raise failure
            if real_close:
                return process.close_codex_app(*args, **kwargs)
            return ["111"]
        def check(path):
            self.assertEqual(path, lock)
            self.assertEqual(order, ["close"])
            order.append("writer")
            if held_lock:
                raise RuntimeError("still locked")
        def headless(**kwargs):
            self.assertEqual(order, ["close", "writer"])
            order.append("headless")
            return 0
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(cli, "__file__", str(self.root / "afk_supervisor" / "cli.py")))
            stack.enter_context(patch.object(cli, "keep_awake"))
            stack.enter_context(patch.object(cli, "detect_system_proxy", return_value=None))
            stack.enter_context(patch.object(cli, "backup_workspace", return_value=None))
            stack.enter_context(patch.object(cli, "atexit"))
            stack.enter_context(patch.object(cli, "load_codex_thread_titles", return_value={}))
            stack.enter_context(patch.object(supervise, "find_codex_session_by_id", return_value=("target", self.rollout, str(self.root))))
            stack.enter_context(patch.object(supervise, "CODEX_LOCKS", locks))
            stack.enter_context(patch.object(supervise, "close_codex_app", side_effect=close))
            stack.enter_context(patch.object(cli, "verify_codex_writer_released", side_effect=check))
            stack.enter_context(patch.object(supervise.WorkspaceSupervisorLock, "acquire", return_value=(not competing_supervisor, "other supervisor")))
            release = stack.enter_context(patch.object(supervise.WorkspaceSupervisorLock, "release"))
            stack.enter_context(patch.object(cli.sys.stdin, "isatty", return_value=tty))
            prompt = stack.enter_context(patch("builtins.input", side_effect=AssertionError("kill must not ask")))
            worker = stack.enter_context(patch.object(cli, "run_headless_supervisor", side_effect=headless))
            notify = stack.enter_context(patch("afk_supervisor.reporting.send_terminal_notification"))
            rc = cli.main(["--adopt", "target", "--quick", *options])
            if competing_supervisor:
                self.assertEqual(order, [])
                worker.assert_not_called()
            if failure or held_lock:
                worker.assert_not_called()
                release.assert_called_once()
                notify.assert_called_once()
                run = next((self.root / "runs").iterdir())
                self.assertEqual(json.loads((run / "supervisor_state.json").read_text(encoding="utf-8"))["state"], "FAILED")
                self.assertTrue((run / "report.md").exists())
            prompt.assert_not_called()
        if lock_exists:
            self.assertTrue(lock.exists(), "Never delete the writer lock")
        return rc

    def test_real_close_flows_into_headless_resume(self):
        self.assertEqual(self.run_cli(real_close=True), 0)

    def test_competing_supervisor_does_not_close_app(self):
        self.assertEqual(self.run_cli(competing_supervisor=True), 1)

    def test_yes_automatically_closes(self):
        self.assertEqual(self.run_cli(("--yes",)), 0)

    def test_noninteractive_automatically_closes(self):
        self.assertEqual(self.run_cli(), 0)

    def test_interactive_only_session_selection_not_close_confirmation(self):
        self.assertEqual(self.run_cli(tty=True), 0)

    def test_missing_lock_does_not_skip_close(self):
        self.assertEqual(self.run_cli(lock_exists=False), 0)

    def test_close_failure_reports_without_resume(self):
        self.assertEqual(self.run_cli(failure=RuntimeError("not safe")), 1)

    def test_process_timeout_reports_without_resume(self):
        self.assertEqual(self.run_cli(failure=subprocess.TimeoutExpired("taskkill", 10)), 1)

    def test_external_writer_prevents_resume(self):
        self.assertEqual(self.run_cli(held_lock=True), 1)


class TestDiscoveryAndLock(KillFixture):
    def test_discovery_failure_is_not_empty_process_list(self):
        with patch.object(process.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"denied")):
            with self.assertRaises(RuntimeError):
                process.get_codex_desktop_pids()

    def test_standalone_server_and_chatgpt_are_not_targets(self):
        rows = [dict(ProcessId=1, ParentProcessId=0, Name="ChatGPT.exe", ExecutablePath=r"C:\Programs\ChatGPT\ChatGPT.exe", CommandLine="ChatGPT.exe"),
                dict(ProcessId=2, ParentProcessId=0, Name="codex.exe", ExecutablePath=r"C:\OpenAI\Codex\bin\codex.exe", CommandLine="codex.exe -c a=b app-server")]
        with patch.object(process.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(rows).encode(), b"")):
            self.assertEqual(process.get_codex_desktop_pids(), [])

    def test_probe_preserves_empty_lock_file(self):
        lock = self.root / "target.lock"
        lock.write_bytes(b"")
        process.verify_codex_writer_released(lock)
        self.assertEqual(lock.read_bytes(), b"")

    @unittest.skipUnless(os.name == "nt", "Windows byte-range lock")
    def test_probe_rejects_held_windows_lock(self):
        import msvcrt
        lock = self.root / "target.lock"
        lock.write_bytes(b"")
        with lock.open("r+b") as owner:
            msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                with self.assertRaises(RuntimeError):
                    process.verify_codex_writer_released(lock)
            finally:
                msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
        self.assertTrue(lock.exists())


class FakeProc:
    """最小 Popen 替身: 只回答 poll/wait, 绝不真实触碰系统进程。"""

    def __init__(self, pid=4321, reaped=False, dies_on_wait=False):
        self.pid = pid
        self._alive = not reaped
        self.dies_on_wait = dies_on_wait
        self.waits = []

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self._alive and not self.dies_on_wait:
            raise subprocess.TimeoutExpired("worker", timeout)
        self._alive = False
        return 0


class ProcReapedByForcedKill(FakeProc):
    """优雅中断超时, 只有 taskkill 之后的回查才真正回收: 强制终止的正常路径。"""

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if len(self.waits) < 2:
            raise subprocess.TimeoutExpired("worker", timeout)
        self._alive = False
        return 0


class KillTruthfulnessRegressions(KillFixture):
    """击杀必须回报真实结果: taskkill 的退出码说明不了进程已死。"""

    def codex_driver(self):
        from afk_supervisor.drivers.codex import CodexDriver
        run_dir = self.root / "run"
        run_dir.mkdir(exist_ok=True)
        return CodexDriver(self.root, run_dir)

    def driver(self, proc):
        return SimpleNamespace(
            proc=proc, jsonl=self.rollout,
            interrupt=Mock(return_value=True), wait_exit=Mock(return_value=True),
            kill_tree=Mock(return_value=True),
        )

    def test_safe_kill_marks_failure_while_process_survives(self):
        driver = self.driver(FakeProc())
        self.assertEqual(process.safe_kill(driver), "boundary_failed")
        self.assertTrue(driver.kill_tree.called)

    def test_safe_kill_reports_plain_method_once_process_is_gone(self):
        driver = self.driver(FakeProc(reaped=True))
        self.assertEqual(process.safe_kill(driver), "boundary")

    def test_driver_alive_check_never_trusts_a_missing_handle(self):
        self.assertFalse(process.driver_process_alive(SimpleNamespace(proc=None)))
        self.assertTrue(process.driver_process_alive(SimpleNamespace(proc=FakeProc())))
        self.assertFalse(process.driver_process_alive(SimpleNamespace(proc=FakeProc(reaped=True))))
        broken = SimpleNamespace(proc=SimpleNamespace(poll=Mock(side_effect=OSError("gone"))))
        self.assertFalse(process.driver_process_alive(broken))

    def test_codex_kill_tree_reports_access_denied_instead_of_forcing_a_receipt(self):
        driver = self.codex_driver()
        proc = FakeProc()
        driver.proc = proc
        driver.interrupt = Mock(return_value=False)
        shutdown = subprocess.CompletedProcess(
            ["taskkill"], 1, stdout="", stderr="ERROR: The process could not be terminated. Access is denied.")
        with patch("afk_supervisor.drivers.codex.subprocess.run", return_value=shutdown), \
             patch("afk_supervisor.drivers.codex.log") as logged:
            self.assertFalse(driver.kill_tree(), "进程仍在时不得回报已终止")
        messages = " ".join(str(call.args[0]) for call in logged.call_args_list)
        self.assertIn("未能终止", messages)
        self.assertIn("权限不足", messages)
        self.assertNotIn("已强制终止", messages)

    def test_codex_kill_tree_confirms_receipt_only_after_process_dies(self):
        driver = self.codex_driver()
        driver.proc = ProcReapedByForcedKill()
        driver.interrupt = Mock(return_value=False)
        with patch("afk_supervisor.drivers.codex.subprocess.run",
                   return_value=subprocess.CompletedProcess(["taskkill"], 0, stdout="", stderr="")), \
             patch("afk_supervisor.drivers.codex.log") as logged:
            self.assertTrue(driver.kill_tree())
        messages = " ".join(str(call.args[0]) for call in logged.call_args_list)
        self.assertIn("已强制终止", messages)

    def test_codex_kill_tree_closes_handles_even_when_kill_fails(self):
        driver = self.codex_driver()
        handle = Mock()
        driver._open_handles = [handle]
        driver.proc = FakeProc()
        driver.interrupt = Mock(return_value=False)
        with patch("afk_supervisor.drivers.codex.subprocess.run",
                   return_value=subprocess.CompletedProcess(["taskkill"], 1, stdout="", stderr="denied")):
            self.assertFalse(driver.kill_tree())
        handle.close.assert_called_once()
