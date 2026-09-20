"""
afk_supervisor.platform.process — 进程控制、工作区锁与生命周期管理
===================================================================
提供原子工作区监督锁 (WorkspaceSupervisorLock)；
跨平台/Windows 精准检测 PID 存活性；
严格区分 Electron 桌面客户端与无头 CLI worker；
支持带边界感知的优雅与两段式强杀。
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from afk_supervisor.compat import get_sym


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def munged_cwd(cwd: Path) -> str:
    """编码目录路径用于文件名与锁文件 (如 C:\Agents\double -> C--Agents-double)"""
    return str(cwd).replace(":", "").replace("\\", "-").replace("_", "-")


def pid_is_running(pid: int) -> bool:
    """检查指定 PID 的进程是否真实存活。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            try:
                if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return False
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


class WorkspaceSupervisorLock:
    """工程工作区级监管互斥锁，防止多个监管器同时在同一目录接管或派生导致文件写入冲突。"""

    def __init__(self, workspace_path: Path, sid: str, mode: str, lock_dir: Optional[Path] = None):
        self.ws = Path(workspace_path).resolve()
        self.sid = sid
        self.mode = mode
        home = Path.home()
        self.lock_dir = Path(lock_dir) if lock_dir else (home / ".codex" / "afk-workspace-locks")
        self.lock_file = self.lock_dir / f"{munged_cwd(self.ws)}.lock"
        self.acquired = False

    def acquire(self) -> Tuple[bool, str]:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "pid": os.getpid(),
            "workspace": str(self.ws),
            "sid": self.sid,
            "mode": self.mode,
            "acquired_at": datetime.now().isoformat(),
        }
        data_bytes = json.dumps(data, indent=2).encode("utf-8")

        for attempt in range(2):
            try:
                fd = os.open(str(self.lock_file), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                try:
                    os.write(fd, data_bytes)
                finally:
                    os.close(fd)
                self.acquired = True
                return True, ""
            except FileExistsError:
                # 锁文件已存在，检查持有进程存活性 (清理废弃残留锁)
                try:
                    info = json.loads(self.lock_file.read_text(encoding="utf-8", errors="replace"))
                    pid = info.get("pid")
                    if pid and pid_is_running(pid):
                        return False, (
                            f"工作区 {self.ws} 当前正被另一监管器实例占用 "
                            f"(PID {pid}, 模式 {info.get('mode')}, 会话 {str(info.get('sid', ''))[:8]})"
                        )
                    else:
                        log(f"LOCK     检测到工作区残留废弃锁 (PID {pid} 已退出)，自动清理并接管")
                        try:
                            self.lock_file.unlink()
                        except OSError:
                            pass
                        continue
                except Exception:
                    try:
                        self.lock_file.unlink()
                    except OSError:
                        pass
            except Exception as e:
                return False, f"获取工作区锁异常: {e}"

        return False, f"工作区 {self.ws} 加锁冲突，无法原子抢占锁文件"

    def release(self):
        if self.acquired and self.lock_file.exists():
            try:
                info = json.loads(self.lock_file.read_text(encoding="utf-8", errors="replace"))
                if info.get("pid") == os.getpid():
                    self.lock_file.unlink()
            except Exception:
                pass
            self.acquired = False


def get_codex_desktop_pids() -> List[int]:
    """Discover Codex UI and its backend using CIM JSON and parent identity.

    -c is a shared config option, not a CLI-worker marker. Standalone servers
    and the unrelated ChatGPT app are excluded. Query failures are not absence.
    """
    if os.name != "nt":
        return []
    script = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "@(Get-CimInstance Win32_Process -Filter \"Name='codex.exe' OR Name='ChatGPT.exe'\" | "
        "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine) | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise RuntimeError("CIM 进程查询失败")
        raw = result.stdout.decode("utf-8-sig") if isinstance(result.stdout, bytes) else result.stdout
        rows = json.loads(raw) if raw and raw.strip() else []
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise ValueError("进程查询结果不是列表")
        ui, servers = set(), []
        for row in rows:
            pid = int(row["ProcessId"])
            name = (row.get("Name") or "").lower()
            path = (row.get("ExecutablePath") or "").replace("/", "\\").lower()
            cmd = row.get("CommandLine") or ""
            if not path or not cmd:
                raise RuntimeError(f"无法读取候选进程 {pid} 的路径/命令行")
            tokens = [token.strip('"').lower() for token in re.findall(r'"[^"\n]*"|[^\s]+', cmd)]
            args = tokens[1:]
            if any(token in ("exec", "fork", "resume") for token in args):
                continue
            if name == "codex.exe" and "app-server" in args:
                servers.append((pid, int(row.get("ParentProcessId") or 0)))
            elif name in ("chatgpt.exe", "codex.exe") and (
                "\\openai.codex" in path or "\\openai\\codex\\" in path
            ) and (name == "chatgpt.exe" or "\\app\\" in path):
                ui.add(pid)
        return sorted(ui | {pid for pid, parent in servers if parent in ui})
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"无法可靠识别 Codex 桌面进程: {exc}") from exc


def codex_app_running() -> bool:
    """检测 Codex 桌面App是否存活。自动排除无头 CLI worker。"""
    _pids_fn = get_sym("get_codex_desktop_pids", get_codex_desktop_pids)
    pids = _pids_fn()
    return len(pids) > 0


def close_codex_app(rollout_path=None, max_wait=40, wait_boundary=True, on_event=None) -> List[str]:
    """Automatically close verified desktop PIDs; never resume on uncertainty."""
    from afk_supervisor.sessions.rollout import codex_handoff_state
    pids_fn = get_sym("get_codex_desktop_pids", get_codex_desktop_pids)
    target_pids = pids_fn()
    if not target_pids:
        log("CLOSE    未发现 Codex 桌面进程；继续检查写锁，不以文件静默推断退出")
        return []
    if wait_boundary:
        deadline = time.monotonic() + max_wait
        previous = None
        while True:
            snapshot = codex_handoff_state(rollout_path)
            signature = (snapshot["state"], snapshot["reason"], snapshot["last_event"])
            if signature != previous:
                log(f"HANDOFF  {snapshot}")
                if on_event:
                    on_event("HANDOFF_BOUNDARY", **snapshot)
                previous = signature
            if snapshot["state"] == "safe":
                if not snapshot["turn_ended"]:
                    # Freeze model-side generation before closing so it cannot
                    # dispatch a new tool between the boundary sample and kill.
                    from afk_supervisor.platform.gui import pause_codex_gui_session
                    pause_fn = get_sym("pause_codex_gui_session", pause_codex_gui_session)
                    remaining = max(0, deadline - time.monotonic())
                    if on_event:
                        on_event("HANDOFF_PAUSE", reason="模型侧边界可中断，先暂停以防新工具进入")
                    if not pause_fn(rollout_path, max_wait=remaining):
                        raise RuntimeError("自动暂停原任务失败，未关闭 App、未启动无头端")
                    confirmed = codex_handoff_state(rollout_path)
                    if confirmed["state"] != "safe" or not confirmed["turn_ended"]:
                        raise RuntimeError(f"暂停后缺少安全终止事件: {confirmed}")
                    if on_event:
                        on_event("HANDOFF_BOUNDARY", **confirmed)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"安全退出检查超时，未关闭 App、未启动无头端: {snapshot['reason']}")
            time.sleep(min(1, remaining))

    pid_fn = get_sym("pid_is_running", pid_is_running)
    log(f"CLOSE    自动关闭已识别的桌面进程: {target_pids}")
    for pid in target_pids:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, timeout=10)
    deadline = time.monotonic() + 8
    while any(pid_fn(pid) for pid in target_pids) and time.monotonic() < deadline:
        time.sleep(0.2)
    for pid in target_pids:
        if pid_fn(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
    deadline = time.monotonic() + 3
    while any(pid_fn(pid) for pid in target_pids) and time.monotonic() < deadline:
        time.sleep(0.2)
    survivors = sorted(set(pid for pid in target_pids if pid_fn(pid)) | set(pids_fn()))
    if survivors:
        raise RuntimeError(f"桌面进程未完全退出或已重启，禁止 resume: {survivors}")
    return [str(pid) for pid in target_pids]


def verify_codex_writer_released(lock_path: Path) -> None:
    """Probe the OS lock without deleting/replacing the writer-lock inode.

    Existence alone says nothing: current Codex lock files are empty and may
    remain after exit. The real resume still acquires Codex's own writer lock.
    """
    try:
        stream = lock_path.open("r+b")
    except FileNotFoundError:
        return
    with stream:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise RuntimeError(f"写锁仍被占用或无法验证，禁止 resume: {lock_path}: {exc}") from exc


def safe_kill(driver, max_wait=30) -> str:
    """无害击杀: 等到事件不在工具执行中 → CTRL_BREAK优雅中断 → 硬杀兜底。"""
    from afk_supervisor.sessions.rollout import rollout_tail_state
    rp = getattr(driver, "jsonl", None)
    deadline = time.time() + max_wait
    while rp is not None and time.time() < deadline:
        if rollout_tail_state(rp) == "safe":
            driver.interrupt()
            if driver.wait_exit(12):
                driver.kill_tree()
                return "boundary"
            driver.kill_tree()
            return "boundary+hard"
        time.sleep(1)
    if driver.interrupt() and driver.wait_exit(15):
        driver.kill_tree()
        return "graceful"
    driver.kill_tree()
    return "hard"
