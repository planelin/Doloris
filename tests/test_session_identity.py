import json
import os
import sqlite3
import time
from unittest.mock import patch

from afk_supervisor.sessions.discovery import (
    _read_meta,
    find_codex_session_by_id,
    find_codex_session_rollouts,
    load_codex_thread_title_candidates,
    load_codex_thread_titles,
)
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

    def test_desktop_fork_child_rollout_is_resolvable_by_filename_suffix(self):
        """桌面端 fork 子文件名带 child_sid 后缀，但 session_meta 继承 root_sid。"""
        root_id = "01a0cc11-580a-7ca0-97bf-8d44b2ae978f"
        child_id = "01a0cc51-d875-78c0-a39a-d203c893da0a"
        child = self.root / f"rollout-2026-09-23T11-31-51-{root_id}_{child_id}.jsonl"
        child.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": root_id, "session_id": root_id, "cwd": str(self.ws)},
        }) + "\n", encoding="utf-8")
        with patch("afk_supervisor.sessions.discovery.get_codex_sessions_dir", return_value=self.root):
            root_found = find_codex_session_by_id(root_id)
            child_found = find_codex_session_by_id(child_id)
            rollouts = find_codex_session_rollouts(root_id)
        self.assertEqual(root_found[0], root_id)
        self.assertEqual(root_found[1], child)
        self.assertEqual(child_found[0], child_id)
        self.assertEqual(child_found[1], child)
        self.assertEqual([item[1] for item in rollouts], [child])

    def test_origin_rollout_wins_over_newer_fork_child(self):
        """父文件与 fork 子文件同时存在时，按 root SID 查询必须返回父文件。

        子文件 mtime 更新（fork 之后持续写入），旧实现按 mtime 取"最新"，
        会导致父会话暂停校验、目标提炼全部读到子会话轨迹。
        """
        root_id = "01a0cc11-580a-7ca0-97bf-8d44b2ae978f"
        child_id = "01a0cc51-d875-78c0-a39a-d203c893da0a"
        parent = self.root / f"rollout-2026-09-23T10-21-23-{root_id}.jsonl"
        child = self.root / f"rollout-2026-09-23T11-31-51-{root_id}_{child_id}.jsonl"
        meta_line = json.dumps({
            "type": "session_meta",
            "payload": {"id": root_id, "session_id": root_id, "cwd": str(self.ws)},
        }) + "\n"
        parent.write_text(meta_line, encoding="utf-8")
        child.write_text(meta_line, encoding="utf-8")
        now = time.time()
        os.utime(parent, (now - 600, now - 600))
        os.utime(child, (now, now))

        with patch("afk_supervisor.sessions.discovery.get_codex_sessions_dir", return_value=self.root):
            by_root = find_codex_session_by_id(root_id)
            by_child = find_codex_session_by_id(child_id)
            rollouts = find_codex_session_rollouts(root_id)

        self.assertEqual(by_root[0], root_id)
        self.assertEqual(by_root[1], parent)
        self.assertEqual(by_child[1], child)
        # 回溯业务主线时父文件必须排在 fork 子文件之前。
        self.assertEqual([item[1] for item in rollouts], [parent, child])

    def test_sidebar_name_beats_stale_session_index_title(self):
        """侧栏显示名 (state_5.sqlite.name) 优先于滞后的 session_index.jsonl。"""
        sid = "01a0cc3a-2f4f-7791-be89-f5f59e005336"
        (self.root / "session_index.jsonl").write_text(
            json.dumps({"id": sid, "thread_name": "帮我全面审查 Doloris"}) + "\n",
            encoding="utf-8",
        )
        con = sqlite3.connect(self.root / "state_5.sqlite")
        try:
            con.execute("CREATE TABLE threads (id TEXT, name TEXT, title TEXT)")
            con.execute(
                "INSERT INTO threads VALUES (?, ?, ?)",
                (sid, "[Fork] 帮我全面审查 Doloris", "继续"),
            )
            con.commit()
        finally:
            con.close()

        with patch("afk_supervisor.sessions.discovery.get_codex_home", return_value=self.root):
            titles = load_codex_thread_titles()
            candidates = load_codex_thread_title_candidates(sid)

        self.assertEqual(titles[sid], "[Fork] 帮我全面审查 Doloris")
        self.assertEqual(candidates[0], "[Fork] 帮我全面审查 Doloris")
        self.assertIn("继续", candidates)
        self.assertIn("帮我全面审查 Doloris", candidates)
        # 候选去重且顺序稳定（name → title → index）。
        self.assertEqual(len(candidates), len(set(candidates)))

    def test_preview_url_is_not_a_windows_delivery_directory(self):
        self.assertIsNone(extract_explicit_delivery_dir("预览 http://localhost:4173`。"))
        self.assertIsNone(extract_explicit_delivery_dir("预览 https://localhost:4173"))
        self.assertEqual(extract_explicit_delivery_dir("项目在 `C:/project/testproj`。"), "C:/project/testproj")

    def test_default_l2_wait_supports_long_analysis_and_can_be_overridden(self):
        parser = build_arg_parser()
        self.assertEqual(parser.parse_args([]).timeout_sec, 1800)
        self.assertEqual(parser.parse_args(["--timeout-sec", "90"]).timeout_sec, 90)
