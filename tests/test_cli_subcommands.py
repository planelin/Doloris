"""doloris.cmd 子命令转发与 cli 侧解析的回归测试。

历史缺陷: doloris.cmd 用 `shift` 消费子命令后再转发 `%*`, 但 cmd.exe 的 %*
不受 shift 影响, 导致子命令词重复进入 supervise.py 的 argv, fork/goal/resume
全部 exit 2。修复后 cmd 只做纯转发, 解析收敛到 afk_supervisor.cli。
"""
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import supervise
from afk_supervisor import cli


class ExtractSubcommandTests(unittest.TestCase):
    def test_subcommand_first_with_following_target(self):
        rest, sub, target = cli._extract_subcommand(["fork", "3", "--yes"])
        self.assertEqual((rest, sub, target), (["--yes"], "fork", "3"))

    def test_subcommand_without_target(self):
        rest, sub, target = cli._extract_subcommand(["goal", "--quick"])
        self.assertEqual((rest, sub, target), (["--quick"], "goal", ""))

    def test_bare_target_consumes_only_head(self):
        rest, sub, target = cli._extract_subcommand(["session-abc", "--quick"])
        self.assertEqual((rest, sub, target), (["--quick"], "", "session-abc"))

    def test_case_insensitive_subcommand(self):
        rest, sub, target = cli._extract_subcommand(["FORK"])
        self.assertEqual((rest, sub, target), ([], "fork", ""))

    def test_options_first_leave_head_untouched(self):
        rest, sub, target = cli._extract_subcommand(["--adopt", "3", "--quick"])
        self.assertEqual((rest, sub, target), (["--adopt", "3", "--quick"], "", ""))

    def test_empty_argv(self):
        self.assertEqual(cli._extract_subcommand([]), ([], "", ""))


class SubcommandCLITests(unittest.TestCase):
    """端到端: 经 cli.main 的子命令翻译, 断言最终进入监管引擎的模式与目标。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="afk-cli-subcmd-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rollout = self.root / "rollout.jsonl"
        self.rollout.write_text("", encoding="utf-8")

    def run_cli(self, argv, sessions=None, engine="headless"):
        """在完整补丁下运行 cli.main, 返回 (rc, engine调用kwargs, popen mock)。"""
        sessions = sessions if sessions is not None else [("target", self.rollout, str(self.root), "标题", 0)]
        captured = {}
        popen = None

        def recorder(**kwargs):
            captured.update(kwargs)
            return 0

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(cli, "keep_awake"))
            stack.enter_context(patch.object(cli, "detect_system_proxy", return_value=None))
            stack.enter_context(patch.object(cli, "backup_workspace", return_value=None))
            stack.enter_context(patch.object(cli, "atexit"))
            stack.enter_context(patch.object(cli, "load_codex_thread_titles", return_value={}))
            stack.enter_context(patch.object(cli, "list_recent_codex_sessions", return_value=sessions))
            stack.enter_context(patch.object(
                supervise, "find_codex_session_by_id",
                return_value=("target", self.rollout, str(self.root))))
            stack.enter_context(patch.object(supervise, "pause_codex_gui_session", return_value=True))
            # resume 子命令走 kill 接管路径, 必须模拟进程关闭与写锁探针
            stack.enter_context(patch.object(supervise, "close_codex_app", return_value=["111"]))
            stack.enter_context(patch.object(cli, "verify_codex_writer_released", return_value="ok"))
            stack.enter_context(patch.object(supervise.WorkspaceSupervisorLock, "acquire", return_value=(True, "")))
            stack.enter_context(patch.object(supervise.WorkspaceSupervisorLock, "release"))
            stack.enter_context(patch.object(cli.sys.stdin, "isatty", return_value=False))
            if engine == "headless":
                stack.enter_context(patch.object(cli, "run_headless_supervisor", side_effect=recorder))
            else:
                stack.enter_context(
                    patch("afk_supervisor.goal_engine.run_goal_supervisor", side_effect=recorder))
            if argv and argv[0].strip().lower() == "app":
                popen = stack.enter_context(patch.object(cli.subprocess, "Popen"))
            rc = cli.main(list(argv))
        return rc, captured, popen

    def test_fork_subcommand_with_target(self):
        rc, kwargs, _ = self.run_cli(["fork", "target"])
        self.assertEqual(rc, 0)
        self.assertEqual(kwargs["adopt_mode"], "fork")
        self.assertEqual(kwargs["driver"].session_id, "target")

    def test_resume_subcommand_defaults_to_kill_takeover(self):
        rc, kwargs, _ = self.run_cli(["resume", "target"])
        self.assertEqual(rc, 0)
        self.assertEqual(kwargs["adopt_mode"], "resume")

    def test_bare_target_implies_fork_quick(self):
        rc, kwargs, _ = self.run_cli(["target"])
        self.assertEqual(rc, 0)
        self.assertEqual(kwargs["adopt_mode"], "fork")
        self.assertEqual(kwargs["driver"].session_id, "target")

    def test_empty_argv_defaults_to_fork_last_quick(self):
        rc, kwargs, _ = self.run_cli([])
        self.assertEqual(rc, 0)
        self.assertEqual(kwargs["adopt_mode"], "fork")
        self.assertEqual(kwargs["driver"].session_id, "target")

    def test_goal_subcommand_selects_goal_engine(self):
        rc, kwargs, _ = self.run_cli(["goal", "target"], engine="goal")
        self.assertEqual(rc, 0)
        self.assertEqual(kwargs.get("sid"), "target")

    def test_app_subcommand_launches_detached_mascot(self):
        rc, _, popen = self.run_cli(["app", "--skin", "gold"])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(popen)
        command = popen.call_args.args[0]
        self.assertEqual(command[1:], ["-m", "doloris_app.main", "--skin", "gold"])
        self.assertTrue(popen.call_args.kwargs.get("creationflags"), "桌宠必须以分离进程启动")

    def test_adopt_flag_conflicts_with_positional_target(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["--adopt", "other", "target"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
