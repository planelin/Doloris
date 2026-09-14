#!/usr/bin/env python3
"""
supervise.py — L1/L1.5 看门狗 v0.2 (Claude 无头驱动)
====================================================
监管一个 headless agent (当前驱动: claude CLI) 跑长任务:
  启动 → 心跳监控(会话jsonl mtime) → 异常分类(崩溃/挂死/提前退出)
       → 退避后 resume 同一会话继续
       → 同一供应商连续2次死亡 → 自动切换 cc-switch 供应商池下一个端点(L1.5 换端点)
       → 终态(成功/失败) → 报告

用法:
  python supervise.py --task tasks/selftest/task.md [--chaos kill:120]
                      [--max-resumes 8] [--max-run-sec 3600]

关键设计:
  - 会话ID预先指定(--session-id), 日志路径确定: ~/.claude/projects/<munged-cwd>/<sid>.jsonl
    会话历史是本地文件, 换供应商不影响会话连续性(每轮请求都带全量历史)
  - 中转配置: 只读 cc-switch 数据库取 app_type=claude-desktop 的供应商池
    (当前供应商优先, healthy=1 的排前面, 仅作参考不作保证), env 注入工作进程,
    token 不落盘不打印; 不触碰 ~/.codex / cc-switch 任何文件
  - 心跳阈值按总击杀次数梯度放宽 [150,300,450]s: 区分"慢"与"死"
  - chaos 注入: --chaos kill:NN 一次性模拟真实崩溃
  - 终态只写报告, 不真正关机 (SHUTDOWN WOULD HAPPEN HERE)
"""

import argparse
import atexit
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

HOME = Path.home()
WS = Path(__file__).resolve().parent
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CC_SWITCH_DB = HOME / ".cc-switch" / "cc-switch.db"

ERROR_PATTERNS = [
    "API Error", "429", "500", "502", "503", "504", "Overloaded", "overloaded",
    "Connection error", "ECONNRESET", "ETIMEDOUT", "Credit balance",
    "usage limit", "rate limit", "rate_limit", "stream error", "disconnected",
    "invalid_request_error", "authentication_error",
]

BACKOFFS = [15, 45, 90, 120, 120, 120, 120, 120]  # 秒, resume 之间
STALE_LIMITS = [600, 1200, 1800]                   # 按总击杀次数取值, 尾部封顶
# ↑ 600/1200/1800: mujica4实弹教训——大上下文会话的单次生成/长命令轻松超过5分钟,
#   150s阈值导致4次误杀健康worker(17:54-18:30连续DETECT_HANG)。真死relay时
#   codex自会在~4分钟内自行退出(crash路径), 挂死击杀只是最后手段, 阈值必须宽
FAILS_BEFORE_SWITCH = 2                            # 同供应商连续死亡次数→换端点



def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def munged_cwd(cwd: Path) -> str:
    # Claude Code 项目目录的编码规则: C:\Agents\zcode\double -> C--Agents-zcode-double
    return str(cwd).replace(":", "").replace("\\", "-").replace("_", "-")


def detect_system_proxy():
    """读 Windows 系统代理(IE设置)。中转端点通常必须走系统代理出站,
    直连常被墙(症状=ConnectionRefused/黑洞僵死, 与中转宕机难以区分)。"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
        server, _ = winreg.QueryValueEx(k, "ProxyServer")
        if enable and server:
            return f"http://{server}"
    except Exception:
        pass
    return None


def keep_awake():
    """本进程存活期间阻止系统睡眠(允许熄屏)。
    harness自带的'运行时不睡眠'常只在流式活跃期短暂生效, 空闲计时器一到期系统照睡;
    看门狗自己持有 ES_CONTINUOUS|ES_SYSTEM_REQUIRED 执行状态才是进程级的可靠方案。"""
    if os.name != "nt":
        return
    import ctypes
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    if ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED) == 0:
        log("WARN: SetThreadExecutionState 失败, 睡眠抑制未生效!")
    else:
        log("KEEP-AWAKE 睡眠抑制已生效(进程级, 允许熄屏)")
    # admin 可用时展示当前系统级 sleep 请求, 便于肉眼核实 (bytes 方式避免 GBK 解码炸)
    try:
        r = subprocess.run(["powercfg", "/requests"], capture_output=True,
                           timeout=10)
        if r.returncode == 0 and b"SYSTEM" in r.stdout:
            log("powercfg /requests 可读(管理员), 系统请求清单已可核查")
    except Exception:
        pass


def probe_pool(pool, proxy, keep=3, timeout=45):
    """逐个用 1-token PONG 请求实测供应商(带代理出站), 保留前 keep 个可用者。
    结果缓存 pool-health.json。CLI 兼容性(如 403 group dispatch)只有实测才知道。"""
    working, tried = [], []
    for name, env in pool:
        if len(working) >= keep:
            break
        e = dict(os.environ)
        e.update(env)
        if proxy:
            e["HTTPS_PROXY"] = proxy
            e["HTTP_PROXY"] = proxy
        t0 = time.time()
        proc = subprocess.Popen(
            ["cmd.exe", "/c", "claude", "-p", "Reply with exactly one word: PONG"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=e, cwd=str(WS))
        try:
            out, _ = proc.communicate(timeout=timeout)
            ok = proc.returncode == 0 and "PONG" in (out or "").upper()
        except subprocess.TimeoutExpired:
            # 必须杀整棵树: node 孙进程握着管道会挂住 communicate
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
            proc.wait()
            ok = False
        sec = round(time.time() - t0)
        tried.append((name, ok, sec))
        log(f"PROBE   {name}: {'PASS' if ok else 'FAIL'} ({sec}s)")
        if ok:
            working.append((name, env))
    (WS / "pool-health.json").write_text(json.dumps({
        "ts": datetime.now().isoformat(timespec="seconds"), "proxy": proxy,
        "results": [{"name": n, "ok": o, "sec": s} for n, o, s in tried]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return working


# ---------------------------------------------------------------- cc-switch
def get_relay_pool(app_type="claude-desktop"):
    """只读 cc-switch 数据库, 返回供应商池 [(name, env), ...]。
    当前供应商排最前; 其余按 provider_health(1优先, 顺序其次)排列。
    跳过无 token 的条目(CLI 未登录时无法使用)。永不打印密钥。"""
    if not CC_SWITCH_DB.exists():
        log("WARN: cc-switch 数据库不存在, worker 将以当前环境变量运行")
        return [("(shell-env)", {})]
    con = sqlite3.connect(f"file:{CC_SWITCH_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select id,name,settings_config,is_current from providers where app_type=?",
            (app_type,)).fetchall()
        health = dict(con.execute(
            "select provider_id,is_healthy from provider_health where app_type=?",
            (app_type,)).fetchall())
        current, others = None, []
        for pid, name, cfg, cur in rows:
            try:
                env = json.loads(cfg).get("env", {}) or {}
            except Exception:
                continue
            if not (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")):
                continue
            item = (name, dict(env))
            if cur == 1:
                current = item
            else:
                others.append((health.get(pid) == 1, item))
        others.sort(key=lambda x: not x[0])  # healthy=1 优先, 稳定排序保持原序
        pool = ([current] if current else []) + [i for _, i in others]
        for i, (name, env) in enumerate(pool):
            tag = "当前" if i == 0 else f"备胎{i}"
            log(f"pool[{i}] {tag}: {name} -> "
                f"{env.get('ANTHROPIC_BASE_URL', '(官方)')}")
        return pool if pool else [("(shell-env)", {})]
    finally:
        con.close()


# ---------------------------------------------------------------- 驱动
class ClaudeDriver:
    """claude CLI 无头驱动。预指定 session-id; 池内可切换供应商(同会话续跑)。"""

    def __init__(self, cwd: Path, run_dir: Path, pool, proxy=None):
        self.cwd = cwd
        self.run_dir = run_dir
        self.pool = pool
        self.proxy = proxy
        self.pidx = 0
        self.session_id = str(uuid.uuid4())
        self.jsonl = CLAUDE_PROJECTS / munged_cwd(cwd) / f"{self.session_id}.jsonl"
        self.proc = None

    @property
    def relay_env(self):
        return self.pool[self.pidx][1]

    @property
    def provider_name(self):
        return self.pool[self.pidx][0]

    def has_next(self):
        return self.pidx + 1 < len(self.pool)

    def switch_provider(self):
        if not self.has_next():
            return None
        self.pidx += 1
        return self.provider_name

    def _spawn(self, args, stdin_path: Path):
        env = dict(os.environ)
        env.update(self.relay_env)  # 只影响本工作进程
        if self.proxy:  # 中转出站必须走系统代理, 否则直连裸奔(被墙=黑洞/拒连)
            env["HTTPS_PROXY"] = self.proxy
            env["HTTP_PROXY"] = self.proxy
        out = open(self.run_dir / "worker-stdout.log", "ab")
        err = open(self.run_dir / "worker-stderr.log", "ab")
        self.proc = subprocess.Popen(
            ["cmd.exe", "/c", "claude", *args],
            cwd=str(self.cwd), env=env,
            stdin=open(stdin_path, "rb"), stdout=out, stderr=err,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        return self.proc

    def launch(self, prompt_path: Path):
        log(f"LAUNCH session={self.session_id[:8]} 供应商={self.provider_name}")
        return self._spawn(
            ["-p", "--output-format", "text",
             "--permission-mode", "acceptEdits",
             "--session-id", self.session_id],
            prompt_path,
        )

    def resume(self, prompt_path: Path):
        log(f"RESUME  session={self.session_id[:8]} 供应商={self.provider_name}")
        return self._spawn(
            ["-p", "--output-format", "text",
             "--permission-mode", "acceptEdits",
             "--resume", self.session_id],
            prompt_path,
        )

    def kill_tree(self, timeout=5):
        if self.proc and self.proc.poll() is None:
            try:
                self.interrupt()
                if self.wait_exit(timeout):
                    log(f"KILL    进程树 pid={self.proc.pid} 已优雅退出")
                    return
            except Exception:
                pass
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           capture_output=True)
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass
            log(f"KILL    进程树 pid={self.proc.pid} 已强制终止")

    def heartbeat_age(self, launched_at) -> float:
        if self.jsonl.exists():
            return time.time() - self.jsonl.stat().st_mtime
        return time.time() - launched_at  # 日志还没出现, 从启动算起

    def interrupt(self):
        """优雅中断: CTRL_BREAK 到独立进程组, claude 自行收尾当前操作。"""
        if self.proc and self.proc.poll() is None:
            try:
                os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
                return True
            except Exception:
                return False
        return False

    def wait_exit(self, timeout=15):
        try:
            self.proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def rollout_size(self):
        try:
            return self.jsonl.stat().st_size if self.jsonl.exists() else None
        except OSError:
            return None

    def rollout_since(self, offset):
        try:
            with open(self.jsonl, "rb") as f:
                f.seek(offset)
                return f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""


CODEX_SESSIONS = HOME / ".codex" / "sessions"
CODEX_LOCKS = HOME / ".codex" / "thread-writer-locks"


def _json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s


def _read_meta(p: Path):
    """读 rollout 头部 session_meta, 返回 (session_id, cwd) 或 None。"""
    try:
        head = p.open("r", encoding="utf-8", errors="replace").read(4096)
    except OSError:
        return None
    m_sid = re.search(r'"session_id":"([^"]+)"', head)
    if not m_sid:
        return None
    m_cwd = re.search(r'"cwd":"((?:[^"\\]|\\.)*)"', head)
    cwd = _json_str(m_cwd.group(1)) if m_cwd else None
    return m_sid.group(1), cwd


def codex_app_running():
    """检测 Codex 桌面App是否存活: Electron UI 进程名 ChatGPT*,
    持锁的 app-server 进程名 codex*。返回 True/False; None=检测失败。"""
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=15)
    except Exception:
        return None
    out = r.stdout.decode("gbk", errors="replace").lower()
    for line in out.splitlines():
        name = line.split('","')[0].strip('"').strip()
        if name.startswith("codex") or name.startswith("chatgpt"):
            return True
    return False


def read_session_title(p: Path, scan=262144):
    """从 rollout 提取任务标题(首条真实用户输入, 截断60字)。兼容三种形态:
    user消息 content / input_text条目 / queue-operation enqueue。
    跳过 < 开头的环境注入内容; 扫描256KB(桌面会话头部环境块很大)。"""
    try:
        head = p.open("r", encoding="utf-8", errors="replace").read(scan)
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


def list_recent_codex_sessions(n=5):
    """最近 n 个会话, 按活跃时间降序。
    返回 [(sid, rollout_path, session_cwd, title, age_str), ...]"""
    items = []
    if not CODEX_SESSIONS.exists():
        return items
    for p in CODEX_SESSIONS.rglob("rollout-*.jsonl"):
        meta = _read_meta(p)
        if not meta:
            continue
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        items.append((mt, meta[0], p, meta[1]))
    items.sort(reverse=True)
    out = []
    for mt, sid, p, scwd in items[:n]:
        title = read_session_title(p)
        age = time.strftime("%m-%d %H:%M", time.localtime(mt))
        out.append((sid, p, scwd, title, age))
    return out


def rollout_tail_state(path, tail=16384):
    """解析rollout尾部事件状态机, 判定中断安全性:
    unsafe = 存在未回结果的工具调用(执行中, 硬杀可能留半成品)
    safe   = 最后事件在模型侧(生成/思考/工具间隙, 无文件操作进行)
    unknown = 读不到/解析不出(视为可直接处理)"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return "unknown"
    pending = False
    seen = False
    for line in data.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue  # 截断的半行
        seen = True
        blob = json.dumps(obj, ensure_ascii=False)
        if ('"function_call_output"' in blob or '"tool_result"' in blob
                or '"local_shell_output"' in blob):
            pending = False
        elif ('"function_call"' in blob or '"custom_tool_call"' in blob
              or '"local_shell_call"' in blob or '"tool_use"' in blob):
            pending = True
    if not seen:
        return "unknown"
    return "unsafe" if pending else "safe"


def close_codex_app(rollout_path=None, max_wait=40):
    """结束 Codex 桌面App进程族, 单写者锁随进程消亡。
    边界感知: 解析rollout尾部事件状态机, 等到最后事件不在工具执行中(≤max_wait),
    再两段式关闭(礼貌WM_CLOSE→8秒→强杀残余)。
    仅允许在【尚无我方worker】的接管准备阶段调用。"""
    if rollout_path and Path(rollout_path).exists():
        deadline = time.time() + max_wait
        while time.time() < deadline:
            st = rollout_tail_state(rollout_path)
            if st != "unsafe":
                log(f"CLOSE    事件边界确认({st}), 开始关闭App")
                break
            time.sleep(1)
    killed = []
    for img in ("ChatGPT.exe", "codex.exe"):
        subprocess.run(["taskkill", "/IM", img], capture_output=True)  # 礼貌关闭
    deadline = time.time() + 8
    while time.time() < deadline:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=15)
        alive = ("chatgpt" in r.stdout.decode("gbk", errors="replace").lower()
                 or "codex" in r.stdout.decode("gbk", errors="replace").lower())
        if not alive:
            break
        time.sleep(1)
    for img in ("ChatGPT.exe", "codex.exe"):
        r = subprocess.run(["taskkill", "/IM", img, "/F"], capture_output=True)
        if r.returncode == 0:
            killed.append(img)
    time.sleep(1)
    return killed


def safe_kill(driver, max_wait=30):
    """无害击杀: 解析rollout事件状态机, 等到最后事件不在工具执行中(≤max_wait)
    → CTRL_BREAK优雅中断 → 硬杀兜底。返回实际方式。"""
    rp = getattr(driver, "jsonl", None)
    deadline = time.time() + max_wait
    while rp is not None and time.time() < deadline:
        if rollout_tail_state(rp) == "safe":
            driver.interrupt()
            if driver.wait_exit(12):
                driver.kill_tree()
                return "boundary"
            driver.kill_tree()
            return "boundary+hard"
        time.sleep(1)
    if driver.interrupt() and driver.wait_exit(15):
        driver.kill_tree()
        return "graceful"
    driver.kill_tree()
    return "hard"


def find_last_codex_session(cwd=None):
    """找最近活跃的 codex 会话: 全局按 rollout mtime 降序取最新。
    'last'的语义 = 用户最近在用的那个(正在生成的会话 mtime 必然最新);
    cwd 只用于日志提示, 不作筛选——否则真实项目在其他目录时,
    目录偏好会错误命中本目录的陈旧测试会话。
    返回 (session_id, rollout_path, session_cwd) 或 None。"""
    best = None
    if not CODEX_SESSIONS.exists():
        return None
    for p in CODEX_SESSIONS.rglob("rollout-*.jsonl"):
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
        log(f"ADOPT   最新会话 cwd={scwd} (非{WS}), 验收锚点随之转移")
    return sid, p, scwd


def find_codex_session_by_id(sid: str):
    if not CODEX_SESSIONS.exists():
        return None
    for p in CODEX_SESSIONS.rglob("rollout-*.jsonl"):
        meta = _read_meta(p)
        if meta and meta[0] == sid:
            return p, meta[1]
    return None


def find_connected_adapter():
    """netsh 找已连接的网卡名 (GBK/UTF-8 双解码容错)。"""
    try:
        r = subprocess.run(["netsh", "interface", "show", "interface"],
                           capture_output=True, timeout=10)
    except Exception:
        return None
    out = ""
    for enc in ("gbk", "utf-8"):
        try:
            out = r.stdout.decode(enc)
            break
        except Exception:
            continue
    for line in out.splitlines():
        if ("已连接" in line or "connected" in line.lower()) and line.strip():
            cols = line.split()
            if len(cols) >= 4:
                return cols[-1]
    return None


def net_disable(adapter):
    r = subprocess.run(["netsh", "interface", "set", "interface",
                        f"interface={adapter}", "admin=disable"],
                       capture_output=True, timeout=15)
    return r.returncode == 0


def net_enable(adapter):
    subprocess.run(["netsh", "interface", "set", "interface",
                    f"interface={adapter}", "admin=enable"],
                   capture_output=True, timeout=15)


# ------------------------------------------------- L2-antigravity 桥接通路
ANTIGRAVITY_PROMPT_TMPL = """你是监管系统的L2诊断修复agent(独立于故障的codex中转通道)。

背景: 一个 codex CLI 无头 worker 在接管会话 {sid} (cwd={scwd}) 中反复失败,
L1看门狗已耗尽预算。最近错误:
{errors}

worker stderr 尾部:
{err_tail}

可用修复动作(你有文件与命令权限, 直接执行):
1. 中转层修复: 读写 ~/.codex/config.toml 与 auth 配置, 可从 ~/.cc-switch/cc-switch.db
   (只读) 的 providers/provider_endpoints 表读取备选端点与key, 改写配置绕开故障路由
2. 若判断为会话上下文污染: 决议输出 NEW_SESSION
3. 若判断为基础设施故障且无可为: 决议输出 UNFIXABLE 并附一句原因
约束: 不要动 ~/.codex/sessions/ 下的会话轨迹; 改配置前先复制原文件为 *.bak_{n}。

【重要】完成全部工作后, 你必须把最终决议(单独一个词: FIXED 或 NEW_SESSION 或
UNFIXABLE)写入这个文件: {verdict_file}
"""


class AntigravityManager:
    """管理 Antigravity L2 桥接生命周期与单一会话强绑定。
    - 优先复用系统中已有运行的 Antigravity 实例 (开发/调试时可视)
    - 若未运行，在后台直接启动轻量语言服务核心 (language_server.exe --standalone)，
      完全规避与桌面端 Electron 的单实例互斥锁，用户前台可自由开关 App。
    - 严格保持 1 Codex Session <-> 1 AGY Conversation 映射:
      首次 new-conversation 获得 cid 后，写入内存并落盘至 runs/<ts>/agy_session.json;
      后续交互一律走 send-message 增量推进;
    - 任务结束或异常退出时，两段式优雅回收自主拉起的独立后台进程，保护 SQLite WAL。
    """

    def __init__(self, run_dir: Path, codex_session_id: str = None):
        self.run_dir = run_dir
        self.codex_session_id = codex_session_id or "unknown"
        self.session_file = run_dir / "agy_session.json"
        self.cid = None
        self.port = None
        self.spawned_proc = None
        self.custom_csrf = None
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

    def _find_ports_for_pid(self, pid: int) -> list:
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
        return ports

    def ensure_bridge(self, timeout=30):
        """确保 Antigravity 桥可用: 优先复用已有实例; 没有则独立启动后台 language_server。"""
        csrf, ports, agexe = discover_antigravity_bridge()
        if csrf and ports and agexe.exists():
            return csrf, ports, agexe

        if not agexe.exists():
            return None, [], agexe

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
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=cflags,
                    cwd=str(WS)
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
        """两段式安全优雅回收后台语言服务进程 (保护 SQLite WAL 与未结事务)"""
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
                        subprocess.run(["taskkill", "/PID", str(self.spawned_proc.pid), "/T", "/F"],
                                       capture_output=True)
            except Exception as e:
                log(f"WARN      回收后台进程异常: {e}")
            finally:
                self.spawned_proc = None


def discover_antigravity_bridge():
    """动态发现Antigravity桥: csrf(App日志最新spawn行) + LS网关端口(netstat)。"""
    csrf = None
    logf = Path(os.environ.get("APPDATA", "")) / "Antigravity" / "logs" / "main.log"
    if logf.exists():
        toks = re.findall(r"--csrf_token ([0-9a-f-]{36})",
                          logf.read_text(encoding="utf-8", errors="replace"))
        csrf = toks[-1] if toks else None
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "(Get-Process language_server -ErrorAction SilentlyContinue).Id"],
                       capture_output=True, text=True, timeout=25)
    pids = [int(x) for x in (r.stdout or "").split() if x.isdigit()]
    ports = []
    if pids:
        r2 = subprocess.run(["netstat", "-ano"], capture_output=True,
                            text=True, timeout=25)
        for line in (r2.stdout or "").splitlines():
            if "LISTENING" in line and "127.0.0.1" in line and any(
                    line.rstrip().endswith(str(pid)) for pid in pids):
                port = line.split()[1].rsplit(":", 1)[-1]
                if port not in ports:
                    ports.append(port)
    agexe = (Path(os.environ.get("LOCALAPPDATA", "")) /
             "Programs/antigravity/resources/bin/language_server.exe")
    return csrf, ports, agexe


def discover_antigravity_project_id(agexe, csrf, ports):
    """取最近一个会话元数据里的 projectId (App每次运行随机换端口/CSRF, 项目较稳定)。"""
    conv_dir = HOME / ".gemini" / "antigravity" / "conversations"
    if not conv_dir.exists():
        return None
    dbs = sorted(conv_dir.glob("*.db"), key=lambda x: x.stat().st_mtime, reverse=True)
    for port in ports:
        env = dict(os.environ)
        env["ANTIGRAVITY_CSRF_TOKEN"] = csrf
        env["ANTIGRAVITY_LS_ADDRESS"] = f"127.0.0.1:{port}"
        for db in dbs[:3]:
            try:
                r = subprocess.run(
                    [str(agexe), "agentapi", "get-conversation-metadata",
                     db.stem], capture_output=True, timeout=60, env=env,
                     cwd=str(WS))
                m = re.search(r'"projectId"\s*:\s*"([0-9a-f-]{36})"',
                              (r.stdout or b"").decode("utf-8", errors="replace"))
                if m:
                    return m.group(1)
            except Exception:
                continue
    return None


def run_l2_antigravity(run_dir, full_prompt, short_prompt, n, scwd, verdict_file,
                       conv_holder=None, project_id=None, model="flash",
                       timeout_sec=1800, log_name=None, agy_mgr=None):
    """经agentapi桥调用Antigravity。一个codex会话对应一个agy会话:
    首次 new-conversation(全量背景) 并记住conversationId,
    后续 send-message(增量提问) 复用同一会话——agy保持上下文不再重读。
    连接类错误(端口gRPC间歇EOF)保留cid换端口重试send;
    仅会话不存在才降级重建。App重启导致会话失效时自动降级重建。
    轮询verdict文件回收输出。"""
    verdict_file = Path(scwd) / verdict_file
    log_path = run_dir / f"{log_name or 'l2'}.log"
    if agy_mgr is not None:
        csrf, ports, agexe = agy_mgr.ensure_bridge()
    else:
        csrf, ports, agexe = discover_antigravity_bridge()
    if not csrf or not ports or not agexe.exists():
        with open(log_path, "wb") as f:
            f.write("NO-BRIDGE: 未发现运行中的Antigravity language_server".encode("utf-8"))
        return "NO-BRIDGE", "NO-BRIDGE", log_path
    verdict_file.parent.mkdir(parents=True, exist_ok=True)
    if verdict_file.exists():
        verdict_file.unlink()

    cid = getattr(conv_holder, "_agy_cid", None) if conv_holder else None
    if not cid and agy_mgr is not None:
        cid = agy_mgr.cid
        if cid and conv_holder is not None:
            conv_holder._agy_cid = cid

    good_port = getattr(conv_holder, "_agy_port", None) if conv_holder else None
    if not good_port and agy_mgr is not None:
        good_port = agy_mgr.port

    if good_port and good_port in ports:
        ports.remove(good_port)
        ports.insert(0, good_port)  # 上次成功的端口优先
    env = dict(os.environ)
    env["ANTIGRAVITY_CSRF_TOKEN"] = csrf
    if project_id:
        env["ANTIGRAVITY_PROJECT_ID"] = project_id
    last_err = ""
    for port in ports:
        env["ANTIGRAVITY_LS_ADDRESS"] = f"127.0.0.1:{port}"
        try:
            if cid:
                r = subprocess.run(
                    [str(agexe), "agentapi", "send-message", cid, short_prompt],
                    capture_output=True, timeout=120, env=env, cwd=str(WS))
            else:
                r = subprocess.run(
                    [str(agexe), "agentapi", "new-conversation",
                     f"--model={model}", full_prompt],
                    capture_output=True, timeout=120, env=env, cwd=str(WS))
        except subprocess.TimeoutExpired:
            last_err = "调用超时"
            continue
        out = (r.stdout or b"").decode("utf-8", errors="replace")
        mode = "send" if cid else "new"
        with open(log_path, "ab") as f:
            f.write(f"--- port {port} {mode} ---\n{out}\n".encode("utf-8",
                                                                 errors="replace"))
        if '"error"' in out:
            last_err = out[:300]
            transient = ("Unavailable" in out or "connection error" in out
                         or "server preface" in out)
            stale = (cid and ("not found" in out.lower()
                              or "invalid" in out.lower()
                              or "no such" in out.lower()))
            if stale:
                cid = None  # 会话确实失效, 降级重建
                if conv_holder is not None:
                    conv_holder._agy_cid = None
                if agy_mgr is not None:
                    agy_mgr.persist_cid("")
                continue
            if transient and cid:
                continue  # 瞬时连接故障: 保留cid换下一端口重试send
            continue
        m = re.search(r'"conversationId"\s*:\s*"([^"]+)"', out)
        if m:
            new_cid = m.group(1)
            if conv_holder is not None:
                conv_holder._agy_cid = new_cid
                conv_holder._agy_port = port
            if agy_mgr is not None:
                agy_mgr.persist_cid(new_cid)
                agy_mgr.port = port
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            time.sleep(20)
            if verdict_file.exists():
                v = verdict_file.read_text(encoding="utf-8", errors="replace")
                v = v.lstrip("\ufeff\u200b").strip()
                verdict = v.split()[-1] if v else "NO-VERDICT"
                return verdict, v, log_path
        last_err = "verdict文件超时未出现"
        break
    with open(log_path, "ab") as f:
        f.write(f"\n[L2-ANTIGRAVITY-FAIL] {last_err}\n".encode("utf-8", errors="replace"))
    return "NO-VERDICT", last_err, log_path


# ---------------------------------------------------------------- L2 升级agent
ASK_MARKERS = ("【决策请求】", "【需要决策】", "[决策请求]",
               "需要用户确认", "请确认后继续", "请选择以下", "等待你的指示")

L2_INTERACT_TMPL = """你是监管系统的L2决策代理, 用户暂时不在场, 由你代表用户为 codex worker 的决策请求拍板。

任务上下文: 会话 {sid} (cwd={scwd})

worker的决策请求原文:
{question}

委托策略(代表用户决策的边界):
- 技术方向/实现方案/参数选择/路径取舍/依赖选型: 你直接决定, 给出明确、可直接执行的指令并说明理由
- 涉及花钱、删除数据、对外发布、不可逆破坏性操作: 不要拍板, 最后一行输出 DEFER

【输出规范】
请简要说明决定与理由，随后务必包含一段明确的「给 Codex Worker 的执行指令：」，列出 worker 接下来应执行的具体动作。
最后一行单独输出一个词: PROCEED 或 DEFER。
"""

L2_PROMPT_TMPL = """你是监管系统的L2诊断修复agent, 运行在稳定通道上(与故障的codex中转无关)。
背景: 一个 codex CLI 无头 worker 在接管会话 {sid} (cwd={scwd}) 中反复失败,
L1看门狗(退避重试/换端点)已耗尽预算。你是最后一道自动防线。

最近错误签名:
{errors}

worker stderr 尾部:
{err_tail}

可用修复动作(直接执行, 你有文件与命令权限):
1. 中转层修复: 读写 ~/.codex/config.toml 与 auth 配置, 可从 ~/.cc-switch/cc-switch.db
   (只读) 的 providers/provider_endpoints 表读取备选端点与key, 直接改写配置绕过故障路由
2. 沙箱修复(已知坑): worker可能以只读沙箱被无头resume(桌面创建的会话继承此设置),
   表现为"无法写入任何文件/系统强制只读", 修复 = 将 ~/.codex/config.toml 的
   sandbox_mode 改为 workspace-write
3. 若判断为会话上下文污染: 在最终回复中单独一行输出 NEW_SESSION
4. 若判断为基础设施故障且无可为: 输出 UNFIXABLE 并附一句原因

约束: 不要动 ~/.codex/sessions/ 下的会话轨迹文件; 修改配置前把原文件复制为 *.bak_{n}。
完成后, 在最终回复的最后一行单独输出决议: FIXED 或 NEW_SESSION 或 UNFIXABLE。
"""


def clean_l2_decision_text(raw_text: str) -> str:
    """清洗L2决策代答文本，去除前导分析与尾部控制标记，提取纯净的执行指令回喂worker。"""
    if not raw_text:
        return "继续，自行决定并完成剩余工作。\n"
    text = raw_text.lstrip("\ufeff\u200b").strip()
    lines = [line.rstrip() for line in text.splitlines()]

    # 去除首尾的 ``` 标记与空行
    while lines and (lines[0].strip().startswith("```") or not lines[0].strip()):
        lines.pop(0)
    while lines and (lines[-1].strip().startswith("```") or not lines[-1].strip()):
        lines.pop()

    # 过滤末尾单独成行的 PROCEED / DEFER
    while lines and (lines[-1].strip().upper() in ("PROCEED", "DEFER") or not lines[-1].strip()):
        lines.pop()

    # 再次去除可能由 PROCEED 前后包裹的代码块闭合 ```
    while lines and (lines[-1].strip().startswith("```") or not lines[-1].strip()):
        lines.pop()

    # 寻找明确的执行指令起始段
    instruction_markers = [
        "给 Codex Worker 的执行指令：",
        "给 Codex Worker 的执行指令:",
        "给worker的执行指令：",
        "给worker的执行指令:",
        "【给 Codex Worker 的执行指令】",
        "【执行指令】",
        "执行指令：",
        "执行指令:",
        "【行动指令】",
        "行动指令：",
        "行动指令:"
    ]
    marker_idx = -1
    for idx, line in enumerate(lines):
        for m in instruction_markers:
            if m in line:
                marker_idx = idx
                break
        if marker_idx != -1:
            break

    if marker_idx != -1:
        instruction_lines = lines[marker_idx:]
        cleaned = "\n".join(instruction_lines).strip()
    else:
        cleaned = "\n".join(lines).strip()

    # 过滤掉内容中残留的独立代码块标记行
    cleaned_lines = [l for l in cleaned.splitlines() if not l.strip().startswith("```")]
    return "\n".join(cleaned_lines).strip() + "\n"


def run_l2_agent(l2_cmd, run_dir, prompt, n, proxy):
    """以无头模式调用L2 agent。claude 需要中转env+系统代理注入; 其他命令原样运行。
    返回 (verdict, log_path)。"""
    log_path = run_dir / f"l2-{n}.log"
    prompt_file = run_dir / f"l2-{n}-prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    parts = l2_cmd.split()
    env = dict(os.environ)
    if parts[0].lower() == "claude":
        pool = get_relay_pool("claude-desktop")
        if pool:
            env.update(pool[0][1])
        if proxy:
            env["HTTPS_PROXY"] = proxy
            env["HTTP_PROXY"] = proxy
    args = ["cmd.exe", "/c", *parts, "-p"]
    try:
        r = subprocess.run(args, stdin=open(prompt_file, "rb"),
                           stdout=open(log_path, "wb"), stderr=subprocess.STDOUT,
                           env=env, cwd=str(WS), timeout=900)
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        ok = False
        with open(log_path, "ab") as f:
            f.write(b"\n[L2 TIMEOUT 900s]\n")
    text = ""
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="replace")
    verdict = "NO-VERDICT"
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.endswith(("FIXED", "NEW_SESSION", "UNFIXABLE",
                          "PROCEED", "DEFER")):
            verdict = line.split()[-1]
            break
    return verdict, text, log_path


def worker_last_message(run_dir):
    """worker 最后留言: codex 走 -o 落盘文件; claude 取 stdout 尾部。"""
    lm = run_dir / "codex-last-message.txt"
    try:
        if lm.exists():
            return lm.read_text(encoding="utf-8", errors="replace")[-1500:]
    except OSError:
        pass
    try:
        out = run_dir / "worker-stdout.log"
        return (out.read_text(encoding="utf-8", errors="replace")[-1500:]
                if out.exists() else "")
    except OSError:
        return ""


def l2_dispatch(l2_cmd, args, proxy, run_dir, driver, session_cwd, n,
                outcome_detail, errors_text, err_tail, kind="repair", agy_mgr=None):
    """按通道与种类路由调用L2 agent, 返回 (verdict, text, log_path)。
    kind=repair: 诊断修复(决议: FIXED/NEW_SESSION/UNFIXABLE)
    kind=interaction: 代答决策请求(决议: PROCEED/DEFER, text=给codex的指令)"""
    if l2_cmd.lower() in ("antigravity", "agy"):
        if kind == "interaction":
            vfile = "afk-l2-answer.txt"
            full = L2_INTERACT_TMPL.format(sid=driver.session_id,
                                           scwd=session_cwd,
                                           question=errors_text)
            full += (f"\n【交付】把你的完整决定(可直接发给codex执行的指令)写入文件: "
                     f"{Path(session_cwd) / vfile}，并在最后一行单独输出: PROCEED 或 DEFER。")
            short = (f"新的决策请求:\n{errors_text}\n【交付】把完整决定写入 "
                     f"{Path(session_cwd) / vfile}，最后一行单独输出: PROCEED 或 DEFER。")
        else:
            vfile = "afk-l2-verdict.txt"
            full = L2_PROMPT_TMPL.format(sid=driver.session_id, scwd=session_cwd,
                                         errors=errors_text,
                                         err_tail=err_tail or "(无)", n=n)
            full += (f"\n【交付】把最终决议(单独一词: FIXED/NEW_SESSION/UNFIXABLE)"
                     f"写入文件: {Path(session_cwd) / vfile}。")
            short = (f"新的故障情况:\n{errors_text}\nstderr尾部:\n{err_tail or '(无)'}\n"
                     f"【交付】把最终决议(FIXED/NEW_SESSION/UNFIXABLE)写入 "
                     f"{Path(session_cwd) / vfile}。")
        project_id = args.l2_project_id or None
        if not project_id:
            if agy_mgr is not None:
                csrf, ports, agexe = agy_mgr.ensure_bridge()
            else:
                csrf, ports, agexe = discover_antigravity_bridge()
            if csrf and ports:
                project_id = discover_antigravity_project_id(agexe, csrf, ports)
                log(f"L2        项目id自动发现: {project_id or '失败'}")
        return run_l2_antigravity(run_dir, full, short, n, session_cwd, vfile,
                                  conv_holder=driver, project_id=project_id,
                                  model=args.l2_model, agy_mgr=agy_mgr)
    if kind == "interaction":
        prompt = L2_INTERACT_TMPL.format(sid=driver.session_id,
                                         scwd=session_cwd, question=errors_text)
    else:
        prompt = L2_PROMPT_TMPL.format(sid=driver.session_id, scwd=session_cwd,
                                       errors=errors_text,
                                       err_tail=err_tail or "(无)", n=n)
    return run_l2_agent(l2_cmd, run_dir, prompt, n, proxy)


def wait_session_quiet(rollout: Path, quiet_sec=15, max_wait=90):
    """接管前等待原会话静默: 同一会话不能有两个写入者。"""
    log(f"ADOPT   等待会话静默(最多{max_wait}s)——请确认原界面已停止输入")
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            m1 = rollout.stat().st_mtime
        except OSError:
            return True
        time.sleep(quiet_sec)
        try:
            m2 = rollout.stat().st_mtime
        except OSError:
            return True
        if m1 == m2:
            log("ADOPT   会话已静默, 开始接管")
            return True
        log("  会话仍在活动, 继续等待...")
    log("WARN    静默等待超时, 强行接管(若原界面仍开着, 请立即关闭它!)")
    return False


BACKUP_EXCLUDE_DIRS = {
    ".git", ".svn", ".hg", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".codex", ".idea", ".vscode", "dist", "build",
    ".next", ".nuxt", "target", "bin", "obj",
}


def backup_workspace(scwd: Path, run_dir: Path, max_size_mb: int = 300) -> Path | None:
    """在接管前对目标工作区做一次轻量快照备份。
    安全设计:
      - 仅读 scwd, 产物严格保存在 run_dir/ (如 runs/<ts>/backup-pre-adopt-<proj>-<ts>.zip), 零侵入目标工程
      - 自动忽略 .git, node_modules, .venv, __pycache__, .codex 等巨型/派生目录
      - 单文件 > 50MB 忽略, 整体解压前容量超限熔断, 避免拖慢接管或撑爆磁盘
      - 发生任何异常只打印告警, 绝不阻断接管主流程
    """
    if not scwd.exists() or not scwd.is_dir():
        log(f"BACKUP  目标目录不存在或非目录, 跳过备份: {scwd}")
        return None

    proj_name = scwd.name or "workspace"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = run_dir / f"backup-pre-adopt-{proj_name}-{ts}.zip"

    max_bytes = max_size_mb * 1024 * 1024
    total_bytes = 0
    file_count = 0
    skipped_large = 0

    log(f"BACKUP  正在对工作区进行快照备份: {scwd} -> {archive_path.name}")
    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(scwd):
                dirs[:] = [d for d in dirs if d.lower() not in BACKUP_EXCLUDE_DIRS]
                for file in files:
                    fp = Path(root) / file
                    try:
                        st = fp.stat()
                        if st.st_size > 50 * 1024 * 1024:
                            skipped_large += 1
                            continue
                        if total_bytes + st.st_size > max_bytes:
                            log(f"BACKUP  工作区快照达到上限 ({max_size_mb}MB), 停止追加剩余文件")
                            break
                        rel_path = fp.relative_to(scwd)
                        zf.write(fp, arcname=str(rel_path))
                        total_bytes += st.st_size
                        file_count += 1
                    except (OSError, PermissionError):
                        continue
                if total_bytes > max_bytes:
                    break
        if file_count == 0:
            log("BACKUP  工作区为空或所有文件均被过滤, 无需备份")
            try:
                archive_path.unlink()
            except OSError:
                pass
            return None

        size_mb = archive_path.stat().st_size / (1024 * 1024)
        log(f"BACKUP  工作区快照完成: {archive_path.name} ({file_count} 个文件, 压缩后 {size_mb:.2f}MB, 过滤超大文件: {skipped_large})")
        return archive_path
    except Exception as e:
        log(f"WARN    工作区快照备份异常: {e}")
        try:
            if archive_path.exists():
                archive_path.unlink()
        except OSError:
            pass
        return None


class CodexDriver:
    """codex CLI 驱动 (codex exec / codex exec resume)。
    与 claude 驱动的差异:
      - 无 --session-id 预分配: 启动后扫 ~/.codex/sessions 最新 rollout 锁定会话,
        session_id 取 rollout 首行 session_meta (权威)
      - resume 会新建 rollout 文件或续写原文件 → 每次 spawn 后重新发现 jsonl
      - 出站走 cc-switch 本地代理(127.0.0.1:15721/v1, localhost), 无需注入系统代理;
        供应商故障转移由 cc-switch 本地代理的 endpoint 自动选择负责, 驱动内不再轮换
      - -o 参数把 agent 最终消息落盘, 供完成判定与报告使用
    """

    def __init__(self, cwd: Path, run_dir: Path):
        self.cwd = cwd
        self.run_dir = run_dir
        self.session_id = None          # 由 rollout session_meta 发现
        self.jsonl = None               # 心跳文件, 每次 spawn 后重新发现
        self.proc = None
        self.last_msg = run_dir / "codex-last-message.txt"

    @property
    def provider_name(self):
        return "codex(cc-switch本地代理)"

    def has_next(self):
        return False

    def _spawn_once(self, args, stdin_path: Path):
        out = open(self.run_dir / "worker-stdout.log", "ab")
        err = open(self.run_dir / "worker-stderr.log", "ab")
        self.proc = subprocess.Popen(
            ["cmd.exe", "/c", "codex", *args],
            cwd=str(self.cwd), stdin=open(stdin_path, "rb"),
            stdout=out, stderr=err,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

    def _common(self):
        return ["-C", str(self.cwd), "-s", "workspace-write",
                "-c", "sandbox_workspace_write.network_access=true",
                "--skip-git-repo-check", "--json", "-o", str(self.last_msg)]

    def launch(self, prompt_path: Path):
        self._spawn_once(["exec", *self._common(), "-"], prompt_path)
        log("LAUNCH codex exec (session运行时发现)")
        return self.proc

    def resume(self, prompt_path: Path):
        if not self.session_id:
            raise RuntimeError("resume 前必须先发现 session_id")
        self.jsonl = None  # resume 可能新建 rollout, 重新发现
        # 显式指定 -C self.cwd, 锁定工作区跟原会话物理路径绝对一致, 杜绝codex切换工作区
        # 并接受 -c 配置覆盖: 显式改写沙箱为可写
        self._spawn_once(["exec", "-C", str(self.cwd), "resume", self.session_id,
                          "-c", "sandbox_mode=workspace-write",
                          "-c", "approval_policy=never",
                          "-c", "sandbox_workspace_write.network_access=true",
                          "--skip-git-repo-check", "--json",
                          "-o", str(self.last_msg), "-"], prompt_path)
        log(f"RESUME  codex session={self.session_id[:8]} cwd={self.cwd}")
        return self.proc

    def discover_session(self, since_ts) -> bool:
        """扫描 sessions 目录, 找 since_ts 之后新建的 rollout;
        若无新文件但已知 jsonl 仍存在(续写场景), 沿用之。"""
        cands = []
        if CODEX_SESSIONS.exists():
            for p in CODEX_SESSIONS.rglob("rollout-*.jsonl"):
                try:
                    c = p.stat().st_ctime
                except OSError:
                    continue
                if c >= since_ts - 2:
                    cands.append((c, p))
        if cands:
            _, p = max(cands)
            if p != self.jsonl:
                self.jsonl = p
                try:
                    head = p.open("r", encoding="utf-8", errors="replace").read(2048)
                    m = re.search(r'"session_id":"([^"]+)"', head)
                    if m and not self.session_id:
                        self.session_id = m.group(1)
                        log(f"SESSION 发现 id={self.session_id[:8]} file={p.name[:60]}")
                except OSError:
                    pass
            return True
        return self.jsonl is not None  # 续写场景: 无新文件, 沿用已知 jsonl

    def kill_tree(self, timeout=5):
        if self.proc and self.proc.poll() is None:
            try:
                self.interrupt()
                if self.wait_exit(timeout):
                    log(f"KILL    进程树 pid={self.proc.pid} 已优雅退出")
                    return
            except Exception:
                pass
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           capture_output=True)
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass
            log(f"KILL    进程树 pid={self.proc.pid} 已强制终止")

    def heartbeat_age(self, launched_at) -> float:
        if self.jsonl and self.jsonl.exists():
            return time.time() - self.jsonl.stat().st_mtime
        return time.time() - launched_at  # rollout 未出现, 从启动算起

    def interrupt(self):
        """优雅中断: CTRL_BREAK 到独立进程组, codex 自行收尾当前操作。"""
        if self.proc and self.proc.poll() is None:
            try:
                os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
                return True
            except Exception:
                return False
        return False

    def wait_exit(self, timeout=15):
        try:
            self.proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def rollout_size(self):
        try:
            return self.jsonl.stat().st_size if (self.jsonl and self.jsonl.exists()) else None
        except OSError:
            return None

    def rollout_since(self, offset):
        if not (self.jsonl and self.jsonl.exists()):
            return ""
        try:
            with open(self.jsonl, "rb") as f:
                f.seek(offset)
                return f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""


# ---------------------------------------------------------------- 验收
def check_acceptance(task_dir: Path, work_dir: Path):
    """通用验收。优先读 tasks/<name>/acceptance.md, 每行一个断言:
         <glob>              至少匹配1个非空文件 (相对仓库根)
         <glob> :N           至少匹配 N 个非空文件
         checklist: <path> :N   文件内 '- [x]' 数量 ≥ N
       无 acceptance.md 时回退 selftest 默认(12章+12勾)。
       路径断言相对 work_dir 的父目录解析(= 启动目录/被接管会话的工作目录)。"""
    spec = task_dir / "acceptance.md"
    if not spec.exists():
        return _acceptance_selftest(work_dir)
    base = work_dir.parent
    problems = []
    for raw in spec.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("checklist:"):
            parts = [p.strip() for p in line[len("checklist:"):].split(":") if p.strip()]
            path, need = parts[0], (int(parts[1]) if len(parts) > 1 else 1)
            f = base / path
            if not f.exists():
                problems.append(f"{path} 不存在")
                continue
            txt = f.read_text(encoding="utf-8", errors="replace")
            done = txt.count("- [x]") + txt.count("- [X]")
            if done < need:
                problems.append(f"{path} 勾选{done}<{need}")
        else:
            parts = line.rsplit(":", 1)
            if len(parts) == 2 and parts[1].strip().isdigit():
                pat, need = parts[0].strip(), int(parts[1])
            else:
                pat, need = line, 1
            hits = [m for m in base.glob(pat) if m.is_file() and m.stat().st_size > 0]
            if len(hits) < need:
                problems.append(f"{pat} 非空文件{len(hits)}<{need}")
    return (not problems), ("全部满足" if not problems else "; ".join(problems[:4]))


DONE_SIGNALS = [
    "已全部完成", "全部完成", "任务已完成", "所有任务已完成", "没有等待执行的后续阶段",
    "无剩余工作", "全部阶段已完成", "已完成所有", "所有要求已完成", "全部进度项",
    "all tasks completed", "all done", "work complete", "finished all tasks",
    "everything is complete", "all requirements completed"
]


def check_acceptance_natural(session_cwd: Path, last_msg: str = "") -> tuple:
    """无感透明验收:
    1. 优先在被接管的工作目录内探测项目原生清单 (PROGRESS.md / TODO.md / last_msg 中提及的清单);
       只要清单中全部勾选 (done>=1, todo==0) 则判定完工;
       若清单中存在明确未勾选项 (todo>0), 则判定未完成。
    2. 若无清单, 则根据 worker 最后留言的完工语义 (DONE_SIGNALS 且无提问请求) 判定。"""
    last_msg = last_msg or ""
    cwd = Path(session_cwd)

    # 1. 寻找可能存在的项目清单文件
    checklist_candidates = []
    # 检查 last_msg 中是否明确提到了某个清单路径 (如 `decisionlab/PROGRESS.md`)
    for m in re.finditer(r'([a-zA-Z0-9_\-/\\]+\.md)', last_msg):
        cand = cwd / m.group(1).replace("\\", "/")
        if cand.exists() and cand.is_file() and cand not in checklist_candidates:
            checklist_candidates.append(cand)

    # 检查根目录及子目录下的常见清单
    for name in ("PROGRESS.md", "TODO.md", "checklist.md"):
        root_cand = cwd / name
        if root_cand.exists() and root_cand.is_file() and root_cand not in checklist_candidates:
            checklist_candidates.append(root_cand)
        for sub in cwd.glob(f"*/{name}"):
            if sub.is_file() and sub not in checklist_candidates:
                checklist_candidates.append(sub)

    # 评估发现的清单
    for p in checklist_candidates:
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        done = txt.count("- [x]") + txt.count("- [X]")
        todo = txt.count("- [ ]")
        if done >= 1 and todo == 0:
            try:
                rel_path = p.relative_to(cwd)
            except ValueError:
                rel_path = p.name
            return True, f"项目清单已全部勾选完成 ({rel_path}: 勾选{done}, 剩余0)"
        elif todo > 0:
            try:
                rel_path = p.relative_to(cwd)
            except ValueError:
                rel_path = p.name
            return False, f"项目清单仍有未完成项 ({rel_path}: 勾选{done}, 剩余{todo})"

    # 2. 若未发现有效清单，依赖 worker 自然完工语义
    has_ask = any(m in last_msg for m in ASK_MARKERS)
    if not has_ask:
        for sig in DONE_SIGNALS:
            if sig in last_msg:
                return True, f"识别到自然完工语义: '{sig}'"

    return False, "未检测到已完成清单或明确完工语义"


def check_acceptance_quick(work_dir: Path, last_msg: str = ""):
    return check_acceptance_natural(work_dir, last_msg)


def _acceptance_selftest(work_dir: Path):
    """selftest 12章格式: work_dir/PROGRESS.md 12项勾选 + 12个章节文件非空。"""
    prog = work_dir / "PROGRESS.md"
    if not prog.exists():
        return False, f"PROGRESS.md 不存在 ({work_dir})"
    text = prog.read_text(encoding="utf-8", errors="replace")
    done = text.count("- [x]") + text.count("- [X]")
    missing = [f"ch{i:02d}" for i in range(1, 13)
               if not (work_dir / "chapters" / f"ch{i:02d}.md").exists()
               or (work_dir / "chapters" / f"ch{i:02d}.md").stat().st_size < 100]
    ok = done >= 12 and not missing
    detail = f"PROGRESS勾选={done}/12, 缺失章节={missing or '无'}"
    return ok, detail


# ---------------------------------------------------------------- 主循环
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="",
                    help="任务书路径; 快速模式(--adopt + --quick)可不填")
    ap.add_argument("--chaos", default="", help='故障注入, 如 "kill:120"')
    ap.add_argument("--max-resumes", type=int, default=8)
    ap.add_argument("--max-run-sec", type=int, default=3600,
                    help="总时长上限(秒), 0=不设限; 真实数据收集建议 0")
    ap.add_argument("--no-probe", action="store_true", help="跳过启动探针")
    ap.add_argument("--driver", choices=["claude", "codex"], default=None,
                    help="不填时自动推断: --adopt→codex, 否则claude")
    ap.add_argument("--work-dir", default="work",
                    help="worker产物目录(相对启动cwd), 验收也在此目录")
    ap.add_argument("--adopt", default="",
                    help="接管已有codex会话: 'last'(本目录最近会话) 或 session-id")
    ap.add_argument("--quick", action="store_true",
                    help="快速挂机: 无感接管当前会话, 基于自然完工语义与项目清单自动验收")
    ap.add_argument("--yes", action="store_true",
                    help="跳过交互确认(自动选最新会话/自动关App)")
    ap.add_argument("--l2-cmd", default="antigravity",
                    help="L2升级agent: antigravity(默认, Google官方通道) / claude(中转, 不推荐) / off")
    ap.add_argument("--l2-max", type=int, default=2, help="L2升级次数上限")
    ap.add_argument("--l2-model", default="flash",
                    help="antigravity L2模型档位: flash_lite/flash/pro")
    ap.add_argument("--max-interactions", type=int, default=10,
                    help="交互决策(agy代答)次数上限")
    ap.add_argument("--l2-project-id", default="",
                    help="antigravity L2的项目id; 留空则自动从最近会话元数据发现")
    args = ap.parse_args()

    if args.driver is None:
        args.driver = "codex" if args.adopt else "claude"
    if not args.task and not (args.adopt and args.quick):
        ap.error('--task 必填 (零准备挂机请用: --adopt last --quick)')

    task_md = Path(args.task).resolve() if args.task else None
    session_cwd = str(WS)          # 接管模式下跟随被接管会话的工作目录
    work_dir = Path(session_cwd)   # 默认工作区对齐项目真实根目录
    l2_cmd = None if args.l2_cmd.strip().lower() in ("off", "none") else args.l2_cmd.strip()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = WS / "runs" / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    ivl_path = run_dir / "interventions.jsonl"
    agy_mgr = AntigravityManager(run_dir)
    atexit.register(agy_mgr.teardown)

    def ivl(event, **kw):
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **kw}
        with open(ivl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log(f"{event} {kw if kw else ''}")

    chaos = None
    if args.chaos:
        parts = args.chaos.split(":")
        try:
            chaos = (parts[0],) + tuple(int(x) for x in parts[1:])
        except ValueError:
            ap.error('chaos 格式: kill:30 (杀进程) 或 net:30:120 (30秒时断网120秒)')

    # 提示词哲学: 接管/续跑一律只发"继续"——类比用户重开客户端后点发送,
    # 任务语境全在会话历史里, afk不注入合成指令(注入会带偏worker, 22:14实锤)。
    # 任务书(--task)是用户显式提供的例外: 仍会发送, 且同时作为验收规格锚点。
    prompt_path = run_dir / "prompt.txt"
    if args.quick:
        prompt_path.write_text("继续\n", encoding="utf-8")
    else:
        prompt_path.write_text(
            task_md.read_text(encoding="utf-8") +
            "\n\n[运行约束] 只使用文件读取/创建/编辑工具, 禁止执行shell命令。"
            "遇到需要用户决策的问题时, 结束回合并在最终消息以【决策请求】开头, "
            "列出问题与选项后停止; 其余情况完成全部要求后停止。\n",
            encoding="utf-8")
    resume_path = run_dir / "resume-prompt.txt"
    resume_path.write_text("继续\n", encoding="utf-8")

    def verify():
        """终态验收分派: 显式任务模式=acceptance.md/selftest; 接管/快速挂机=自然完工语义+项目原生清单"""
        if task_md:
            return check_acceptance(task_md.parent, Path(session_cwd))
        last_msg = worker_last_message(run_dir)
        return check_acceptance_natural(Path(session_cwd), last_msg)

    keep_awake()
    proxy = detect_system_proxy()
    ivl("EGRESS", proxy=proxy or "(直连)", driver=args.driver)
    if args.driver == "claude":
        pool = get_relay_pool("claude-desktop")
        if args.no_probe:
            working = pool
        else:
            working = probe_pool(pool, proxy)
            if not working:
                ivl("TERMINAL", state="FAILED",
                    detail=f"探针筛出0个可用端点(池{len(pool)}个, 代理={proxy or '直连'})。"
                           f"可能: 中转全体故障 / token均不兼容CLI / 代理端口死。详见 pool-health.json")
                log("TERMINAL FAILED — SHUTDOWN WOULD HAPPEN HERE")
                return 1
        driver = ClaudeDriver(WS, run_dir, working, proxy)
    else:
        driver = CodexDriver(work_dir, run_dir)
        if args.adopt:
            if args.adopt == "last":
                cands = list_recent_codex_sessions(5)
                if not cands:
                    ivl("TERMINAL", state="FAILED", detail="未找到可接管的codex会话")
                    log("TERMINAL FAILED — SHUTDOWN WOULD HAPPEN HERE")
                    return 1
                pick = 1
                if args.yes or not sys.stdin.isatty():
                    log("ADOPT   非交互模式, 自动选择最新会话")
                else:
                    print("选择要接管的会话:")
                    for i, (sid_, p_, scwd_, title_, age_) in enumerate(cands, 1):
                        mark = "*" if scwd_ == str(WS) else " "
                        print(f"  [{i}]{mark} {age_}  {title_ or '(无标题)'}")
                        print(f"      cwd={scwd_}")
                    try:
                        raw = input("序号[1]: ").strip()
                    except (EOFError, OSError):
                        raw = ""
                    pick = int(raw) if raw.isdigit() and 1 <= int(raw) <= len(cands) else 1
                sid, rollout, scwd, _, _ = cands[pick - 1]
                if scwd and scwd != str(WS):
                    log(f"ADOPT   接管会话 cwd={scwd} (非{WS}), 工作目录与验收锚点绝对对齐目标工程")
            else:
                got = find_codex_session_by_id(args.adopt)
                if not got:
                    ivl("TERMINAL", state="FAILED", detail=f"找不到会话 {args.adopt}")
                    log("TERMINAL FAILED — SHUTDOWN WOULD HAPPEN HERE")
                    return 1
                rollout, scwd = got
                sid = args.adopt
            title = read_session_title(rollout) if rollout else ""
            ivl("ADOPT", session=sid, title=title,
                rollout=rollout.name[:60] if rollout else "(文件未定位)",
                session_cwd=scwd or "?")
            if rollout:
                log(f"ADOPT   任务标题: {title or '(未提取到)'}")
            if rollout:
                lockf0 = CODEX_LOCKS / f"{sid}.lock"
                app_killed = False
                if lockf0.exists():
                    log("LOCK     目标会话被 App 占用(单写者锁)")
                    if args.yes or not sys.stdin.isatty():
                        log("LOCK     非交互模式: 请自行彻底退出 Codex App, afk将轮询锁文件")
                    else:
                        try:
                            ans = input("由 afk 强制结束 Codex App 以释放锁?[Y/n]: ").strip().lower()
                        except (EOFError, OSError):
                            ans = "y"
                        if ans in ("", "y", "yes"):
                            killed = close_codex_app(rollout)
                            app_killed = True
                            ivl("APP_CLOSED", killed=killed)
                            log(f"LOCK     已结束进程: {killed or '(本就未运行)'}")
                        else:
                            log("LOCK     跳过强杀; 请自行退出 App, afk将轮询锁文件")
                    try:
                        lockf0.unlink()
                        ivl("STALE_LOCK_REMOVED", pre="接管准备")
                    except OSError:
                        pass
                if not app_killed:
                    wait_session_quiet(rollout)
            driver.session_id = sid
            driver.jsonl = rollout  # 心跳基线; resume后由 discover_session 重新定位
            agy_mgr.set_codex_session_id(sid)
            if scwd:
                # 验收锚点与工作区完全跟随被接管会话的真实工作区
                session_cwd = scwd
                work_dir = Path(scwd)
                driver.cwd = Path(scwd)
                # 接管前自动对目标工作区建立快照备份 (存放在 runs/<ts>/, 零污染目标工程)
                bk = backup_workspace(Path(scwd), run_dir)
                if bk:
                    ivl("WORKSPACE_BACKUP", archive=str(bk.name), src=scwd,
                        size_mb=round(bk.stat().st_size / (1024 * 1024), 2))
    ivl("LAUNCH", session=driver.session_id or "(运行时发现)",
        provider=driver.provider_name,
        chaos=str(chaos) if chaos else "off")

    launched_at = time.time()
    run_started_at = launched_at  # 总时长上限的计时基准(含所有退避)
    worker_runtime = 0.0        # 累计运行时间(不含resume退避), chaos计时基准
    chaos_fired = False
    resumes = 0
    busy_waits = 0              # SESSION_BUSY 耐心等待次数(不占resume预算)
    l2_calls = 0                # L2升级agent已调用次数
    interactions = 0            # 交互决策(agy代答)已处理次数
    early_exits = 0             # worker空转退出(rc=0但验收不过)计数→矛盾态熔断
    total_kills = 0             # 梯度心跳阈值基准
    net_off_adapter = None      # 断网chaos: 当前被断的网卡
    net_on_wall = 0.0           # 恢复时刻(wall clock, worker死了也要恢复)
    fails_on_provider = 0       # 当前供应商连续死亡计数
    providers_tried = [driver.provider_name]
    last_hb_log = 0.0
    last_error_note = ""
    outcome, outcome_detail = None, ""

    if args.driver == "codex" and args.adopt:
        driver.resume(prompt_path)  # 接管 = 对既有会话发出续跑指令
        ivl("ADOPTED_RESUME", pid=driver.proc.pid)
    else:
        driver.launch(prompt_path)
        ivl("SPAWNED", pid=driver.proc.pid)

    while True:
        time.sleep(5)
        now = time.time()
        if hasattr(driver, "discover_session"):  # codex: 每次 spawn 后重新发现 rollout
            driver.discover_session(launched_at)
            if driver.session_id and agy_mgr.codex_session_id != driver.session_id:
                agy_mgr.set_codex_session_id(driver.session_id)
        alive = driver.proc.poll() is None
        if alive:
            worker_runtime += 5

        # --- chaos 注入 (一次性) ---
        if chaos and not chaos_fired and alive:
            if chaos[0] == "kill" and worker_runtime >= chaos[1]:
                chaos_fired = True
                method = safe_kill(driver)
                ivl("CHAOS_KILL", at_sec=worker_runtime, method=method)
            elif chaos[0] == "net" and worker_runtime >= chaos[1]:
                chaos_fired = True
                adapter = find_connected_adapter()
                if adapter and net_disable(adapter):
                    net_off_adapter = adapter
                    net_on_wall = time.time() + chaos[2]
                    ivl("CHAOS_NET_OFF", at_sec=worker_runtime,
                        adapter=adapter, duration_sec=chaos[2])
                else:
                    ivl("CHAOS_NET_FAIL",
                        reason="断网失败: 未找到已连接网卡或需管理员权限")

        # --- 断网chaos恢复 (按墙钟, worker死了也恢复) ---
        if net_off_adapter and time.time() >= net_on_wall:
            net_enable(net_off_adapter)
            ivl("CHAOS_NET_ON", at_sec=worker_runtime)
            net_off_adapter = None

        # --- 心跳 ---
        hb = driver.heartbeat_age(launched_at)
        if now - last_hb_log >= 30:
            ivl("HEARTBEAT", alive=alive, stale_sec=round(hb))
            last_hb_log = now

        # 错误模式扫描 (会话日志尾部; codex resume 期间 jsonl 可能为 None)
        if driver.jsonl is not None and driver.jsonl.exists() \
                and driver.jsonl.stat().st_size > 0:
            tail = driver.jsonl.read_bytes()[-4096:].decode("utf-8", errors="replace")
            hits = [p for p in ERROR_PATTERNS if p in tail]
            if hits and hits[0] != last_error_note:
                ivl("ERROR_SIGNATURE", patterns=hits[:4])
                last_error_note = hits[0]

        # --- 分类与处置 ---
        stale_cap = STALE_LIMITS[min(total_kills, len(STALE_LIMITS) - 1)]
        if alive and hb > stale_cap:
            total_kills += 1
            fails_on_provider += 1
            method = safe_kill(driver)
            ivl("DETECT_HANG", stale_sec=round(hb), cap=stale_cap, kill=method)
            outcome, outcome_detail = "hang", f"心跳停跳{round(hb)}s"
        elif not alive:
            rc = driver.proc.returncode
            total_kills += 1
            fails_on_provider += 1
            if rc == 0:
                last_msg = worker_last_message(run_dir)
                if any(m in last_msg for m in ASK_MARKERS):
                    interactions += 1
                    ivl("INTERACTION", n=interactions, snippet=last_msg[:160])
                    outcome, outcome_detail = "interaction", last_msg
                else:
                    ok, detail = verify()
                    ivl("EXIT_OK", acceptance=detail)
                    if ok:
                        outcome, outcome_detail = "success", detail
                    else:
                        early_exits += 1
                        outcome, outcome_detail = "early_exit", detail
            else:
                err_tail = ""
                try:
                    err_log = run_dir / "worker-stderr.log"
                    if err_log.exists():
                        lines = [l for l in err_log.read_text(
                            encoding="utf-8", errors="replace").strip().splitlines()
                            if l.strip()]
                        err_tail = lines[-1][:220] if lines else ""
                except OSError:
                    pass
                ivl("EXIT_CRASH", rc=rc, provider=driver.provider_name, err=err_tail)
                if "already has an active writer" in err_tail:
                    ivl("SESSION_BUSY",
                        hint="原会话仍被桌面/界面占用(codex单写者锁)。请停止或关闭原Codex"
                             "界面中的该会话, 看门狗将耐心重试(不占续跑预算)")
                    outcome, outcome_detail = "busy", "会话被占用"
                else:
                    outcome, outcome_detail = "crash", f"exit_code={rc}"

        # --- 验收前置守卫: worker死亡/空转/被锁, 但产物已齐 → 免唤醒直接成功 ---
        if outcome in ("crash", "hang", "early_exit", "busy", "interaction"):
            ok, detail = verify()
            if ok:
                outcome, outcome_detail = "success", "验收通过(免唤醒): " + detail

        # --- 交互决策: worker提问 → agy按委托策略代答 → 决定喂回codex ---
        if outcome == "interaction":
            if interactions >= args.max_interactions:
                ivl("TERMINAL", state="FAILED",
                    detail=f"交互请求超过上限({interactions}次), 需人工介入")
                break
            ivl("L2_CONSULT", n=interactions, question=outcome_detail[:150])
            verdict, answer, l2_log = l2_dispatch(
                l2_cmd, args, proxy, run_dir, driver, session_cwd,
                interactions, "interaction", outcome_detail, outcome_detail,
                kind="interaction", agy_mgr=agy_mgr)
            ivl("L2_ANSWER", verdict=verdict, answer=answer[:150],
                log=str(l2_log.name))
            if verdict in ("NO-VERDICT", "NO-BRIDGE"):
                # L2通道故障(非决策结果): 绝不把错误文本当决定喂回codex,
                # 委托回worker自行判断(实测worker会按自己此前的建议继续)
                ivl("L2_UNAVAILABLE", verdict=verdict, fallback="worker自决")
                answer_path = run_dir / f"answer-{interactions}.txt"
                answer_path.write_text("继续，自行决定并完成剩余工作。\n",
                                       encoding="utf-8")
                driver.resume(answer_path)
                ivl("RESUMED_SELFCALL", n=interactions, pid=driver.proc.pid)
                launched_at = time.time()
                outcome, outcome_detail = None, ""
                last_error_note = ""
                continue
            if verdict == "DEFER" or not answer.strip():
                ivl("TERMINAL", state="FAILED",
                    detail="L2将决策DEFER给用户 — 需人工介入")
                break
            cleaned_answer = clean_l2_decision_text(answer)
            answer_path = run_dir / f"answer-{interactions}.txt"
            answer_path.write_text(cleaned_answer, encoding="utf-8")
            driver.resume(answer_path)
            ivl("RESUMED_WITH_DECISION", n=interactions, pid=driver.proc.pid)
            launched_at = time.time()
            outcome, outcome_detail = None, ""
            last_error_note = ""
            continue

        # --- busy 耐心通道: 轮询锁文件(锁在=App开着, 不spawn注定失败的进程) ---
        if outcome == "busy":
            if args.max_run_sec > 0 and time.time() - run_started_at > args.max_run_sec:
                ivl("TERMINAL", state="FAILED",
                    detail=f"会话始终被占用(耐心等待{busy_waits}次)")
                break
            lockf = CODEX_LOCKS / f"{driver.session_id}.lock"
            if lockf.exists():
                busy_waits += 1
                app = codex_app_running()
                if app is False:
                    # App确认已退出但锁文件残留 → 立即清除, ≤20秒后接管
                    try:
                        lockf.unlink()
                        ivl("STALE_LOCK_REMOVED", n=busy_waits)
                    except OSError as e:
                        ivl("WARN", msg=f"清理残留锁失败: {e}")
                    time.sleep(5)
                    continue
                if app is None and busy_waits % 6 == 0:
                    # 进程检测失败时退回真实试探
                    ivl("BUSY_TAKEOVER", lock="probe-unknown")
                    driver.resume(resume_path)
                    ivl("RESUMED_BUSY", pid=driver.proc.pid,
                        provider=driver.provider_name)
                    launched_at = time.time()
                    outcome, outcome_detail = None, ""
                    last_error_note = ""
                    continue
                if busy_waits % 9 == 1:
                    ivl("BUSY_WAIT", wait_sec=20, n=busy_waits,
                        lock="held", app="alive")
                time.sleep(20)
                continue
            ivl("BUSY_TAKEOVER", lock="released")
            driver.resume(resume_path)
            ivl("RESUMED_BUSY", pid=driver.proc.pid, provider=driver.provider_name)
            launched_at = time.time()
            outcome, outcome_detail = None, ""
            last_error_note = ""
            continue

        # --- 终态判定 ---
        if outcome == "success":
            ivl("TERMINAL", state="SUCCESS", detail=outcome_detail)
            break
        if outcome in ("crash", "hang", "early_exit"):
            if outcome == "early_exit" and early_exits >= 2:
                if l2_cmd and l2_calls < args.l2_max:
                    l2_calls += 1
                    last_msg = ""
                    try:
                        lm = run_dir / "codex-last-message.txt"
                        if lm.exists():
                            last_msg = lm.read_text(encoding="utf-8",
                                                    errors="replace")[-600:]
                    except OSError:
                        pass
                    ivl("L2_ESCALATE", agent=l2_cmd, call=l2_calls,
                        kind="contradiction")
                    verdict, text, l2_log = l2_dispatch(
                        l2_cmd, args, proxy, run_dir, driver, session_cwd,
                        l2_calls, outcome_detail,
                        f"矛盾态: worker连续2次空转退出但验收不通过: {outcome_detail}\n"
                        f"worker最后留言: {last_msg}", outcome_detail,
                        kind="repair", agy_mgr=agy_mgr)
                    ivl("L2_RESULT", verdict=verdict, log=str(l2_log.name))
                    if verdict == "UNFIXABLE":
                        ivl("TERMINAL", state="FAILED",
                            detail=f"L2判定无法修复: {text.strip()[-200:]}")
                        break
                    resumes = max(0, resumes - 4)  # L2修复后追加续跑预算
                    early_exits = 0
                    ivl("L2_BUDGET_GRANTED", extra=4)
                    driver.resume(resume_path)
                    ivl("RESUMED", attempt=resumes + 1, pid=driver.proc.pid,
                        provider=driver.provider_name)
                    launched_at = time.time()
                    outcome, outcome_detail = None, ""
                    last_error_note = ""
                    continue
                ivl("TERMINAL", state="FAILED",
                    detail=f"矛盾态: worker连续{early_exits}次空转退出但验收不通过"
                           f"({outcome_detail}) — L2不可用, 需人工介入")
                break
            if resumes >= args.max_resumes:
                if l2_cmd and l2_calls < args.l2_max and outcome in ("crash", "hang"):
                    l2_calls += 1
                    err_tail = ""
                    try:
                        err_log = run_dir / "worker-stderr.log"
                        if err_log.exists():
                            el = [l for l in err_log.read_text(
                                encoding="utf-8", errors="replace")
                                .strip().splitlines() if l.strip()]
                            err_tail = "\n".join(el[-5:])[:800]
                    except OSError:
                        pass
                    try:
                        recent = [json.loads(l) for l in
                                  ivl_path.read_text(encoding="utf-8")
                                  .strip().splitlines()[-40:]]
                    except Exception:
                        recent = []
                    sigs = [r for r in recent if r.get("event") in
                            ("ERROR_SIGNATURE", "EXIT_CRASH", "DETECT_HANG",
                             "PROVIDER_SWITCH", "CHAOS_NET_OFF")]
                    errors = "\n".join(
                        f"- {r['ts']} {r['event']} "
                        f"{r.get('err', r.get('patterns', r.get('reason', '')))}"
                        for r in sigs[-8:])
                    ivl("L2_ESCALATE", agent=l2_cmd, call=l2_calls)
                    verdict, text, l2_log = l2_dispatch(
                        l2_cmd, args, proxy, run_dir, driver, session_cwd,
                        l2_calls, outcome_detail, errors, err_tail,
                        kind="repair", agy_mgr=agy_mgr)
                    ivl("L2_RESULT", verdict=verdict, log=str(l2_log.name))
                    if verdict == "UNFIXABLE":
                        ivl("TERMINAL", state="FAILED",
                            detail=f"L2判定无法修复: {text.strip()[-200:]}")
                        break
                    resumes = max(0, resumes - 4)  # L2修复后追加4次续跑预算
                    ivl("L2_BUDGET_GRANTED", extra=4)
                else:
                    ivl("TERMINAL", state="FAILED",
                        detail=f"resume预算耗尽({resumes}次), 最后状态={outcome}")
                    break
            if args.max_run_sec > 0 and time.time() - run_started_at > args.max_run_sec:
                ivl("TERMINAL", state="FAILED", detail="总时长超限")
                break
            # L1.5 换端点: 同供应商连续 FAILS_BEFORE_SWITCH 次死亡 → 轮换
            if driver.has_next() and fails_on_provider >= FAILS_BEFORE_SWITCH:
                new_name = driver.switch_provider()
                providers_tried.append(new_name)
                fails_on_provider = 0
                wait = 10
                ivl("PROVIDER_SWITCH", to=new_name,
                    after_failures=FAILS_BEFORE_SWITCH, wait_sec=wait)
            else:
                wait = BACKOFFS[min(resumes, len(BACKOFFS) - 1)]
            ivl("RESUME_WAIT", backoff_sec=wait, attempt=resumes + 1,
                reason=outcome, provider=driver.provider_name)
            time.sleep(wait)
            resumes += 1
            driver.resume(resume_path)
            ivl("RESUMED", attempt=resumes, pid=driver.proc.pid,
                provider=driver.provider_name)
            launched_at = time.time()
            outcome, outcome_detail = None, ""
            last_error_note = ""

    # --- 终态报告 ---
    ok, detail = verify()
    state = "SUCCESS" if outcome == "success" else "FAILED"
    report = f"""# 监管运行报告 — {ts}

- 终态: **{state}**
- 会话: `{driver.session_id}`
- 供应商尝试顺序: {' → '.join(providers_tried)}
- 干预/续跑次数: {resumes} (chaos注入: {chaos})
- 验收: {detail}
- 详细时间线: interventions.jsonl
- worker日志: worker-stdout.log / worker-stderr.log
- 会话轨迹: {driver.jsonl}

## 时间线
"""
    for line in ivl_path.read_text(encoding="utf-8").strip().splitlines():
        r = json.loads(line)
        report += f"- `{r['ts']}` **{r['event']}** {json.dumps({k:v for k,v in r.items() if k not in('ts','event')}, ensure_ascii=False)}\n"
    report += "\n## 系统动作\n- v0.2: 此处本应执行关机 (`shutdown /s /t 60`), 已跳过\n"
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    log(f"REPORT   {run_dir / 'report.md'}")
    log(f"TERMINAL {state} — SHUTDOWN WOULD HAPPEN HERE")
    agy_mgr.teardown()
    return 0 if state == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
