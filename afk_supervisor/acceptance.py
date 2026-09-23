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

# cli.py 从本模块再导出该符号；保留以免破坏既有调用方。
from afk_supervisor.l2.transport import worker_last_message  # noqa: F401

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
    """Legacy selftest 12章格式: work_dir/PROGRESS.md 12项勾选 + 12个章节文件非空。

    该契约只适用于章节式写作任务，必须由 --selftest-12ch 显式启用，
    不能作为通用任务的默认验收标准。
    """
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


def _is_within(base: Path, target: Path) -> bool:
    try:
        Path(target).resolve().relative_to(base.resolve())
        return True
    except (OSError, ValueError):
        return False


def _resolve_spec_path(base: Path, raw_path: str, label: str = "") -> Tuple[Optional[Path], str]:
    """把验收规范中的相对路径解析到工作区根内；拒绝绝对路径与目录穿越。"""
    text = raw_path.strip().strip('"').strip("'")
    shown = label or text
    if not text:
        return None, "空路径断言"
    if Path(text).is_absolute() or re.match(r"^[a-zA-Z]:", text) or text.startswith(("\\\\", "//")):
        return None, f"{shown} 使用绝对路径/UNC，验收规范只允许工作区相对路径"
    parts = [part for part in re.split(r"[\\/]+", text) if part not in ("", ".")]
    if ".." in parts:
        return None, f"{shown} 含 .. 路径穿越，已拒绝"
    resolved = (base / text).resolve()
    if not _is_within(base, resolved):
        return None, f"{shown} 解析后越出工作区根目录，已拒绝"
    return resolved, ""


_GLOB_CLASS_RE = re.compile(r"\[([^\]]*)\]")


def _glob_probe(pattern: str) -> str:
    """把 glob 模式展开为等价字面路径，供穿越预检使用。

    字符集按最保守的字面含义处理: 只要类内可能出现 '.' 就当作点，
    使 [.a][.a]/x、.[.]/[.]x 这类等价于 ../x 的写法同样被拒绝，
    不依赖 glob 实现是否会把点分量交给字符类匹配。
    其余通配符替换为 x 以保持路径形状。
    """
    def expand_class(match) -> str:
        body = match.group(1).lstrip("!^")
        return "." if "." in body else "x"
    probe = _GLOB_CLASS_RE.sub(expand_class, pattern)
    return probe.replace("*", "x").replace("?", "x")


def evaluate_acceptance_spec(spec: Path, base: Path) -> Tuple[bool, str]:
    """刚性断言评估；acceptance.py 与 evidence.py 共用同一实现与同一基准。

    支持的断言形式 (全部相对 base 解析):
         <glob>                至少匹配 1 个非空文件
         <glob> :N             至少匹配 N 个非空文件
         checklist: <path> :N  文件内 '- [x]' 数量 ≥ N
    空规范、仅注释、绝对路径、.. 穿越一律判失败。
    """
    base = Path(base).resolve()
    try:
        lines = Path(spec).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        return False, f"验收规范不可读: {error}"

    problems: List[str] = []
    assertions = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assertions += 1
        if line.lower().startswith("checklist:"):
            body = line[len("checklist:"):].strip()
            head, sep, tail = body.rpartition(":")
            if sep and tail.strip().isdigit():
                path_text, need = head.strip(), int(tail.strip())
            else:
                path_text, need = body, 1
            if need < 1:
                problems.append(f"checklist: {path_text or '(空)'} 断言语义无效 (N<1)")
                continue
            target, error = _resolve_spec_path(base, path_text)
            if error:
                problems.append(error)
                continue
            if not target.is_file():
                problems.append(f"{path_text} 不存在")
                continue
            try:
                txt = target.read_text(encoding="utf-8", errors="replace")
            except OSError as read_error:
                problems.append(f"{path_text} 不可读: {read_error}")
                continue
            done = txt.count("- [x]") + txt.count("- [X]")
            if done < need:
                problems.append(f"{path_text} 勾选{done}<{need}")
        else:
            head, sep, tail = line.rpartition(":")
            if sep and tail.strip().isdigit():
                pat, need = head.strip(), int(tail.strip())
            else:
                pat, need = line, 1
            if need < 1:
                problems.append(f"{pat or '(空)'} 断言语义无效 (N<1)")
                continue
            _, error = _resolve_spec_path(base, _glob_probe(pat), label=pat)
            if error:
                problems.append(error)
                continue
            hits = []
            try:
                for match in base.glob(pat):
                    try:
                        if match.is_file() and match.stat().st_size > 0 and _is_within(base, match):
                            hits.append(match)
                    except OSError:
                        continue
            except (OSError, ValueError) as glob_error:
                problems.append(f"{pat} 匹配失败: {glob_error}")
                continue
            if len(hits) < need:
                problems.append(f"{pat} 非空文件{len(hits)}<{need}")

    if assertions == 0:
        return False, f"验收规范为空或没有任何有效断言: {spec}"
    return (not problems), ("全部满足" if not problems else "; ".join(problems[:4]))


def _acceptance_default_contract(artifact_dir: Path) -> Tuple[bool, str]:
    """无 acceptance.md 时的通用兜底契约 (与 task.md 基线声明保持一致):
    PROGRESS.md 全部勾选且无待办，report.md 非空。
    """
    artifact_dir = Path(artifact_dir)
    problems: List[str] = []
    prog = artifact_dir / "PROGRESS.md"
    if not prog.is_file():
        problems.append(f"PROGRESS.md 不存在于 {artifact_dir}")
    else:
        try:
            txt = prog.read_text(encoding="utf-8", errors="replace")
        except OSError:
            problems.append("PROGRESS.md 不可读")
        else:
            done = txt.count("- [x]") + txt.count("- [X]")
            todo = txt.count("- [ ]")
            if done < 1:
                problems.append("PROGRESS.md 勾选0<1")
            if todo > 0:
                problems.append(f"PROGRESS.md 仍有{todo}项待办")
    report = artifact_dir / "report.md"
    if not report.is_file() or report.stat().st_size <= 0:
        problems.append("report.md 不存在或为空")
    detail = "默认契约满足: PROGRESS.md 全勾选且 report.md 非空" if not problems else "; ".join(problems[:4])
    return (not problems), detail


def check_acceptance(
    task_dir: Path,
    workspace_root: Path,
    artifact_dir: Optional[Path] = None,
    selftest_12ch: bool = False,
) -> Tuple[bool, str]:
    """通用显式验收。优先读 tasks/<name>/acceptance.md:
         <glob>              至少匹配1个非空文件 (相对 workspace_root 解析)
         <glob> :N           至少匹配 N 个非空文件
         checklist: <path> :N   文件内 '- [x]' 数量 ≥ N

    无 acceptance.md 时默认执行 task.md 声明的通用契约；
    章节式 12 章 selftest 仅在 selftest_12ch=True 时启用。
    所有路径断言严格统一相对 workspace_root 解析。
    """
    spec = Path(task_dir) / "acceptance.md" if task_dir else None
    if spec and spec.is_file():
        return evaluate_acceptance_spec(spec, Path(workspace_root).resolve())
    if selftest_12ch:
        return _acceptance_selftest(artifact_dir or (Path(workspace_root).resolve() / "work"))
    return _acceptance_default_contract(artifact_dir or Path(workspace_root).resolve())


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

    def is_real_negation(prefix_str: str, neg_word: str) -> bool:
        if neg_word == "不":
            cleaned = re.sub(r"(?:还|挺|真|很)?不错|不仅|不得不|不妨", "", prefix_str)
            return "不" in cleaned
        return neg_word in prefix_str

    def find_positive_done_signal() -> Tuple[bool, str]:
        """识别未被否定/暂停语境覆盖的完工语义，空字符串表示未命中。"""
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
        return False, ""

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
                    # 清单由 Worker 自己写入，不能单独构成完工证据；本回合
                    # (min_mtime 之后) 新写入的全勾选清单必须同时有明确完工语义。
                    if min_mtime > 0:
                        signaled, signal_reason = find_positive_done_signal()
                        if not signaled:
                            detail = signal_reason or (
                                f"项目清单已全勾选 ({rel_path}: 勾选{done}, 剩余0)，"
                                "但最终消息缺少明确完工语义；拒绝仅凭 Worker 自写清单放行"
                            )
                            return False, detail
                        return True, (
                            f"项目清单已全部勾选完成 ({rel_path}: 勾选{done}, 剩余0)；"
                            f"{signal_reason}"
                        )
                    return True, f"项目清单已全部勾选完成 ({rel_path}: 勾选{done}, 剩余0)"
            except OSError:
                pass

    signaled, signal_reason = find_positive_done_signal()
    if signaled:
        return True, signal_reason
    if signal_reason:
        return False, signal_reason

    return False, "未检测到活跃的已完成清单或明确完工语义"


def check_acceptance_quick(work_dir: Path, last_msg: str = "") -> Tuple[bool, str]:
    return check_acceptance_natural(work_dir, last_msg)
