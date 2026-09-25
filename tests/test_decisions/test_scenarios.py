"""P5 Doloris 真实测试场景: 验证路由边界, 而非概率精确小数。

10 个场景 fixture (fixtures/scenarios/):
1. 主题二选一            6. 测试失败
2. 语言二选一            7. 任务可能完成但证据不足
3. 需要 AGY 的复杂架构    8. 高风险配置修改请求
4. 需要采证的模糊状态     9. Jev API 超时
5. 临时网络错误          10. Jev 返回非法 JSON

每个场景断言: 期望路由被允许、禁止路由被拒绝、未显式允许的路由 fail-closed 拒绝、
失败场景绝不产生任何路由假设 (错误不会被转换成默认同意)。
"""

import unittest
from pathlib import Path

from afk_supervisor.decisions.jev import JevConfig, JevProvider
from afk_supervisor.decisions.models import DecisionRequest
from afk_supervisor.decisions.normalize import normalize_answers
from afk_supervisor.decisions.replay import ReplayProvider
from afk_supervisor.decisions.routing import (
    ROUTE_ACTIONS,
    RouteScenario,
    check_route,
    load_scenarios,
)
from afk_supervisor.decisions.shadow import ShadowDecisionObserver, build_shadow_questions

SCENARIO_DIR = Path(__file__).resolve().parent / "fixtures" / "scenarios"

DECISION_SCENARIOS = [s for s in load_scenarios(SCENARIO_DIR) if not s.failure]
FAILURE_SCENARIOS = [s for s in load_scenarios(SCENARIO_DIR) if s.failure]


class ScenarioFixtureTests(unittest.TestCase):
    def test_ten_scenarios_loaded(self):
        self.assertEqual(len(DECISION_SCENARIOS) + len(FAILURE_SCENARIOS), 10)

    def test_all_scenarios_pass_policy_validation(self):
        for scenario in DECISION_SCENARIOS + FAILURE_SCENARIOS:
            with self.subTest(scenario=scenario.name):
                scenario.validate()

    def test_decision_scenario_questions_match_production_shadow_questions(self):
        """场景问题定义必须与生产 Shadow 问题集完全一致, 防止 fixture 漂移。"""
        for scenario in DECISION_SCENARIOS + FAILURE_SCENARIOS:
            with self.subTest(scenario=scenario.name):
                self.assertEqual(scenario.questions, build_shadow_questions())

    def test_expected_route_is_within_allowed_boundary(self):
        for scenario in DECISION_SCENARIOS:
            with self.subTest(scenario=scenario.name):
                ok, reason = check_route(
                    scenario, {"route": {"type": "choice", "choice": scenario.expected_route}})
                self.assertTrue(ok, reason)

    def test_scenario_states_are_sanitized_summaries(self):
        for scenario in DECISION_SCENARIOS + FAILURE_SCENARIOS:
            with self.subTest(scenario=scenario.name):
                self.assertIsInstance(scenario.state, dict)
                self.assertEqual(scenario.state.get("interaction_type"), "finite_choice")
                self.assertLessEqual(len(str(scenario.state.get("last_message_summary", ""))), 800)


class ReplayRouteBoundaryTests(unittest.TestCase):
    def make_request(self, scenario):
        return DecisionRequest(
            request_id=f"req-replay-{scenario.name}",
            state=scenario.state,
            questions=scenario.questions,
        )

    def test_replay_provider_satisfies_expected_route_boundary(self):
        for scenario in DECISION_SCENARIOS:
            with self.subTest(scenario=scenario.name):
                provider = ReplayProvider(scenario.replay_answers)
                result = provider.decide(self.make_request(scenario))
                self.assertEqual(result.status, "OK", result.error_code)
                ok, reason = check_route(scenario, result.answers)
                self.assertTrue(ok, reason)
                self.assertEqual(result.answers["route"]["choice"], scenario.expected_route)

    def test_forbidden_routes_are_always_rejected(self):
        """篡改回放答案为每个禁止路由: 边界必须拒绝, 而不是依赖预置数据恰好合规。"""
        for scenario in DECISION_SCENARIOS:
            for forbidden in scenario.forbidden_routes:
                with self.subTest(scenario=scenario.name, route=forbidden):
                    tampered = dict(scenario.replay_answers)
                    tampered["route"] = {"type": "choice", "choice": forbidden,
                                         "probabilities": {forbidden: 1.0}, "top1": forbidden}
                    result = ReplayProvider(tampered).decide(self.make_request(scenario))
                    self.assertEqual(result.status, "OK")
                    ok, reason = check_route(scenario, result.answers)
                    self.assertFalse(ok)
                    self.assertIn("禁止", reason)

    def test_routes_outside_allowed_set_fail_closed(self):
        """既不允许也不禁止的路由同样必须拒绝 (fail-closed, 未显式允许即拒绝)。"""
        for scenario in DECISION_SCENARIOS:
            neither = [r for r in ROUTE_ACTIONS
                       if r not in scenario.allowed_routes and r not in scenario.forbidden_routes]
            for route in neither:
                with self.subTest(scenario=scenario.name, route=route):
                    ok, reason = check_route(scenario, {"route": {"type": "choice", "choice": route}})
                    self.assertFalse(ok)
                    self.assertIn("未被显式允许", reason)

    def test_risk_scores_respect_scenario_risk_boundary(self):
        """低风险场景风险评分必须落在低区; 中/高风险场景不得低于中档 (边界断言, 非精确小数)。"""
        for scenario in DECISION_SCENARIOS:
            with self.subTest(scenario=scenario.name):
                result = ReplayProvider(scenario.replay_answers).decide(self.make_request(scenario))
                score = int(result.answers["risk"]["score"])
                if scenario.state.get("risk") == "low":
                    self.assertLessEqual(score, 2)
                else:
                    self.assertGreaterEqual(score, 3)

    def test_completion_scenario_never_routes_to_auto_answer(self):
        """'任务可能完成但证据不足' 场景: 自动确认完成被禁止, 完成判断不属于 Jev。"""
        scenario = next(s for s in DECISION_SCENARIOS if "completion" in s.name)
        self.assertIn("auto_answer", scenario.forbidden_routes)

    def test_shadow_observer_audits_replay_route(self):
        """场景经 Shadow 观察器端到端: 审计 top_actions 与期望路由一致。"""
        scenario = next(s for s in DECISION_SCENARIOS if "high-risk" in s.name)
        events = []
        observer = ShadowDecisionObserver(
            provider=ReplayProvider(scenario.replay_answers),
            audit_fn=lambda event, **kw: events.append({"event": event, **kw}),
            mode="shadow",
        )
        observer.observe(scenario.state["last_message_summary"], "req-scenario-8")
        self.assertEqual(events[0]["top_actions"], {"route": "consult_agy"})
        self.assertEqual(events[0]["status"], "OK")


class JevFailureScenarioTests(unittest.TestCase):
    """场景 9/10: Jev 失败必须得到结构化错误, 绝不转换成默认路由或默认同意。"""

    def run_failure_scenario(self, scenario):
        config = JevConfig(endpoint="https://api.mindshub.ai/v1/decisions",
                           token="mock-token", model="jev", timeout_sec=5.0)
        kind = scenario.failure["kind"]

        class FakeResponse:
            def __init__(self, body=b""):
                self._body = body

            def read(self, size=-1):
                out, self._body = self._body[:size], self._body[size:]
                return out

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        if kind == "timeout":
            def urlopen(request, timeout=None):
                raise TimeoutError("mock api timeout")
        elif kind == "invalid_json":
            def urlopen(request, timeout=None):
                return FakeResponse(b'{"answers": {"route": {"choice": "auto_ans"}}}')
        else:
            raise ValueError(f"未知失败类型: {kind}")

        provider = JevProvider(config=config, urlopen=urlopen)
        request = DecisionRequest(
            request_id=f"req-{scenario.name}",
            state=scenario.state,
            questions=scenario.questions,
        )
        return provider.decide(request)

    def test_failure_scenarios_return_structured_errors_without_routes(self):
        for scenario in FAILURE_SCENARIOS:
            with self.subTest(scenario=scenario.name):
                result = self.run_failure_scenario(scenario)
                self.assertEqual(result.status, scenario.expected_status)
                self.assertEqual(result.error_code, scenario.expected_error_code)
                self.assertEqual(result.answers, {})
                for forbidden in scenario.forbidden_routes:
                    self.assertNotIn(forbidden, result.answers)

    def test_timeout_scenario_yields_no_assumed_route(self):
        scenario = next(s for s in FAILURE_SCENARIOS if "timeout" in s.name)
        result = self.run_failure_scenario(scenario)
        self.assertNotIn("route", result.answers)
        self.assertNotIn("PROCEED", result.status)
        self.assertNotEqual(result.status, "OK")

    def test_invalid_json_scenario_rejects_truncated_response(self):
        scenario = next(s for s in FAILURE_SCENARIOS if "invalid-json" in s.name)
        result = self.run_failure_scenario(scenario)
        self.assertEqual(result.status, "INVALID_RESPONSE")
        self.assertEqual(result.answers, {})


class ScenarioAnswerNormalizationTests(unittest.TestCase):
    def test_every_replay_answer_set_survives_strict_normalization(self):
        for scenario in DECISION_SCENARIOS:
            with self.subTest(scenario=scenario.name):
                normalized = normalize_answers(scenario.questions, scenario.replay_answers)
                self.assertEqual(set(normalized), set(scenario.questions))

    def test_route_scenario_rejects_unknown_actions_at_load_time(self):
        data = {
            "name": "bad-scenario",
            "questions": {"route": {"type": "choice", "criteria": {"a": "A"}}},
            "state": {"interaction_type": "finite_choice"},
            "allowed_routes": ["nuke_everything"],
            "forbidden_routes": [],
        }
        with self.assertRaises(ValueError):
            RouteScenario.from_dict(data)

    def test_route_scenario_rejects_allowed_forbidden_overlap(self):
        data = {
            "name": "overlap-scenario",
            "questions": {"route": {"type": "choice", "criteria": {"a": "A"}}},
            "state": {"interaction_type": "finite_choice"},
            "allowed_routes": ["auto_answer"],
            "forbidden_routes": ["auto_answer"],
        }
        with self.assertRaises(ValueError):
            RouteScenario.from_dict(data)


if __name__ == "__main__":
    unittest.main()
