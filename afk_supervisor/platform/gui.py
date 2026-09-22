"""
afk_supervisor.platform.gui — Windows 原生 GUI 交互与窗口守护
=============================================================
基于 Win32 API 与 PowerShell UI Automation (gui_inject.ps1)；
实现毫秒级原生检测 Codex / ChatGPT 桌面主窗口；
提供无损直写注入、目标会话聚焦激活、自动防最小化守护与 Fork 前安全暂停。
"""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

from afk_supervisor.platform.process import log
from afk_supervisor.sessions.rollout import is_codex_working, codex_handoff_state


def get_workspace_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def find_best_codex_window() -> int:
    """毫秒级原生检测 Codex / ChatGPT 桌面主窗口 (纯 ctypes, 严格根据 chatgpt.exe/codex.exe 过滤)。"""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        h_desk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        best_hwnd = 0
        max_score = -1

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                        ("showCmd", ctypes.c_uint), ("ptMinPosition", POINT),
                        ("ptMaxPosition", POINT), ("rcNormalPosition", RECT)]

        def get_process_name(pid: int) -> str:
            h = kernel32.OpenProcess(0x1000, False, pid)
            if not h:
                return ""
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            kernel32.CloseHandle(h)
            return buf.value.split("\\")[-1].lower()

        def enum_cb(hwnd, lparam):
            nonlocal best_hwnd, max_score
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pname = get_process_name(pid.value)
            if pname not in ("chatgpt.exe", "codex.exe"):
                return True

            buf_cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf_cls, 256)
            cls = buf_cls.value
            if "Chrome_WidgetWin" not in cls:
                return True

            buf_title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf_title, 512)
            t = buf_title.value.lower()

            style = user32.GetWindowLongW(hwnd, -16)
            wp = WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(WINDOWPLACEMENT)
            user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
            norm_w = wp.rcNormalPosition.right - wp.rcNormalPosition.left
            norm_h = wp.rcNormalPosition.bottom - wp.rcNormalPosition.top

            score = 10
            if "chatgpt" in t or "codex" in t:
                score += 100
            if (style & 0x00010000) != 0:
                score += 100
            if (style & 0x00040000) != 0:
                score += 50
            if norm_w >= 600 and norm_h >= 400:
                score += 60
            if user32.IsWindowVisible(hwnd):
                score += 30

            if score > max_score:
                max_score = score
                best_hwnd = hwnd
            return True

        user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
        return best_hwnd
    except Exception:
        return 0


def reveal_codex_session_in_ui(session_id: str, target_hwnd: int = 0) -> bool:
    """使派生出来的 Fork 会话在运行中的 Codex 桌面端即时显现并打开。"""
    if sys.platform != "win32" or not session_id:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        h_desk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_desk:
            user32.SetThreadDesktop(h_desk)

        if not target_hwnd:
            target_hwnd = find_best_codex_window()

        if target_hwnd:
            if user32.IsIconic(target_hwnd):
                user32.ShowWindow(target_hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(target_hwnd)
            time.sleep(0.1)

            VK_CONTROL = 0x11
            VK_R = 0x52
            KEYEVENTF_KEYUP = 0x0002

            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            user32.keybd_event(VK_R, 0, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(VK_R, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
            log(f"UI_SYNC  已向桌面端 (HWND {target_hwnd}) 发送 Ctrl+R 刷新侧边栏")

        try:
            os.startfile(f"codex://threads/{session_id}")
            log(f"UI_SYNC  已通过深链在桌面端打开新派生会话: codex://threads/{session_id[:8]}...")
        except Exception:
            pass
        return True
    except Exception as e:
        log(f"UI_SYNC WARN 即时显现会话异常: {e}")
        return False


def _navigate_target(target_sid):
    from afk_supervisor.sessions.discovery import clean_session_id
    clean_sid = clean_session_id(target_sid) if target_sid else ""
    if not clean_sid:
        return False
    try:
        os.startfile(f"codex://threads/{clean_sid}")
        time.sleep(0.5)
        return True
    except Exception as error:
        log(f"GUI TARGET 无法导航至目标任务: {error}")
        return False


def inject_into_codex_gui(
    text: str, target_hwnd: int = 0, target_sid: str = "", target_title: str = "",
    rollout_path: Optional[Path] = None,
):
    """Navigate and require selected-task identity before send; ACK is event-based."""
    from afk_supervisor.models import DeliveryResult
    from afk_supervisor.sessions.discovery import clean_session_id, load_codex_thread_titles, read_session_title
    import tempfile
    ws = get_workspace_root()
    ps_script = ws / "gui_inject.ps1"
    if not ps_script.exists():
        return DeliveryResult("NOT_SENT", f"GUI注入脚本未找到: {ps_script}")
    target_sid = clean_session_id(target_sid) if target_sid else ""
    if not target_title:
        if rollout_path and Path(rollout_path).exists():
            target_title = read_session_title(Path(rollout_path))
        if not target_title and target_sid:
            target_title = load_codex_thread_titles().get(target_sid, "")
    if target_sid:
        _navigate_target(target_sid)
        time.sleep(0.8)
    target_hwnd = target_hwnd or find_best_codex_window()
    if not target_hwnd:
        return DeliveryResult("NOT_SENT", "未找到桌面主窗口，未发送")
    tmp_path = Path(tempfile.gettempdir()) / f"afk_payload_{uuid.uuid4().hex}.txt"
    try:
        tmp_path.write_text(text, encoding="utf-8")
        command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_script),
                   "-PayloadFile", str(tmp_path), "-TargetHwnd", str(target_hwnd),
                   "-TargetSid", target_sid, "-TargetTitle", target_title, "-TimeoutMs", "10000"]
        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=18, cwd=str(ws), creationflags=no_win)
        for line in (result.stdout or "").splitlines():
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                status = data.get("delivery_status")
                if status not in {"NOT_SENT", "SENT", "UNCERTAIN"}:
                    status = "SENT" if data.get("ok") else "UNCERTAIN"
                err_detail = data.get("error") or data.get("method", "gui")
                # 若未送达且提示身份未对齐，尝试深链导航至该任务窗口后单次重试
                if status == "NOT_SENT" and "could not be verified" in str(err_detail).lower() and target_sid:
                    log(f"GUI NAV  尝试深链聚焦目标会话 codex://threads/{target_sid[:8]}...")
                    _navigate_target(target_sid)
                    time.sleep(1.0)
                    new_hwnd = find_best_codex_window() or target_hwnd
                    command[7] = str(new_hwnd)
                    retry_res = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=18, cwd=str(ws), creationflags=no_win)
                    for r_line in (retry_res.stdout or "").splitlines():
                        try:
                            r_data = json.loads(r_line)
                            if isinstance(r_data, dict):
                                r_status = r_data.get("delivery_status")
                                if r_status not in {"NOT_SENT", "SENT", "UNCERTAIN"}:
                                    r_status = "SENT" if r_data.get("ok") else "UNCERTAIN"
                                return DeliveryResult(r_status, r_data.get("error") or r_data.get("method", "gui"))
                        except ValueError:
                            continue
                return DeliveryResult(status, err_detail)
        return DeliveryResult("UNCERTAIN", "注入脚本未返回有效送达回执；观察目标事件，禁止盲目重发")
    except FileNotFoundError as error:
        return DeliveryResult("NOT_SENT", str(error))
    except Exception as error:
        return DeliveryResult("UNCERTAIN", f"注入结果不确定: {error}")
    finally:
        tmp_path.unlink(missing_ok=True)


def pause_codex_gui_session(rollout_path=None, max_wait: Optional[float] = 30.0, *, target_sid="", target_title="") -> bool:
    """Pause the exact parent and confirm its terminal event before handoff.

    AFK2 passes max_wait=None: elapsed time never permits a fork or ends the
    wait. Kill mode retains its explicit finite budget. A GUI receipt, quiet
    file, missing trajectory or malformed event is not proof of a stopped turn.
    """
    if not rollout_path:
        return False
    from afk_supervisor.sessions.discovery import clean_session_id, load_codex_thread_titles, read_session_title
    p_roll = Path(rollout_path)
    snapshot = codex_handoff_state(p_roll)
    if snapshot["state"] == "safe" and snapshot["turn_ended"]:
        log(f"PAUSE 原任务已停止，无需额外打断 ({snapshot['last_event']})")
        return True
    started = time.monotonic()
    deadline = None if max_wait is None else started + max(0, max_wait)
    if deadline is not None and time.monotonic() >= deadline:
        return False
    if not target_sid:
        from afk_supervisor.sessions.discovery import _read_meta
        meta = _read_meta(p_roll)
        target_sid = meta[0] if meta else ""
    target_sid = clean_session_id(target_sid) if target_sid else ""
    if not target_title:
        if p_roll.exists():
            target_title = read_session_title(p_roll)
        if not target_title and target_sid:
            target_title = load_codex_thread_titles().get(target_sid, "")
    if target_sid:
        _navigate_target(target_sid)
    hwnd = find_best_codex_window()
    ps_script = get_workspace_root() / "gui_inject.ps1"
    if hwnd and ps_script.is_file():
        command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_script),
                   "-PauseOnly", "-TargetHwnd", str(hwnd), "-TargetSid", target_sid,
                   "-TargetTitle", target_title, "-TimeoutMs", "6000"]
        try:
            no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            response = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=10, cwd=str(get_workspace_root()), creationflags=no_win)
            log(f"PAUSE 目标 {target_sid[:8]} 暂停回执: {response.stdout.strip()[:160]}")
        except Exception as error:
            log(f"PAUSE 暂停回执不确定，继续等待轨迹确认: {error}")
    else:
        log("PAUSE 暂无法发送 GUI 暂停请求，继续观察原任务；未确认停止绝不 Fork")
    previous = None
    last_log = started
    while True:
        snapshot = codex_handoff_state(p_roll)
        if snapshot["state"] == "safe" and snapshot["turn_ended"]:
            log(f"PAUSE 原任务已确认停止 ({snapshot['last_event']})，等待 {time.monotonic() - started:.1f}s")
            return True
        now = time.monotonic()
        signature = (snapshot["state"], snapshot["last_event"], snapshot["reason"])
        if signature != previous or now - last_log >= 30:
            limit = "无限期" if deadline is None else f"最多 {max_wait:g}s"
            log(f"PAUSE_WAIT {limit}等待原任务停止，已等 {now - started:.1f}s；"
                f"state={snapshot['state']} event={snapshot['last_event']} "
                f"pending={snapshot['pending_calls']} reason={snapshot['reason']}")
            previous, last_log = signature, now
        if deadline is not None and now >= deadline:
            log("PAUSE 未确认原任务停止，禁止交接以免父子并发")
            return False
        time.sleep(1 if deadline is None else min(1, deadline - now))


def ensure_codex_window_restored() -> int:
    """双有头守护：若用户无意将 Codex 最小化到任务栏或隐藏到托盘，自动恢复为常规桌面显示。"""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        user32 = ctypes.windll.user32
        best_hwnd = find_best_codex_window()
        if best_hwnd:
            if user32.IsIconic(best_hwnd) or not user32.IsWindowVisible(best_hwnd):
                user32.ShowWindow(best_hwnd, 9)  # SW_RESTORE
                user32.ShowWindow(best_hwnd, 5)  # SW_SHOW
                log(f"GUI GUARD 检测到 Codex 窗口(HWND {best_hwnd})被最小化或隐藏，已自动恢复为常规桌面窗口")
                return best_hwnd
    except Exception:
        pass
    return 0


# 兼容导出
from afk_supervisor.drivers.dummy import DummyDriver
