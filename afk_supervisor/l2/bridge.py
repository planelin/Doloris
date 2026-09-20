"""
afk_supervisor.l2.bridge — Antigravity (AGY) L2 桥接生命周期与会话通道
======================================================================
管理有头/无头双模 Antigravity 实例绑定；
支持复用桌面端或在后台启动独立轻量核心 (language_server.exe --standalone)；
严格维护 1 Codex Session <-> 1 AGY Conversation 单一会话映射；
直接从本地转录日志读取 PLANNER_RESPONSE 并做偏移量与 request_id 防护。
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

from afk_supervisor.models import AgyResponseResult
from afk_supervisor.platform.process import log
from afk_supervisor.l2.protocol import extract_protocol_json


def get_agy_brain_dir() -> Path:
    import sys
    if "supervise" in sys.modules and hasattr(sys.modules["supervise"], "HOME"):
        home = sys.modules["supervise"].HOME
    else:
        home = Path.home()
    return home / ".gemini" / "antigravity" / "brain"


def get_skill_metadata(project_root: Optional[Path] = None) -> Tuple[str, str]:
    """读取 afk-supervisor-reviewer 的版本及内容 sha256 摘要。"""
    root = project_root or Path(__file__).resolve().parent.parent.parent
    skill_file = root / ".agents" / "skills" / "afk-supervisor-reviewer" / "SKILL.md"
    if not skill_file.exists():
        return "1.0.0-unloaded", "none"
    try:
        content = skill_file.read_text(encoding="utf-8")
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        return "1.0.0", h
    except Exception:
        return "1.0.0-err", "unknown"


def probe_ls_grpc_port(port: str) -> bool:
    """探测端口是否为可正常接收明文 HTTP/gRPC 请求的语言服务端口。
    Antigravity 的 HTTPS Web 窗口端口遇到明文 HTTP 会报 400 或握手异常，而 gRPC 端口会返回 200。
    """
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"User-Agent": "AntigravityProbe"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            return resp.status == 200
    except Exception:
        return False


def discover_antigravity_bridge() -> Tuple[Optional[str], List[str], Path]:
    """动态发现 Antigravity 桥: csrf + LS 网关端口。"""
    csrf = None
    logf = Path(os.environ.get("APPDATA", "")) / "Antigravity" / "logs" / "main.log"
    https_ports = set()
    if logf.exists():
        try:
            log_text = logf.read_text(encoding="utf-8", errors="replace")
            toks = re.findall(r"--csrf_token ([0-9a-f-]{36})", log_text)
            csrf = toks[-1] if toks else None
            hp_matches = re.findall(r"https://127\.0\.0\.1:(\d+)", log_text)
            if hp_matches:
                https_ports.update(hp_matches[-3:])
        except Exception:
            pass
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "(Get-Process language_server -ErrorAction SilentlyContinue).Id"],
        capture_output=True, text=True, timeout=25
    )
    pids = [int(x) for x in (r.stdout or "").split() if x.isdigit()]
    ports = []
    if pids:
        r2 = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=25)
        for line in (r2.stdout or "").splitlines():
            if "LISTENING" in line and "127.0.0.1" in line and any(line.rstrip().endswith(str(pid)) for pid in pids):
                port = line.split()[1].rsplit(":", 1)[-1]
                if port not in ports:
                    ports.append(port)
    if len(ports) > 1:
        def _port_priority(p: str) -> int:
            if probe_ls_grpc_port(p):
                return 0
            if p in https_ports:
                return 2
            return 1
        ports.sort(key=_port_priority)
    agexe = (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/antigravity/resources/bin/language_server.exe")
    return csrf, ports, agexe


def discover_antigravity_project_id(agexe: Path, csrf: str, ports: List[str]) -> Optional[str]:
    """取最近一个会话元数据里的 projectId。"""
    conv_dir = Path.home() / ".gemini" / "antigravity" / "conversations"
    if not conv_dir.exists():
        return None
    dbs = sorted(conv_dir.glob("*.db"), key=lambda x: x.stat().st_mtime, reverse=True)
    ws = Path(__file__).resolve().parent.parent.parent
    for port in ports:
        env = dict(os.environ)
        env["ANTIGRAVITY_CSRF_TOKEN"] = csrf
        env["ANTIGRAVITY_LS_ADDRESS"] = f"127.0.0.1:{port}"
        for db in dbs[:3]:
            try:
                r = subprocess.run(
                    [str(agexe), "agentapi", "get-conversation-metadata", db.stem],
                    capture_output=True, timeout=60, env=env, cwd=str(ws)
                )
                m = re.search(r'"projectId"\s*:\s*"([0-9a-f-]{36})"', (r.stdout or b"").decode("utf-8", errors="replace"))
                if m:
                    return m.group(1)
            except Exception:
                continue
    return None


def check_agy_transcript_error(cid: str) -> Optional[str]:
    """检查 AGY 本地会话日志是否已中断报错且停止更新。"""
    if not cid:
        return None
    agy_brain = get_agy_brain_dir()
    t_path = agy_brain / cid / ".system_generated" / "logs" / "transcript.jsonl"
    if not t_path.exists():
        return None
    try:
        st = t_path.stat()
        if time.time() - st.st_mtime < 15:
            return None
        lines = t_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            return None
        for line in reversed(lines[-5:]):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            if data.get("type") == "ERROR_MESSAGE" or data.get("status") == "ERROR":
                content = data.get("content", "")
                if "stream was interrupted" in content or "network issue" in content:
                    return f"AGY云端网络流中断: {content[:100]}"
                return f"AGY执行报错: {content[:100]}"
    except Exception:
        pass
    return None


def parse_verdict_from_text(
    raw_text: str,
    valid_verdicts=("COMPLETED", "PROCEED", "STOP", "DEFER", "FIXED", "NEW_SESSION", "UNFIXABLE", "PASS", "FAIL", "INCONCLUSIVE")
) -> str:
    """从回复文本中健壮提取决议关键字。"""
    if not raw_text:
        return "NO-VERDICT"
    text = raw_text.lstrip("\ufeff\u200b").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        cleaned_line = line.strip("`*# \t\r\n").rstrip("。.!！:：;,")
        words = [w.strip("`*# \t\r\n").rstrip("。.!！:：;,") for w in cleaned_line.split()]
        if not words:
            continue
        last_word = words[-1].upper()
        if last_word in valid_verdicts:
            return last_word
        for v in valid_verdicts:
            if v in [w.upper() for w in words] or v == cleaned_line.upper():
                return v
    return "NO-VERDICT"


def read_agy_latest_response(cid: str, min_line_idx: int = 0, request_id: Optional[str] = None) -> Optional[AgyResponseResult]:
    """直接从 AGY 本地转录日志 (transcript.jsonl) 中读取最新的 PLANNER_RESPONSE。
    仅检索大于等于 min_line_idx 偏移量的新增记录，并根据 request_id 校验匹配。
    """
    if not cid:
        return None
    brain_dir = get_agy_brain_dir()
    t_path = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
    if not t_path.exists():
        return None
    try:
        lines = t_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= min_line_idx:
            return None
        new_lines = lines[min_line_idx:]

        has_req_input = True
        if request_id:
            has_req_input = any(
                request_id in l for l in new_lines
                if ('"USER_INPUT"' in l or '"USER_EXPLICIT"' in l)
            )

        for line in reversed(new_lines):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if (obj.get("type") == "PLANNER_RESPONSE"
                and obj.get("status") == "DONE"
                and obj.get("content", "").strip()
                and not obj.get("tool_calls")):
                content = obj.get("content", "").strip()
                if request_id and not has_req_input and request_id not in content:
                    continue
                payload = extract_protocol_json(content)
                if payload:
                    v = str(payload.get("verdict", "NO-VERDICT")).upper()
                    return AgyResponseResult(v, content, payload=payload)
                v = parse_verdict_from_text(content)
                if v != "NO-VERDICT":
                    return AgyResponseResult(v, content, payload=None)
    except Exception:
        pass
    return None


def is_agy_working(cid: str) -> Tuple[bool, str]:
    """严格检测 AGY L2 会话当前是否处于活跃工作状态。"""
    if not cid:
        return False, "cid 为空"
    brain_dir = get_agy_brain_dir()
    t_path = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
    if not t_path.exists():
        return False, "转录日志不存在 (视为新建空闲)"
    try:
        st = t_path.stat()
        stale_sec = time.time() - st.st_mtime
        if stale_sec < 2.0:
            return True, f"AGY 正在活跃写盘 (静默仅 {stale_sec:.1f}s < 2.0s)"
        lines = [l.strip() for l in t_path.read_text(encoding="utf-8", errors="replace").splitlines()
                 if l.strip().startswith("{")]
        if not lines:
            return False, "转录日志为空"

        last_obj = json.loads(lines[-1])
        stype = last_obj.get("type")
        status = last_obj.get("status")
        tool_calls = last_obj.get("tool_calls") or []

        if status in ("RUNNING", "IN_PROGRESS"):
            return True, f"步骤执行中 (status={status})"
        if stype == "USER_INPUT":
            return True, "用户输入已进入，等待 AGY 开始响应"
        if stype == "PLANNER_RESPONSE":
            if tool_calls:
                return True, f"AGY 已发起 {len(tool_calls)} 个工具调用，正在执行/等待结果"
            if status == "DONE":
                return False, "AGY 当前回合已全部完成 (DONE 待命)"
        if stype in ("GENERIC", "TOOL_OUTPUT"):
            return True, "工具执行完毕，AGY 正在处理输出继续推理"

        return False, f"最后步骤类型为 {stype} (status={status})"
    except Exception as e:
        return False, f"检测异常: {e}"


def wait_for_agy_idle(cid: str, timeout_sec: float = 60.0) -> bool:
    """阻塞等待 AGY 退出活跃工作状态进入空闲。"""
    if not cid:
        return True
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        is_working, _ = is_agy_working(cid)
        if not is_working:
            return True
        time.sleep(2)
    return False


class AntigravityManager:
    """管理 Antigravity L2 桥接生命周期与单一会话强绑定。"""

    def __init__(self, run_dir: Path, codex_session_id: Optional[str] = None):
        self.run_dir = Path(run_dir).resolve()
        self.codex_session_id = codex_session_id or "unknown"
        self.session_file = self.run_dir / "agy_session.json"
        self.cid: Optional[str] = None
        self.port: Optional[str] = None
        self.spawned_proc: Optional[subprocess.Popen] = None
        self.custom_csrf: Optional[str] = None
        self._load_persisted_cid()

    def set_codex_session_id(self, sid: str):
        self.codex_session_id = sid or "unknown"
        self._load_persisted_cid()

    def _load_persisted_cid(self):
        try:
            if self.session_file.exists():
                data = json.loads(self.session_file.read_text(encoding="utf-8"))
                saved_cid = data.get("agy_conversation_id")
                if saved_cid:
                    self.cid = saved_cid
                    log(f"L2        从历史文件恢复单一AGY会话: {self.cid}")
        except Exception:
            pass

    def persist_cid(self, cid: str):
        self.cid = cid
        try:
            data = {
                "codex_session_id": self.codex_session_id,
                "agy_conversation_id": cid,
                "updated_at": datetime.now().isoformat(timespec="seconds")
            }
            self.session_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log(f"WARN      持久化AGY会话ID失败: {e}")

    def _find_ports_for_pid(self, pid: int) -> List[str]:
        ports = []
        try:
            r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10)
            for line in (r.stdout or "").splitlines():
                if "LISTENING" in line and "127.0.0.1" in line and line.rstrip().endswith(str(pid)):
                    port = line.split()[1].rsplit(":", 1)[-1]
                    if port not in ports:
                        ports.append(port)
        except Exception:
            pass
        if len(ports) > 1:
            ports.sort(key=lambda p: 0 if probe_ls_grpc_port(p) else 1)
        return ports

    def ensure_bridge(self, timeout: int = 30) -> Tuple[Optional[str], List[str], Path]:
        csrf, ports, agexe = discover_antigravity_bridge()
        if csrf and ports and agexe.exists():
            return csrf, ports, agexe

        if not agexe.exists():
            return None, [], agexe

        ws = Path(__file__).resolve().parent.parent.parent
        if self.spawned_proc is None or self.spawned_proc.poll() is not None:
            log("L2        未检测到运行中的Antigravity，启动专用独立后台服务(language_server.exe)...")
            self.custom_csrf = str(uuid.uuid4())
            cmd = [
                str(agexe),
                "--standalone",
                "--app_data_dir=antigravity",
                "--subclient_type=hub",
                "--override_ide_name=antigravity",
                "--override_ide_version=2.12.2",
                "--override_user_agent_name=antigravity",
                f"--csrf_token={self.custom_csrf}",
                "--https_server_port=0",
                "--api_server_url=https://generativelanguage.googleapis.com",
                "--cloud_code_endpoint=https://daily-cloudcode-pa.googleapis.com",
            ]
            cflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            try:
                self.spawned_proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=cflags, cwd=str(ws)
                )
                log(f"L2        独立后台服务已启动 (PID {self.spawned_proc.pid})，等待网关就绪...")
            except Exception as e:
                log(f"L2        启动独立后台服务失败: {e}")
                return None, [], agexe

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            ports = self._find_ports_for_pid(self.spawned_proc.pid)
            if ports:
                log(f"L2        独立后台网关就绪 (端口: {','.join(ports)})")
                return self.custom_csrf, ports, agexe
        log(f"WARN      独立后台网关等待超时({timeout}s)")
        return None, [], agexe

    def teardown(self):
        """安全优雅回收后台语言服务进程。"""
        if self.spawned_proc is not None:
            try:
                if self.spawned_proc.poll() is None:
                    log(f"L2        正在优雅回收后台Antigravity进程 (PID {self.spawned_proc.pid})...")
                    try:
                        if os.name == "nt":
                            os.kill(self.spawned_proc.pid, signal.CTRL_BREAK_EVENT)
                        else:
                            self.spawned_proc.terminate()
                    except Exception:
                        self.spawned_proc.terminate()
                    try:
                        self.spawned_proc.wait(timeout=5)
                        log("L2        后台Antigravity进程已优雅退出")
                    except subprocess.TimeoutExpired:
                        log("L2        优雅退出超时，执行强杀兜底...")
                        subprocess.run(["taskkill", "/PID", str(self.spawned_proc.pid), "/T", "/F"], capture_output=True)
            except Exception as e:
                log(f"WARN      回收后台进程异常: {e}")
            finally:
                self.spawned_proc = None
