"""Kill takeover regression tests; all process/GUI boundaries are simulated."""
import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import supervise
from afk_supervisor import cli
from afk_supervisor.platform import process
from afk_supervisor.sessions.rollout import codex_handoff_state
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
