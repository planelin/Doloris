"""
afk_supervisor.baseline — 任务基线与路径/授权管理
=================================================
严格分离 session_cwd、requested_delivery_dir、effective_delivery_dir、writable_roots；
保护用户显式指定交付目录，拦截未授权路径改写，严格区分用户本人确认与主管代答范围。
"""

import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class TaskBaseline:
    task_id: str
    original_requirements: str
    subsequent_changes: List[Dict[str, Any]] = field(default_factory=list)
    session_cwd: str = ""
    requested_delivery_dir: str = ""
    effective_delivery_dir: str = ""
    writable_roots: List[str] = field(default_factory=list)
    delivery_dir: str = ""
    _delivery_dir: str = ""
    required_criteria: List[Dict[str, Any]] = field(default_factory=list)
    authorized_delegation_scope: str = "技术方向/方案选择/参数决策/依赖取舍/故障诊断，不涉及资金、删除数据、对外发布"
    human_confirmation_required: List[str] = field(default_factory=list)
    delegated_topics: List[str] = field(default_factory=lambda: [
        "技术方向", "方案选择", "参数决策", "依赖取舍", "故障诊断"
    ])
    unconfirmed_requirements: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    path_blockers: List[str] = field(default_factory=list)
    baseline_version: int = 1
    has_full_spec: bool = True

    def __post_init__(self):
        target = self.delivery_dir or self._delivery_dir
        if target:
            self.delivery_dir = target
            self._delivery_dir = target
            if not self.requested_delivery_dir:
                self.requested_delivery_dir = target
            if not self.effective_delivery_dir and not self.blockers:
                self.effective_delivery_dir = target
        else:
            self.delivery_dir = self.effective_delivery_dir or self.requested_delivery_dir or self.session_cwd
            self._delivery_dir = self.delivery_dir

    def add_authorized_change(self, source: str, change_description: str):
        """记录来自可信来源的用户授权变更，并更新基线版本。"""
        self.baseline_version += 1
        self.subsequent_changes.append({
            "version": self.baseline_version,
            "ts": datetime.now().isoformat(),
            "source": source,
            "change": change_description,
        })

    def requires_human_confirmation(self, question_or_context: str) -> Tuple[bool, str]:
        """检查 Worker 的问题是否命中必须由用户本人确认的事项。"""
        if not question_or_context:
            return False, ""
        q_low = question_or_context.lower()
        for item in self.human_confirmation_required:
            it_low = item.lower()
            # 提取核心关键词 (去掉'向用户确认'等前缀)
            clean_kw = re.sub(r'^(?:向用户确认|请示用户|需用户确认|确认)\s*', '', it_low).strip()
            if clean_kw and clean_kw in q_low:
                return True, f"命中用户明确指定的本人确认项: '{item}'"
        return False, ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["delivery_dir"] = self.delivery_dir
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskBaseline":
        raw_deliv = data.get("delivery_dir", "")
        req_deliv = data.get("requested_delivery_dir", "") or raw_deliv
        eff_deliv = data.get("effective_delivery_dir", "") or raw_deliv
        scwd = data.get("session_cwd", "")

        # 兼容旧格式中的 subsequent_changes 为字符串列表
        changes = data.get("subsequent_changes", [])
        norm_changes = []
        for c in changes:
            if isinstance(c, dict):
                norm_changes.append(c)
            else:
                norm_changes.append({"version": 1, "source": "legacy", "change": str(c)})

        return cls(
            task_id=data.get("task_id", "unknown"),
            original_requirements=data.get("original_requirements", ""),
            subsequent_changes=norm_changes,
            session_cwd=scwd,
            requested_delivery_dir=req_deliv,
            effective_delivery_dir=eff_deliv,
            writable_roots=data.get("writable_roots", [scwd] if scwd else []),
            _delivery_dir=raw_deliv,
            required_criteria=data.get("required_criteria", []),
            authorized_delegation_scope=data.get("authorized_delegation_scope", ""),
            human_confirmation_required=data.get("human_confirmation_required", []),
            delegated_topics=data.get("delegated_topics", []),
            unconfirmed_requirements=data.get("unconfirmed_requirements", []),
            blockers=data.get("blockers", []),
            path_blockers=data.get("path_blockers", []),
            baseline_version=data.get("baseline_version", 1),
            has_full_spec=data.get("has_full_spec", True),
        )

    def persist(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


def _is_path_within_roots(path_str: str, roots: List[str]) -> bool:
    """检查指定路径是否在允许的可写根目录树内。"""
    try:
        target = Path(path_str).resolve()
        for r in roots:
            root_p = Path(r).resolve()
            if target == root_p or root_p in target.parents:
                return True
        return False
    except Exception:
        return False


def extract_explicit_delivery_dir(text: str) -> Optional[str]:
    """从文本中提取用户显式指定的交付目录（若无则返回 None）。"""
    if not text:
        return None
    cleaned = re.sub(r"\\([_`*~])", r"\1", text)
    kw_abs_match = re.search(
        r'(?:工作区|交付目录|delivery_dir|workspace)[\s:：=]*([a-zA-Z]:[\\/][^\s,，;；"\'\r\n]+)',
        cleaned,
        re.IGNORECASE,
    )
    if kw_abs_match:
        return kw_abs_match.group(1).rstrip('\\/`。')
    abs_matches = re.findall(r'(?<![a-zA-Z0-9])([a-zA-Z]:[\\/][^\s,，;；"\'`。\r\n]+)', text)
    for cand_path in abs_matches:
        cand_p = cand_path.rstrip('\\/`。')
        if not cand_p.lower().endswith(('.py', '.cmd', '.bat', '.sh', '.json', '.md', '.log')):
            return cand_p
    return None


def delivery_path_blockers(path_str: str, roots: List[str]) -> List[str]:
    """Read-only environment checks; never creates directories or changes permissions."""
    target = Path(path_str)
    problems = []
    if not target.exists():
        problems.append(f"请求的交付目录不存在: '{path_str}'")
    elif not target.is_dir():
        problems.append(f"请求的交付路径不是目录: '{path_str}'")
    if roots and not _is_path_within_roots(path_str, roots):
        problems.append(f"请求的交付目录 '{path_str}' 不在会话授权可写根目录范围 ({roots}) 内，属于跨工作区未授权路径")
    if target.is_dir() and not os.access(target, os.W_OK):
        problems.append(f"请求的交付目录无写权限: '{path_str}'")
    return problems


def _human_confirmations(text: str) -> List[str]:
    patterns = [
        r'(?:向用户确认|请示用户|需用户确认|向用户请示)([^,，;；。\n]+)',
        r'([^,，;；。\n]+?)(?:再向用户确认|经用户确认|由用户确认)(?:后再继续|再继续|方可继续)?',
        r'确认([^,，;；。\n]+?)(?:后继续|再继续)',
    ]
    return list(dict.fromkeys(m.group(0).strip() for pat in patterns for m in re.finditer(pat, text)))


def _resolve_delivery_dir(cwd: Path, texts: List[str], explicit: Optional[str], default: Path) -> str:
    # A current CLI flag overrides historical text. Otherwise use the latest user direction.
    if explicit:
        return str((cwd / explicit).resolve())
    for text in reversed(texts):
        detected = extract_explicit_delivery_dir(text)
        if not detected:
            match = re.search(r'(?:工作区|交付目录|delivery_dir|workspace)[\s:：=]*([a-zA-Z0-9_\-]+[\\/][^\s,，;；"\'\r\n]*)', text, re.IGNORECASE)
            detected = match.group(1) if match else None
        if detected:
            return str((cwd / detected).resolve())
    return str(default.resolve())


def extract_task_baseline(
    rollout_path: Optional[Path] = None,
    session_cwd: Optional[Path] = None,
    title: str = "",
    task_md: Optional[Path] = None,
    work_dir: str = "work",
    explicit_delivery_dir: Optional[str] = None,
    writable_roots: Optional[List[str]] = None,
) -> TaskBaseline:
    """提取并持久化任务基线:
    - 优先解析用户显式指定路径 (绝对路径/明确工作区前缀)，严禁被 cwd 下子目录猜测覆盖；
    - 分离 session_cwd、requested_delivery_dir、effective_delivery_dir、writable_roots；
    - 路径不存在、不可写或冲突时记录 blocker，不静默改写；
    - 提取'向用户确认...'等明确不可代答的人工确认项。
    """
    cwd = Path(session_cwd).resolve() if session_cwd else Path.cwd().resolve()
    w_roots = [str(Path(r).resolve()) for r in (writable_roots or [str(cwd)])]

    blockers: List[str] = []
    unconfirmed: List[str] = []
    human_confirmations: List[str] = []

    # 1. 显式 task_md 模式 (基线由 tasks/<name>/task.md 与 acceptance.md 严格界定)
    if task_md and Path(task_md).exists():
        task_p = Path(task_md).resolve()
        task_id = task_p.parent.name
        orig_req = task_p.read_text(encoding="utf-8", errors="replace")
        req_deliv = _resolve_delivery_dir(cwd, [orig_req], explicit_delivery_dir, cwd / work_dir)
        path_problems = delivery_path_blockers(req_deliv, w_roots)
        eff_deliv = req_deliv if not path_problems else ""

        criteria = []
        spec = task_p.parent / "acceptance.md"
        if spec.exists():
            for line in spec.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    criteria.append({
                        "id": f"crit_spec_{len(criteria)+1}",
                        "description": line,
                        "type": "acceptance_spec",
                    })
        else:
            criteria = [
                {"id": "crit_progress_checklist", "description": "PROGRESS.md 勾选完成", "type": "checklist"},
                {"id": "crit_report", "description": "report.md 非空报告", "type": "artifact"},
            ]
        return TaskBaseline(
            task_id=task_id,
            original_requirements=orig_req,
            session_cwd=str(cwd),
            requested_delivery_dir=req_deliv,
            effective_delivery_dir=eff_deliv,
            writable_roots=w_roots,
            _delivery_dir=eff_deliv,
            required_criteria=criteria,
            has_full_spec=True,
            human_confirmation_required=_human_confirmations(orig_req),
            blockers=path_problems,
            path_blockers=path_problems.copy(),
        )

    # 2. 从接管会话的 rollout 轨迹提取完整输入（杜绝截断）
    user_prompts = []
    if rollout_path and Path(rollout_path).exists():
        try:
            with open(rollout_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"user"' not in line and '"input_text"' not in line:
                        continue
                    try:
                        obj = json.loads(line.strip())
                    except Exception:
                        continue
                    p = obj.get("payload") or {}
                    role = p.get("role") or ""
                    if role == "user" or p.get("type") == "input_text":
                        content = p.get("content") or p.get("text") or ""
                        if isinstance(content, list):
                            texts = []
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "input_text":
                                    t = c.get("text", "")
                                    if not t.strip().startswith("<environment_context>"):
                                        texts.append(t.strip())
                            txt = "\n".join(texts).strip()
                        else:
                            txt = str(content).strip()
                            if txt.startswith("<environment_context>"):
                                txt = ""
                        if txt:
                            txt = re.sub(r"\\([_`*~])", r"\1", txt)
                        if txt and txt not in user_prompts:
                            user_prompts.append(txt)
        except Exception:
            pass

    orig_req = user_prompts[0] if user_prompts else (title or "未知任务需求")
    has_full_spec = bool(user_prompts)
    if not has_full_spec:
        unconfirmed.append("未能从会话轨迹中提取完整原始任务输入，使用截断标题作为保底")

    # Preserve follow-up user instructions, without treating worker output as authority.
    subsequent_changes = [
        {"version": index + 2, "source": f"rollout:user:{index + 2}", "change": text}
        for index, text in enumerate(user_prompts[1:])
    ]
    human_confirmations = _human_confirmations("\n".join(user_prompts or [orig_req]))
    requested_delivery_dir = _resolve_delivery_dir(cwd, user_prompts or [orig_req], explicit_delivery_dir, cwd)
    path_problems = delivery_path_blockers(requested_delivery_dir, w_roots)
    blockers.extend(path_problems)
    effective_delivery_dir = requested_delivery_dir if not path_problems else ""

    # 6. 建立必需验收项
    criteria = [
        {
            "id": "crit_core_artifacts",
            "description": "核心需求产物文件已生成且内容完整非空",
            "type": "artifact",
            "required": True,
            "evidence_types": ["artifact"]
        },
        {
            "id": "crit_verification_checks",
            "description": "静态语法检查与自测验证全部通过，无报错无异常",
            "type": "verification",
            "required": True,
            "evidence_types": ["verification_result"]
        },
        {
            "id": "crit_functional_completeness",
            "description": "功能与交互满足原始任务需求，且具备客观执行验证",
            "type": "functional",
            "required": True,
            "evidence_types": ["verification_result", "runtime_check"]
        },
    ]

    task_id = Path(rollout_path).stem if rollout_path else (cwd.name or "adopted_task")
    return TaskBaseline(
        task_id=task_id,
        original_requirements=orig_req,
        session_cwd=str(cwd),
        requested_delivery_dir=requested_delivery_dir,
        effective_delivery_dir=effective_delivery_dir,
        writable_roots=w_roots,
        _delivery_dir=effective_delivery_dir,
        required_criteria=criteria,
        has_full_spec=has_full_spec,
        human_confirmation_required=human_confirmations,
        unconfirmed_requirements=unconfirmed,
        blockers=blockers,
        path_blockers=path_problems,
        subsequent_changes=subsequent_changes,
        baseline_version=len(subsequent_changes) + 1,
    )
