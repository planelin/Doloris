"""afk_supervisor.decisions.models — 决策层统一数据结构
======================================================
DecisionRequest / DecisionResult 是可替换决策层（当前仅 Jev，未来 Laya）的
通用数据契约，与 AGY L2 的 L2Result / verdict 体系严格类型隔离：

- DecisionResult.status 取值空间 (OK / TIMEOUT / AUTH_ERROR / HTTP_ERROR /
  INVALID_RESPONSE / CONFIG_ERROR / NETWORK_ERROR) 与 AGY 的
  (PROCEED / PASS / FAIL / REPAIRED / STOP) 完全不重叠；
- 决策模型结果永远不得被当作 AGY L2 决议使用，反之亦然。

schema 固定为 doloris.decision.v1；questions 必须使用稳定 key；
state 必须是调用方脱敏后的结构化数据；metadata 不得放入密钥。
"""

from dataclasses import dataclass, field
from typing import Any, Dict

from afk_supervisor.decisions.errors import InvalidDecisionRequestError, InvalidDecisionResultError

DECISION_SCHEMA = "doloris.decision.v1"

# Jev 支持的三类问题：有限选项 / 评分 / 布尔倾向（不直接转布尔，除非调用方给阈值）
QUESTION_TYPES = ("choice", "score", "noul")

# DecisionResult.status 允许集合；与 AGY L2 verdict 空间刻意零重叠
RESULT_STATUSES = (
    "OK",
    "TIMEOUT",
    "AUTH_ERROR",
    "HTTP_ERROR",
    "INVALID_RESPONSE",
    "CONFIG_ERROR",
    "NETWORK_ERROR",
)


def validate_question(key: Any, question: Any) -> None:
    """校验单个问题定义；key 必须是稳定非空字符串，结构按类型约束。"""
    if not isinstance(key, str) or not key.strip():
        raise InvalidDecisionRequestError(f"问题 key 必须是非空字符串, 收到: {key!r}")
    if not isinstance(question, dict):
        raise InvalidDecisionRequestError(f"问题 {key!r} 定义必须是 dict, 收到: {type(question).__name__}")
    qtype = question.get("type")
    if qtype not in QUESTION_TYPES:
        raise InvalidDecisionRequestError(f"问题 {key!r} 的 type 必须是 {QUESTION_TYPES} 之一, 收到: {qtype!r}")
    if qtype in ("choice", "score"):
        criteria = question.get("criteria")
        if not isinstance(criteria, dict) or not criteria:
            raise InvalidDecisionRequestError(f"问题 {key!r} ({qtype}) 必须提供非空 criteria 定义")
        for ckey, cval in criteria.items():
            if not isinstance(ckey, str) or not ckey.strip():
                raise InvalidDecisionRequestError(f"问题 {key!r} 的 criteria key 必须是非空字符串, 收到: {ckey!r}")
            if not isinstance(cval, str):
                raise InvalidDecisionRequestError(f"问题 {key!r} 的 criteria[{ckey!r}] 描述必须是字符串")


@dataclass
class DecisionRequest:
    """发往决策 Provider 的结构化决策请求。"""

    schema: str = DECISION_SCHEMA
    request_id: str = ""
    state: Dict[str, Any] = field(default_factory=dict)
    questions: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.validate()

    def validate(self) -> None:
        if self.schema != DECISION_SCHEMA:
            raise InvalidDecisionRequestError(f"schema 必须固定为 {DECISION_SCHEMA}, 收到: {self.schema!r}")
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise InvalidDecisionRequestError("request_id 必须是非空可追踪字符串")
        if not isinstance(self.state, dict):
            raise InvalidDecisionRequestError("state 必须是脱敏后的 dict 结构")
        if not isinstance(self.questions, dict) or not self.questions:
            raise InvalidDecisionRequestError("questions 必须是非空 dict")
        for key, question in self.questions.items():
            validate_question(key, question)
        if not isinstance(self.metadata, dict):
            raise InvalidDecisionRequestError("metadata 必须是 dict, 且不得放入密钥")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "state": self.state,
            "questions": self.questions,
            "metadata": self.metadata,
        }


@dataclass
class DecisionResult:
    """决策 Provider 返回的结构化结果。

    status 只能取 RESULT_STATUSES 中的值；失败时 answers 必须为空 dict、
    status 为对应错误类别，绝不允许把错误结果伪造成默认同意或默认选择。
    """

    provider: str
    model: str
    request_id: str
    answers: Dict[str, Any]
    latency_ms: int
    status: str
    error_code: str = ""
    raw_metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise InvalidDecisionResultError("provider 必须是非空字符串")
        if not isinstance(self.model, str) or not self.model.strip():
            raise InvalidDecisionResultError("model 必须是非空字符串")
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise InvalidDecisionResultError("request_id 必须是非空可追踪字符串")
        if not isinstance(self.answers, dict):
            raise InvalidDecisionResultError("answers 必须是 dict")
        if not isinstance(self.latency_ms, int) or isinstance(self.latency_ms, bool) or self.latency_ms < 0:
            raise InvalidDecisionResultError(f"latency_ms 必须是非负整数, 收到: {self.latency_ms!r}")
        if self.status not in RESULT_STATUSES:
            raise InvalidDecisionResultError(
                f"status 必须是 {RESULT_STATUSES} 之一, 收到: {self.status!r}; "
                "禁止复用 AGY L2 verdict (PROCEED/PASS/FAIL/REPAIRED/STOP)"
            )
        if not isinstance(self.error_code, str):
            raise InvalidDecisionResultError("error_code 必须是字符串")
        if not isinstance(self.raw_metadata, dict):
            raise InvalidDecisionResultError("raw_metadata 必须是 dict")
