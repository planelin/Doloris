"""afk_supervisor.decisions.routing — 决策路由边界校验
====================================================
定义场景化路由边界 (期望路由 / 允许的替代路由 / 禁止的路由) 并做 fail-closed 校验。
本模块服务于 Shadow 阶段的场景回归测试与未来 active 模式的低风险路由门;

原则约束:
- 校验的是路由边界, 不是概率精确值;
- 未被显式允许的路由一律拒绝 (fail-closed);
- stop_blocked 永远不用于"任务完成"语义, 只用于有依据的外部阻碍停止。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 原则 B: Jev 只能返回的五种路由/分类信号
ROUTE_ACTIONS = ("auto_answer", "consult_agy", "gather_evidence", "retry_once", "stop_blocked")


@dataclass
class RouteScenario:
    """单个决策测试场景: 输入状态 + 问题定义 + 路由边界。"""

    name: str
    description: str
    state: Dict[str, Any]
    questions: Dict[str, Any]
    allowed_routes: List[str]
    forbidden_routes: List[str]
    expected_route: str = ""
    replay_answers: Dict[str, Any] = field(default_factory=dict)
    failure: Optional[Dict[str, Any]] = None
    expected_status: str = ""
    expected_error_code: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RouteScenario":
        scenario = cls(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            state=data.get("state") or {},
            questions=data.get("questions") or {},
            allowed_routes=list(data.get("allowed_routes") or []),
            forbidden_routes=list(data.get("forbidden_routes") or []),
            expected_route=str(data.get("expected_route", "")),
            replay_answers=data.get("replay_answers") or {},
            failure=data.get("failure"),
            expected_status=str(data.get("expected_status", "")),
            expected_error_code=str(data.get("expected_error_code", "")),
        )
        scenario.validate()
        return scenario

    def validate(self) -> None:
        if not self.name:
            raise ValueError("场景缺少 name")
        if not self.questions:
            raise ValueError(f"场景 {self.name} 缺少问题定义")
        for route in self.allowed_routes + self.forbidden_routes:
            if route not in ROUTE_ACTIONS:
                raise ValueError(f"场景 {self.name} 含未知路由动作: {route!r}")
        overlap = set(self.allowed_routes) & set(self.forbidden_routes)
        if overlap:
            raise ValueError(f"场景 {self.name} 允许/禁止路由重叠: {sorted(overlap)}")
        if self.expected_route and self.expected_route not in self.allowed_routes:
            raise ValueError(f"场景 {self.name} 期望路由 {self.expected_route!r} 不在允许集合中")


def load_scenario(path: Path) -> RouteScenario:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return RouteScenario.from_dict(data)


def load_scenarios(directory: Path) -> List[RouteScenario]:
    return [load_scenario(p) for p in sorted(Path(directory).glob("*.json"))]


def check_route(scenario: RouteScenario, answers: Dict[str, Any]) -> Tuple[bool, str]:
    """校验决策答案的 route 是否落在场景边界内; 任何不在允许集合的路由都拒绝。"""
    route_answer = answers.get("route")
    if not isinstance(route_answer, dict) or route_answer.get("type") != "choice":
        return False, f"场景 {scenario.name}: 缺少 route choice 答案"
    choice = route_answer.get("choice")
    if choice not in ROUTE_ACTIONS:
        return False, f"场景 {scenario.name}: 未知路由动作 {choice!r}"
    if choice in scenario.forbidden_routes:
        return False, f"场景 {scenario.name}: 路由 {choice!r} 被禁止"
    if choice not in scenario.allowed_routes:
        return False, f"场景 {scenario.name}: 路由 {choice!r} 未被显式允许 (fail-closed)"
    return True, ""
