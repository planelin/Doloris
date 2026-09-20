"""
afk_supervisor.acceptance — 显式验收网关与自然语言完成度过滤
=============================================================
支持基于 tasks/<name>/acceptance.md 的刚性断言；
无 acceptance.md 时回退 selftest 或基于原生清单与完成语义的自然验收；
严格防御否定词、未执行的测试、异常中断与假冒完成。
"""

import re
from pathlib import Path
from typing import List, Optional, Set, Tuple

from afk_supervisor.l2.transport import worker_last_message

ASK_MARKERS = (
    "【决策请求】", "【需要决策】", "[决策请求]",
    "需要用户确认", "请确认后继续", "请选择以下", "等待你的指示"
)

DONE_SIGNALS = [
    "已全部完成", "全部完成", "任务已完成", "所有任务已完成", "没有等待执行的后续阶段",
    "无剩余工作", "全部阶段已完成", "已完成所有", "所有要求已完成", "全部进度项",
    "验收通过", "完成交付", "已交付", "全部交付", "已完成交付", "已终结", "确认成功",
    "all tasks completed", "all done", "work complete", "finished all tasks",
    "everything is complete", "all requirements completed"
]


def is_interaction_request(last_msg: str) -> bool:
    """健壮识别 worker 是否停下发起交互/提问/决策请求。"""
    if not last_msg or not last_msg.strip():
        return False
    text = last_msg.strip()
    if any(m in text for m in ASK_MARKERS):
        return True
    keywords = (
        "请选择", "请先选择", "请确认", "请决定", "请回复", "选择以下",
        "哪一类", "偏向哪", "你的偏好", "设计访谈", "烤问", "关键问题", "主要场景",
        "选择一个主要", "主视觉方向", "哪个方案", "请问需要"
    )
    if any(k in text for k in keywords):
        return True
    has_alpha_opts = bool(re.search(r'(?:^|\n)\s*[A-Da-d][\.\、\)]\s+', text))
    has_num_opts = bool(re.search(r'(?:^|\n)\s*[1-4][\.\、\)]\s+', text)) and (
        "推荐" in text or "方案" in text or "选择" in text or "？" in text or "?" in text
    )
    if has_alpha_opts or has_num_opts:
        return True
    if ("？" in text or "?" in text) and any(w in text for w in ("是否", "哪", "怎么", "如何", "选", "哪个", "偏向")):
        return True
    return False


def _acceptance_selftest(work_dir: Path) -> Tuple[bool, str]:
    """selftest 12章格式: work_dir/PROGRESS.md 12项勾选 + 12个章节文件非空。"""
    prog = work_dir / "PROGRESS.md"
    if not prog.exists():
        return False, f"PROGRESS.md 不存在于 {work_dir}"
    try:
        text = prog.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "PROGRESS.md 不可读"
    done = text.count("- [x]") + text.count("- [X]")
    missing = [
        f"ch{i:02d}" for i in range(1, 13)
        if not (work_dir / "chapters" / f"ch{i:02d}.md").exists()
        or (work_dir / "chapters" / f"ch{i:02d}.md").stat().st_size < 100
    ]
    ok = done >= 12 and not missing
    detail = f"PROGRESS勾选={done}/12, 缺失章节={missing or '无'}"
    return ok, detail


def check_acceptance(task_dir: Path, workspace_root: Path, artifact_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """通用显式验收。优先读 tasks/<name>/acceptance.md:
         <glob>              至少匹配1个非空文件 (相对 workspace_root 解析)
         <glob> :N           至少匹配 N 个非空文件
         checklist: <path> :N   文件内 '- [x]' 数量 ≥ N
       无 acceptance.md 时回退 selftest 默认。
       所有路径断言严格统一相对 workspace_root 解析。
    """
    spec = Path(task_dir) / "acceptance.md" if task_dir else None
    if not spec or not spec.exists():
        return _acceptance_selftest(artifact_dir or (Path(workspace_root) / "work"))

    base = Path(workspace_root).resolve()
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


def check_acceptance_natural(
    session_cwd: Path,
    last_msg: str = "",
    min_mtime: float = 0.0,
    title: str = "",
) -> Tuple[bool, str]:
    """无感透明验收。"""
    last_msg = last_msg or ""
    cwd = Path(session_cwd).resolve()

    # 1. 前置守卫: 决策请求
    for m in ASK_MARKERS:
        if m in last_msg:
            return False, f"worker正在等待交互决策代答 ({m})"

    # 2. 前置守卫: 暂停/中断状态
    PAUSE_MARKERS = ("已暂停", "任务已暂停", "已中断", "已停止", "paused", "aborted", "interrupted")
    for m in PAUSE_MARKERS:
        if m in last_msg:
            return False, f"worker处于暂停/中断状态 ('{m}'), 任务未完工"

    # 3. 前置守卫: 明确失败与报错标记
    FAILURE_MARKERS = (
        "仍然失败", "测试失败", "测试仍然失败", "构建失败", "编译失败",
        "报错", "未通过", "仍然报错", "出现异常", "failed", "failure", "error:"
    )
    for f in FAILURE_MARKERS:
        if f in last_msg.lower():
            return False, f"检测到失败标记 ('{f}'), 任务未完工"

    NEGATION_WORDS = (
        "尚未", "未", "没有", "无法", "不能", "并非", "不", "仍未", "未曾",
        "not", "never", "hasn't", "haven't", "didn't", "cannot", "unable"
    )

    EXCLUDE_DIR_NAMES = {
        "work", "afk-work", "runs", "scratch", "dist", "build", "target", "out",
        ".git", ".github", ".codex", ".gemini", ".agents", ".vscode", ".idea",
        "node_modules", "__pycache__", ".venv", "venv", "env"
    }

    def is_excluded_path(p: Path) -> bool:
        try:
            rel = p.relative_to(cwd)
            parts = rel.parts
        except ValueError:
            parts = p.parts
        for part in parts:
            part_low = part.lower()
            if part_low in EXCLUDE_DIR_NAMES:
                return True
            if part_low.startswith(("backup-", "work-", "afk-", "test-", "work")):
                return True
        return False

    checklist_candidates = []
    seen: Set[Path] = set()

    def add_candidate(p: Path):
        try:
            p_res = p.resolve()
            if p_res.is_file() and not is_excluded_path(p_res) and p_res not in seen:
                seen.add(p_res)
                checklist_candidates.append(p_res)
        except Exception:
            pass

    # 提取子目录
    target_subs = set()
    for text_source in (title or "", last_msg or ""):
        for match in re.finditer(r'([a-zA-Z0-9_\-]+)[/\\]', text_source):
            sub = match.group(1).strip()
            if sub and not is_excluded_path(cwd / sub):
                target_subs.add(sub)

    for sub in target_subs:
        for name in ("PROGRESS.md", "TODO.md", "checklist.md"):
            cand = cwd / sub / name
            if cand.exists():
                add_candidate(cand)

    for m in re.finditer(r'([a-zA-Z0-9_\-/\\]+\.md)', last_msg):
        rel_str = m.group(1).replace("\\", "/")
        cand = cwd / rel_str
        if cand.exists():
            add_candidate(cand)

    for name in ("PROGRESS.md", "TODO.md", "checklist.md"):
        root_cand = cwd / name
        if root_cand.exists():
            add_candidate(root_cand)

    if checklist_candidates:
        checklist_candidates.sort(
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True
        )

        for primary in checklist_candidates:
            try:
                txt = primary.read_text(encoding="utf-8", errors="replace")
                done = txt.count("- [x]") + txt.count("- [X]")
                todo = txt.count("- [ ]")
                try:
                    rel_path = primary.relative_to(cwd)
                except ValueError:
                    rel_path = primary.name

                if todo > 0:
                    return False, f"项目清单仍有未完成项 ({rel_path}: 勾选{done}, 剩余{todo})"
                elif done >= 1 and todo == 0:
                    if min_mtime > 0 and primary.stat().st_mtime < min_mtime:
                        continue
                    return True, f"项目清单已全部勾选完成 ({rel_path}: 勾选{done}, 剩余0)"
            except OSError:
                pass

    def is_real_negation(prefix_str: str, neg_word: str) -> bool:
        if neg_word == "不":
            cleaned = re.sub(r"(?:还|挺|真|很)?不错|不仅|不得不|不妨", "", prefix_str)
            return "不" in cleaned
        return neg_word in prefix_str

    negation_detected = None
    for sig in DONE_SIGNALS:
        idx = last_msg.find(sig)
        while idx != -1:
            prefix = last_msg[max(0, idx - 15):idx].strip()
            neg_matches = [neg for neg in NEGATION_WORDS if is_real_negation(prefix, neg)]
            if neg_matches:
                negation_detected = f"检测到否定语义 ('{neg_matches[0]}{sig}'), 任务未完工"
            elif not any(p in last_msg for p in PAUSE_MARKERS):
                return True, f"识别到自然完工语义: '{sig}'"
            idx = last_msg.find(sig, idx + len(sig))

    if negation_detected:
        return False, negation_detected

    return False, "未检测到活跃的已完成清单或明确完工语义"


def check_acceptance_quick(work_dir: Path, last_msg: str = "") -> Tuple[bool, str]:
    return check_acceptance_natural(work_dir, last_msg)
