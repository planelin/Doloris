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
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
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
STALE_LIMITS = [150, 300, 450]                     # 按总击杀次数取值, 尾部封顶
FAILS_BEFORE_SWITCH = 2                            # 同供应商连续死亡次数→换端点

QUICK_PROMPT = """你被监管系统接管(原会话中断, 现在无头续跑)。
1. 读取会话历史, 确认未完成的工作并继续执行; 不要重做已完成的部分。
2. 新产出的文件一律放入当前工作目录下的 afk-work/ 子目录。
3. 维护 afk-work/PROGRESS.md: 逐条列出剩余工作项(- [ ]), 每完成一项改为 - [x];
   若会话中的任务已全部完成, 也要创建该文件, 写明"无剩余工作"并把清单全部勾选。
4. 清单全部勾完才允许停止。全程不要提问, 不要等待确认。
"""


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

    def kill_tree(self):
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           capture_output=True)
            self.proc.wait()
            log(f"KILL    进程树 pid={self.proc.pid} 已终止")

    def heartbeat_age(self, launched_at) -> float:
        if self.jsonl.exists():
            return time.time() - self.jsonl.stat().st_mtime
        return time.time() - launched_at  # 日志还没出现, 从启动算起


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


def close_codex_app():
    """强制结束 Codex 桌面App进程族, 单写者锁随进程消亡。
    仅允许在【尚无我方worker】的接管准备阶段调用。"""
    killed = []
    for img in ("ChatGPT.exe", "codex.exe"):
        r = subprocess.run(["taskkill", "/IM", img, "/F"], capture_output=True)
        if r.returncode == 0:
            killed.append(img)
    time.sleep(2)
    return killed


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
        )

    def _common(self):
        return ["-C", str(self.cwd), "-s", "workspace-write",
                "--skip-git-repo-check", "--json", "-o", str(self.last_msg)]

    def launch(self, prompt_path: Path):
        self._spawn_once(["exec", *self._common(), "-"], prompt_path)
        log("LAUNCH codex exec (session运行时发现)")
        return self.proc

    def resume(self, prompt_path: Path):
        if not self.session_id:
            raise RuntimeError("resume 前必须先发现 session_id")
        self.jsonl = None  # resume 可能新建 rollout, 重新发现
        # 注意: resume 子命令不认 -C/-s (继承原会话的cwd与沙箱), 只认这些:
        self._spawn_once(["exec", "resume", self.session_id,
                          "--skip-git-repo-check", "--json",
                          "-o", str(self.last_msg), "-"], prompt_path)
        log(f"RESUME  codex session={self.session_id[:8]}")
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

    def kill_tree(self):
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           capture_output=True)
            self.proc.wait()
            log(f"KILL    进程树 pid={self.proc.pid} 已终止")

    def heartbeat_age(self, launched_at) -> float:
        if self.jsonl and self.jsonl.exists():
            return time.time() - self.jsonl.stat().st_mtime
        return time.time() - launched_at  # rollout 未出现, 从启动算起


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


def check_acceptance_quick(work_dir: Path):
    """快速模式验收: afk-work/PROGRESS.md 存在且全部勾完(≥1勾, 0未勾)。"""
    prog = work_dir / "PROGRESS.md"
    if not prog.exists():
        return False, "afk-work/PROGRESS.md 不存在(worker未建立清单)"
    txt = prog.read_text(encoding="utf-8", errors="replace")
    done = txt.count("- [x]") + txt.count("- [X]")
    todo = txt.count("- [ ]")
    ok = done >= 1 and todo == 0
    return ok, f"快速验收: 勾选{done}, 未勾{todo}"


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
                    help="快速挂机: 内置续跑指令, 验收=afk-work/PROGRESS.md全部勾完")
    ap.add_argument("--yes", action="store_true",
                    help="跳过交互确认(自动选最新会话/自动关App)")
    args = ap.parse_args()

    if args.driver is None:
        args.driver = "codex" if args.adopt else "claude"
    if not args.task and not (args.adopt and args.quick):
        ap.error('--task 必填 (零准备挂机请用: --adopt last --quick)')

    task_md = Path(args.task).resolve() if args.task else None
    work_dir = WS / args.work_dir  # worker cwd=WS, 产物约定落在 <启动目录>/<work-dir>
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = WS / "runs" / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    ivl_path = run_dir / "interventions.jsonl"

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

    # 任务提示词: quick=内置接管指令; 任务模式=任务书+行为约束
    prompt_path = run_dir / "prompt.txt"
    if args.quick:
        prompt_path.write_text(QUICK_PROMPT, encoding="utf-8")
    else:
        prompt_path.write_text(
            task_md.read_text(encoding="utf-8") +
            "\n\n[运行约束] 只使用文件读取/创建/编辑工具, 禁止执行shell命令。"
            "完成全部要求后停止, 不要中途停下提问。\n",
            encoding="utf-8")
    resume_path = run_dir / "resume-prompt.txt"
    if args.quick:
        resume_path.write_text(
            "你刚才被中断(进程被终止), 现在无头续跑。读取会话历史与 afk-work/PROGRESS.md,"
            "继续完成全部剩余工作; 不要重做已完成的部分; 新产出文件放入 afk-work/ 子目录;"
            "每完成一项更新清单, 全部勾完才停止; 不要提问。\n", encoding="utf-8")
    else:
        resume_path.write_text(
            "你刚才被中断了(进程被终止)。请对照任务书要求与既有进度文件(PROGRESS.md),"
            "确认已完成哪些内容, 然后继续完成全部剩余工作。不要重做已完成的工作,"
            "不要中途停下提问, 完成后立即更新进度文件。\n", encoding="utf-8")

    def verify():
        """终态验收分派: quick=afk-work清单全勾; 任务模式=acceptance.md/selftest"""
        if args.quick:
            return check_acceptance_quick(work_dir)
        return check_acceptance(task_md.parent, work_dir)

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
        driver = CodexDriver(WS, run_dir)
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
                    log(f"WARN    接管会话 cwd={scwd} (非{WS}), 验收锚点随之转移")
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
                            killed = close_codex_app()
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
            if scwd:
                # 验收锚点跟随被接管会话; quick模式固定用 afk-work
                work_dir = Path(scwd) / ("afk-work" if args.quick else args.work_dir)
    ivl("LAUNCH", session=driver.session_id or "(运行时发现)",
        provider=driver.provider_name,
        chaos=str(chaos) if chaos else "off")

    # quick模式: 归档上一轮遗留的清单, 否则旧清单会让验收瞬间假通过
    if args.quick:
        prog = work_dir / "PROGRESS.md"
        if prog.exists():
            arch = work_dir / "archive"
            arch.mkdir(parents=True, exist_ok=True)
            dest = arch / f"PROGRESS-{ts}.md"
            try:
                prog.replace(dest)
                ivl("ARCHIVE_STALE_CHECKLIST", dest=str(dest))
            except OSError as e:
                ivl("WARN", msg=f"归档旧清单失败: {e}")

    launched_at = time.time()
    run_started_at = launched_at  # 总时长上限的计时基准(含所有退避)
    worker_runtime = 0.0        # 累计运行时间(不含resume退避), chaos计时基准
    chaos_fired = False
    resumes = 0
    busy_waits = 0              # SESSION_BUSY 耐心等待次数(不占resume预算)
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
        alive = driver.proc.poll() is None
        if alive:
            worker_runtime += 5

        # --- chaos 注入 (一次性) ---
        if chaos and not chaos_fired and alive:
            if chaos[0] == "kill" and worker_runtime >= chaos[1]:
                chaos_fired = True
                ivl("CHAOS_KILL", at_sec=worker_runtime)
                driver.kill_tree()
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
            ivl("DETECT_HANG", stale_sec=round(hb), cap=stale_cap, action="kill")
            driver.kill_tree()
            outcome, outcome_detail = "hang", f"心跳停跳{round(hb)}s"
        elif not alive:
            rc = driver.proc.returncode
            total_kills += 1
            fails_on_provider += 1
            if rc == 0:
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
        if outcome in ("crash", "hang", "early_exit", "busy"):
            ok, detail = verify()
            if ok:
                outcome, outcome_detail = "success", "验收通过(免唤醒): " + detail

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
                ivl("TERMINAL", state="FAILED",
                    detail=f"矛盾态: worker连续{early_exits}次空转退出但验收不通过"
                           f"({outcome_detail}) — 需人工/L2介入")
                break
            if resumes >= args.max_resumes:
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
    return 0 if state == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
