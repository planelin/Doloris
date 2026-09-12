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


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def munged_cwd(cwd: Path) -> str:
    # Claude Code 项目目录的编码规则: C:\Agents\zcode\double -> C--Agents-zcode-double
    return str(cwd).replace(":", "").replace("\\", "-").replace("_", "-")


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

    def __init__(self, cwd: Path, run_dir: Path, pool):
        self.cwd = cwd
        self.run_dir = run_dir
        self.pool = pool
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


# ---------------------------------------------------------------- 验收
def check_acceptance(task_dir: Path):
    """验收: work/PROGRESS.md 有12个勾选项 且 12章文件都存在且非空。"""
    work = task_dir / "work"
    prog = work / "PROGRESS.md"
    if not prog.exists():
        return False, "PROGRESS.md 不存在"
    text = prog.read_text(encoding="utf-8", errors="replace")
    done = text.count("- [x]") + text.count("- [X]")
    missing = [f"ch{i:02d}" for i in range(1, 13)
               if not (work / "chapters" / f"ch{i:02d}.md").exists()
               or (work / "chapters" / f"ch{i:02d}.md").stat().st_size < 100]
    ok = done >= 12 and not missing
    detail = f"PROGRESS勾选={done}/12, 缺失章节={missing or '无'}"
    return ok, detail


# ---------------------------------------------------------------- 主循环
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="任务书路径 (tasks/xxx/task.md)")
    ap.add_argument("--chaos", default="", help='故障注入, 如 "kill:120"')
    ap.add_argument("--max-resumes", type=int, default=8)
    ap.add_argument("--max-run-sec", type=int, default=3600)
    args = ap.parse_args()

    task_md = Path(args.task).resolve()
    task_dir = task_md.parent
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
        kind, _, val = args.chaos.partition(":")
        chaos = (kind, int(val))

    # 任务提示词: 任务书 + 行为约束
    prompt_path = run_dir / "prompt.txt"
    prompt_path.write_text(
        task_md.read_text(encoding="utf-8") +
        "\n\n[运行约束] 只使用文件读取/创建/编辑工具, 禁止执行shell命令。"
        "每完成一章立即更新PROGRESS.md勾选。一口气完成全部12章, 不要中途停下提问。\n",
        encoding="utf-8")
    resume_path = run_dir / "resume-prompt.txt"
    resume_path.write_text(
        "你刚才被中断了(进程被终止)。请读取 work/PROGRESS.md 和 work/chapters/ 下已有文件,"
        "确认已完成哪些章节, 然后继续完成全部剩余章节。不要重做已完成的工作,"
        "不要中途停下提问, 只使用文件读写工具, 完成每章立即更新PROGRESS.md。\n",
        encoding="utf-8")

    pool = get_relay_pool("claude-desktop")
    driver = ClaudeDriver(WS, run_dir, pool)
    ivl("LAUNCH", session=driver.session_id, provider=driver.provider_name,
        chaos=str(chaos) if chaos else "off", pool_size=len(pool))

    launched_at = time.time()
    run_started_at = launched_at  # 总时长上限的计时基准(含所有退避)
    worker_runtime = 0.0        # 累计运行时间(不含resume退避), chaos计时基准
    chaos_fired = False
    resumes = 0
    total_kills = 0             # 梯度心跳阈值基准
    fails_on_provider = 0       # 当前供应商连续死亡计数
    providers_tried = [driver.provider_name]
    last_hb_log = 0.0
    last_error_note = ""
    outcome, outcome_detail = None, ""

    driver.launch(prompt_path)
    ivl("SPAWNED", pid=driver.proc.pid)

    while True:
        time.sleep(5)
        now = time.time()
        alive = driver.proc.poll() is None
        if alive:
            worker_runtime += 5

        # --- chaos 注入 (一次性: 只模拟一次随机崩溃, 不重复触发) ---
        if chaos and chaos[0] == "kill" and not chaos_fired \
                and worker_runtime >= chaos[1] and alive:
            chaos_fired = True
            ivl("CHAOS_KILL", at_sec=worker_runtime)
            driver.kill_tree()

        # --- 心跳 ---
        hb = driver.heartbeat_age(launched_at)
        if now - last_hb_log >= 30:
            ivl("HEARTBEAT", alive=alive, stale_sec=round(hb))
            last_hb_log = now

        # 错误模式扫描 (会话日志尾部)
        if driver.jsonl.exists() and driver.jsonl.stat().st_size > 0:
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
                ok, detail = check_acceptance(task_dir)
                ivl("EXIT_OK", acceptance=detail)
                if ok:
                    outcome, outcome_detail = "success", detail
                else:
                    outcome, outcome_detail = "early_exit", detail
            else:
                outcome, outcome_detail = "crash", f"exit_code={rc}"

        # --- 终态判定 ---
        if outcome == "success":
            ivl("TERMINAL", state="SUCCESS", detail=outcome_detail)
            break
        if outcome in ("crash", "hang", "early_exit"):
            if resumes >= args.max_resumes:
                ivl("TERMINAL", state="FAILED",
                    detail=f"resume预算耗尽({resumes}次), 最后状态={outcome}")
                break
            if time.time() - run_started_at > args.max_run_sec:
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
    ok, detail = check_acceptance(task_dir)
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
