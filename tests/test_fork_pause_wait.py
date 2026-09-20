"""AFK2 waits for evidence of parent pause, never a wall-clock grace period."""
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from afk_supervisor.platform import gui
from afk_supervisor.sessions.rollout import is_codex_working
from tests.test_supervisor_loops import FakeClock


class ForkPauseWaitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="afk-fork-wait-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.rollout = self.root / "parent.jsonl"
        (self.root / "gui_inject.ps1").write_text("# simulated", encoding="utf-8")
        self.write("task_started")
        self.clock = FakeClock()
        self.started = self.clock.now
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("time.monotonic", side_effect=self.clock.monotonic))
        stack.enter_context(patch.object(gui, "get_workspace_root", return_value=self.root))
        stack.enter_context(patch.object(gui, "find_best_codex_window", return_value=123))
        stack.enter_context(patch.object(gui, "_navigate_target"))
        self.send = stack.enter_context(patch.object(gui.subprocess, "run", return_value=SimpleNamespace(stdout='{"ok":true}')))

    def write(self, event):
        self.rollout.write_text(json.dumps({"type":"event_msg", "payload":{"type":event}}) + "\n", encoding="utf-8")

    def after_180_seconds(self, seconds):
        self.clock.sleep(seconds)
        if self.clock.now - self.started >= 180:
            self.write("turn_aborted")

    def test_unlimited_wait_outlasts_20_30_and_90_seconds(self):
        with patch("time.sleep", side_effect=self.after_180_seconds):
            self.assertTrue(gui.pause_codex_gui_session(self.rollout, max_wait=None))
        self.assertEqual(self.clock.now - self.started, 180)
        self.send.assert_called_once()  # No repeated interrupt storm.

    def test_bounded_kill_wait_still_times_out(self):
        with patch("time.sleep", side_effect=self.clock.sleep):
            self.assertFalse(gui.pause_codex_gui_session(self.rollout, max_wait=3))
        self.assertEqual(self.clock.now - self.started, 3)

    def test_already_paused_has_no_delay_or_extra_interrupt(self):
        self.write("task_complete")
        with patch("time.sleep", side_effect=AssertionError("No fixed buffer")):
            self.assertTrue(gui.pause_codex_gui_session(self.rollout, max_wait=None))
        self.send.assert_not_called()
        self.assertFalse(is_codex_working(self.rollout)[0])

    def test_bad_or_missing_evidence_waits_instead_of_authorising_fork(self):
        for text in ("", "{}\n", '{"type":'):
            with self.subTest(text=text):
                self.rollout.write_text(text, encoding="utf-8")
                self.clock.now = self.started
                with patch("time.sleep", side_effect=self.after_180_seconds):
                    self.assertTrue(gui.pause_codex_gui_session(self.rollout, max_wait=None))
                self.assertEqual(self.clock.now - self.started, 180)
        self.rollout.unlink()
        self.clock.now = self.started
        with patch("time.sleep", side_effect=self.after_180_seconds):
            self.assertTrue(gui.pause_codex_gui_session(self.rollout, max_wait=None))
        self.assertEqual(self.clock.now - self.started, 180)

    def test_uncertain_pause_request_still_waits_for_actual_stop(self):
        self.send.side_effect = OSError("no receipt")
        with patch("time.sleep", side_effect=self.after_180_seconds):
            self.assertTrue(gui.pause_codex_gui_session(self.rollout, max_wait=None))
        self.assertEqual(self.clock.now - self.started, 180)

    def test_no_gui_window_does_not_allow_fork_or_end_wait(self):
        with patch.object(gui, "find_best_codex_window", return_value=0), patch("time.sleep", side_effect=self.after_180_seconds):
            self.assertTrue(gui.pause_codex_gui_session(self.rollout, max_wait=None))
        self.send.assert_not_called()
        self.assertEqual(self.clock.now - self.started, 180)

    def test_infinite_wait_can_be_cancelled_without_returning_success(self):
        with patch("time.sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            gui.pause_codex_gui_session(self.rollout, max_wait=None)
