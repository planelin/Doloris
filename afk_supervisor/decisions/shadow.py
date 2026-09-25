"""afk_supervisor.decisions.shadow — Shadow Mode 观察器
======================================================
Shadow Mode 是当前唯一允许的生产路径:

    DecisionProvider(Jev) → Shadow Mode → 记录 → 继续原有流程

硬性约束:
- 仅在 DOLORIS_DECISION_MODE=shadow 时启用; active 尚未支持, 按 off 处理;
- observe() 只把 Jev 判断写入审计日志, 绝不返回决策给监管流程,
  绝不自动向 Codex 注入结果、跳过 AGY、结束任务或修改工作区;
- 审计事件只含脱敏摘要与 request_hash, 不含完整原始 state;
- observe() 自身绝不抛出异常——决策层的任何失败都不允许影响监管流程。
"""

import hashlib
import json
from typing import Any, Callable, Dict, Optional

from afk_supervisor.decisions.jev import JevProvider, resolve_decision_mode
from afk_supervisor.decisions.models import DECISION_SCHEMA, DecisionRequest, DecisionResult
from afk_supervisor.decisions.redaction import redact_text

SUMMARY_MAX_CHARS = 800

# 原则 B: Jev 只能返回分类/路由信号, 不能返回可执行指令
ROUTE_CRITERIA = {
    "auto_answer": "低风险、有明确默认答案，可直接安全代答",
    "consult_agy": "需要 AGY 阅读上下文或代码后做复杂决策",
    "gather_evidence": "证据不足，应先采集证据再决策",
    "retry_once": "疑似瞬时故障（网络/限流），有界重试一次可能恢复",
    "stop_blocked": "存在不可逾越的外部阻碍，应有依据地停止",
}

RISK_LEVELS = {"1": "极低", "2": "低", "3": "中", "4": "高", "5": "极高"}


def build_shadow_questions() -> Dict[str, Any]:
    """Shadow 观察使用的固定问题集: route / risk / can_auto_answer。"""
    return {
        "route": {
            "type": "choice",
            "instructions": "Worker 停在一个有限选项的决策请求上。选择 Doloris 监管层应采取的下一步路由。",
            "criteria": ROUTE_CRITERIA,
        },
        "risk": {
            "type": "score",
            "instructions": "评估直接自动采纳该决策路线的风险等级 (1=极低 .. 5=极高)。",
            "criteria": RISK_LEVELS,
        },
        "can_auto_answer": {
            "type": "noul",
            "instructions": "该决策请求是否可以不经 AGY 复杂决策而直接自动回答。",
        },
    }


def build_shadow_state(last_msg: str) -> Dict[str, Any]:
    """构造发送给 Jev 的脱敏状态; 只发送摘要, 不发送完整日志、源码或密钥。"""
    return {
        "interaction_type": "finite_choice",
        "last_message_summary": redact_text((last_msg or "").strip())[:SUMMARY_MAX_CHARS],
    }


class ShadowDecisionObserver:
    """Shadow Mode 观察器: 调用决策 Provider 并只写入审计事件。"""

    def __init__(
        self,
        provider: Optional[Any] = None,
        audit_fn: Optional[Callable[..., None]] = None,
        mode: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ):
        self._mode = mode if mode is not None else resolve_decision_mode(env)
        self._provider = provider
        self._audit = audit_fn or (lambda event, **kw: None)

    @property
    def enabled(self) -> bool:
        return self._mode == "shadow"

    def observe(self, last_msg: str, request_id: str, n_interaction: int = 0) -> Optional[DecisionResult]:
        """观察一次决策请求; 返回值仅供测试诊断, 监管流程不得使用。"""
        if not self.enabled:
            return None
        state = build_shadow_state(last_msg)
        questions = build_shadow_questions()
        question_keys = sorted(questions)
        try:
            request = DecisionRequest(
                request_id=request_id,
                state=state,
                questions=questions,
                metadata={"interaction_number": n_interaction, "observer": "shadow"},
            )
            provider = self._provider
            if provider is None:
                provider = JevProvider()
            result = provider.decide(request)
        except Exception as error:  # 决策层任何异常都不允许影响监管流程
            self._audit(
                "DECISION_HEAD_SHADOW",
                provider="jev",
                model="",
                schema=DECISION_SCHEMA,
                request_id=request_id,
                question_keys=question_keys,
                top_actions={},
                answers={},
                latency_ms=0,
                status="INVALID_RESPONSE",
                error_code=f"shadow_observer_error:{type(error).__name__}",
                request_hash=self.request_hash(state, questions),
                n=n_interaction,
            )
            return None
        top_actions = {
            key: answer["choice"]
            for key, answer in result.answers.items()
            if isinstance(answer, dict) and answer.get("type") == "choice" and answer.get("choice")
        }
        self._audit(
            "DECISION_HEAD_SHADOW",
            provider=result.provider,
            model=result.model,
            schema=DECISION_SCHEMA,
            request_id=result.request_id,
            question_keys=question_keys,
            top_actions=top_actions,
            answers=result.answers,
            latency_ms=result.latency_ms,
            status=result.status,
            error_code=result.error_code,
            request_hash=self.request_hash(state, questions),
            n=n_interaction,
        )
        return result

    @staticmethod
    def request_hash(state: Dict[str, Any], questions: Dict[str, Any]) -> str:
        """对脱敏后的请求体计算短哈希, 用于跨轮关联; 不含任何敏感原文。"""
        blob = json.dumps({"state": state, "questions": questions}, ensure_ascii=False, sort_keys=True)
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
