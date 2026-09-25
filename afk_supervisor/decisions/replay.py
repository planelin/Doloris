"""afk_supervisor.decisions.replay — Replay/Fake Provider
========================================================
确定性回放 Provider: 返回预置答案, 不发起任何网络调用。

用途:
- 决策层测试 (无需 mock HTTP 即可驱动完整标准化/审计链路);
- 未来离线复放真实 Jev 响应, 评估路由策略变化 (架构目标中的 ReplayProvider)。

回放答案同样必须通过 normalize_answers 校验——伪造的坏数据不会被静默接受。
"""

import time
from typing import Any, Dict

from afk_supervisor.decisions.errors import ProviderError
from afk_supervisor.decisions.models import DECISION_SCHEMA, DecisionRequest, DecisionResult
from afk_supervisor.decisions.normalize import normalize_answers


class ReplayProvider:
    """按预置答案回放的决策 Provider。"""

    provider_name = "replay"

    def __init__(self, answers: Dict[str, Any], model: str = "replay", model_revision: str = "replay-1"):
        self._raw_answers = answers
        self._model = model
        self._model_revision = model_revision

    def decide(self, request: DecisionRequest) -> DecisionResult:
        started = time.monotonic()
        try:
            normalized = normalize_answers(request.questions, self._raw_answers)
        except ProviderError as error:
            return DecisionResult(
                provider=self.provider_name,
                model=self._model,
                request_id=request.request_id,
                answers={},
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                status=error.result_status,
                error_code=error.error_code,
            )
        return DecisionResult(
            provider=self.provider_name,
            model=self._model,
            request_id=request.request_id,
            answers=normalized,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            status="OK",
            raw_metadata={
                "model_revision": self._model_revision,
                "threshold_profile": "replay",
                "schema": DECISION_SCHEMA,
            },
        )
