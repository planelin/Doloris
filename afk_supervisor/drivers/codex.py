"""
afk_supervisor.drivers.codex — Codex CLI 驱动器 (exec / resume / fork)
======================================================================
管理无头 Codex 进程生命周期；出站走 cc-switch 本地代理；
解析 rollout session_meta 与 stdout 事件绑定 session_id。
"""

import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from afk_supervisor.platform.process import log
from afk_supervisor.sessions.discovery import register_thread_for_codex_ui, get_codex_sessions_dir


class CodexDriver:
    """codex CLI 驱动 (codex exec / codex exec resume / codex exec fork)。"""

    def __init__(self, cwd: Path, run_dir: Path):
        self.cwd = Path(cwd).resolve()
        self.run_dir = Path(run_dir).resolve()
        self.session_id: Optional[str] = None
        self.jsonl: Optional[Path] = None
        self.proc: Optional[subprocess.Popen] = None
        self.last_msg = self.run_dir / "codex-last-message.txt"
        self.last_stdout_offset = 0
        self._open_handles: List[object] = []
        self._fork_parent_id: Optional[str] = None
        self.title: str = ""
        self._last_activity_time: float = 0.0
        self._last_rollout_size: int = -1
        self._last_stdout_size: int = -1

    def _close_handles(self):
        for h in getattr(self, "_open_handles", []):
            try:
                h.close()
            except Exception:
                pass
        self._open_handles = []

    @property
    def provider_name(self) -> str:
        return "codex(cc-switch本地代理)"

    def has_next(self) -> bool:
        return False

    def _verify_rollout_ownership(self, p: Path, expected_sid: Optional[str] = None, expected_cwd: Optional[Path] = None) -> bool:
        """严格核验证据文件是否属于当前 session_id 以及当前工作目录 cwd。"""
        if not p or not p.exists():
            return False
        from afk_supervisor.sessions.discovery import _read_meta
        meta = _read_meta(p)
        if not meta or (expected_sid and meta[0] != expected_sid):
            return False
        if expected_cwd and (not meta[1] or Path(meta[1]).resolve() != Path(expected_cwd).resolve()):
            return False
        return True

    def _spawn_once(self, args: List[str], stdin_path: Path):
        self._close_handles()
        stdout_file = self.run_dir / "worker-stdout.log"
        self.last_stdout_offset = stdout_file.stat().st_size if stdout_file.exists() else 0
        self._last_activity_time = 0.0
        self._last_rollout_size = -1
        self._last_stdout_size = -1
        f_in = open(stdin_path, "rb")
        f_out = open(stdout_file, "ab")
        f_err = open(self.run_dir / "worker-stderr.log", "ab")
        self._open_handles = [f_in, f_out, f_err]
        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        cflags = subprocess.CREATE_NEW_PROCESS_GROUP | no_win
        self.proc = subprocess.Popen(
            ["cmd.exe", "/c", "codex", *args],
            cwd=str(self.cwd), stdin=f_in,
            stdout=f_out, stderr=f_err,
            creationflags=cflags,
        )

    def _common(self) -> List[str]:
        return [
            "-C", str(self.cwd), "-s", "workspace-write",
            "-c", "sandbox_workspace_write.network_access=true",
            "--skip-git-repo-check", "--json", "-o", str(self.last_msg)
        ]

    def launch(self, prompt_path: Path):
        self._spawn_once(["exec", *self._common(), "-"], prompt_path)
        log("LAUNCH codex exec (session运行时发现)")
        return self.proc

    def resume(self, prompt_path: Path):
        if not self.session_id:
            raise RuntimeError("resume 前必须先发现 session_id")
        from afk_supervisor.platform.process import verify_codex_writer_released
        from afk_supervisor.sessions.discovery import get_codex_locks_dir
        lock_path = get_codex_locks_dir() / f"{self.session_id}.lock"
        if lock_path.exists():
            for _ in range(15):
                try:
                    verify_codex_writer_released(lock_path)
                    break
                except (OSError, RuntimeError):
                    time.sleep(0.2)
        self._spawn_once([
            "exec", "-C", str(self.cwd), "resume", self.session_id,
            "-c", "sandbox_mode=workspace-write",
            "-c", "approval_policy=never",
            "-c", "sandbox_workspace_write.network_access=true",
            "--skip-git-repo-check", "--json",
            "-o", str(self.last_msg), "-"
        ], prompt_path)
        log(f"RESUME  codex session={self.session_id[:8]} cwd={self.cwd}")
        return self.proc

    def fork(self, prompt_path: Path):
        if not self.session_id:
            raise RuntimeError("fork 前必须先发现 session_id")
        parent_id = self.session_id
        self._fork_parent_id = parent_id
        self.session_id = None
        self.jsonl = None
        self._spawn_once([
            "exec", "-C", str(self.cwd), "fork", parent_id,
            "-c", "sandbox_mode=workspace-write",
            "-c", "approval_policy=never",
            "-c", "sandbox_workspace_write.network_access=true",
            "--skip-git-repo-check", "--json",
            "-o", str(self.last_msg), "-"
        ], prompt_path)
        log(f"FORK    codex parent={parent_id[:8]} cwd={self.cwd}")
        return self.proc

    def discover_session(self, since_ts: float) -> bool:
        """精准绑定会话。"""
        stdout_file = self.run_dir / "worker-stdout.log"
        thread_id_from_stdout = None
        if stdout_file.exists():
            try:
                with open(stdout_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self.last_stdout_offset)
                    for line in f:
                        if '"thread.started"' in line:
                            m = re.search(r'"thread_id"\s*:\s*"([^"]+)"', line)
                            if m:
                                thread_id_from_stdout = m.group(1)
                                break
            except Exception:
                pass

        if thread_id_from_stdout:
            if not self.session_id:
                self.session_id = thread_id_from_stdout
                log(f"SESSION 官方事件精准捕获 thread_id={self.session_id[:8]}")
                parent_id = getattr(self, "_fork_parent_id", None)
                if parent_id:
                    log(f"FORK    新派生会话就绪: id={self.session_id}")
                    log(f"FORK    客户端深链: codex://threads/{self.session_id} (原父会话: {parent_id[:8]})")
                    register_thread_for_codex_ui(self.session_id, parent_id=parent_id)
            elif self.session_id != thread_id_from_stdout:
                log(f"WARN    输出的 thread_id ({thread_id_from_stdout[:8]}) 与既有 session_id ({self.session_id[:8]}) 不一致!")

        codex_sessions = get_codex_sessions_dir()

        # 若已有明确 session_id，按该 ID 精准检索并核验
        if self.session_id:
            if self.jsonl and self.jsonl.exists() and self._verify_rollout_ownership(self.jsonl, self.session_id, self.cwd):
                return True
            if codex_sessions.exists():
                for p in codex_sessions.rglob(f"*{self.session_id}*.jsonl"):
                    if self._verify_rollout_ownership(p, self.session_id, self.cwd):
                        self.jsonl = p
                        log(f"SESSION 精准绑定轨迹文件: {p.name[:60]}")
                        return True

        # 扫描 since_ts 之后的候选文件
        cands = []
        if codex_sessions.exists():
            for p in codex_sessions.rglob("rollout-*.jsonl"):
                try:
                    c = p.stat().st_ctime
                except OSError:
                    continue
                if c >= since_ts - 2:
                    if self._verify_rollout_ownership(p, self.session_id, self.cwd):
                        cands.append((c, p))
        if cands:
            _, p = max(cands)
            if p != self.jsonl:
                self.jsonl = p
                try:
                    head = p.open("r", encoding="utf-8", errors="replace").read(2048)
                    m = re.search(r'"session_id":"([^"]+)"', head)
                    if m and not self.session_id:
                        self.session_id = m.group(1)
                        log(f"SESSION 发现 id={self.session_id[:8]} file={p.name[:60]}")
                except OSError:
                    pass
            return True

        return bool(self.jsonl and self.jsonl.exists() and self._verify_rollout_ownership(self.jsonl, self.session_id, self.cwd))

    def note_activity(self, t: Optional[float] = None) -> None:
        """记录最新的 Worker 活跃时间戳（例如发现新的工具调用/输出/思维链/写盘），防止长耗时任务误报假死。"""
        self._last_activity_time = time.time() if t is None else float(t)

    def kill_tree(self, timeout: float = 5):
        if self.proc and self.proc.poll() is None:
            try:
                self.interrupt()
                if self.wait_exit(timeout):
                    log(f"KILL    进程树 pid={self.proc.pid} 已优雅退出")
                    self._close_handles()
                    return True
            except Exception:
                pass
            no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            failure_detail = ""
            try:
                killed = subprocess.run(
                    ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                    capture_output=True, text=True, errors="replace", timeout=15, creationflags=no_win,
                )
                if getattr(killed, "returncode", 0):
                    failure_detail = f"{getattr(killed, 'stderr', '') or ''}{getattr(killed, 'stdout', '') or ''}".strip()[:200]
            except Exception as error:
                failure_detail = f"{type(error).__name__}: {error}"
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass
            # 真回执: taskkill 的退出码说明不了进程已死 (权限不足时它只是失败),
            # 必须回查进程状态, 否则会得到"已强制终止"的假回执。
            if self.proc.poll() is None:
                hint = ""
                if "denied" in failure_detail.lower() or "拒绝访问" in failure_detail:
                    hint = " (权限不足: Worker 以更高权限运行, 请以相同或更高权限启动守护进程)"
                log(f"KILL    pid={self.proc.pid} 未能终止: {failure_detail or 'taskkill 未杀死进程'}{hint}")
                self._close_handles()
                return False
            log(f"KILL    进程树 pid={self.proc.pid} 已强制终止")
        self._close_handles()
        return True

    def heartbeat_age(self, launched_at: float) -> float:
        # A resumed process inherits an old rollout. Its startup grace begins at
        # this launch, not at the previous turn's last write; never touch evidence.
        anchor = max(launched_at, getattr(self, "_last_activity_time", 0.0))
        now = time.time()
        try:
            if self.jsonl and self.jsonl.exists():
                st = self.jsonl.stat()
                anchor = max(anchor, st.st_mtime)
                sz = st.st_size
                last_sz = getattr(self, "_last_rollout_size", -1)
                if last_sz != -1 and sz != last_sz:
                    self._last_activity_time = now
                    anchor = max(anchor, now)
                self._last_rollout_size = sz
        except OSError:
            pass

        stdout_log = self.run_dir / "worker-stdout.log"
        try:
            if stdout_log.exists():
                st = stdout_log.stat()
                anchor = max(anchor, st.st_mtime)
                sz = st.st_size
                last_sz = getattr(self, "_last_stdout_size", -1)
                if last_sz != -1 and sz != last_sz:
                    self._last_activity_time = now
                    anchor = max(anchor, now)
                self._last_stdout_size = sz
        except OSError:
            pass

        return max(0.0, time.time() - anchor)

    def interrupt(self) -> bool:
        """优雅中断: CTRL_BREAK 到独立进程组。"""
        if self.proc and self.proc.poll() is None:
            try:
                os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
                return True
            except Exception:
                return False
        return False

    def wait_exit(self, timeout: float = 15) -> bool:
        try:
            self.proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def rollout_size(self) -> Optional[int]:
        try:
            return self.jsonl.stat().st_size if (self.jsonl and self.jsonl.exists()) else None
        except OSError:
            return None

    def rollout_since(self, offset: int) -> str:
        if not (self.jsonl and self.jsonl.exists()):
            return ""
        try:
            with open(self.jsonl, "rb") as f:
                f.seek(offset)
                return f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
