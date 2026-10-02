"""afk_supervisor.core.log — 包内统一日志原语 (第 0 层, 零内部依赖)
==================================================================
从 afk_supervisor.platform.process 下沉至此: sessions/baseline 等下层模块
只需要 log(), 不应为此反向依赖 platform 层。
"""

import sys
from datetime import datetime


def log(msg: str):
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)
    except (UnicodeEncodeError, OSError):
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe = msg.encode(enc, errors="replace").decode(enc, errors="replace")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {safe}", flush=True)
