"""reveal_codex_session_in_ui 全局按键安全回归测试。

历史缺陷: SetForegroundWindow 可能被 Windows 前台锁拒绝, 但 Ctrl+R 是
keybd_event 全局键盘事件——拒绝时按键落进用户正在使用的任意窗口
(刷新浏览器页面/编辑器)。修复后必须核实目标窗口确实拿到前台才发送。
"""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from afk_supervisor.platform import gui


def make_user32(foreground_hwnd: int) -> MagicMock:
    user32 = MagicMock()
    user32.OpenDesktopW.return_value = 0
    user32.IsIconic.return_value = 0
    user32.ShowWindow.return_value = 1
    user32.SetForegroundWindow.return_value = 1
    user32.GetForegroundWindow.return_value = foreground_hwnd
    return user32


class RevealGlobalKeySafetyTests(unittest.TestCase):
    def run_reveal(self, user32):
        fake_ctypes = SimpleNamespace(windll=SimpleNamespace(user32=user32))
        with patch("sys.platform", "win32"), \
                patch.dict(sys.modules, {"ctypes": fake_ctypes}), \
                patch.object(gui.os, "startfile") as startfile, \
                patch.object(gui.time, "sleep"):
            ok = gui.reveal_codex_session_in_ui("sid-abc-123", target_hwnd=424242)
        return ok, startfile

    def test_keys_sent_only_when_target_is_foreground(self):
        user32 = make_user32(424242)
        ok, startfile = self.run_reveal(user32)
        self.assertTrue(ok)
        self.assertEqual(user32.keybd_event.call_count, 4, "前台锁定成时应发送 Ctrl+R (按下/抬起各2)")
        startfile.assert_called_once()

    def test_global_keys_never_leak_when_foreground_locked_out(self):
        """前台锁被拒: 绝不允许 Ctrl+R 落进用户任意窗口, 仅走深链导航。"""
        user32 = make_user32(99999)  # 前台是别的窗口
        ok, startfile = self.run_reveal(user32)
        self.assertTrue(ok)
        user32.keybd_event.assert_not_called()
        startfile.assert_called_once()

    def test_no_target_window_sends_no_keys(self):
        user32 = make_user32(0)
        fake_ctypes = SimpleNamespace(windll=SimpleNamespace(user32=user32))
        with patch("sys.platform", "win32"), \
                patch.dict(sys.modules, {"ctypes": fake_ctypes}), \
                patch.object(gui.os, "startfile") as startfile, \
                patch.object(gui.time, "sleep"):
            ok = gui.reveal_codex_session_in_ui("sid-abc-123", target_hwnd=0)
        self.assertTrue(ok)
        user32.keybd_event.assert_not_called()
        startfile.assert_called_once()

    def test_non_windows_platform_never_touches_input(self):
        with patch("sys.platform", "linux"), \
                patch.object(gui.os, "startfile") as startfile:
            ok = gui.reveal_codex_session_in_ui("sid-abc-123")
        self.assertFalse(ok)
        startfile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
