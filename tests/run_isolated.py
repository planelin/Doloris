"""Run all regression suites on a disposable source copy, never on live AFK state.

Usage: python -B -X utf8 tests/run_isolated.py
The harness only permits Python/Node syntax checks and the pure C# identity test. Process termination,
GUI input, bridge discovery, keep-awake, proxies and notifications are simulated.
"""

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def main():
    source = Path(__file__).resolve().parents[1]
    original_run = subprocess.run
    original_popen = subprocess.Popen
    suppressed = []

    def allowed_check(command):
        if not isinstance(command, (list, tuple)) or not command:
            return False
        executable = str(command[0]).lower()
        return (
            executable in ("node", "node.exe") and len(command) == 3 and command[1] == "--check"
        ) or (
            executable == sys.executable.lower()
            and list(command[1:5]) == ["-I", "-B", "-c", "import pathlib, sys; compile(pathlib.Path(sys.argv[1]).read_bytes(), sys.argv[1], 'exec')"]
        )

    def allowed_identity_check(command):
        return (isinstance(command, (list, tuple)) and len(command) == 7
                and list(command[:6]) == ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
                and Path(command[6]).resolve() == (clone / "tests" / "check_gui_identity.ps1").resolve())

    def isolated_run(command, *args, **kwargs):
        if allowed_check(command) or allowed_identity_check(command):
            return original_run(command, *args, **kwargs)
        # Older driver-lifecycle tests use fake PIDs but did not mock taskkill.
        if isinstance(command, (list, tuple)) and command and str(command[0]).lower() == "taskkill":
            suppressed.append("taskkill (simulated)")
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"Unmocked external process in isolated suite: {command!r}")

    def isolated_popen(command, *args, **kwargs):
        if not (allowed_check(command) or allowed_identity_check(command)):
            raise AssertionError(f"Unmocked process launch in isolated suite: {command!r}")
        return original_popen(command, *args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="afk-isolated-suite-") as temporary:
        root = Path(temporary).resolve()
        assert root.parent == Path(tempfile.gettempdir()).resolve()
        assert root.name.startswith("afk-isolated-suite-")
        clone = root / "source"
        clone.mkdir()
        for name in ("supervise.py", "afk_protocol.py", "test_supervise.py", "gui_inject.ps1"):
            shutil.copy2(source / name, clone / name)
        for name in ("afk_supervisor", "tests"):
            shutil.copytree(source / name, clone / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # Skill metadata tests read documentation only; no skill instructions run.
        skill = source / ".agents" / "skills" / "afk-supervisor-reviewer" / "SKILL.md"
        if skill.is_file():
            target = clone / ".agents" / "skills" / "afk-supervisor-reviewer" / "SKILL.md"
            target.parent.mkdir(parents=True)
            shutil.copy2(skill, target)
        home = root / "home"
        home.mkdir()
        old_cwd = Path.cwd()
        sys.path.insert(0, str(clone))
        os.chdir(clone)
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {
                    "HOME": str(home), "USERPROFILE": str(home), "CODEX_HOME": str(home / ".codex"),
                    "APPDATA": str(home / "AppData" / "Roaming"), "LOCALAPPDATA": str(home / "AppData" / "Local"),
                    "PYTHONDONTWRITEBYTECODE": "1", "AFK_WEBHOOK_URL": "",
                }))
                stack.enter_context(patch.object(Path, "home", return_value=home))
                stack.enter_context(patch.object(subprocess, "run", side_effect=isolated_run))
                stack.enter_context(patch.object(subprocess, "Popen", side_effect=isolated_popen))
                stack.enter_context(patch.object(os, "kill"))
                stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("No network in isolated tests")))
                import supervise
                from afk_supervisor import cli, reporting
                from afk_supervisor.l2.bridge import AntigravityManager
                stack.enter_context(patch.object(cli, "keep_awake"))
                stack.enter_context(patch.object(cli, "detect_system_proxy", return_value=None))
                stack.enter_context(patch.object(cli, "atexit"))
                stack.enter_context(patch.object(reporting, "send_terminal_notification"))
                stack.enter_context(patch.object(AntigravityManager, "ensure_bridge", return_value=(None, [], home / "missing-bridge.exe")))
                stack.enter_context(patch.object(AntigravityManager, "teardown"))
                for name, value in (
                    ("ensure_codex_window_restored", None),
                    ("inject_into_codex_gui", (False, "GUI disabled in isolated tests")),
                    ("pause_codex_gui_session", False),
                ):
                    stack.enter_context(patch.object(supervise, name, return_value=value))
                suite = unittest.defaultTestLoader.discover(str(clone), pattern="test_*.py", top_level_dir=str(clone))
                result = unittest.TextTestRunner(verbosity=2).run(suite)
                print(json.dumps({
                    "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                    "skipped": len(result.skipped), "live_sessions_used": False,
                    "real_workspace_locks_acquired": False, "suppressed_calls": suppressed,
                }, ensure_ascii=False))
                return 0 if result.wasSuccessful() else 1
        finally:
            os.chdir(old_cwd)
            sys.path.remove(str(clone))


if __name__ == "__main__":
    raise SystemExit(main())
