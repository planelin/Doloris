"""
Tests for rate limit error resilience, delivery path authorization, and workspace protection.
"""
import json
import tempfile
import unittest
from pathlib import Path

from afk_supervisor.baseline import (
    extract_explicit_delivery_dir,
    extract_task_baseline,
    is_forbidden_system_root,
)
from afk_supervisor.sessions.rollout import (
    is_rate_limit_error,
    extract_retry_delay,
    codex_session_state,
    is_codex_working,
    peek_rollout_activity,
)


class TestRateLimitResilience(unittest.TestCase):
    def test_is_rate_limit_error_detection(self):
        # The exact error payload from the user's failed run
        user_error = {
            "message": "rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2 have exceeded rate limit. Please retry after 20 seconds. Visit https://platform.openai.com/docs/guides/rate-limits to learn more.",
            "codex_error_info": "rate_limit_exceeded",
        }
        self.assertTrue(is_rate_limit_error(user_error))
        self.assertEqual(extract_retry_delay(user_error), 25.0)  # 20 + 5s buffer

        # Other variants
        self.assertTrue(is_rate_limit_error("HTTP 429 Too Many Requests"))
        self.assertTrue(is_rate_limit_error("Server overloaded, retry in 30s"))
        self.assertEqual(extract_retry_delay("Server overloaded, retry in 30s"), 35.0)
        self.assertTrue(is_rate_limit_error({"code": 503, "message": "Service Unavailable"}))
        self.assertTrue(is_rate_limit_error("connection error: connection reset by peer"))
        self.assertTrue(is_rate_limit_error("timed out waiting for response"))

        # Non rate limit errors
        self.assertFalse(is_rate_limit_error(None))
        self.assertFalse(is_rate_limit_error(""))
        self.assertFalse(is_rate_limit_error("SyntaxError: invalid syntax"))
        self.assertFalse(is_rate_limit_error("FileNotFoundError: [Errno 2] No such file or directory"))

    def test_rollout_captures_rate_limit_state(self):
        with tempfile.TemporaryDirectory() as td:
            rollout_p = Path(td) / "rollout.jsonl"
            lines = [
                json.dumps({"type": "session_meta", "payload": {"id": "sess-123"}}) + "\n",
                json.dumps({"type": "event_msg", "payload": {"type": "turn_started"}}) + "\n",
                json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-001",
                        "error": {
                            "message": "rate limit exceeded: Your requests to gpt-6-astra for gpt-6-astra in eastus2 have exceeded rate limit. Please retry after 20 seconds.",
                            "codex_error_info": "rate_limit_exceeded",
                        }
                    }
                }) + "\n",
            ]
            rollout_p.write_text("".join(lines), encoding="utf-8")

            snapshot = codex_session_state(rollout_p)
            self.assertEqual(snapshot["status"], "stopped")
            self.assertTrue(snapshot["is_rate_limited"])
            self.assertEqual(snapshot["retry_delay_sec"], 25.0)
            self.assertIn("rate limit exceeded", snapshot["turn_error_message"])

            # is_codex_working should report stopped but with rate limit indicator
            is_working, reason, _ = is_codex_working(rollout_p)
            self.assertFalse(is_working)
            self.assertIn("速率受限", reason)

            # peek_rollout_activity should indicate rate limit rather than normal complete
            peek = peek_rollout_activity(rollout_p)
            self.assertIn("限流", peek)


class TestDeliveryDirAndWorkspaceAuthorization(unittest.TestCase):
    def test_attachment_and_pdf_not_mistaken_for_delivery_dir(self):
        # Prompt containing Codex user attachment preamble
        prompt_with_attachment = (
            "# Files mentioned by the user:\n"
            "- C:\\Users\\lastnut\\.codex\\attachments\\简历_工程师_长春工业大学.pdf\n\n"
            "doloris1测试1，制作一个简单的前端界面，工作区C:\\Agents\\codex\\doloris\\testproj 这是一次测试"
        )
        detected = extract_explicit_delivery_dir(prompt_with_attachment)
        self.assertEqual(detected, r"C:\Agents\codex\doloris\testproj")

    def test_file_paths_never_extracted_as_delivery_dir(self):
        self.assertIsNone(extract_explicit_delivery_dir("请阅读 C:\\docs\\readme.pdf 并分析"))
        self.assertIsNone(extract_explicit_delivery_dir("输出到 C:\\output\\data.json 汇总"))
        self.assertIsNone(extract_explicit_delivery_dir("截图保存于 C:\\images\\preview.png"))

    def test_explicit_workspace_keywords_extracted(self):
        self.assertEqual(
            extract_explicit_delivery_dir("制作前端页面 工作区 C:\\Agents\\codex\\doloris\\testproj 尽快完成"),
            r"C:\Agents\codex\doloris\testproj"
        )
        self.assertEqual(
            extract_explicit_delivery_dir("工作区: C:/Users/test/my_app/"),
            "C:/Users/test/my_app"
        )
        self.assertEqual(
            extract_explicit_delivery_dir("项目在 `D:/projects/web_demo`。"),
            "D:/projects/web_demo"
        )

    def test_user_requested_workspace_authorized_without_false_cross_workspace_blocker(self):
        with tempfile.TemporaryDirectory() as td:
            base_p = Path(td)
            session_cwd = base_p / "default_cwd"
            session_cwd.mkdir()

            user_workspace = base_p / "user_project_dir"
            user_workspace.mkdir()

            user_prompt = f"构建前端界面，工作区: {user_workspace}"

            baseline = extract_task_baseline(
                session_cwd=session_cwd,
                title=user_prompt,
                writable_roots=None,  # Not explicitly passed via CLI
            )

            # Requested delivery dir must match the user's prompt
            self.assertEqual(Path(baseline.requested_delivery_dir).resolve(), user_workspace.resolve())

            # It MUST be authorized in writable_roots
            self.assertIn(str(user_workspace.resolve()), baseline.writable_roots)

            # It must NOT produce a cross-workspace blocker
            for blocker in baseline.blockers:
                self.assertNotIn("跨工作区未授权路径", blocker)
            self.assertEqual(len(baseline.blockers), 0)
            self.assertEqual(Path(baseline.effective_delivery_dir).resolve(), user_workspace.resolve())

    def test_forbidden_system_roots_rejected(self):
        self.assertTrue(is_forbidden_system_root("C:\\"))
        self.assertTrue(is_forbidden_system_root("C:/"))
        self.assertTrue(is_forbidden_system_root("C:\\Windows"))
        self.assertTrue(is_forbidden_system_root("C:\\Windows\\System32"))
        self.assertTrue(is_forbidden_system_root("C:\\Program Files"))
        self.assertTrue(is_forbidden_system_root("C:\\Program Files (x86)\\test"))
        self.assertTrue(is_forbidden_system_root("/etc"))
        self.assertTrue(is_forbidden_system_root("/usr/bin"))

        # User workspace paths are NOT forbidden
        self.assertFalse(is_forbidden_system_root("C:\\Agents\\codex\\doloris\\testproj"))
        self.assertFalse(is_forbidden_system_root("D:\\workspace\\demo"))


if __name__ == "__main__":
    unittest.main()
