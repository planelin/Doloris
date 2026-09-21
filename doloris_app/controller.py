"""Asynchronous supervision controller for Doloris Desktop Companion."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional


def _get_python_exe() -> str:
    """Inherit standard python.exe from afk2.cmd / cmd.exe instead of pythonw.exe."""
    py = sys.executable
    if py.lower().endswith("pythonw.exe"):
        cand = Path(py).with_name("python.exe")
        if cand.exists():
            return str(cand)
    return py


class SupervisionController:
    """Controls the background supervise.py execution and emits state events."""

    def __init__(
        self,
        on_state_change: Callable[[str, str, Optional[str]], None],
    ):
        """
        on_state_change(state, message, report_path)
        state: 'idle', 'working', 'thinking', 'fixing', 'success', 'failed'
        """
        self.on_state_change = on_state_change
        self.process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_requested = False
        self.latest_report_path: Optional[str] = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start_supervision(self, mode: str = "fork", session_target: str = "last"):
        """Starts supervision in a background thread."""
        if self.is_running():
            return

        self._stop_requested = False
        self._thread = threading.Thread(target=self._run_worker, args=(mode, session_target), daemon=True)
        self._thread.start()

    def stop_supervision(self):
        """Stops the current supervision process and all child processes."""
        self._stop_requested = True
        if self.process and self.process.poll() is None:
            pid = self.process.pid
            try:
                no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=no_win)
            except Exception:
                try:
                    self.process.terminate()
                except Exception:
                    pass
        self.on_state_change("idle", "已停止托管，桌宠休息中~", None)

    def _run_worker(self, mode: str, session_target: str = "last"):
        workspace = Path(__file__).resolve().parent.parent
        script = workspace / "supervise.py"
        py_exe = _get_python_exe()

        adopt_val = str(session_target).strip() or "last"
        args = [
            py_exe, "-B", str(script),
            "--adopt", adopt_val,
            "--quick",
            "--yes",
        ]
        if mode == "fork":
            args.append("--fork")
        elif mode == "gui":
            args.append("--gui")

        target_desc = f" [{adopt_val[:8]}]" if adopt_val != "last" else ""
        self.on_state_change("working", f"已启动 {mode.upper()}{target_desc} 托管，主人放心去忙吧！", None)

        runs_dir = workspace / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        log_file = runs_dir / "doloris_supervisor.log"

        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            self.process = subprocess.Popen(
                args,
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=no_win,
                env=env,
            )

            with open(log_file, "a", encoding="utf-8", errors="replace") as f_log:
                f_log.write(f"\n--- DOLORIS SUPERVISION STARTED ({mode} adopt={adopt_val}) ---\n")
                for line in iter(self.process.stdout.readline, ""):
                    if self._stop_requested:
                        break
                    f_log.write(line)
                    f_log.flush()
                    line_str = line.strip()
                    if not line_str:
                        continue

                    # Parse key events to drive pet states and dialogue
                    if "HANDOFF_START" in line_str or "APP_CLOSED" in line_str or "WRITER_RELEASED" in line_str:
                        self.on_state_change("thinking", "正在安全关闭桌面端 App 并确认写锁释放...", None)
                    elif "LAUNCH_GUI" in line_str or "GUI_INJECT" in line_str:
                        self.on_state_change("working", "双有头模式：正在向桌面端窗口自动注入决策指令...", None)
                    elif "PAUSE" in line_str or "FORK_PARENT_WAIT" in line_str:
                        self.on_state_change("thinking", "正在向 Codex 请求安全暂停并确认...", None)
                    elif "L2_CONSULT" in line_str or "DECIDE" in line_str or "QUESTION" in line_str:
                        self.on_state_change("thinking", "遇到设计选择题啦，正在委托 AGY 拍板！", None)
                    elif "L2 WAIT" in line_str:
                        self.on_state_change("thinking", "AGY 主管正在深度分析决策中，请稍候...", None)
                    elif "L2_ANSWER" in line_str:
                        self.on_state_change("working", "AGY 拍板完成！正在下发执行指令...", None)
                    elif "FORK" in line_str or "RESUME" in line_str or "LAUNCH" in line_str:
                        self.on_state_change("working", "子任务已接管续跑，代码飞速生成中...", None)
                    elif "DETECT_HANG" in line_str or "CHAOS_KILL" in line_str or "RESUME_WAIT" in line_str:
                        self.on_state_change("fixing", "检测到网络或进程卡顿，正在自动自愈重试...", None)
                    elif "TERMINAL SUCCESS" in line_str:
                        report = self._find_latest_report()
                        self.latest_report_path = report
                        self.on_state_change("success", "🎉 任务已 100% 完成并通过验收！点击查看终态报告 📄", report)
                    elif "TERMINAL FAILED" in line_str or "TERMINAL BLOCKED" in line_str:
                        report = self._find_latest_report()
                        self.latest_report_path = report
                        self.on_state_change("failed", "任务已保存现场，点击查看详情 📄", report)

            self.process.stdout.close()
            self.process.wait()

        except Exception as e:
            self.on_state_change("failed", f"启动异常: {e}", None)
        finally:
            self.process = None

    def _find_latest_report(self) -> Optional[str]:
        """Finds the latest report.md in runs/."""
        workspace = Path(__file__).resolve().parent.parent
        runs_dir = workspace / "runs"
        if not runs_dir.exists():
            return None
        reports = list(runs_dir.glob("*/report.md"))
        if not reports:
            return None
        latest = max(reports, key=lambda p: p.stat().st_mtime)
        return str(latest)
