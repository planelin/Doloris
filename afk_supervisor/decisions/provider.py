"""afk_supervisor.decisions.provider — 决策 Provider 抽象契约
============================================================
所有决策模型（当前 JevProvider，未来本地 LayaProvider、Replay/FakeProvider）
都实现同一 Protocol 接口；核心监管逻辑只依赖本接口，不得依赖具体 Provider。

未来 Laya 适配时只允许改变: 模型加载 / 调用方式 / 设备选择 / 模型版本 /
置信度校准；不得改变 DecisionRequest 语义、DecisionResult 语义、
Doloris 审计事件、安全策略、AGY 回退逻辑与机械验收逻辑。
"""

from typing import Protocol, runtime_checkable

from afk_supervisor.decisions.models import DecisionRequest, DecisionResult


@runtime_checkable
class DecisionProvider(Protocol):
    """结构化决策 Provider 接口。

    实现方约定:
    - provider_name 标识 Provider 类型（如 "jev"），用于审计;
    - decide() 永不抛出未捕获的网络/解析异常，失败一律返回带错误 status 的
      DecisionResult; 绝不把失败伪造成默认同意或默认选择;
    - 实现方应在结果 raw_metadata 中保存 model_revision / threshold_profile /
      schema，供审计与未来 Laya 置信度校准隔离使用。
    """

    provider_name: str

    def decide(self, request: DecisionRequest) -> DecisionResult:
        ...
