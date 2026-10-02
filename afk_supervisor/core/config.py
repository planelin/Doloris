"""afk_supervisor.core.config — 路径常量配置 (第 0 层)
====================================================
集中管理依赖用户主目录的路径常量。历史缺陷: sessions/discovery 与 l2/bridge
通过 `sys.modules["supervise"]` 反查这些常量 (为兼容旧测试热补丁), 形成对
门面模块的隐藏反向依赖。现收敛到本模块; 测试如需重定向, 直接 patch 本模块
的属性 (消费者以 `config.X` 动态属性读取, 补丁即时生效)。
"""

import os
from pathlib import Path

HOME = Path.home()
# 仓库根 (afk_supervisor/core/config.py -> 上三级)
WS = Path(__file__).resolve().parent.parent.parent
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CC_SWITCH_DB = HOME / ".cc-switch" / "cc-switch.db"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
CODEX_LOCKS = HOME / ".codex" / "thread-writer-locks"
CODEX_APP_DATA = Path(os.environ.get("APPDATA", "")) / "Codex"
