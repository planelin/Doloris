"""
afk_supervisor.models — 核心数据模型与抽象契约
=================================================
定义跨模块通用的数据结构、时间预算、结果对象及测试替换边界。
"""

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol


class Clock(Protocol):
    """时钟边界抽象，便于测试注入确定性时钟。"""
    def time(self) -> float: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """系统默认真实时钟。"""
    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class DeadlineBudget:
    """单调时钟总时间预算管理器。
    用于长程挂机生命周期总上限约束，杜绝 wall clock 回退导致的不确定性。
    """
    def __init__(self, max_run_sec: float, clock: Optional[Clock] = None):
        self.max_run_sec = max_run_sec
        self.clock = clock or SystemClock()
        self.t0 = self.clock.monotonic()
        self.deadline_mono = (self.t0 + max_run_sec) if max_run_sec and max_run_sec > 0 else float("inf")

    def is_expired(self) -> bool:
        if not self.max_run_sec or self.max_run_sec <= 0:
            if hasattr(self, "deadline_mono") and self.deadline_mono != float("inf"):
                return self.clock.monotonic() >= self.deadline_mono
            return False
        if hasattr(self, "deadline_mono"):
            return self.clock.monotonic() >= self.deadline_mono
        return (self.clock.monotonic() - self.t0) >= self.max_run_sec

    @property
    def remaining_sec(self) -> float:
        if hasattr(self, "deadline_mono"):
            if self.deadline_mono == float("inf"):
                return float("inf")
            return max(0.0, self.deadline_mono - self.clock.monotonic())
        if not self.max_run_sec or self.max_run_sec <= 0:
            return float("inf")
        rem = self.max_run_sec - (self.clock.monotonic() - self.t0)
        return max(0.0, rem)

    def bound_timeout(self, timeout_sec: float) -> float:
        """用剩余总时限约束单次操作超时上限。"""
        if not self.max_run_sec or self.max_run_sec <= 0:
            if hasattr(self, "deadline_mono") and self.deadline_mono != float("inf"):
                rem = max(0.0, self.deadline_mono - self.clock.monotonic())
                return min(timeout_sec, rem)
            return timeout_sec
        rem = self.remaining_sec
        if rem <= 0:
            return 0.0
        return min(timeout_sec, rem)


@dataclass
class L2Result:
    """L2 专家咨询与分派结果对象。
    支持向下兼容元组解包: verdict, answer, l2_log = result
    同时支持 text/log 与 answer/l2_log 双向兼容访问。
    """
    verdict: str
    answer: str = ""
    l2_log: Any = None
    payload: Optional[Dict[str, Any]] = None
    text: str = ""
    log: Any = None

    def __post_init__(self):
        if not self.answer and self.text:
            self.answer = self.text
        elif not self.text and self.answer:
            self.text = self.answer
        if self.l2_log is None and self.log is not None:
            self.l2_log = self.log
        elif self.log is None and self.l2_log is not None:
            self.log = self.l2_log

    def __iter__(self):
        return iter((self.verdict, self.answer, self.l2_log))

    def __len__(self):
        return 3

    def __getitem__(self, index):
        return (self.verdict, self.answer, self.l2_log)[index]


@dataclass
class AgyResponseResult:
    """AGY 交互响应结果对象。
    支持向下兼容元组解包: verdict, text, log = result
    """
    verdict: str
    text: str
    log: Optional[Path] = None
    payload: Optional[Dict[str, Any]] = None

    def __iter__(self):
        return iter((self.verdict, self.text, self.log))

    def __len__(self):
        return 3

    def __getitem__(self, index):
        return (self.verdict, self.text, self.log)[index]


@dataclass
class CriterionSpec:
    """单个验收准则定义。"""
    id: str
    description: str
    type: str = "artifact"  # artifact, verification, functional, checklist, acceptance_spec
    required: bool = True
    evidence_types: List[str] = field(default_factory=lambda: ["artifact", "verification_result"])


@dataclass
class EvidenceItem:
    """客观证据项。"""
    id: str
    category: str  # artifact, verification_result, worker_statement, progress_doc, runtime_check
    summary: str
    details: str = ""
    path: str = ""
    mtime: float = 0.0
    size: int = 0
    sha256_short: str = ""
    sha256: str = ""
    verification_kind: str = ""  # syntax, acceptance_spec, unit_test, browser_test, ...
    status: str = ""  # PASS, FAIL, UNKNOWN; not inferred from an agent's prose
    artifact_revision: str = ""
    criterion_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvidencePacket:
    """审查轮次证据包。包含业务产物版本与审查包版本双标识。"""
    task_id: str
    delivery_dir: str
    reviewed_revision: str
    artifact_revision: str = ""
    request_id: str = ""
    items: List[EvidenceItem] = field(default_factory=list)
    mechanical_failures: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "delivery_dir": self.delivery_dir,
            "reviewed_revision": self.reviewed_revision,
            "artifact_revision": self.artifact_revision,
            "request_id": self.request_id,
            "items": [item.to_dict() for item in self.items],
            "mechanical_failures": self.mechanical_failures,
        }

    def persist(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


class ActionType:
    """标准动作类型常量。"""
    WORKER_INSTRUCTION = "worker_instruction"
    WORKER_FIX = "worker_fix"
    GATHER_EVIDENCE = "gather_evidence"
    SWITCH_TO_REPAIR = "switch_to_repair"
    REQUEST_USER = "request_user"
    TERMINATE_SUCCESS = "terminate_success"
    TERMINATE_BLOCKED = "terminate_blocked"

    ALL_TYPES = (
        WORKER_INSTRUCTION,
        WORKER_FIX,
        GATHER_EVIDENCE,
        SWITCH_TO_REPAIR,
        REQUEST_USER,
        TERMINATE_SUCCESS,
        TERMINATE_BLOCKED,
    )


@dataclass
class DeliveryResult:
    """Structured send receipt; legacy (bool, detail) unpacking remains available."""
    status: str  # NOT_SENT, SENT, ACCEPTED, UNCERTAIN
    detail: str = ""

    def __iter__(self):
        return iter((self.status in {"SENT", "ACCEPTED"}, self.detail))


def delivery_result(value):
    if isinstance(value, DeliveryResult):
        return value
    # A legacy False does NOT prove no send occurred. Never blindly resend it.
    ok, detail = value
    return DeliveryResult("SENT" if ok else "UNCERTAIN", str(detail))
