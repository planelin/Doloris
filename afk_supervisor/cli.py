"""
afk_supervisor.cli — AFK 监管系统统一命令行入口与启动装配
=========================================================
解析 CLI 参数，完成会话探测、工作区快照备份、锁竞争处理与运行上下文装配；
分流调度无头监管循环 (run_headless_supervisor) 与双有头监管循环 (run_gui_supervisor)。
保持对 afk.cmd / afk2.cmd / afk3.cmd 与全部历史参数的 100% 兼容。
"""

import argparse
import atexit
import json
import os
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from afk_supervisor.models import DeadlineBudget
from afk_supervisor.platform.process import log, WorkspaceSupervisorLock, close_codex_app, verify_codex_writer_released
from afk_supervisor.platform.windows import keep_awake, detect_system_proxy
from afk_supervisor.baseline import extract_task_baseline
from afk_supervisor.evidence import collect_evidence
from afk_supervisor.coordinator import SupervisorCoordinator
from afk_supervisor.state import SupervisorState
from afk_supervisor.l2.bridge import AntigravityManager
from afk_supervisor.drivers.claude import ClaudeDriver, get_relay_pool, probe_pool
from afk_supervisor.drivers.codex import CodexDriver
from afk_supervisor.sessions.discovery import (
    clean_session_id,
    list_recent_codex_sessions,
    find_codex_session_by_id,
    load_codex_thread_titles,
    read_session_title,
    get_codex_locks_dir,
)
from afk_supervisor.platform.gui import pause_codex_gui_session
from afk_supervisor.acceptance import check_acceptance, check_acceptance_natural, worker_last_message
from afk_supervisor.engine import run_headless_supervisor
from afk_supervisor.gui_engine import run_gui_supervisor
from afk_supervisor.compat import get_sym

BACKUP_EXCLUDE_DIRS = {
    ".git", ".svn", ".hg", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".codex", ".idea", ".vscode", "dist", "build",
    ".next", ".nuxt", "target", "bin", "obj",
}


def wait_session_quiet(rollout: Path, quiet_sec: float = 15, max_wait: float = 90) -> bool:
    """Legacy helper: require explicit turn termination, not an unchanged mtime.

    This does NOT establish writer ownership and is not used by kill takeover.
    quiet_sec remains accepted only for compatibility.
    """
    from afk_supervisor.sessions.rollout import codex_handoff_state
    deadline = time.monotonic() + max_wait
    while True:
        snapshot = codex_handoff_state(rollout)
        log(f"IDLE_CHECK {snapshot}")
        if snapshot["state"] == "safe" and snapshot["turn_ended"]:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log("WARN    未确认回合结束；不强行接管，也不要求用户手工关闭")
            return False
        time.sleep(min(1, remaining))


def backup_workspace(scwd: Path, run_dir: Path, max_size_mb: int = 300) -> Optional[Path]:
    """在接管前对目标工作区做一次轻量快照备份。"""
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


def build_arg_parser() -> argparse.ArgumentParser:
    """构建 AFK 监管系统统一命令行解析器。"""
    ap = argparse.ArgumentParser(description="AFK Supervisor - 长任务自主监管守护系统")
    ap.add_argument("--task", default="", help="任务书路径; 快速模式(--adopt + --quick)可不填")
    ap.add_argument("--chaos", default="", help='故障注入, 如 "kill:120"')
    ap.add_argument("--max-resumes", type=int, default=8)
    ap.add_argument("--max-run-sec", type=int, default=0, help="总时长上限(秒), 默认0=不设限(跑完为止); 亦可显式指定秒数")
    ap.add_argument("--no-probe", action="store_true", help="跳过启动探针")
    ap.add_argument("--driver", choices=["claude", "codex"], default=None, help="不填时自动推断: --adopt→codex, 否则claude")
    ap.add_argument("--work-dir", default="work", help="worker产物目录(相对启动cwd), 验收也在此目录")
    ap.add_argument("--adopt", default="", help="接管已有codex会话: 'last'(本目录最近会话) 或 session-id")
    ap.add_argument("--quick", action="store_true", help="快速挂机: 无感接管当前会话, 基于自然完工语义与项目清单自动验收")
    ap.add_argument("--fork", action="store_true", help="Fork无损接管: 基于目标会话派生新Thread并后台续跑，保留桌面端App存活且不触发单写锁冲突")
    ap.add_argument("--gui", action="store_true", help="双有头GUI监管: 保持桌面端App前台活跃，监听事件并通过Windows原生UI自动化注入指令")
    ap.add_argument("--adopt-mode", choices=["resume", "fork", "gui"], default=None, help="接管模式: resume(默认/杀App原地续写), fork(派生新会话且不杀App), gui(双有头原生UI自动化)")
    ap.add_argument("--yes", action="store_true", help="跳过会话选择，自动选最新会话；resume 模式始终自动关闭 App")
    ap.add_argument("--handoff-timeout-sec", type=float, default=90, help="kill 模式确认安全退出的最长等待秒数；超时记录失败，不盲杀/不并发 resume")
    ap.add_argument("--l2-cmd", default="antigravity", help="L2升级agent: antigravity(默认, Google官方通道) / claude(中转, 不推荐) / off")
    ap.add_argument("--l2-max", type=int, default=2, help="L2升级次数上限")
    ap.add_argument("--l2-model", default="flash", help="antigravity L2模型档位: flash_lite/flash/pro")
    ap.add_argument("--max-interactions", type=int, default=10, help="交互决策(agy代答)次数上限")
    ap.add_argument("--l2-project-id", default="", help="antigravity L2的项目id; 留空则自动从最近会话元数据发现")
    ap.add_argument("--delivery-dir", default="", help="显式指定用户交付目录 (若不指定则从任务输入或工作区中检测)")
    ap.add_argument("--proxy", default="", help="显式指定网络代理 (若不指定则自动探测系统代理)")
    ap.add_argument("--timeout-sec", type=int, default=1800, help="L2单次等待预算(秒)，超时保留同一AGY会话和请求")
    ap.add_argument("--resume", default="", help="兼容参数: 原地续跑指定的 session-id")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 主入口函数。"""
    ap = build_arg_parser()
    args = ap.parse_args(argv)

    if args.handoff_timeout_sec <= 0:
        ap.error("--handoff-timeout-sec 必须大于 0")

    adopt_mode = "resume"
    if args.adopt_mode:
        adopt_mode = args.adopt_mode
    elif args.fork:
        adopt_mode = "fork"
    elif args.gui:
        adopt_mode = "gui"

    if args.resume and not args.adopt:
        args.adopt = args.resume

    if args.driver is None:
        args.driver = "codex" if args.adopt else "claude"
    if not args.task and not (args.adopt and args.quick):
        ap.error('--task 必填 (零准备挂机请用: --adopt last --quick)')

    # 确定监管工作根目录
    ws_dir = Path(__file__).resolve().parent.parent
    task_md = Path(args.task).resolve() if args.task else None
    session_cwd = str(ws_dir)
    work_dir = Path(session_cwd)
    l2_cmd = None if args.l2_cmd.strip().lower() in ("off", "none") else args.l2_cmd.strip()
    ts = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    run_dir = ws_dir / "runs" / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    ivl_path = run_dir / "interventions.jsonl"
    agy_mgr = AntigravityManager(run_dir)
    atexit.register(agy_mgr.teardown)

    def ivl(event: str, **kw: Any) -> None:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **kw}
        with open(ivl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        act = kw.pop("activity", None)
        parts = [event]
        if kw:
            parts.append(str(kw))
        if act:
            parts.append(f"| {act}")
        log(" ".join(parts))

    chaos = None
    if args.chaos:
        parts = args.chaos.split(":")
        try:
            chaos = (parts[0],) + tuple(int(x) for x in parts[1:])
        except ValueError:
            ap.error('chaos 格式: kill:30 (杀进程) 或 net:30:120 (30秒时断网120秒)')

    prompt_path = run_dir / "prompt.txt"
    if getattr(args, "gui", False):
        prompt_path.write_text("# 双有头模式 (GUI): 会话已由桌面端保持，所有输入均由AGY主管按暂停决策动态注入\n", encoding="utf-8")
    elif args.quick:
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

    keep_awake()
    proxy = args.proxy or detect_system_proxy()
    ivl("EGRESS", proxy=proxy or "(直连)", driver=args.driver)

    driver = None
    rollout = None
    title = ""
    sid = ""
    scwd = ""

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
        driver = ClaudeDriver(ws_dir, run_dir, working, proxy)
    else:
        driver = CodexDriver(work_dir, run_dir)
        if args.adopt:
            adopt_arg = clean_session_id(args.adopt)
            is_digit_index = adopt_arg.isdigit() and 1 <= int(adopt_arg) <= 20

            if args.adopt == "last" or is_digit_index:
                cands = list_recent_codex_sessions(8)
                if not cands:
                    ivl("TERMINAL", state="FAILED", detail="未找到可接管的codex会话")
                    log("TERMINAL FAILED — SHUTDOWN WOULD HAPPEN HERE")
                    return 1

                if is_digit_index:
                    pick_idx = int(adopt_arg)
                    if pick_idx > len(cands):
                        ivl("TERMINAL", state="FAILED",
                            detail=f"序号 {pick_idx} 超出会话列表范围 (当前仅有 {len(cands)} 个会话)")
                        log(f"TERMINAL FAILED — 序号 {pick_idx} 超出范围")
                        return 1
                    sid, rollout, scwd, title, _ = cands[pick_idx - 1]
                    log(f"ADOPT   指定序号 [{pick_idx}] 接管会话: {sid[:8]} ({title or '无标题'})")
                elif args.yes or not sys.stdin.isatty():
                    log("ADOPT   非交互模式, 自动选择最新会话")
                    sid, rollout, scwd, title, _ = cands[0]
                else:
                    print("\n选择要接管的会话:")
                    for i, (sid_, p_, scwd_, title_, age_) in enumerate(cands, 1):
                        mark = "*" if scwd_ == str(ws_dir) else " "
                        print(f"  [{i}]{mark} {age_}  [{sid_[:8]}]  {title_ or '(无标题)'}")
                        print(f"      cwd={scwd_}")
                    try:
                        raw = input(f"\n输入序号 (1-{len(cands)}) 或 会话ID/URL [1]: ").strip()
                    except (EOFError, OSError):
                        raw = ""

                    if not raw:
                        sid, rollout, scwd, title, _ = cands[0]
                    elif raw.isdigit() and 1 <= int(raw) <= len(cands):
                        sid, rollout, scwd, title, _ = cands[int(raw) - 1]
                    else:
                        cleaned_input = clean_session_id(raw)
                        matched = [c for c in cands if c[0] == cleaned_input or c[0].startswith(cleaned_input)]
                        if matched:
                            sid, rollout, scwd, title, _ = matched[0]
                        else:
                            find_fn = get_sym("find_codex_session_by_id", find_codex_session_by_id)
                            got = find_fn(cleaned_input)
                            if not got:
                                ivl("TERMINAL", state="FAILED", detail=f"找不到指定的会话: {raw}")
                                log(f"TERMINAL FAILED — 找不到指定的会话: {raw}")
                                return 1
                            sid, rollout, scwd = got
                            title = load_codex_thread_titles().get(sid) or read_session_title(rollout)

                if scwd and scwd != str(ws_dir):
                    log(f"ADOPT   接管会话 cwd={scwd} (非{ws_dir}), 工作目录与验收锚点绝对对齐目标工程")
            else:
                find_fn = get_sym("find_codex_session_by_id", find_codex_session_by_id)
                got = find_fn(args.adopt)
                if not got:
                    ivl("TERMINAL", state="FAILED", detail=f"找不到会话 {args.adopt}")
                    log(f"TERMINAL FAILED — 找不到会话 {args.adopt}")
                    return 1
                sid, rollout, scwd = got
                title = load_codex_thread_titles().get(sid) or read_session_title(rollout)

            ivl("ADOPT", session=sid, title=title,
                rollout=rollout.name[:60] if rollout else "(文件未定位)",
                session_cwd=scwd or "?")
            if rollout:
                log(f"ADOPT   任务标题: {title or '(未提取到)'}")
                if adopt_mode == "fork":
                    log("ADOPT    模式: [FORK无头续跑] 保持桌面端App存活，先暂停原任务，再派生独立子会话")
                elif adopt_mode == "gui":
                    log("ADOPT    模式: [双有头GUI监管] 保持桌面端App存活并前台运行")
                else:
                    log("ADOPT    模式: [KILL原地续跑] 自动检查安全边界、退出 App、确认写锁释放；无需手工关闭")

            driver.session_id = sid
            driver.jsonl = rollout
            agy_mgr.set_codex_session_id(sid)
            if scwd:
                session_cwd = scwd
                work_dir = Path(scwd)
                driver.cwd = Path(scwd)

    ws_path = Path(session_cwd).resolve()
    lock_cls = get_sym("WorkspaceSupervisorLock", WorkspaceSupervisorLock)
    ws_lock = lock_cls(ws_path, sid=getattr(driver, "session_id", "") or "new", mode=adopt_mode or "fresh")
    state_mgr = SupervisorState(run_dir, sid=getattr(driver, "session_id", "") or "", mode=adopt_mode or "fresh", ws=ws_path)
    state_mgr.transition("INIT", detail=f"Supervisor started ({adopt_mode})")

    def finish_handoff(state, detail):
        from afk_supervisor.reporting import generate_final_report
        ivl("TERMINAL", state=state, detail=detail)
        state_mgr.transition(state, detail=detail)
        try:
            generate_final_report(run_dir, state, detail, run_dir.name,
                                  getattr(driver, "session_id", "") or "", rollout,
                                  [], 0, None, ivl_path, title=title)
        finally:
            ws_lock.release()
        return 1

    try:
        ok_lock, lock_msg = ws_lock.acquire()
        if not ok_lock:
            return finish_handoff("FAILED", f"工作区已被其他看门狗锁定: {lock_msg}")
        if args.adopt and adopt_mode == "fork":
            state_mgr.transition("HANDOFF_WAIT", detail="无限等待父任务停止确认", parent_rollout=str(rollout or ""))
            ivl("FORK_PARENT_WAIT", session=sid, timeout_sec=None, until="confirmed_paused")
            pause_fn = get_sym("pause_codex_gui_session", pause_codex_gui_session)
            if not pause_fn(rollout, max_wait=None):
                return finish_handoff("FAILED", "未确认原任务暂停，未启动 Fork")
            ivl("FORK_PARENT_PAUSED", session=sid)
        if args.adopt and scwd:
            bk = backup_workspace(Path(scwd), run_dir)
            if bk:
                ivl("WORKSPACE_BACKUP", archive=str(bk.name), src=scwd,
                    size_mb=round(bk.stat().st_size / (1024 * 1024), 2))
    except KeyboardInterrupt:
        return finish_handoff("CANCELLED", "用户取消交接，未启动无头端")
    except Exception as exc:
        return finish_handoff("FAILED", f"交接准备失败（未启动无头端）: {type(exc).__name__}: {exc}")

    # The unlimited AFK2 handoff is not part of the worker runtime budget.
    budget = DeadlineBudget(args.max_run_sec)
    if args.adopt and adopt_mode == "resume":
        try:
            ivl("HANDOFF_START", session=sid, mode="kill", timeout_sec=args.handoff_timeout_sec)
            close_fn = get_sym("close_codex_app", close_codex_app)
            killed = close_fn(rollout, max_wait=args.handoff_timeout_sec, on_event=ivl)
            ivl("APP_CLOSED", killed=killed, verified=True)
            # Never unlink an active OS lock: that can create a second writer.
            lock_path = get_codex_locks_dir() / f"{sid}.lock"
            verify_codex_writer_released(lock_path)
            ivl("WRITER_RELEASED", lock_path=str(lock_path), lock_file_deleted=False)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            from afk_supervisor.reporting import generate_final_report
            detail = f"kill 交接失败（基础设施/安全边界未确认；未启动无头端）: {exc}"
            ivl("TERMINAL", state="FAILED", detail=detail)
            state_mgr.transition("FAILED", detail=detail)
            try:
                generate_final_report(run_dir, "FAILED", detail, run_dir.name, sid,
                                      rollout, [], 0, None, ivl_path, title=title)
            finally:
                ws_lock.release()
            return 1

    task_baseline = extract_task_baseline(
        rollout_path=getattr(driver, "jsonl", None) or rollout,
        session_cwd=ws_path,
        title=title or getattr(driver, "title", ""),
        task_md=task_md,
        work_dir=args.work_dir,
        explicit_delivery_dir=args.delivery_dir or None,
    )
    baseline_file = run_dir / "task_baseline.json"
    try:
        from dataclasses import asdict
        baseline_file.write_text(json.dumps(asdict(task_baseline), indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    coordinator = SupervisorCoordinator(
        run_dir=run_dir,
        workspace_root=ws_path,
        delivery_dir=Path(task_baseline.delivery_dir),
        task_baseline=task_baseline,
        agy_mgr=agy_mgr,
        l2_cmd=args.l2_cmd,
        args=args,
        proxy=proxy,
        budget=budget,
        ivl_fn=ivl,
        task_dir=task_md.parent if task_md else None
    )

    def verify(custom_last_msg: Optional[str] = None, min_mtime: float = 0.0, title_str: str = "") -> Tuple[bool, str]:
        if task_md:
            return check_acceptance(task_md.parent, ws_path, Path(task_baseline.delivery_dir))
        last_msg = custom_last_msg if custom_last_msg is not None else worker_last_message(run_dir)
        if not min_mtime and getattr(driver, "jsonl", None) and driver.jsonl.exists():
            min_mtime = driver.jsonl.stat().st_ctime - 120
        return check_acceptance_natural(ws_path, last_msg, min_mtime=min_mtime, title=title_str or title)

    if adopt_mode == "gui":
        return run_gui_supervisor(
            sid=sid,
            rollout=rollout,
            scwd=scwd or session_cwd,
            title=title,
            args=args,
            run_dir=run_dir,
            agy_mgr=agy_mgr,
            proxy=proxy,
            verify_fn=verify,
            ivl=ivl,
            budget=budget,
            ws_lock=ws_lock,
            state_mgr=state_mgr,
            coordinator=coordinator,
        )

    return run_headless_supervisor(
        driver=driver,
        args=args,
        run_dir=run_dir,
        ws_path=ws_path,
        adopt_mode=adopt_mode,
        task_baseline=task_baseline,
        coordinator=coordinator,
        state_mgr=state_mgr,
        budget=budget,
        ivl=ivl,
        verify_fn=verify,
        agy_mgr=agy_mgr,
        proxy=proxy,
        chaos=chaos,
        rollout=rollout,
        title=title,
        task_md=task_md,
        ws_lock=ws_lock,
    )
