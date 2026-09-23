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
from typing import Optional

from afk_supervisor.platform.process import log
from afk_supervisor.sessions.rollout import codex_handoff_state

# 同一会话的标题候选分隔符 (Unit Separator)，与 gui_inject.ps1 中 SplitTitles 一致。
GUI_TITLE_SEP = "\x1f"

# 身份探针 (gui_inject.ps1 -ProbeOnly) 的稳定机读码 -> 处置建议。
# 这些理由都是环境性失败 (权限或可访问性树不可用)：无论重试多少次都不会成功，
# 因此必须立刻给出可读结论，而不是把有界重试预算耗在盲重试上。
GUI_PROBE_HARD_BLOCKERS = {
    "UIA_ACCESS_DENIED": "目标桌面端以更高权限运行 (Win32 错误 5=拒绝访问)；请以相同或更高权限启动本守护进程，或让桌面端以普通权限重启",
    "UIA_TREE_UNAVAILABLE": "桌面端渲染进程未暴露 UI Automation 可访问性树；请确认桌面端窗口已正常渲染后重试",
    "PROBE_ERROR": "身份探针内部错误",
    "PROBE_TIMEOUT": "身份探针超时",
    "SCRIPT_MISSING": "GUI 注入脚本未找到",
}


def get_workspace_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def find_gui_inject_script() -> Optional[Path]:
    """定位 gui_inject.ps1: 优先仓库/工作区根 (源码态与显式覆盖)，其次包内 (安装态)。"""
    candidates = (
        get_workspace_root() / "gui_inject.ps1",
        Path(__file__).resolve().parent / "gui_inject.ps1",
    )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


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


def resolve_target_titles(target_sid: str, target_title: str = "", rollout_path=None) -> str:
    """把同一 SID 下所有可用标题候选拼成一个串交给 UI 身份校验。

    标题只是同一机读 SID 下的辅助判据：桌面端侧栏显示名 (state_5.sqlite.name)、
    同表短标题 (title)、session_index.jsonl 旧索引名与 rollout 首条用户输入经常
    互不相同 (例如 fork 线程侧栏名带 "[Fork] " 前缀)。任何单一来源都会让分步
    身份校验退化成必然失败，因此这里做并集后逐个精确比较。
    """
    from afk_supervisor.sessions.discovery import (
        clean_session_id,
        load_codex_thread_title_candidates,
        read_session_title,
    )

    candidates = []

    def add(value) -> None:
        text = (value or "").strip()
        if text and text not in candidates:
            candidates.append(text)

    add(target_title)
    sid = clean_session_id(target_sid) if target_sid else ""
    if sid:
        for item in load_codex_thread_title_candidates(sid):
            add(item)
    if rollout_path and Path(rollout_path).exists():
        add(read_session_title(Path(rollout_path)))
    return GUI_TITLE_SEP.join(candidates)


def parse_codex_gui_probe(stdout: str) -> dict:
    """从探针 stdout 中取出最后一条 JSON 回执 (优先带 reason 的那条)。"""
    found = {}
    for line in (stdout or "").splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            found = data
            if data.get("reason"):
                break
    return found


def decode_powershell_output(raw) -> str:
    """解码 PowerShell 回执字节流，避免中文结论变成乱码。

    gui_inject.ps1 已尽量把控制台输出统一成 UTF-8，但被重定向 stdout 的
    Windows PowerShell 5.1 仍可能按系统 OEM 代码页 (本机 CP936) 写字节。
    这里两种编码都试一遍，取乱码更少的那份：机读字段都是 ASCII，人工结论
    才需要中文，所以宁可退化成可读的旧编码，也不要把乱码塞进心跳与终局。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        utf8_text = raw.decode("utf-8", errors="replace")
    except (AttributeError, ValueError):
        return str(raw)
    if "\ufffd" not in utf8_text:
        return utf8_text
    try:
        legacy_text = raw.decode("cp936", errors="replace")
    except (AttributeError, LookupError, ValueError):
        return utf8_text
    return legacy_text if legacy_text.count("\ufffd") < utf8_text.count("\ufffd") else utf8_text


def _probe_codex_gui_once(target_hwnd: int, target_sid: str, target_title: str, timeout_ms: int) -> dict:
    """调用 gui_inject.ps1 -ProbeOnly: 只读身份自检，不敲键、不点按、不写剪贴板。"""
    ps_script = find_gui_inject_script()
    if ps_script is None:
        return {"ok": False, "reason": "SCRIPT_MISSING", "error": "GUI注入脚本未找到"}
    ws = get_workspace_root()
    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_script),
               "-ProbeOnly", "-TargetHwnd", str(int(target_hwnd or 0)),
               "-TargetSid", target_sid or "", "-TargetTitle", target_title or "",
               "-TimeoutMs", str(int(timeout_ms))]
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        command, capture_output=True,
        timeout=max(8.0, int(timeout_ms) / 1000.0 + 8.0), cwd=str(ws), creationflags=no_win,
    )
    return parse_codex_gui_probe(decode_powershell_output(result.stdout))


def probe_codex_gui(target_hwnd: int = 0, target_sid: str = "", target_title: str = "",
                    timeout_ms: int = 12000) -> dict:
    """运行一次只读身份探针；任何异常都折叠成可判定的 reason，绝不向上抛。"""
    try:
        return _probe_codex_gui_once(target_hwnd, target_sid, target_title, timeout_ms) or {}
    except Exception as error:
        return {"ok": False, "reason": "PROBE_ERROR", "error": f"{type(error).__name__}: {error}"}


def gui_probe_blocker(probe: dict) -> str:
    """把探针结果翻译成阻断结论；无法识别的结果一律放行，交由注入脚本本身把关。"""
    if not isinstance(probe, dict):
        return ""
    reason = str(probe.get("reason") or "").strip().upper()
    hint = GUI_PROBE_HARD_BLOCKERS.get(reason)
    if hint is None:
        return ""
    detail = str(probe.get("error") or "").strip()
    if hint not in detail:
        detail = f"{detail} {hint}".strip()
    return f"GUI 身份探针判定不可送达 [{reason}]: {detail or reason}"


def inject_into_codex_gui(
    text: str, target_hwnd: int = 0, target_sid: str = "", target_title: str = "",
    rollout_path: Optional[Path] = None,
):
    """Navigate and require selected-task identity before send; ACK is event-based."""
    from afk_supervisor.models import DeliveryResult
    from afk_supervisor.sessions.discovery import clean_session_id, load_codex_thread_titles, read_session_title
    import tempfile
    ws = get_workspace_root()
    ps_script = find_gui_inject_script()
    if ps_script is None:
        return DeliveryResult("NOT_SENT", f"GUI注入脚本未找到: {ws / 'gui_inject.ps1'}")
    target_sid = clean_session_id(target_sid) if target_sid else ""
    if not target_title:
        if rollout_path and Path(rollout_path).exists():
            target_title = read_session_title(Path(rollout_path))
        if not target_title and target_sid:
            target_title = load_codex_thread_titles().get(target_sid, "")
    if target_sid:
        target_title = resolve_target_titles(target_sid, target_title, rollout_path)
    if target_sid:
        _navigate_target(target_sid)
        time.sleep(0.8)
    target_hwnd = target_hwnd or find_best_codex_window()
    if not target_hwnd:
        return DeliveryResult("NOT_SENT", "未找到桌面主窗口，未发送")
    # 注入前先做只读身份探针：权限/可访问性树这类环境性失败在这里就被判死，
    # 不再浪费有界重试预算；探针无法识别时放行，由注入脚本自身继续把关。
    probe_block = gui_probe_blocker(probe_codex_gui(
        target_hwnd=target_hwnd, target_sid=target_sid, target_title=target_title))
    if probe_block:
        log(f"GUI PROBE {probe_block}")
        return DeliveryResult("NOT_SENT", probe_block)
    tmp_path = Path(tempfile.gettempdir()) / f"afk_payload_{uuid.uuid4().hex}.txt"
    try:
        tmp_path.write_text(text, encoding="utf-8")
        command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_script),
                   "-PayloadFile", str(tmp_path), "-TargetHwnd", str(target_hwnd),
                   "-TargetSid", target_sid, "-TargetTitle", target_title, "-TimeoutMs", "10000"]
        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(command, capture_output=True, timeout=18, cwd=str(ws), creationflags=no_win)
        for line in decode_powershell_output(result.stdout).splitlines():
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
                    command[9] = str(new_hwnd)
                    retry_res = subprocess.run(command, capture_output=True, timeout=18, cwd=str(ws), creationflags=no_win)
                    for r_line in decode_powershell_output(retry_res.stdout).splitlines():
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
        target_title = resolve_target_titles(target_sid, target_title, p_roll)
    if target_sid:
        _navigate_target(target_sid)
    hwnd = find_best_codex_window()
    ps_script = find_gui_inject_script()
    if hwnd and ps_script is not None:
        command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_script),
                   "-PauseOnly", "-TargetHwnd", str(hwnd), "-TargetSid", target_sid,
                   "-TargetTitle", target_title, "-TimeoutMs", "6000"]
        try:
            no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            response = subprocess.run(command, capture_output=True, timeout=10, cwd=str(get_workspace_root()), creationflags=no_win)
            log(f"PAUSE 目标 {target_sid[:8]} 暂停回执: {decode_powershell_output(response.stdout).strip()[:160]}")
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
from afk_supervisor.drivers.dummy import DummyDriver  # noqa: E402,F401
