"""
Tests for AGY backend 400 (geo restriction) detection and Codex quota exhaustion handling.

背景 (2026-09-27 实测):
1. AGY 生成失败 (FAILED_PRECONDITION code 400, User location is not supported)
   只落 language_server.log 与会话 DB, transcript.jsonl 永远没有 ERROR 条目,
   等待方原本只能空等到超时, 且换节点后仍对着死会话等待。
2. Codex 配额耗尽 ("unexpected status 402 Payment Required: Budget pool quota
   has been exhausted") 杀死回合并留下空 last_agent_message, 此前不在
   RATE_LIMIT_KEYWORDS 内, 导致未完工的空回合被当成正常回合结束发起 REVIEW。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from afk_supervisor.sessions.rollout import (
    codex_session_state,
    is_codex_working,
    is_rate_limit_error,
)


QUOTA_402_MESSAGE = (
    "unexpected status 402 Payment Required: "
    "Budget pool quota has been exhausted"
)


class TestQuotaExhaustionRecognized(unittest.TestCase):
    def test_402_budget_pool_message_matches_rate_limit_keywords(self):
        """402 配额耗尽必须命中资源类错误关键词, 否则会被送去 REVIEW。"""
        self.assertTrue(is_rate_limit_error(QUOTA_402_MESSAGE))
        self.assertTrue(is_rate_limit_error({"message": QUOTA_402_MESSAGE}))
        self.assertTrue(is_rate_limit_error("Payment Required: quota exhausted"))
        # 不受影响的原有判别
        self.assertFalse(is_rate_limit_error("SyntaxError: invalid syntax"))

    def test_errored_blank_turn_is_not_a_review_point(self):
        """402 杀死的空回合: stopped + is_rate_limited, 且留言为空。"""
        with tempfile.TemporaryDirectory() as td:
            rollout_p = Path(td) / "rollout.jsonl"
            lines = [
                json.dumps({"type": "session_meta", "payload": {"id": "sess-402"}}) + "\n",
                json.dumps({"type": "event_msg", "payload": {"type": "turn_started"}}) + "\n",
                json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-402",
                        "last_agent_message": None,
                        "error": {"message": QUOTA_402_MESSAGE},
                    },
                }) + "\n",
            ]
            rollout_p.write_text("".join(lines), encoding="utf-8")

            snapshot = codex_session_state(rollout_p)
            self.assertEqual(snapshot["status"], "stopped")
            self.assertTrue(snapshot["is_rate_limited"])
            self.assertTrue(snapshot.get("turn_error_message"))
            self.assertFalse(snapshot.get("last_agent_message"))

            working, _, last_msg = is_codex_working(rollout_p)
            self.assertFalse(working)
            self.assertEqual(last_msg, "")


class TestAgyBackendErrorWatcher(unittest.TestCase):
    def _watcher(self, log_p: Path):
        from afk_supervisor.l2.bridge import AgyBackendErrorWatcher
        return AgyBackendErrorWatcher(log_path=log_p)

    def test_detects_error_after_mark_only(self):
        with tempfile.TemporaryDirectory() as td:
            log_p = Path(td) / "language_server.log"
            log_p.write_text("I0927 18:06:31.878782 server.go] startup\n", encoding="utf-8")
            watcher = self._watcher(log_p)
            watcher.mark()

            log_p.write_text(
                "I0927 18:06:31.878782 server.go] startup\n"
                "I0927 20:40:00.000000 store_client.go] ok line\n",
                encoding="utf-8",
            )
            self.assertIsNone(watcher.check())

            with log_p.open("a", encoding="utf-8") as f:
                f.write(
                    "E0927 20:39:02.137953 errorreport.go:224] agent executor error: "
                    "generating and executing: FAILED_PRECONDITION (code 400): "
                    "User location is not supported for the API use.\n"
                )
            hit = watcher.check()
            self.assertIsNotNone(hit)
            self.assertIn("FAILED_PRECONDITION", hit)
            self.assertIn("User location is not supported", hit)
            # 已消费: 同一窗口不会重复告警
            self.assertIsNone(watcher.check())

    def test_log_rotation_resets_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            log_p = Path(td) / "language_server.log"
            log_p.write_text("x" * 2048, encoding="utf-8")
            watcher = self._watcher(log_p)
            watcher.mark()
            log_p.write_text("tiny", encoding="utf-8")
            self.assertIsNone(watcher.check())

    def test_missing_log_is_safe(self):
        with tempfile.TemporaryDirectory() as td:
            watcher = self._watcher(Path(td) / "not_exist.log")
            watcher.mark()
            self.assertIsNone(watcher.check())


class TestUnbindAgyConversation(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._td.name)
        self.fake_reg = self.run_dir / "test_registry.json"

    def tearDown(self):
        self._td.cleanup()

    def test_unbind_clears_registry_and_session_file(self):
        from afk_supervisor.l2.bridge import (
            bind_agy_conversation_for_codex,
            get_agy_conversation_for_codex,
            unbind_agy_conversation_for_codex,
        )
        fake_brain = self.run_dir / "fake_brain"
        fake_brain.mkdir(parents=True, exist_ok=True)
        (fake_brain / "01b1b66f-b04f-4bf4-a91f-d039e3faadbc").mkdir(parents=True, exist_ok=True)
        (fake_brain / "other-cid").mkdir(parents=True, exist_ok=True)
        sid = "01a0e2b1-89d4-76c0-88b1-02e0cab926b5"
        cid = "01b1b66f-b04f-4bf4-a91f-d039e3faadbc"
        with patch("afk_supervisor.l2.bridge.get_codex_agy_registry_file", return_value=self.fake_reg), \
             patch("afk_supervisor.l2.bridge.get_agy_brain_dir", return_value=fake_brain):
            bind_agy_conversation_for_codex(sid, cid, run_dir=self.run_dir)
            self.assertEqual(get_agy_conversation_for_codex(sid, run_dir=self.run_dir), cid)

            unbind_agy_conversation_for_codex(sid, self.run_dir)

            self.assertIsNone(get_agy_conversation_for_codex(sid, run_dir=self.run_dir))
            session_data = json.loads(
                (self.run_dir / "agy_session.json").read_text(encoding="utf-8")
            )
            self.assertEqual(session_data.get("agy_conversation_id"), "")
            # 其他任务的绑定不受影响
            bind_agy_conversation_for_codex("01a0e2b1-aaaa-76c0-88b1-02e0cab926b5", "other-cid", run_dir=self.run_dir)
            unbind_agy_conversation_for_codex(sid, self.run_dir)
            self.assertEqual(
                get_agy_conversation_for_codex("01a0e2b1-aaaa-76c0-88b1-02e0cab926b5", run_dir=self.run_dir),
                "other-cid",
            )

    def test_unbind_unknown_session_is_noop(self):
        from afk_supervisor.l2.bridge import unbind_agy_conversation_for_codex
        with patch("afk_supervisor.l2.bridge.get_codex_agy_registry_file", return_value=self.fake_reg):
            unbind_agy_conversation_for_codex("unknown", self.run_dir)
            unbind_agy_conversation_for_codex("", self.run_dir)
            self.assertFalse(self.fake_reg.exists())


if __name__ == "__main__":
    unittest.main()
