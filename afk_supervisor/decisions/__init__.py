"""afk_supervisor.decisions — 可替换结构化决策层
================================================
当前仅实现 JevProvider（Shadow Mode 观察）；未来本地 LayaProvider 实现同一
DecisionProvider 接口即可替换，核心监管逻辑与 AGY 回退路径保持不变。

设计原则:
- 规则负责事实（进程/文件/测试退出码等确定性检查仍由 Doloris 代码判断）;
- 决策 Provider 只负责分类、路由与有限选项判断，绝不负责执行;
- 默认 DOLORIS_DECISION_MODE=off，行为与未接入决策层时完全一致。
"""

from afk_supervisor.decisions.errors import (
    DecisionError,
    InvalidDecisionRequestError,
    InvalidDecisionResultError,
    ProviderError,
)
from afk_supervisor.decisions.models import (
    DECISION_SCHEMA,
    QUESTION_TYPES,
    RESULT_STATUSES,
    DecisionRequest,
    DecisionResult,
)
from afk_supervisor.decisions.provider import DecisionProvider

__all__ = [
    "DECISION_SCHEMA",
    "QUESTION_TYPES",
    "RESULT_STATUSES",
    "DecisionError",
    "DecisionProvider",
    "DecisionRequest",
    "DecisionResult",
    "InvalidDecisionRequestError",
    "InvalidDecisionResultError",
    "ProviderError",
]
