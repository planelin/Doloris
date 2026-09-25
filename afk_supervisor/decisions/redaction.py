"""afk_supervisor.decisions.redaction — 发往决策 API 前的脱敏
============================================================
发送给 Jev 的 state / metadata 必须经过本模块脱敏；原则: 宁可多删，不可泄漏。

移除或替换:
- 值得怀疑的密钥类键 (token / secret / password / cookie / authorization / 凭据...);
- 字符串中的 Bearer Token、Authorization 头、SSH 私钥块、带凭据的仓库 URL;
- 完整用户路径中的用户名 (C:\\Users\\<name> / /home/<name> / /Users/<name>);
- 超长文本 (完整源码 / 完整终端历史的兜底护栏) 与超深嵌套。

调用方仍应只发送摘要而非完整日志——本模块是兜底防线，不是授权发送敏感数据的许可。
"""

import re
from typing import Any, Iterable

REDACTED = "[REDACTED]"
TRUNCATED = "...[TRUNCATED]"

MAX_DEPTH = 6
MAX_STR_LEN = 4000

SENSITIVE_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "credential",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "session_key",
    "auth_header",
)

_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|\S+)")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:api_?key|access_?key|secret|token|password|passwd|private_?key)\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|\S+)"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
_URL_CREDENTIALS_RE = re.compile(r"(?i)((?:https?|ssh|git)://)([^/\s:@]+):([^/\s@]+)@")
_USER_PATH_RE = re.compile(r"(?i)((?:C:\\Users\\|/home/|/Users/))([^\\/\s\"']+)")


def is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """对单个字符串应用模式级脱敏，并替换显式提供的密钥原文。"""
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, REDACTED)
    out = _BEARER_RE.sub("Bearer " + REDACTED, out)
    out = _AUTH_HEADER_RE.sub(r"\1" + REDACTED, out)
    out = _PRIVATE_KEY_RE.sub(REDACTED, out)
    out = _URL_CREDENTIALS_RE.sub(r"\1" + REDACTED + ":" + REDACTED + "@", out)
    out = _ASSIGNMENT_RE.sub(r"\1" + REDACTED, out)
    out = _USER_PATH_RE.sub(r"\1[USER]", out)
    if len(out) > MAX_STR_LEN:
        out = out[:MAX_STR_LEN] + TRUNCATED
    return out


def redact_state(value: Any, secrets: Iterable[str] = (), _depth: int = 0) -> Any:
    """递归脱敏任意 JSON 形态的状态数据; 非 JSON 可序列化对象转为脱敏后的字符串。"""
    if _depth >= MAX_DEPTH:
        return TRUNCATED
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                out[key if isinstance(key, str) else str(key)] = REDACTED
            else:
                out[key if isinstance(key, str) else str(key)] = redact_state(item, secrets, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_state(item, secrets, _depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return redact_text(str(value), secrets)
