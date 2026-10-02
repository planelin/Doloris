"""Small atomic JSON checkpoints; persistence errors must not silently authorize work."""
import errno
import json
import os
import time
import uuid
from pathlib import Path

# Windows 上 os.replace 会被杀毒/索引器的瞬时共享冲突打断 (WinError 5 / 32)。
# 这类错误与真正的权限问题无法从错误码本身区分，因此只做有界重试，最终仍如实抛错。
# 重试窗口须覆盖常见 AV 扫描时长 (>0.3s 的锁很常见, 曾把健康 run 打成 FAILED)。
_TRANSIENT_WINERRORS = frozenset({5, 32})
_REPLACE_ATTEMPTS = 8
_REPLACE_BACKOFF_SEC = 0.25


def _is_transient_replace_error(error: OSError) -> bool:
    winerror = getattr(error, "winerror", None)
    if winerror is not None:
        return winerror in _TRANSIENT_WINERRORS
    return error.errno == errno.EACCES


def atomic_json(path: Path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        last_error: OSError | None = None
        for attempt in range(_REPLACE_ATTEMPTS):
            if attempt:
                time.sleep(_REPLACE_BACKOFF_SEC)
            try:
                temporary.write_text(payload, encoding="utf-8")
                os.replace(temporary, path)
                return
            except OSError as error:
                last_error = error
                if not _is_transient_replace_error(error):
                    raise
        assert last_error is not None
        raise last_error
    finally:
        temporary.unlink(missing_ok=True)
