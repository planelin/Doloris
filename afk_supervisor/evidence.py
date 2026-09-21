"""
afk_supervisor.evidence — 客观证据采集与双版本哈希引擎
======================================================
收集客观交付证据，执行轻量自动化机械核验。
区分“产物内容版本 (artifact_revision)”与“审查包版本 (reviewed_revision)”，
避免纯时间戳 (mtime) 刷新或监管日志变动伪装成业务进展。
严格保护交付目录：若交付目录不存在或不可写，严禁扫描 cwd 冒充成功。
"""

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from afk_supervisor.models import EvidenceItem, EvidencePacket
from afk_supervisor.baseline import TaskBaseline, delivery_path_blockers


def compute_file_sha256(path: Path, chunk_size: int = 65536) -> str:
    """Hash the whole file in bounded chunks, reporting read/race failures to the caller."""
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        before = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(max(1, chunk_size)), b""):
            hasher.update(chunk)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError(f"文件在采证读取期间发生变动: {path}")
    return hasher.hexdigest()


def compute_file_sha256_short(path: Path, max_bytes: int = 65536) -> str:
    """Legacy short display hash; max_bytes is now the read chunk size, not a limit."""
    try:
        return compute_file_sha256(path, max_bytes)[:12]
    except OSError:
        return ""


def _revision(prefix: str, data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def calculate_artifact_revision(task_id: str, delivery_dir: Path, items: List[EvidenceItem]) -> str:
    """Content identity, excluding timestamps, supervisor logs and progress checklists."""
    files = [
        (it.id, it.path, it.size, it.sha256 or it.sha256_short)
        for it in sorted(items, key=lambda item: item.id) if it.category == "artifact"
    ]
    return _revision("art-", [task_id, str(delivery_dir.resolve()), files])


def calculate_reviewed_revision(
    task_id: str,
    delivery_dir: Path,
    items: List[EvidenceItem],
    artifact_rev: str = "",
    task_baseline: Optional[TaskBaseline] = None,
    mechanical_failures: Optional[List[str]] = None,
) -> str:
    """Bind evidence contents/results and requirements, but not request IDs or mtime."""
    contents = []
    for item in sorted(items, key=lambda item: item.id):
        data = item.to_dict()
        data.pop("mtime", None)
        contents.append(data)
    return _revision("rev-", {
        "task_id": task_id, "delivery_dir": str(delivery_dir.resolve()),
        "artifact_revision": artifact_rev, "items": contents,
        "baseline": task_baseline.to_dict() if task_baseline else None,
        "mechanical_failures": sorted(mechanical_failures or []),
    })


def collect_evidence(
    session_cwd: Path,
    last_msg: str,
    task_baseline: TaskBaseline,
    min_mtime: float = 0.0,
    task_dir: Optional[Path] = None,
    request_id: str = "",
    verification_runner=None,
) -> EvidencePacket:
    """收集客观交付证据并执行轻量自动化机械核验。
    已有成果满足要求不强制要求刷新时间戳。
    若目标交付目录不存在或与环境冲突，绝不静默扫描 cwd。
    """
    cwd = Path(session_cwd).resolve()
    target_dir_str = task_baseline.effective_delivery_dir or task_baseline.requested_delivery_dir
    deliv_dir = Path(target_dir_str).resolve() if target_dir_str else cwd

    items: List[EvidenceItem] = []
    mechanical_failures: List[str] = []

    # Recheck environment blockers, while retaining authority/other baseline blockers.
    # Never rewrite the authorized directory or widen writable roots after a repair.
    for blocker in task_baseline.blockers:
        if blocker not in task_baseline.path_blockers:
            mechanical_failures.append(f"基线阻塞: {blocker}")
    mechanical_failures.extend(delivery_path_blockers(str(deliv_dir), task_baseline.writable_roots))

    # 1. 检查目标交付目录存在性 (严禁静默回退到 cwd 扫描)
    if not deliv_dir.is_dir():
        fail_msg = f"目标交付目录不存在: '{deliv_dir}' (严禁静默扫描当前目录冒充交付)"
        mechanical_failures.append(fail_msg)
        items.append(EvidenceItem(
            id="ev_err_delivery_dir_missing",
            category="runtime_check",
            summary=fail_msg,
            details=str(deliv_dir),
        ))
        # 即使目录不存在，仍保留 Worker 留言供诊断
        if last_msg:
            items.append(EvidenceItem(
                id="ev_worker_last_msg",
                category="worker_statement",
                summary=f"Worker最后留言 (长度{len(last_msg)})",
                details=last_msg[:1200],
            ))
        art_rev = calculate_artifact_revision(task_baseline.task_id, deliv_dir, items)
        rev = calculate_reviewed_revision(task_baseline.task_id, deliv_dir, items, art_rev, task_baseline, mechanical_failures)
        return EvidencePacket(
            task_id=task_baseline.task_id,
            delivery_dir=str(deliv_dir),
            reviewed_revision=rev,
            artifact_revision=art_rev,
            request_id=request_id or f"req-{uuid.uuid4().hex[:8]}",
            items=items,
            mechanical_failures=mechanical_failures,
        )

    # 2. 扫描交付目录下的真实产物文件 (排除系统、日志与临时目录)
    EXCLUDE_DIRS = {
        "runs", "afk-work", "scratch", "dist", "build", "node_modules",
        "__pycache__", ".git", ".github", ".codex", ".gemini", ".agents",
        ".vscode", ".idea", ".venv", "venv", "env"
    }

    scanned_files = []
    try:
        for root, dirs, files in os.walk(deliv_dir, onerror=lambda error: mechanical_failures.append(f"扫描交付目录发生异常: {error}")):
            dirs[:] = [
                d for d in dirs
                if d.lower() not in EXCLUDE_DIRS and not d.lower().startswith(("backup-", "afk-", "runs"))
            ]
            dirs.sort()
            for f in sorted(files):
                p = Path(root) / f
                if p.name.lower() not in {"progress.md", "todo.md", "checklist.md"}:
                    scanned_files.append(p)
    except Exception as e:
        mechanical_failures.append(f"扫描交付目录发生异常: {e}")

    scanned_files.sort(key=lambda p: p.relative_to(deliv_dir).as_posix())
    # Preserve legacy readable IDs unless punctuation/separators make two paths
    # collide; artifact and syntax entries must use the same disambiguation.
    path_ids = {
        p: p.relative_to(deliv_dir).as_posix().replace("/", "_").replace(".", "_")
        for p in scanned_files
    }
    id_counts = {}
    for clean_id in path_ids.values():
        id_counts[clean_id] = id_counts.get(clean_id, 0) + 1
    for p, clean_id in path_ids.items():
        if id_counts[clean_id] > 1:
            suffix = hashlib.sha256(p.relative_to(deliv_dir).as_posix().encode("utf-8")).hexdigest()
            path_ids[p] = f"{clean_id}_{suffix}"
    for p in scanned_files:
        try:
            st = p.stat()
            if st.st_size > 0:
                rel = p.relative_to(deliv_dir)
                clean_id = path_ids[p]
                sha = compute_file_sha256(p)
                items.append(EvidenceItem(
                    id=f"ev_file_{clean_id}",
                    category="artifact",
                    summary=f"产物文件: {rel} ({st.st_size} 字节)",
                    path=str(rel),
                    mtime=st.st_mtime,
                    size=st.st_size,
                    sha256_short=sha[:12],
                    sha256=sha,
                ))
        except (OSError, ValueError) as error:
            mechanical_failures.append(f"产物读取失败 ({p}): {error}")

    # 3. Worker 最后留言
    if last_msg:
        items.append(EvidenceItem(
            id="ev_worker_last_msg",
            category="worker_statement",
            summary=f"Worker最后留言 (长度{len(last_msg)})",
            details=last_msg[:1200],
        ))

    # 4. 进度清单 (PROGRESS.md / TODO.md / checklist.md)
    for name in ("PROGRESS.md", "TODO.md", "checklist.md"):
        cand = deliv_dir / name
        if not cand.exists() and cwd != deliv_dir:
            cand = cwd / name
        if cand.exists():
            try:
                txt = cand.read_text(encoding="utf-8", errors="replace")
                done = txt.count("- [x]") + txt.count("- [X]")
                todo = txt.count("- [ ]")
                clean_name = name.replace(".", "_")
                items.append(EvidenceItem(
                    id=f"ev_chk_{clean_name}",
                    category="progress_doc",
                    summary=f"清单 {name}: 勾选={done}, 待办={todo}",
                    path=name,
                    mtime=cand.stat().st_mtime,
                    size=cand.stat().st_size,
                    sha256=compute_file_sha256(cand),
                    details=txt[:1000],
                ))
            except Exception as error:
                mechanical_failures.append(f"清单读取失败 ({cand}): {error}")

    # 5. 本地自动化机械核验 (语法检查与显式断言)
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # (a) JS 语法检查 (node --check)
    js_files = [p for p in scanned_files if p.suffix.lower() == ".js"]
    for js_p in js_files:
        rel = js_p.relative_to(deliv_dir)
        clean_id = path_ids[js_p]
        try:
            r = subprocess.run(["node", "--check", str(js_p)], capture_output=True, text=True, timeout=10, creationflags=no_win)
            if r.returncode != 0:
                err_out = (r.stderr or r.stdout or "").strip()[:400]
                mechanical_failures.append(f"JS语法检查失败 ({rel}): {err_out[:100]}")
                items.append(EvidenceItem(
                    id=f"ev_syntax_js_{clean_id}",
                    category="verification_result",
                    summary=f"JS语法检查失败: {rel}",
                    details=err_out,
                ))
            else:
                items.append(EvidenceItem(
                    id=f"ev_syntax_js_{clean_id}",
                    category="verification_result",
                    summary=f"JS语法检查通过: {rel}",
                ))
        except FileNotFoundError:
            # 运行器缺失不伪装通过
            err_msg = f"未找到 Node.js 执行程序，无法完成 JS 语法核验 ({rel})"
            mechanical_failures.append(err_msg)
            items.append(EvidenceItem(
                id=f"ev_syntax_js_{clean_id}",
                category="verification_result",
                summary=err_msg,
                details="Node executable not found",
            ))
        except subprocess.TimeoutExpired:
            err_msg = f"JS 语法检查执行超时 ({rel})"
            mechanical_failures.append(err_msg)
            items.append(EvidenceItem(
                id=f"ev_syntax_js_{clean_id}",
                category="verification_result",
                summary=err_msg,
            ))
        except Exception as e:
            err_msg = f"JS 语法核验异常 ({rel}): {e}"
            mechanical_failures.append(err_msg)
            items.append(EvidenceItem(
                id=f"ev_syntax_js_{clean_id}",
                category="verification_result",
                summary=err_msg,
            ))

    # (b) Compile without execution or bytecode writes in the reviewed directory.
    py_files = [p for p in scanned_files if p.suffix.lower() == ".py"]
    for py_p in py_files:
        rel = py_p.relative_to(deliv_dir)
        clean_id = path_ids[py_p]
        try:
            r = subprocess.run([sys.executable, "-I", "-B", "-c", "import pathlib, sys; compile(pathlib.Path(sys.argv[1]).read_bytes(), sys.argv[1], 'exec')", str(py_p)], capture_output=True, text=True, timeout=10, creationflags=no_win)
            if r.returncode != 0:
                err_out = (r.stderr or r.stdout or "").strip()[:400]
                mechanical_failures.append(f"Python语法检查失败 ({rel}): {err_out[:100]}")
                items.append(EvidenceItem(
                    id=f"ev_syntax_py_{clean_id}",
                    category="verification_result",
                    summary=f"Python语法检查失败: {rel}",
                    details=err_out,
                ))
            else:
                items.append(EvidenceItem(
                    id=f"ev_syntax_py_{clean_id}",
                    category="verification_result",
                    summary=f"Python语法检查通过: {rel}",
                ))
        except subprocess.TimeoutExpired:
            err_msg = f"Python 语法检查执行超时 ({rel})"
            mechanical_failures.append(err_msg)
            items.append(EvidenceItem(
                id=f"ev_syntax_py_{clean_id}",
                category="verification_result",
                summary=err_msg,
            ))
        except Exception as e:
            err_msg = f"Python 语法核验异常 ({rel}): {e}"
            mechanical_failures.append(err_msg)
            items.append(EvidenceItem(
                id=f"ev_syntax_py_{clean_id}",
                category="verification_result",
                summary=err_msg,
            ))

    # (c) 显式 acceptance.md 规格核验 (机械断言)
    if task_dir:
        spec = Path(task_dir) / "acceptance.md"
        if spec.exists():
            problems = []
            try:
                for raw in spec.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.lower().startswith("checklist:"):
                        parts = [p.strip() for p in line[len("checklist:"):].split(":") if p.strip()]
                        path_str, need = parts[0], (int(parts[1]) if len(parts) > 1 else 1)
                        target_file = cwd / path_str
                        if not target_file.exists():
                            problems.append(f"{path_str} 不存在")
                            continue
                        txt = target_file.read_text(encoding="utf-8", errors="replace")
                        d_count = txt.count("- [x]") + txt.count("- [X]")
                        if d_count < need:
                            problems.append(f"{path_str} 勾选{d_count}<{need}")
                    else:
                        parts = line.rsplit(":", 1)
                        if len(parts) == 2 and parts[1].strip().isdigit():
                            pat, need = parts[0].strip(), int(parts[1])
                        else:
                            pat, need = line, 1
                        hits = [m for m in cwd.glob(pat) if m.is_file() and m.stat().st_size > 0]
                        if len(hits) < need:
                            problems.append(f"{pat} 非空文件{len(hits)}<{need}")
            except Exception as e:
                problems.append(f"读取 acceptance.md 发生异常: {e}")

            if problems:
                detail_str = "; ".join(problems[:4])
                mechanical_failures.append(f"显式验收未满足: {detail_str}")
                items.append(EvidenceItem(
                    id="ev_spec_acceptance",
                    category="verification_result",
                    summary=f"显式验收未满足: {detail_str}",
                    details=detail_str,
                ))
            else:
                items.append(EvidenceItem(
                    id="ev_spec_acceptance",
                    category="verification_result",
                    summary="显式验收全部满足",
                    details="全部满足",
                ))

    # 6. 计算双版本哈希
    art_rev = calculate_artifact_revision(task_baseline.task_id, deliv_dir, items)
    for item in items:
        if item.id.startswith("ev_syntax_"):
            item.verification_kind = "syntax"
            item.status = "PASS" if "检查通过" in item.summary else "FAIL"
            item.artifact_revision = art_rev
        elif item.id == "ev_spec_acceptance":
            item.verification_kind = "acceptance_spec"
            item.status = "PASS" if item.details == "全部满足" else "FAIL"
            item.artifact_revision = art_rev
            item.criterion_ids = [c["id"] for c in task_baseline.required_criteria if c.get("type") == "acceptance_spec"]
    if verification_runner is not None:
        executed, failures = verification_runner.collect(
            art_rev, task_baseline,
            lambda: collect_evidence(session_cwd, last_msg, task_baseline, task_dir=task_dir).artifact_revision,
        )
        items.extend(executed)
        mechanical_failures.extend(failures)
    rev = calculate_reviewed_revision(task_baseline.task_id, deliv_dir, items, art_rev, task_baseline, mechanical_failures)

    return EvidencePacket(
        task_id=task_baseline.task_id,
        delivery_dir=str(deliv_dir),
        reviewed_revision=rev,
        artifact_revision=art_rev,
        request_id=request_id or f"req-{uuid.uuid4().hex[:8]}",
        items=items,
        mechanical_failures=mechanical_failures,
    )
