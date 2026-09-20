"""
afk_supervisor.drivers.dummy — 虚拟驱动器 (用于测试或双有头桌面GUI监管)
========================================================================
"""

from pathlib import Path


class DummyDriver:
    """双有头模式下的虚拟 Driver 接口，供 l2_dispatch 提取 session_id 与 provider_name"""
    def __init__(self, sid: str, cwd: Path, title: str = ""):
        self.session_id = sid
        self.cwd = cwd
        self.provider_name = "codex-desktop(GUI)"
        self.title = title
        self.jsonl = None
        self.proc = None

    def has_next(self) -> bool:
        return False

    def interrupt(self) -> bool:
        return True

    def wait_exit(self, timeout: float) -> bool:
        return True

    def kill_tree(self) -> None:
        pass

    def note_activity(self, t: float = None) -> None:
        pass

    def heartbeat_age(self, launched_at: float) -> float:
        return 0.0
