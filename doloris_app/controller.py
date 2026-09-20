"""Asynchronous supervision controller for Doloris Desktop Companion."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional


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

    def start_supervision(self, mode: str = "fork"):
        """Starts supervision in a background thread."""
        if self.is_running():
            return

        self._stop_requested = False
        self._thread = threading.Thread(target=self._run_worker, args=(mode,), daemon=True)
        self._thread.start()

    def stop_supervision(self):
        """Stops the current supervision process."""
        self._stop_requested = True
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass
        self.on_state_change("idle", "已停止托管，桌宠休息中~", None)

    def _run_worker(self, mode: str):
        workspace = Path.cwd()
        script = workspace / "supervise.py"

        args = [sys.executable, "-B", "-X", "utf8", str(script), "--adopt", "last", "--quick"]
        if mode == "fork":
            args.append("--fork")
        elif mode == "gui":
            args.append("--gui")

        self.on_state_change("working", f"已启动 {mode.upper()} 托管，主人放心去忙吧！", None)

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
            )

            for line in iter(self.process.stdout.readline, ""):
                if self._stop_requested:
                    break
                line_str = line.strip()
                if not line_str:
                    continue

                # Parse key events to drive pet states and dialogue
                if "PAUSE" in line_str or "FORK_PARENT_WAIT" in line_str:
                    self.on_state_change("thinking", "正在向 Codex 请求安全暂停并确认...", None)
                elif "FORK" in line_str or "RESUME" in line_str or "LAUNCH" in line_str:
                    self.on_state_change("working", "子任务已接管续跑，代码飞速生成中...", None)
                elif "L2_CONSULT" in line_str or "DECIDE" in line_str or "QUESTION" in line_str:
                    self.on_state_change("thinking", "遇到设计选择题啦，正在委托 AGY 拍板！", None)
                elif "DETECT_HANG" in line_str or "CHAOS_KILL" in line_str or "RESUME_WAIT" in line_str:
                    self.on_state_change("fixing", "检测到网络或进程卡顿，正在自动自愈重试...", None)
                elif "TERMINAL SUCCESS" in line_str or "TERMINAL SUCCESS" in line_str:
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
        runs_dir = Path.cwd() / "runs"
        if not runs_dir.exists():
            return None
        reports = list(runs_dir.glob("*/report.md"))
        if not reports:
            return None
        latest = max(reports, key=lambda p: p.stat().st_mtime)
        return str(latest)
