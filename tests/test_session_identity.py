import json
import unittest
from unittest.mock import patch

from afk_supervisor.sessions.discovery import _read_meta, find_codex_session_by_id
from afk_supervisor.baseline import extract_explicit_delivery_dir
from afk_supervisor.cli import build_arg_parser
from tests.test_debug_regressions import DebugFixture


class SessionIdentityTests(DebugFixture):
    def test_guardian_with_parent_session_id_is_never_adopted(self):
        parent = self.root / "rollout-parent.jsonl"
        child = self.root / "rollout-guardian.jsonl"
        parent.write_text(json.dumps({"type": "session_meta", "payload": {"id": "parent", "cwd": str(self.ws)}}) + "\n", encoding="utf-8")
        child.write_text(json.dumps({"type": "session_meta", "payload": {"session_id": "parent", "id": "guardian", "thread_source": "guardian_review", "cwd": str(self.ws)}}) + "\n", encoding="utf-8")
        self.assertIsNone(_read_meta(child))
        with patch("afk_supervisor.sessions.discovery.get_codex_sessions_dir", return_value=self.root):
            self.assertEqual(find_codex_session_by_id("parent")[1], parent)

    def test_exact_id_wins_over_inherited_session_id(self):
        path = self.root / "rollout-child.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"session_id": "parent", "id": "child", "cwd": str(self.ws)}}), encoding="utf-8")
        self.assertEqual(_read_meta(path)[0], "child")

    def test_preview_url_is_not_a_windows_delivery_directory(self):
        self.assertIsNone(extract_explicit_delivery_dir("预览 http://localhost:4173`。"))
        self.assertIsNone(extract_explicit_delivery_dir("预览 https://localhost:4173"))
        self.assertEqual(extract_explicit_delivery_dir("项目在 `C:/project/testproj`。"), "C:/project/testproj")

    def test_default_l2_wait_supports_long_analysis_and_can_be_overridden(self):
        parser = build_arg_parser()
        self.assertEqual(parser.parse_args([]).timeout_sec, 1800)
        self.assertEqual(parser.parse_args(["--timeout-sec", "90"]).timeout_sec, 90)
