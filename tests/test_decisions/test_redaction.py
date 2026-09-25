"""脱敏模块测试: 发往决策 API 前的状态必须移除/替换敏感信息。"""

import unittest

from afk_supervisor.decisions.redaction import (
    MAX_DEPTH,
    MAX_STR_LEN,
    TRUNCATED,
    is_sensitive_key,
    redact_state,
    redact_text,
)

SAMPLE_SECRETS = ("tok_live_9f8e7d", "bearer-raw-secret")


class SensitiveKeyTests(unittest.TestCase):
    def test_credential_like_keys_are_flagged(self):
        for key in ("api_token", "API_KEY", "password", "user_password", "COOKIE",
                    "Authorization", "ssh_private_key", "repo_credentials", "client_secret"):
            with self.subTest(key=key):
                self.assertTrue(is_sensitive_key(key))

    def test_benign_keys_are_not_flagged(self):
        for key in ("worker_alive", "tests_failed", "author", "authored_by", "task_stage",
                    "last_message_summary", "risk", "choice"):
            with self.subTest(key=key):
                self.assertFalse(is_sensitive_key(key))


class RedactTextTests(unittest.TestCase):
    def test_bearer_token_is_replaced(self):
        out = redact_text("Authorization: Bearer tok_live_9f8e7d; 请继续")
        self.assertNotIn("tok_live_9f8e7d", out)
        self.assertIn("[REDACTED]", out)

    def test_authorization_header_value_is_replaced(self):
        out = redact_text('authorization = "Basic dXNlcjpwYXNz"')
        self.assertNotIn("dXNlcjpwYXNz", out)

    def test_private_key_block_is_removed(self):
        blob = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
        out = redact_text(f"key={blob}")
        self.assertNotIn("OPENSSH", out)
        self.assertNotIn("abc", out)

    def test_url_embedded_credentials_are_replaced(self):
        out = redact_text("clone from https://user:ghp_token123@github.com/org/repo.git")
        self.assertNotIn("ghp_token123", out)
        self.assertNotIn("user:", out)
        self.assertIn("github.com/org/repo.git", out)

    def test_windows_user_path_username_is_replaced(self):
        out = redact_text(r"日志位于 C:\Users\alice\project\runs\xyz")
        self.assertNotIn("alice", out)
        self.assertIn(r"C:\Users\[USER]\project\runs\xyz", out)

    def test_posix_user_path_username_is_replaced(self):
        out = redact_text("log at /home/bob/work/out.txt and /Users/carol/tmp")
        self.assertNotIn("bob", out)
        self.assertNotIn("carol", out)

    def test_explicit_secrets_are_replaced_everywhere(self):
        out = redact_text("a tok_live_9f8e7d b tok_live_9f8e7d c", secrets=SAMPLE_SECRETS)
        self.assertNotIn("tok_live_9f8e7d", out)

    def test_oversized_text_is_truncated(self):
        out = redact_text("x" * (MAX_STR_LEN + 500))
        self.assertEqual(len(out), MAX_STR_LEN + len(TRUNCATED))

    def test_normal_text_passes_through_unchanged(self):
        text = "Worker 正在等待用户在两个视觉主题中选择一个。"
        self.assertEqual(redact_text(text), text)


class RedactStateTests(unittest.TestCase):
    def test_sensitive_keys_are_replaced_recursively(self):
        state = {
            "env": {"GH_TOKEN": "ghp_x", "PATH": "ok"},
            "headers": {"Authorization": "Bearer ghp_y"},
            "list": [{"password": "p", "keep": 1}],
        }
        out = redact_state(state)
        self.assertEqual(out["env"]["GH_TOKEN"], "[REDACTED]")
        self.assertEqual(out["env"]["PATH"], "ok")
        self.assertEqual(out["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(out["list"][0]["password"], "[REDACTED]")
        self.assertEqual(out["list"][0]["keep"], 1)

    def test_scalars_pass_through(self):
        state = {"worker_alive": True, "tests_failed": False, "exit_code": 2,
                 "risk": "low", "ratio": 0.5, "nothing": None}
        self.assertEqual(redact_state(state), state)

    def test_non_string_scalar_values_in_sensitive_keys_are_still_redacted(self):
        out = redact_state({"api_key": 12345})
        self.assertEqual(out["api_key"], "[REDACTED]")

    def test_deep_nesting_is_truncated(self):
        state = current = {}
        for _ in range(MAX_DEPTH + 5):
            current["child"] = {}
            current = current["child"]
        out = redact_state(state)
        depth = 0
        node = out
        while isinstance(node, dict) and "child" in node:
            node = node["child"]
            depth += 1
        self.assertLessEqual(depth, MAX_DEPTH)
        self.assertEqual(node, TRUNCATED)

    def test_non_json_objects_become_redacted_strings(self):
        class Opaque:
            def __str__(self):
                return "token=tok_live_9f8e7d obj"

        out = redact_state({"blob": Opaque()})
        self.assertNotIn("tok_live_9f8e7d", out["blob"])

    def test_no_secret_value_survives_in_any_string(self):
        state = {
            "note": "tok_live_9f8e7d",
            "nested": {"inner": ["x tok_live_9f8e7d y", {"k": "bearer-raw-secret"}]},
            "url": "https://user:bearer-raw-secret@git.example/repo.git",
        }
        out = redact_state(state, secrets=SAMPLE_SECRETS)
        self.assertNotIn("tok_live_9f8e7d", str(out))
        self.assertNotIn("bearer-raw-secret", str(out))


if __name__ == "__main__":
    unittest.main()
