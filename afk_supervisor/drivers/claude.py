"""
afk_supervisor.drivers.claude — Claude CLI 驱动器与中转探针
===========================================================
支持通过 cc-switch 数据库读取供应商池；
实测 1-token PONG 过滤不可用节点；
支持在同会话内自动轮换中转提供商。
"""

import json
import os
import signal
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from afk_supervisor.platform.process import log, munged_cwd


def get_claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def get_cc_switch_db() -> Path:
    return Path.home() / ".cc-switch" / "cc-switch.db"


def probe_pool(pool: List[Tuple[str, dict]], proxy: Optional[str], keep: int = 3, timeout: int = 45) -> List[Tuple[str, dict]]:
    """逐个用 1-token PONG 请求实测供应商，保留前 keep 个可用者。"""
    working, tried = [], []
    ws = Path(__file__).resolve().parent.parent.parent
    for name, env in pool:
        if len(working) >= keep:
            break
        e = dict(os.environ)
        e.update(env)
        if proxy:
            e["HTTPS_PROXY"] = proxy
            e["HTTP_PROXY"] = proxy
        t0 = time.time()
        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            ["cmd.exe", "/c", "claude", "-p", "Reply with exactly one word: PONG"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=e, cwd=str(ws),
            creationflags=no_win
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
            ok = proc.returncode == 0 and "PONG" in (out or "").upper()
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=no_win)
            proc.wait()
            ok = False
        sec = round(time.time() - t0)
        tried.append((name, ok, sec))
        log(f"PROBE   {name}: {'PASS' if ok else 'FAIL'} ({sec}s)")
        if ok:
            working.append((name, env))
    (ws / "pool-health.json").write_text(json.dumps({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "proxy": proxy,
        "results": [{"name": n, "ok": o, "sec": s} for n, o, s in tried]
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return working


def get_relay_pool(app_type: str = "claude-desktop") -> List[Tuple[str, dict]]:
    """只读 cc-switch 数据库，返回供应商池。"""
    db_path = get_cc_switch_db()
    if not db_path.exists():
        log("WARN: cc-switch 数据库不存在，worker 将以当前环境变量运行")
        return [("(shell-env)", {})]
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select id,name,settings_config,is_current from providers where app_type=?",
            (app_type,)
        ).fetchall()
        health = dict(con.execute(
            "select provider_id,is_healthy from provider_health where app_type=?",
            (app_type,)
        ).fetchall())
        current, others = None, []
        for pid, name, cfg, cur in rows:
            try:
                env = json.loads(cfg).get("env", {}) or {}
            except Exception:
                continue
            if not (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")):
                continue
            item = (name, dict(env))
            if cur == 1:
                current = item
            else:
                others.append((health.get(pid) == 1, item))
        others.sort(key=lambda x: not x[0])
        pool = ([current] if current else []) + [i for _, i in others]
        for i, (name, env) in enumerate(pool):
            tag = "当前" if i == 0 else f"备胎{i}"
            log(f"pool[{i}] {tag}: {name} -> {env.get('ANTHROPIC_BASE_URL', '(官方)')}")
        return pool if pool else [("(shell-env)", {})]
    finally:
        con.close()


class ClaudeDriver:
    """claude CLI 无头驱动。预指定 session-id; 池内可切换供应商(同会话续跑)。"""

    def __init__(self, cwd: Path, run_dir: Path, pool: List[Tuple[str, dict]], proxy: Optional[str] = None):
        self.cwd = Path(cwd).resolve()
        self.run_dir = Path(run_dir).resolve()
        self.pool = pool
        self.proxy = proxy
        self.pidx = 0
        self.session_id = str(uuid.uuid4())
        self.jsonl = get_claude_projects_dir() / munged_cwd(self.cwd) / f"{self.session_id}.jsonl"
        self.proc: Optional[subprocess.Popen] = None
        self._open_handles: List[object] = []
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
    def relay_env(self) -> dict:
        return self.pool[self.pidx][1]

    @property
    def provider_name(self) -> str:
        return self.pool[self.pidx][0]

    def has_next(self) -> bool:
        return self.pidx + 1 < len(self.pool)

    def switch_provider(self) -> Optional[str]:
        if not self.has_next():
            return None
        self.pidx += 1
        return self.provider_name

    def _spawn(self, args: List[str], stdin_path: Path):
        self._close_handles()
        self._last_activity_time = 0.0
        self._last_rollout_size = -1
        self._last_stdout_size = -1
        env = dict(os.environ)
        env.update(self.relay_env)
        if self.proxy:
            env["HTTPS_PROXY"] = self.proxy
            env["HTTP_PROXY"] = self.proxy
        f_in = open(stdin_path, "rb")
        f_out = open(self.run_dir / "worker-stdout.log", "ab")
        f_err = open(self.run_dir / "worker-stderr.log", "ab")
        self._open_handles = [f_in, f_out, f_err]
        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            ["cmd.exe", "/c", "claude", *args],
            cwd=str(self.cwd), env=env,
            stdin=f_in, stdout=f_out, stderr=f_err,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | no_win,
        )
        return self.proc

    def launch(self, prompt_path: Path):
        log(f"LAUNCH session={self.session_id[:8]} 供应商={self.provider_name}")
        return self._spawn(
            ["-p", "--output-format", "text",
             "--permission-mode", "acceptEdits",
             "--session-id", self.session_id],
            prompt_path,
        )

    def resume(self, prompt_path: Path):
        log(f"RESUME  session={self.session_id[:8]} 供应商={self.provider_name}")
        return self._spawn(
            ["-p", "--output-format", "text",
             "--permission-mode", "acceptEdits",
             "--resume", self.session_id],
            prompt_path,
        )

    def kill_tree(self, timeout: float = 5):
        if self.proc and self.proc.poll() is None:
            try:
                self.interrupt()
                if self.wait_exit(timeout):
                    log(f"KILL    进程树 pid={self.proc.pid} 已优雅退出")
                    self._close_handles()
                    return
            except Exception:
                pass
            no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True, creationflags=no_win)
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass
            log(f"KILL    进程树 pid={self.proc.pid} 已强制终止")
        self._close_handles()

    def note_activity(self, t: Optional[float] = None) -> None:
        """记录最新的 Worker 活跃时间戳（例如发现新的工具调用/输出/思维链/写盘），防止长耗时任务误报假死。"""
        self._last_activity_time = time.time() if t is None else float(t)

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
            return self.jsonl.stat().st_size if self.jsonl.exists() else None
        except OSError:
            return None

    def rollout_since(self, offset: int) -> str:
        try:
            with open(self.jsonl, "rb") as f:
                f.seek(offset)
                return f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
