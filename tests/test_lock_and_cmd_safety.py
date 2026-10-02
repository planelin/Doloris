"""Batch 1-3 回归: 工作区锁 TOCTOU 防护 + cmd.exe 元字符快速失败。

历史缺陷 1: acquire() 在 inspect 判定 stale 后盲删锁文件 —— 两个监管者同时
启动时会互相删掉对方的新锁, 双双"持有"工作区锁 (恰是锁要防的父子双写)。
历史缺陷 2: codex/claude/l2 参数经 cmd.exe /c 透传, 含 & % 等元字符的合法
路径会被 cmd 二次解析成命令分隔符/变量展开 (命令注入)。
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from afk_supervisor.platform.process import (
    WorkspaceSupervisorLock,
    ensure_cmd_arg_safe,
)


def make_lock(root: Path, ws: Path, name: str = "a") -> WorkspaceSupervisorLock:
    return WorkspaceSupervisorLock(ws, sid=name, mode="test")


class LockTakeoverSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="afk-lock-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        from afk_supervisor.platform import process as proc
        self.proc = proc

    def test_stale_takeover_does_not_delete_a_replaced_lock(self):
        """残留锁在 inspect 与 unlink 之间被另一实例替换时, 绝不能删除新锁。"""
        lock_a = make_lock(self.root, self.ws, "a")
        stale = {
            "pid": 999999, "workspace": str(self.ws), "sid": "ghost",
            "mode": "resume", "acquired_at": "2026-01-01T00:00:00",
        }
        lock_a.lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_a.lock_file.write_text(json.dumps(stale), encoding="utf-8")

        real_inspect = WorkspaceSupervisorLock._inspect_existing
        state = {"swapped": False}

        def inspect_with_swap(self):
            result = real_inspect(self)
            if result[0] == "stale" and not state["swapped"]:
                # 模拟竞态: 正当本实例判定 stale 后, 另一监管者写入了新锁
                state["swapped"] = True
                self.lock_file.write_text(json.dumps({
                    "pid": 1, "workspace": str(self.ws), "sid": "fresh",
                    "mode": "fork", "acquired_at": "2026-10-02T16:00:00",
                }), encoding="utf-8")
            return result

        with patch.object(WorkspaceSupervisorLock, "_inspect_existing", inspect_with_swap), \
                patch("supervise.pid_is_running", side_effect=lambda pid: pid == 1):
            ok, msg = lock_a.acquire()
        self.assertFalse(ok, "另一实例已持锁, 本实例绝不能接管")
        self.assertTrue(lock_a.lock_file.exists(), "新持有者的锁绝不能被删除")
        content = json.loads(lock_a.lock_file.read_text(encoding="utf-8"))
        self.assertEqual(content["sid"], "fresh", "被删的必须是判过 stale 的那份内容")

    def test_genuine_stale_lock_is_still_taken_over(self):
        """正常残留锁 (死 PID 且内容未被替换) 仍应被自动清理接管。"""
        lock = make_lock(self.root, self.ws, "me")
        lock.lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock.lock_file.write_text(json.dumps({
            "pid": 999999, "workspace": str(self.ws), "sid": "ghost",
            "mode": "resume", "acquired_at": "2026-01-01T00:00:00",
        }), encoding="utf-8")
        with patch("supervise.pid_is_running", return_value=False):
            ok, msg = lock.acquire()
        self.assertTrue(ok)
        content = json.loads(lock.lock_file.read_text(encoding="utf-8"))
        self.assertEqual(content["sid"], "me")


class CmdArgSafetyTests(unittest.TestCase):
    def test_safe_args_pass(self):
        ensure_cmd_arg_safe(["codex", "-C", "C:\\dev\\proj (x86)\\", "--session-id", "abc-123"], context="t")

    def test_command_separator_chars_are_rejected(self):
        for bad in ("C:\\dev\\foo & bar", "a|b", "a<b", "a>b", "a^b"):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeError) as ctx:
                    ensure_cmd_arg_safe(["-C", bad], context="t")
                self.assertIn("cmd.exe", str(ctx.exception))

    def test_expansion_chars_are_rejected_even_inside_quotes(self):
        for bad in ("%TEMP%\\x", "a!b"):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeError):
                    ensure_cmd_arg_safe([bad], context="t")

    def test_codex_driver_refuses_metachar_workspace_before_popen(self):
        """含 & 的工作区路径必须在 Popen 之前快速失败, 而不是注入 cmd。"""
        from afk_supervisor.drivers.codex import CodexDriver
        with TemporaryDirectory(prefix="afk-cmdsafe-") as td:
            root = Path(td)
            evil_ws = root / "foo & bar"
            evil_ws.mkdir()
            driver = CodexDriver(evil_ws, root / "run")
            prompt = root / "prompt.txt"
            prompt.write_text("continue", encoding="utf-8")
            with patch("afk_supervisor.drivers.codex.subprocess.Popen",
                       side_effect=AssertionError("Popen must never be reached")):
                with self.assertRaisesRegex(RuntimeError, "cmd.exe"):
                    driver.launch(prompt)


if __name__ == "__main__":
    unittest.main()
