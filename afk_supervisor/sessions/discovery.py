"""
afk_supervisor.sessions.discovery — Codex 会话探测、索引绑定与 UI 注册
====================================================================
负责从 ~/.codex/ 目录扫描会话、读取 session_meta、提取官方会话标题，
以及将后台派生的 Fork 会话登记到桌面端数据库 (state_5.sqlite)。
"""

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from afk_supervisor.platform.process import log


def get_home_dir() -> Path:
    import sys
    if "supervise" in sys.modules and hasattr(sys.modules["supervise"], "HOME"):
        return sys.modules["supervise"].HOME
    return Path.home()


def get_codex_home() -> Path:
    return get_home_dir() / ".codex"


def get_codex_sessions_dir() -> Path:
    import sys
    if "supervise" in sys.modules and hasattr(sys.modules["supervise"], "CODEX_SESSIONS"):
        return sys.modules["supervise"].CODEX_SESSIONS
    return get_codex_home() / "sessions"


def get_codex_locks_dir() -> Path:
    import sys
    if "supervise" in sys.modules and hasattr(sys.modules["supervise"], "CODEX_LOCKS"):
        return sys.modules["supervise"].CODEX_LOCKS
    return get_codex_home() / "thread-writer-locks"


def _json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s


def _read_meta(p: Path) -> Optional[Tuple[str, Optional[str]]]:
    """读 rollout 头部 session_meta，返回 (session_id, cwd) 或 None。"""
    try:
        with p.open("r", encoding="utf-8") as stream:
            record = json.loads(stream.readline())
        meta = record.get("payload", record)
        source = meta.get("source")
        if meta.get("thread_source") == "guardian_review" or (isinstance(source, dict) and "subagent" in source):
            return None
        sid = meta.get("id") or meta.get("session_id")
        return (sid, meta.get("cwd")) if isinstance(sid, str) and sid else None
    except (OSError, ValueError, TypeError):
        return None


def clean_session_id(raw: str) -> str:
    """清洗会话标识，兼容 URL (codex://threads/<uuid>)、相对路径 (threads/<uuid>) 等。"""
    s = raw.strip()
    if s.startswith("codex://"):
        s = s[len("codex://"):]
    s = s.strip("/\\")
    if s.startswith("threads/"):
        s = s[len("threads/"):]
    return s.strip()


def _load_session_index_titles() -> Dict[str, str]:
    """读取 ~/.codex/session_index.jsonl 的 thread_name (旧式索引，可能落后于侧栏)。"""
    titles: Dict[str, str] = {}
    session_index = get_codex_home() / "session_index.jsonl"
    if not session_index.exists():
        return titles
    try:
        for line in session_index.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if "id" in d and d.get("thread_name"):
                    titles[str(d["id"]).strip()] = str(d["thread_name"]).strip()
            except Exception:
                continue
    except Exception:
        pass
    return titles


def _load_state_db_titles() -> Dict[str, Tuple[str, str]]:
    """读取 state_5.sqlite 的 (name, title)。

    name 是桌面端侧栏实际显示的会话名 (含 [Fork] 等前缀)，
    title 是同一行的简短标题；两者都比 session_index.jsonl 更接近 UI 真值。
    """
    rows: Dict[str, Tuple[str, str]] = {}
    state_db = get_codex_home() / "state_5.sqlite"
    if not state_db.exists():
        return rows
    try:
        con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            cur.execute("SELECT id, name, title FROM threads")
            for sid, name, title in cur.fetchall():
                key = str(sid or "").strip()
                if not key:
                    continue
                rows[key] = (str(name or "").strip(), str(title or "").strip())
        finally:
            con.close()
    except Exception:
        pass
    return rows


def load_codex_thread_titles() -> Dict[str, str]:
    """加载官方会话标题，优先桌面端侧栏显示名 (state_5.sqlite.name)。

    session_index.jsonl 会滞后于侧栏 (例如缺少 "[Fork] " 前缀)，若优先使用它，
    传入 UI Automation 的目标标题将与实际 DOM 名称不一致，导致身份校验必然失败。
    """
    index_titles = _load_session_index_titles()
    db_titles = _load_state_db_titles()
    titles: Dict[str, str] = dict(index_titles)
    for sid, (name, _title) in db_titles.items():
        if name:
            titles[sid] = name
    return titles


def load_codex_thread_title_candidates(sid: str) -> List[str]:
    """返回某个会话可用于 UI 身份校验的全部标题候选 (按可信度排序、去重)。

    只作为"同一 SID 下的名字候选"，不得脱离 SID 单独用于标识任务。
    """
    clean = clean_session_id(sid)
    if not clean:
        return []
    candidates: List[str] = []
    name, short_title = _load_state_db_titles().get(clean, ("", ""))
    index_title = _load_session_index_titles().get(clean, "")
    for value in (name, short_title, index_title):
        text = str(value or "").strip()
        if text and text not in candidates:
            candidates.append(text)
    return candidates


def read_session_title(p: Path, scan: int = 262144) -> str:
    """从 rollout 提取任务标题 (首条真实用户输入，截断60字)。"""
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            head = f.read(scan)
    except OSError:
        return ""
    pats = [
        r'"role"\s*:\s*"user"\s*,\s*"content"\s*:\s*"((?:[^"\\]|\\.)*)"',
        r'"type"\s*:\s*"input_text"\s*,\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"',
        r'"operation"\s*:\s*"enqueue"[^\n]*?"content"\s*:\s*"((?:[^"\\]|\\.)*)"',
    ]
    for pat in pats:
        for m in re.finditer(pat, head):
            t = _json_str(m.group(1)).strip()
            if t and not t.startswith("<"):
                return t[:60]
    return ""


def list_recent_codex_sessions(n: int = 8) -> List[Tuple[str, Path, Optional[str], str, str]]:
    """最近 n 个去重后的会话，按活跃时间降序。
    返回 [(sid, rollout_path, session_cwd, title, age_str), ...]
    """
    codex_sessions = get_codex_sessions_dir()
    if not codex_sessions.exists():
        return []
    titles_map = load_codex_thread_titles()
    sessions = {}
    for p in codex_sessions.rglob("rollout-*.jsonl"):
        meta = _read_meta(p)
        if not meta:
            continue
        sid, scwd = meta
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if sid not in sessions or mt > sessions[sid]["mtime"]:
            sessions[sid] = {
                "mtime": mt,
                "path": p,
                "sid": sid,
                "scwd": scwd,
            }

    sorted_sessions = sorted(sessions.values(), key=lambda x: x["mtime"], reverse=True)
    out = []
    for item in sorted_sessions[:n]:
        sid = item["sid"]
        p = item["path"]
        scwd = item["scwd"]
        mt = item["mtime"]
        title = titles_map.get(sid) or read_session_title(p)
        age = time.strftime("%m-%d %H:%M", time.localtime(mt))
        out.append((sid, p, scwd, title, age))
    return out


def find_last_codex_session(cwd: Optional[Path] = None) -> Optional[Tuple[str, Path, Optional[str]]]:
    """找最近活跃的 codex 会话: 全局按 rollout mtime 降序取最新。
    返回 (session_id, rollout_path, session_cwd) 或 None。
    """
    codex_sessions = get_codex_sessions_dir()
    if not codex_sessions.exists():
        return None
    best = None
    for p in codex_sessions.rglob("rollout-*.jsonl"):
        meta = _read_meta(p)
        if not meta:
            continue
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if best is None or mt > best[0]:
            best = (mt, meta[0], p, meta[1])
    if not best:
        return None
    _, sid, p, scwd = best
    if cwd and scwd and scwd != str(cwd):
        log(f"ADOPT   最新会话 cwd={scwd}，验收锚点随之转移")
    return sid, p, scwd


def find_codex_session_by_id(raw_sid: str) -> Optional[Tuple[str, Path, Optional[str]]]:
    """根据会话 ID 查找对应的 rollout 文件与 cwd。

    桌面端 fork 子会话的文件名为 rollout-<ts>-<root_sid>_<child_sid>.jsonl，
    但其 session_meta 会继承 root_sid，因此按 ID 匹配会同时命中父文件与所有后代
    fork 文件。后代文件 mtime 更新，若按 mtime 取"最新"，接管到的将是子会话轨迹
    (父会话暂停校验、目标提炼都会读错对象)。这里改为：请求哪个 ID，就优先返回
    文件名以该 ID 结尾的原始 rollout 文件，仅在没有原始文件时才回退。
    返回 (actual_sid, rollout_path, session_cwd) 或 None。
    """
    sid = clean_session_id(raw_sid)
    codex_sessions = get_codex_sessions_dir()
    if not sid or not codex_sessions.exists():
        return None
    matches = []
    for p in codex_sessions.rglob("rollout-*.jsonl"):
        meta = _read_meta(p)
        if meta and (meta[0] == sid or meta[0].startswith(sid)):
            try:
                mt = p.stat().st_mtime
            except OSError:
                mt = 0
            # 原始文件 (rollout-<ts>-<sid>.jsonl) 优先于继承同一 session_meta 的后代 fork 文件。
            is_origin = p.stem.endswith(meta[0])
            matches.append((0 if is_origin else 1, -mt, meta[0], p, meta[1]))
    if matches:
        matches.sort(key=lambda item: (item[0], item[1]))
        _, _neg_mtime, actual_sid, p, scwd = matches[0]
        return actual_sid, p, scwd
    # 兜底：桌面端 fork 子会话的文件名为 rollout-<ts>-<root_sid>_<child_sid>.jsonl，
    # session_meta 会继承 root 会话 ID，只能按文件名后缀识别子会话。
    fork_matches = []
    for p in codex_sessions.rglob("rollout-*.jsonl"):
        if not p.stem.endswith(sid):
            continue
        meta = _read_meta(p)
        scwd = meta[1] if meta else None
        try:
            mt = p.stat().st_mtime
        except OSError:
            mt = 0
        fork_matches.append((mt, sid, p, scwd))
    if not fork_matches:
        return None
    fork_matches.sort(reverse=True)
    _, actual_sid, p, scwd = fork_matches[0]
    return actual_sid, p, scwd


def find_codex_session_rollouts(raw_sid: str) -> List[Tuple[str, Path, Optional[str]]]:
    """返回与某个会话 ID 相关的全部 rollout，按文件名时间前缀从早到晚排序。

    用于回溯 fork 会话的业务主线：fork 子文件名形如
    rollout-<ts>-<root_sid>_<child_sid>.jsonl，其 session_meta 会继承 root 会话 ID，
    因此必须同时按文件名匹配，且原始会话文件（文件名以该 ID 结尾）排在 fork 子文件之前。
    """
    sid = clean_session_id(raw_sid)
    codex_sessions = get_codex_sessions_dir()
    if not sid or not codex_sessions.exists():
        return []
    matches: List[Tuple[str, str, Path, Optional[str]]] = []
    for p in codex_sessions.rglob("rollout-*.jsonl"):
        if sid not in p.name:
            continue
        meta = _read_meta(p)
        if meta is None and not p.stem.endswith(sid):
            continue
        actual_sid, scwd = meta if meta else (sid, None)
        matches.append((p.name, actual_sid, p, scwd))
    matches.sort(key=lambda item: item[0])
    return [(actual_sid, p, scwd) for _, actual_sid, p, scwd in matches]


def register_thread_for_codex_ui(session_id: str, title_prefix: str = "[Fork] ", parent_id: Optional[str] = None) -> bool:
    """将 codex exec fork 创建的后台会话登记到桌面端 UI (修改 state_5.sqlite 中 source='vscode')。"""
    if not session_id:
        return False
    db_path = get_codex_home() / "state_5.sqlite"
    if not db_path.exists():
        return False
    for attempt in range(5):
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(threads)")
            cols = {r[1] for r in cur.fetchall()}
            required_cols = {"id", "name", "source", "title"}
            if not required_cols.issubset(cols):
                log(f"UI_SYNC  state_5.sqlite threads 表字段结构不匹配(缺少 {required_cols - cols})，安全跳过UI登记")
                conn.close()
                return False
            has_thread_source = "thread_source" in cols

            cur.execute("SELECT name, source, title FROM threads WHERE id = ?", (session_id,))
            row = cur.fetchone()
            if row:
                name, _source, title = row[0] or "", row[1] or "", row[2] or ""
                new_name = name
                if title_prefix and not name.startswith(title_prefix):
                    new_name = title_prefix + name
                new_title = title
                if (not new_title or not new_title.strip()) and parent_id:
                    try:
                        cur.execute("SELECT title, name FROM threads WHERE id = ?", (parent_id,))
                        p_row = cur.fetchone()
                        if p_row:
                            new_title = p_row[0] or new_title
                            if not name or not name.strip():
                                new_name = title_prefix + (p_row[1] or "派生任务")
                    except Exception:
                        pass

                set_clause = "source = 'vscode', name = ?, title = ?"
                params = [new_name, new_title]
                if has_thread_source:
                    set_clause += ", thread_source = 'user'"
                params.append(session_id)
                cur.execute(f"UPDATE threads SET {set_clause} WHERE id = ?", params)
                conn.commit()
                log(f"UI_SYNC  已将新派生会话登记到桌面端数据库 (source=vscode, 标题='{new_name[:30]}...')")
                conn.close()
                return True
            conn.close()
        except Exception:
            pass
        time.sleep(0.5)
    return False
