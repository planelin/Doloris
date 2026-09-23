"""
afk_supervisor.l2.transport — L2 传输引擎、提示词封装与请求隔离
==============================================================
管理与 L2 专家 (Antigravity / Claude) 的网络/进程传输；
为每个请求生成独立 request_id 并以 request_id 隔离命名落盘
(prompt-{req_id}.txt, response-{req_id}.txt, evidence-{req_id}.json, protocol-{req_id}.json)；
转录日志基于 min_line_idx 增量检索，杜绝多轮会话串扰。
"""

import json
import hashlib
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional, Tuple

from afk_supervisor.storage import atomic_json
from afk_supervisor.models import DeadlineBudget, EvidencePacket, L2Result
from afk_supervisor.platform.process import log
from afk_supervisor.baseline import TaskBaseline
from afk_supervisor.drivers.claude import get_relay_pool
from afk_supervisor.l2.bridge import (
    AntigravityManager,
    bind_agy_conversation_for_codex,
    check_agy_transcript_error,
    discover_antigravity_bridge,
    discover_antigravity_project_id,
    get_agy_conversation_for_codex,
    get_skill_metadata,
    is_agy_working,
    parse_verdict_from_text,
    read_agy_latest_response,
    wait_for_agy_idle,
)
from afk_supervisor.l2.protocol import (
    build_protocol_prompt,
    extract_protocol_json,
    validate_protocol_payload,
)
from afk_supervisor.compat import get_sym


def get_workspace_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def clean_l2_decision_text(raw_text: str) -> str:
    """清洗 L2 决策代答文本，提取纯净的执行指令回喂 Worker。严禁在文本为空时默认生成授权指令。"""
    if not raw_text or not raw_text.strip():
        return ""
    payload = extract_protocol_json(raw_text)
    if payload and isinstance(payload.get("next_action"), dict):
        inst = payload["next_action"].get("instructions", "").strip()
        if inst:
            return (inst + "\n")
    text = raw_text.lstrip("\ufeff\u200b").strip()
    lines = [line.rstrip() for line in text.splitlines()]

    while lines and (lines[0].strip().startswith("```") or not lines[0].strip()):
        lines.pop(0)
    while lines and (lines[-1].strip().startswith("```") or not lines[-1].strip()):
        lines.pop()

    while lines and (
        lines[-1].strip("`*# \t\r\n").rstrip("。.!！:：;,").upper() in ("PROCEED", "DEFER", "COMPLETED")
        or not lines[-1].strip()
    ):
        lines.pop()

    while lines and (lines[-1].strip().startswith("```") or not lines[-1].strip()):
        lines.pop()

    lines = [
        line for line in lines
        if not re.search(r'\[REQUEST_ID:[^\]]+\]', line, re.IGNORECASE) and not re.search(r'【本次请求ID:[^】]+】', line)
    ]

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
        "行动指令:",
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

    cleaned_lines = [line for line in cleaned.splitlines() if not line.strip().startswith("```")]
    res = "\n".join(cleaned_lines).strip()
    return (res + "\n") if res else ""


def worker_last_message(run_dir: Path) -> str:
    """获取 Worker 最后留言。"""
    lm = Path(run_dir) / "codex-last-message.txt"
    try:
        if lm.exists():
            return lm.read_text(encoding="utf-8", errors="replace")[-1500:]
    except OSError:
        pass
    try:
        out = Path(run_dir) / "worker-stdout.log"
        return out.read_text(encoding="utf-8", errors="replace")[-1500:] if out.exists() else ""
    except OSError:
        return ""


def get_recent_workspace_files(cwd: Path, limit: int = 8) -> List[Tuple[float, str, int]]:
    EXCLUDE = {"afk-work", "runs", "scratch", ".git", "node_modules", "__pycache__", "dist", "build"}
    files = []
    try:
        for p in cwd.rglob("*"):
            if p.is_file():
                try:
                    parts = p.relative_to(cwd).parts
                    if any(part.lower() in EXCLUDE or part.lower().startswith(("backup-", "work-", "afk-")) for part in parts):
                        continue
                    files.append((p.stat().st_mtime, str(p.relative_to(cwd)), p.stat().st_size))
                except (OSError, ValueError):
                    pass
        files.sort(reverse=True)
    except Exception:
        pass
    return files[:limit]


def run_l2_antigravity(
    run_dir: Path,
    full_prompt: str,
    short_prompt: str,
    n: int,
    scwd: str,
    verdict_file: Optional[Path] = None,
    conv_holder: Optional[object] = None,
    project_id: Optional[str] = None,
    model: str = "flash",
    timeout_sec: int = 1800,
    log_name: Optional[str] = None,
    agy_mgr: Optional[AntigravityManager] = None,
    title: str = "",
    budget: Optional[DeadlineBudget] = None,
    request_id: Optional[str] = None,
    mode: str = "DECIDE",
    task_baseline: Optional[TaskBaseline] = None,
    evidence_packet: Optional[EvidencePacket] = None,
) -> L2Result:
    """经 agentapi 桥调用 Antigravity。
    每轮使用独立 request_id 命名保存 prompt、response、evidence、protocol，避免覆盖；
    单调时钟超时控制；优先读取本地转录日志。
    """
    req_id = request_id or f"req-{n}-{uuid.uuid4().hex[:8]}"
    ws = get_workspace_root()

    # 以 request_id 命名归档
    prompt_req_file = run_dir / f"prompt-{req_id}.txt"
    resp_req_file = run_dir / f"response-{req_id}.txt"
    proto_file = run_dir / f"protocol-{req_id}.json"
    evidence_file = run_dir / f"evidence-{req_id}.json"

    # Keep an immutable complete request and archive the exact transmitted bytes
    # separately. Large requests use a local file reference to avoid Windows argv limits.
    req_header = f"【本次请求ID: {req_id} | 模式: {mode}】\n"
    if req_id not in full_prompt:
        full_prompt = req_header + full_prompt
    if req_id not in short_prompt:
        short_prompt = req_header + short_prompt
    request_file = run_dir / f"request-{req_id}.txt"
    lifecycle_file = run_dir / f"request-{req_id}.state.json"
    pending_file = run_dir / "agy_pending_request.json"
    lifecycle = {}
    pending = {}
    try:
        if request_file.exists():
            if request_file.read_bytes() != full_prompt.encode("utf-8"):
                return L2Result("PROTOCOL_ERROR", "同一请求ID内容不同，拒绝覆盖归档", run_dir / f"l2-{n}.log")
        else:
            with request_file.open("xb") as stream:
                stream.write(full_prompt.encode("utf-8"))
            if evidence_packet:
                evidence_packet.persist(evidence_file)
        if lifecycle_file.exists():
            lifecycle = json.loads(lifecycle_file.read_text(encoding="utf-8"))
        if pending_file.exists():
            pending = json.loads(pending_file.read_text(encoding="utf-8"))
        if pending and pending.get("request_id") != req_id:
            return L2Result("NO-VERDICT", "原AGY请求仍在途，禁止并发发送或替换会话", run_dir / f"l2-{n}.log")
        if not lifecycle:
            lifecycle = {"request_id": req_id, "phase": "SENDING" if pending else "PREPARED",
                         "request_sha256": hashlib.sha256(full_prompt.encode("utf-8")).hexdigest()}
            atomic_json(lifecycle_file, lifecycle)
        if lifecycle.get("phase") == "COMPLETED":
            cached = lifecycle["result"]
            return L2Result(cached["verdict"], cached["answer"], run_dir / (log_name or "l2.log"), payload=cached.get("payload"))
        # The immutable archive is not proof that a message was sent. Only the
        # write-ahead SENDING receipt makes delivery uncertain.
        if lifecycle.get("phase") in ("SENDING", "SENT") and not pending:
            return L2Result("NO-VERDICT", "请求已尝试发送但缺少在途状态，禁止重复发送", run_dir / f"l2-{n}.log")
    except (OSError, ValueError, KeyError) as error:
        return L2Result("PROTOCOL_ERROR", f"无法读取/归档本轮请求状态: {error}", run_dir / f"l2-{n}.log")

    def checkpoint(phase, **fields):
        lifecycle.update(phase=phase, **fields)
        atomic_json(lifecycle_file, lifecycle)

    def completed(verdict, text, payload=None):
        # Save a replayable result before clearing the in-flight pointer.
        checkpoint("COMPLETED", result={"verdict": verdict, "answer": text, "payload": payload})
        pending_file.unlink(missing_ok=True)
        return L2Result(verdict, text, log_path, payload=payload)

    def wire_text(text):
        if len(subprocess.list2cmdline([text]).encode("utf-16-le")) < 32000:
            return text
        digest = hashlib.sha256(request_file.read_bytes()).hexdigest()
        return (
            req_header + f"完整请求包: {request_file.resolve()}\nSHA256: {digest}\n"
            + f"task_id={task_baseline.task_id if task_baseline else ''}\n"
            + f"reviewed_revision={evidence_packet.reviewed_revision if evidence_packet else ''}\n"
            + "请先只读加载完整请求包（含本轮需求、证据ID、验证输出与协议），确认哈希后再处理。"
            + "不得依赖上一轮记忆。无法读取/核验时不得判定PASS，应报告证据不可用。不要修改请求包。"
        )

    remaining_initial = budget.bound_timeout(timeout_sec) if budget else timeout_sec
    request_deadline = time.monotonic() + max(0, remaining_initial)

    def remaining_timeout():
        remaining = max(0.0, request_deadline - time.monotonic())
        return budget.bound_timeout(remaining) if budget else remaining

    base_vname = Path(verdict_file).name if verdict_file else f"afk-l2-verdict-{req_id}.txt"
    round_vfile = Path(run_dir) / f"{Path(base_vname).stem}-{req_id}{Path(base_vname).suffix}"
    legacy_vfile = Path(run_dir) / base_vname
    log_path = run_dir / f"{log_name or 'l2'}.log"

    if agy_mgr is not None:
        csrf, ports, agexe = agy_mgr.ensure_bridge()
    else:
        csrf, ports, agexe = discover_antigravity_bridge()

    if not csrf or not ports or not agexe.exists():
        with open(log_path, "wb") as f:
            f.write("NO-BRIDGE: 未发现运行中的Antigravity language_server".encode("utf-8"))
        return L2Result("NO-BRIDGE", "NO-BRIDGE", log_path, payload=None)

    pending_file = run_dir / "agy_pending_request.json"
    round_vfile.parent.mkdir(parents=True, exist_ok=True)
    for vf in (() if pending_file.exists() else (round_vfile, legacy_vfile)):
        try:
            if vf.exists():
                vf.unlink()
        except OSError:
            pass

    pending_file = run_dir / "agy_pending_request.json"
    pending = {}
    if pending_file.exists():
        pending = json.loads(pending_file.read_text(encoding="utf-8"))
    cid = getattr(conv_holder, "_agy_cid", None) if conv_holder else None
    if not cid and agy_mgr is not None:
        cid = agy_mgr.cid
        if cid and conv_holder is not None:
            conv_holder._agy_cid = cid

    codex_sid = (agy_mgr.codex_session_id if agy_mgr and agy_mgr.codex_session_id != "unknown" else "") or getattr(conv_holder, "session_id", "") or (task_baseline.task_id if task_baseline else "")
    if not cid and codex_sid and codex_sid != "unknown":
        reg_cid = get_agy_conversation_for_codex(codex_sid, run_dir=run_dir)
        if reg_cid:
            cid = reg_cid
            if conv_holder is not None:
                conv_holder._agy_cid = cid
            if agy_mgr is not None:
                agy_mgr.cid = cid

    if pending.get("cid"):
        cid = pending["cid"]
    polling_pending = bool(pending and pending.get("request_id") == req_id and cid)
    if pending and not pending.get("cid"):
        try:
            stale_sec = time.time() - pending_file.stat().st_mtime
            if stale_sec > 15.0:
                log(f"L2 WARN   发现超时的未确认建会话挂起记录 (已停滞 {stale_sec:.1f}s)，清理脏状态并允许重试")
                pending_file.unlink(missing_ok=True)
                pending = {}
            else:
                return L2Result("NO-VERDICT", "AGY建会话结果未确认，禁止重复创建；请检查桥接日志", log_path)
        except Exception:
            return L2Result("NO-VERDICT", "AGY建会话结果未确认，禁止重复创建；请检查桥接日志", log_path)

    good_port = getattr(conv_holder, "_agy_port", None) if conv_holder else None
    if not good_port and agy_mgr is not None:
        good_port = agy_mgr.port

    if good_port and good_port in ports:
        ports.remove(good_port)
        ports.insert(0, good_port)

    env = dict(os.environ)
    env["ANTIGRAVITY_CSRF_TOKEN"] = csrf
    if project_id:
        env["ANTIGRAVITY_PROJECT_ID"] = project_id
    last_err = ""
    active_cid = None

    for attempt, port in enumerate(ports, 1):
        env["ANTIGRAVITY_LS_ADDRESS"] = f"127.0.0.1:{port}"
        try:
            if cid and not polling_pending:
                busy, busy_reason = is_agy_working(cid)
                if busy:
                    log(f"L2 WAIT   AGY 会话 {cid[:8]} 当前正在工作 ({busy_reason})，暂停注入等待空闲...")
                    wait_limit = remaining_timeout()
                    if not wait_for_agy_idle(cid, timeout_sec=wait_limit):
                        log(f"L2 WAIT   AGY 会话 {cid[:8]} 仍在工作，保留会话，禁止创建替代会话")
                        return L2Result("NO-VERDICT", "原AGY会话仍在工作", log_path)

            initial_line_count = pending.get("initial_line_count", 0) if polling_pending else 0
            if cid and not polling_pending:
                home = Path.home()
                t_path = home / ".gemini" / "antigravity" / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
                if t_path.exists():
                    try:
                        initial_line_count = len(t_path.read_text(encoding="utf-8", errors="replace").splitlines())
                    except Exception:
                        pass

            call_timeout = min(120, remaining_timeout())
            if call_timeout <= 0:
                last_err = "运行总时限已耗尽"
                break

            sent_prompt = wire_text(full_prompt if task_baseline or evidence_packet or not cid else short_prompt)
            prompt_req_file.write_bytes(sent_prompt.encode("utf-8"))
            (run_dir / f"prompt-{req_id}-attempt-{attempt}.txt").write_bytes(sent_prompt.encode("utf-8"))
            if polling_pending:
                # Resume observation of the original request, never inject it again.
                r = subprocess.CompletedProcess([], 0, stdout=b"{}", stderr=b"")
            else:
                atomic_json(pending_file, {"request_id": req_id, "cid": cid or "", "initial_line_count": initial_line_count, "phase": "SENDING"})
                no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if cid:
                    r = subprocess.run(
                        [str(agexe), "agentapi", "send-message", cid, sent_prompt],
                        capture_output=True, timeout=call_timeout, env=env, cwd=str(ws),
                        creationflags=no_win
                    )
                else:
                    round_title = f"[L2 #{n} {mode}] {title[:20] if title else '监管与验收'}"
                    r = subprocess.run(
                        [str(agexe), "agentapi", "new-conversation", f"--model={model}", f"--title={round_title}", sent_prompt],
                        capture_output=True, timeout=call_timeout, env=env, cwd=str(ws),
                        creationflags=no_win
                    )
        except subprocess.TimeoutExpired:
            last_err = "调用超时，结果不确定，保留原请求等待重试观察"
            break

        out = (r.stdout or b"").decode("utf-8", errors="replace")
        if r.returncode != 0:
            last_err = (r.stderr or r.stdout or b"agentapi failed").decode("utf-8", errors="replace")[:300]
            is_conn_err = any(k in last_err for k in ("Unavailable", "connection error", "server preface", "forcibly closed", "EOF"))
            if not cid and is_conn_err and attempt < len(ports):
                log(f"L2 WARN   端口 {port} 连接失败 ({last_err[:80]})，尝试备用端口...")
                continue
            break
        mode_str = "poll" if polling_pending else ("send" if cid else "new")
        with open(log_path, "ab") as f:
            f.write(f"--- port {port} {mode_str} [req_id={req_id} mode={mode}] ---\n{out}\n".encode("utf-8", errors="replace"))

        if '"error"' in out:
            last_err = out[:300]
            transient = ("Unavailable" in out or "connection error" in out or "server preface" in out or "forcibly closed" in out or "EOF" in out)
            stale = (cid and ("not found" in out.lower() or "invalid" in out.lower() or "no such" in out.lower()))
            if stale:
                last_err = "原AGY会话不可访问；保留绑定，不自动新建: " + out[:200]
                break
            if not cid and transient and attempt < len(ports):
                log(f"L2 WARN   端口 {port} 握手失败 ({last_err[:80]})，尝试备用端口...")
                continue
            # Delivery is uncertain; do not resend on another port.
            break

        active_cid = cid
        m = re.search(r'"conversationId"\s*:\s*"([^"]+)"', out)
        if m:
            active_cid = m.group(1)
            if conv_holder is not None:
                conv_holder._agy_cid = active_cid
                conv_holder._agy_port = port
            if agy_mgr is not None:
                agy_mgr.persist_cid(active_cid)
                agy_mgr.port = port
            if codex_sid and codex_sid != "unknown":
                bind_agy_conversation_for_codex(codex_sid, active_cid, run_dir=run_dir)

        if active_cid:
            atomic_json(pending_file, {"request_id": req_id, "cid": active_cid, "initial_line_count": initial_line_count, "phase": "SENT"})
            checkpoint("SENT", cid=active_cid, initial_line_count=initial_line_count)
        else:
            last_err = "AGY未返回会话ID，禁止重复建会话"
            if attempt < len(ports):
                log(f"L2 WARN   端口 {port} 未能返回会话ID，尝试备用端口...")
                continue
            break

        interrupted = False
        start_wait_t = time.monotonic()
        last_hb_t = start_wait_t

        while remaining_timeout() > 0:
            time.sleep(min(3, remaining_timeout()))
            # 轨1: 优先读取 AGY 本地转录日志落盘的 PLANNER_RESPONSE
            if active_cid:
                read_fn = get_sym("read_agy_latest_response", read_agy_latest_response)
                resp_tuple = read_fn(active_cid, min_line_idx=initial_line_count, request_id=req_id)
                if resp_tuple:
                    verdict = resp_tuple[0]
                    text = resp_tuple[1]
                    payload = getattr(resp_tuple, "payload", None) or (resp_tuple[2] if len(resp_tuple) > 2 and isinstance(resp_tuple[2], dict) else None) or extract_protocol_json(text)
                    for vf in (round_vfile, legacy_vfile):
                        try:
                            if vf.exists():
                                vf.unlink()
                        except OSError:
                            pass

                    # 记录响应
                    try:
                        resp_req_file.write_text(text, encoding="utf-8")
                    except Exception:
                        pass

                    # 严格校验结构化协议
                    if payload:
                        ok_val, val_reason = validate_protocol_payload(
                            payload,
                            expected_request_id=req_id,
                            expected_task_id=task_baseline.task_id if task_baseline else None,
                            expected_mode=mode,
                            expected_revision=evidence_packet.reviewed_revision if (evidence_packet and mode == "REVIEW") else None,
                            task_baseline=task_baseline,
                            evidence_packet=evidence_packet,
                        )
                        if not ok_val:
                            log(f"L2 WARN  AGY协议校验未通过: {val_reason}")
                            with open(log_path, "ab") as f:
                                f.write(f"\n[PROTOCOL-ERROR] {val_reason}\n".encode("utf-8", errors="replace"))
                            return completed("PROTOCOL_ERROR", f"协议校验失败: {val_reason}")

                        try:
                            proto_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                        except Exception:
                            pass
                        return completed(str(payload["verdict"]).upper(), text, payload)
                    else:
                        if mode == "REVIEW" and verdict in ("PASS", "COMPLETED"):
                            log("L2 WARN  REVIEW 模式缺少结构化协议，严禁直接判定 PASS，降级为 INCONCLUSIVE")
                            verdict = "INCONCLUSIVE"
                        return completed(verdict, text)

            # 轨2: 兼容独立的 round_vfile 文本落地
            for cand_vf in (round_vfile, legacy_vfile):
                if cand_vf.exists():
                    v = cand_vf.read_text(encoding="utf-8", errors="replace")
                    try:
                        cand_vf.unlink()
                    except OSError:
                        pass
                    try:
                        resp_req_file.write_text(v, encoding="utf-8")
                    except Exception:
                        pass

                    payload = extract_protocol_json(v)
                    if payload:
                        ok_val, val_reason = validate_protocol_payload(
                            payload,
                            expected_request_id=req_id,
                            expected_task_id=task_baseline.task_id if task_baseline else None,
                            expected_mode=mode,
                            expected_revision=evidence_packet.reviewed_revision if (evidence_packet and mode == "REVIEW") else None,
                            task_baseline=task_baseline,
                            evidence_packet=evidence_packet,
                        )
                        if ok_val:
                            try:
                                proto_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                            except Exception:
                                pass
                            return completed(payload.get("verdict", "NO-VERDICT"), v, payload)
                        else:
                            log(f"L2 WARN  文件协议校验失败: {val_reason}")
                            return completed("PROTOCOL_ERROR", val_reason)
                    else:
                        verdict = parse_verdict_from_text(v)
                        if mode == "REVIEW" and verdict in ("PASS", "COMPLETED"):
                            log("L2 WARN  旧文件单词决议严禁直接判定 PASS，降级为 INCONCLUSIVE")
                            verdict = "INCONCLUSIVE"
                        if verdict != "NO-VERDICT":
                            return completed(verdict, v)

            if time.monotonic() - last_hb_t >= 10.0:
                elapsed = int(time.monotonic() - start_wait_t)
                cid_snip = f" ({active_cid[:8]})" if active_cid else ""
                log(f"L2 WAIT   L2主管(Antigravity)正在深度分析与决策{cid_snip} [req={req_id[:12]}] (已思考 {elapsed}s)...")
                last_hb_t = time.monotonic()

            if active_cid:
                agy_err = check_agy_transcript_error(active_cid)
                if agy_err:
                    log(f"L2      检测到 AGY 本地会话中断 ({active_cid[:8]}): {agy_err}")
                    last_err = agy_err
                    interrupted = True
                    break

        if not interrupted:
            last_err = "verdict超时未出现"
        break

    if not active_cid and not polling_pending:
        if pending_file.exists():
            try:
                pending_file.unlink()
            except OSError:
                pass

    with open(log_path, "ab") as f:
        f.write(f"\n[L2-ANTIGRAVITY-FAIL] {last_err}\n".encode("utf-8", errors="replace"))
    return L2Result("NO-VERDICT", last_err, log_path, payload=None)


def run_l2_agent(l2_cmd: str, run_dir: Path, prompt: str, n: int, proxy: Optional[str], timeout_sec: float = 900) -> L2Result:
    """以无头模式调用 L2 agent (claude 等)。"""
    log_path = run_dir / f"l2-{n}.log"
    prompt_file = run_dir / f"l2-{n}-prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    parts = l2_cmd.split()
    if not parts:
        return L2Result("NO-VERDICT", "L2 command is empty", log_path)
    if timeout_sec <= 0:
        return L2Result("NO-VERDICT", "L2 deadline exhausted", log_path)
    env = dict(os.environ)
    if parts[0].lower() == "claude":
        pool = get_relay_pool("claude-desktop")
        if pool:
            env.update(pool[0][1])
        if proxy:
            env["HTTPS_PROXY"] = proxy
            env["HTTP_PROXY"] = proxy
    args = ["cmd.exe", "/c", *parts, "-p"]
    ws = get_workspace_root()
    process_error = ""
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        with prompt_file.open("rb") as prompt_stream, log_path.open("wb") as log_stream:
            result = subprocess.run(
                args, stdin=prompt_stream, stdout=log_stream, stderr=subprocess.STDOUT,
                env=env, cwd=str(ws), timeout=timeout_sec,
                creationflags=no_win
            )
            if result.returncode != 0:
                process_error = f"L2 exit code {result.returncode}"
    except subprocess.TimeoutExpired:
        process_error = f"L2 TIMEOUT {timeout_sec}s"
    except OSError as error:
        process_error = f"L2 process unavailable: {error}"
    if process_error:
        with open(log_path, "ab") as stream:
            stream.write(f"\n[{process_error}]\n".encode("utf-8"))
    text = ""
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="replace")
    if process_error:
        # Partial stdout from a failed/timed-out process is not a valid decision.
        return L2Result("NO-VERDICT", text, log_path)
    verdict = parse_verdict_from_text(text)
    payload = extract_protocol_json(text)
    if payload:
        verdict = str(payload.get("verdict", verdict)).upper()
    return L2Result(verdict, text, log_path, payload=payload)


def l2_dispatch(
    l2_cmd: str,
    args: Any,
    proxy: Optional[str],
    run_dir: Path,
    driver: Any,
    session_cwd: str,
    n: int,
    outcome_detail: str = "",
    errors_text: str = "",
    err_tail: str = "",
    kind: str = "repair",
    mode: Optional[str] = None,
    agy_mgr: Optional[AntigravityManager] = None,
    title: str = "",
    budget: Optional[DeadlineBudget] = None,
    task_baseline: Optional[TaskBaseline] = None,
    evidence_packet: Optional[EvidencePacket] = None,
    request_id: Optional[str] = None,
    protocol_error_feedback: str = "",
) -> L2Result:
    """按通道与种类路由调用 L2 agent。"""
    task_title = title or getattr(driver, "title", "") or "(未提供标题)"
    req_id = request_id or f"req-{n}-{uuid.uuid4().hex[:8]}"
    context_text = errors_text or outcome_detail or ""

    actual_mode = mode
    if not actual_mode:
        if kind in ("interaction", "supervise"):
            actual_mode = "REVIEW" if evidence_packet else "DECIDE"
        else:
            actual_mode = "REPAIR"

    baseline = task_baseline
    if not baseline:
        baseline = TaskBaseline(
            task_id=getattr(driver, "session_id", "unknown") or "unknown",
            original_requirements=task_title or context_text or "未知任务需求",
            session_cwd=str(session_cwd),
            requested_delivery_dir=str(session_cwd),
            effective_delivery_dir=str(session_cwd),
            _delivery_dir=str(session_cwd),
        )

    ws = get_workspace_root()
    skill_ver, skill_hash = get_skill_metadata(ws)
    full, short = build_protocol_prompt(
        mode=actual_mode,
        task_baseline=baseline,
        request_id=req_id,
        evidence_packet=evidence_packet,
        question_or_context=context_text,
        err_tail=err_tail or "",
        skill_version=skill_ver,
        skill_hash=skill_hash,
        protocol_error_feedback=protocol_error_feedback,
    )

    prompt_f = run_dir / f"l2-{n}-prompt.txt"
    try:
        prompt_f.write_text(full, encoding="utf-8")
    except Exception:
        pass

    if l2_cmd.lower() in ("antigravity", "agy"):
        project_id = getattr(args, "l2_project_id", "") or None
        if not project_id:
            if agy_mgr is not None:
                csrf, ports, agexe = agy_mgr.ensure_bridge()
                if csrf and ports and agexe:
                    project_id = discover_antigravity_project_id(agexe, csrf, ports)
        model = getattr(args, "l2_model", "flash")
        log_name = f"l2-{n}" if kind == "repair" else "l2"
        return run_l2_antigravity(
            run_dir=run_dir,
            full_prompt=full,
            short_prompt=short,
            n=n,
            scwd=session_cwd,
            conv_holder=driver,
            project_id=project_id,
            model=model,
            timeout_sec=getattr(args, "timeout_sec", 1800),
            log_name=log_name,
            agy_mgr=agy_mgr,
            title=task_title,
            budget=budget,
            request_id=req_id,
            mode=actual_mode,
            task_baseline=baseline,
            evidence_packet=evidence_packet,
        )
    elif l2_cmd.lower() in ("off", "none"):
        log_path = run_dir / f"l2-{n}.log"
        with open(log_path, "wb") as f:
            f.write(b"L2-DISABLED: --l2-cmd set to off\n")
        return L2Result("NO-VERDICT", "L2 disabled", log_path, payload=None)
    else:
        timeout = getattr(args, "timeout_sec", 900)
        if budget:
            timeout = budget.bound_timeout(timeout)
        result = run_l2_agent(l2_cmd, run_dir, full, n, proxy, timeout_sec=timeout)
        if result.payload:
            valid, reason = validate_protocol_payload(
                result.payload, expected_request_id=req_id, expected_task_id=baseline.task_id,
                expected_mode=actual_mode,
                expected_revision=evidence_packet.reviewed_revision if evidence_packet else None,
                task_baseline=baseline, evidence_packet=evidence_packet,
            )
            if not valid:
                return L2Result("PROTOCOL_ERROR", f"协议校验失败: {reason}", result.l2_log)
        elif actual_mode == "REVIEW" and result.verdict in ("PASS", "COMPLETED"):
            return L2Result("INCONCLUSIVE", "缺少结构化审查协议，不能确认完成", result.l2_log)
        return result
