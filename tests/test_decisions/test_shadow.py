"""P4 Shadow Mode 测试。

通过条件 (pipeline §P4):
- DOLORIS_DECISION_MODE=off (默认) 时完全不调用 Jev;
- DOLORIS_DECISION_MODE=shadow 时调用 Jev 但不改变任务行为;
- 审计事件只记录脱敏摘要与 request_hash, 不含完整原始 state。
"""

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from afk_supervisor.baseline import TaskBaseline
from afk_supervisor.coordinator import SupervisorCoordinator
from afk_supervisor.decisions.models import DecisionRequest, DecisionResult
from afk_supervisor.decisions.shadow import (
    ShadowDecisionObserver,
    build_shadow_state,
)


@contextmanager
def decision_mode(mode):
    env = dict(os.environ)
    if mode is None:
        env.pop("DOLORIS_DECISION_MODE", None)
    else:
        env["DOLORIS_DECISION_MODE"] = mode
    with patch.dict(os.environ, env, clear=True):
        yield


class StubProvider:
    provider_name = "jev"

    def __init__(self, result=None, error=None):
        self.result = result or DecisionResult(
            provider="jev", model="jev", request_id="req-x", latency_ms=12, status="OK",
            answers={"route": {"type": "choice", "choice": "consult_agy",
                               "probabilities": {"consult_agy": 0.81, "auto_answer": 0.12,
                                                 "gather_evidence": 0.05, "stop_blocked": 0.02,
                                                 "retry_once": 0.0},
                               "top1": "consult_agy", "top2": "auto_answer", "margin": 0.69}},
        )
        self.error = error
        self.requests = []

    def decide(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return DecisionResult(**{**self.result.__dict__, "request_id": request.request_id})


class ObserverModeTests(unittest.TestCase):
    def test_default_mode_is_off_and_observer_disabled(self):
        with decision_mode(None):
            observer = ShadowDecisionObserver(provider=StubProvider())
        self.assertFalse(observer.enabled)
        self.assertIsNone(observer.observe("question", "req-1"))

    def test_explicit_off_disables_observer(self):
        with decision_mode("off"):
            observer = ShadowDecisionObserver(provider=StubProvider())
        self.assertFalse(observer.enabled)

    def test_shadow_mode_enables_observer(self):
        with decision_mode("shadow"):
            observer = ShadowDecisionObserver(provider=StubProvider())
        self.assertTrue(observer.enabled)

    def test_active_mode_is_not_supported_in_phase_one(self):
        with decision_mode("active"):
            observer = ShadowDecisionObserver(provider=StubProvider())
        self.assertFalse(observer.enabled)


class ObserverEventTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        with decision_mode("shadow"):
            self.provider = StubProvider()
            self.observer = ShadowDecisionObserver(
                provider=self.provider, audit_fn=lambda event, **kw: self.events.append({"event": event, **kw}))

    def test_event_matches_recommended_schema(self):
        result = self.observer.observe("【决策请求】选择主题甲或主题乙", "req-decide-1", n_interaction=1)
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        for key in ("event", "provider", "model", "schema", "request_id", "question_keys",
                    "top_actions", "latency_ms", "status", "request_hash"):
            self.assertIn(key, event)
        self.assertEqual(event["event"], "DECISION_HEAD_SHADOW")
        self.assertEqual(event["provider"], "jev")
        self.assertEqual(event["schema"], "doloris.decision.v1")
        self.assertEqual(event["request_id"], "req-decide-1")
        self.assertEqual(event["status"], "OK")
        self.assertEqual(event["question_keys"], ["can_auto_answer", "risk", "route"])
        self.assertEqual(event["top_actions"], {"route": "consult_agy"})
        self.assertTrue(event["request_hash"].startswith("sha256:"))
        self.assertIsNotNone(result)

    def test_event_records_probabilities_and_latency_but_not_raw_state(self):
        self.observer.observe("【决策请求】请选择语言: 中文 / English", "req-decide-2")
        blob = json.dumps(self.events[0], ensure_ascii=False)
        self.assertIn("probabilities", blob)
        self.assertIn("latency_ms", blob)
        self.assertNotIn("【决策请求】", blob)
        self.assertNotIn("last_message_summary", blob)
        self.assertNotIn("中文", blob)

    def test_request_sent_to_provider_is_sanitized_and_trackable(self):
        self.observer.observe("【决策请求】选择 A 或 B", "req-decide-3")
        request = self.provider.requests[0]
        self.assertIsInstance(request, DecisionRequest)
        self.assertEqual(request.request_id, "req-decide-3")
        self.assertEqual(request.schema, "doloris.decision.v1")
        self.assertEqual(request.state["interaction_type"], "finite_choice")
        self.assertLessEqual(len(request.state["last_message_summary"]), 800)
        self.assertEqual(set(request.questions), {"route", "risk", "can_auto_answer"})

    def test_provider_error_result_is_recorded_not_swallowed_silently(self):
        error_result = DecisionResult(provider="jev", model="jev", request_id="req-e",
                                      answers={}, latency_ms=5, status="TIMEOUT",
                                      error_code="decision_api_timeout")
        events = []
        with decision_mode("shadow"):
            observer = ShadowDecisionObserver(
                provider=StubProvider(result=error_result),
                audit_fn=lambda event, **kw: events.append({"event": event, **kw}))
        observer.observe("question", "req-e")
        self.assertEqual(events[0]["status"], "TIMEOUT")
        self.assertEqual(events[0]["error_code"], "decision_api_timeout")
        self.assertEqual(events[0]["answers"], {})

    def test_unexpected_provider_exception_degrades_to_audit_event_without_raising(self):
        events = []
        with decision_mode("shadow"):
            observer = ShadowDecisionObserver(
                provider=StubProvider(error=RuntimeError("boom")),
                audit_fn=lambda event, **kw: events.append({"event": event, **kw}))
        result = observer.observe("question", "req-boom")
        self.assertIsNone(result)
        self.assertEqual(events[0]["status"], "INVALID_RESPONSE")
        self.assertIn("shadow_observer_error", events[0]["error_code"])


class ShadowStateTests(unittest.TestCase):
    def test_state_is_summary_only(self):
        state = build_shadow_state("【决策请求】主题甲(暗色) / 主题乙(亮色)")
        self.assertEqual(set(state), {"interaction_type", "last_message_summary"})
        self.assertEqual(state["interaction_type"], "finite_choice")

    def test_state_redacts_secrets(self):
        state = build_shadow_state("使用 Bearer sk-live-9999 后选择主题")
        self.assertNotIn("sk-live-9999", state["last_message_summary"])


class ShadowQuestionsWireContractTests(unittest.TestCase):
    """生产 Shadow 问题集必须能无损转换为 MindsHub 线上格式 (2026-09 实测契约):
    choice criteria 为 name→description 映射; score criteria 为数组 (位置即分值);
    noul 只含 type/instructions。"""

    def setUp(self):
        from afk_supervisor.decisions.jev import JevProvider
        from afk_supervisor.decisions.shadow import build_shadow_questions

        self.wire = JevProvider._wire_questions(build_shadow_questions())

    def test_route_criteria_is_name_description_map_with_five_actions(self):
        route = self.wire["route"]
        self.assertEqual(route["type"], "choice")
        self.assertEqual(set(route["criteria"]), {
            "auto_answer", "consult_agy", "gather_evidence", "retry_once", "stop_blocked"})
        self.assertTrue(all(isinstance(v, str) and v for v in route["criteria"].values()))

    def test_risk_criteria_becomes_ordered_array_from_numeric_keys(self):
        risk = self.wire["risk"]
        self.assertEqual(risk["type"], "score")
        self.assertEqual(risk["criteria"], ["极低", "低", "中", "高", "极高"])

    def test_noul_question_carries_only_type_and_instructions(self):
        noul = self.wire["can_auto_answer"]
        self.assertEqual(noul["type"], "noul")
        self.assertEqual(set(noul), {"type", "instructions"})


class CoordinatorShadowIntegrationTests(unittest.TestCase):
    """端到端: handle_interaction 触发 shadow 观察, 但 DECIDE 行为与审计流不变。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="doloris-shadow-test-"))
        self.ws = self.root / "workspace"
        self.ws.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.baseline = TaskBaseline(
            task_id="shadow-task", original_requirements="Deliver the documented result",
            session_cwd=str(self.ws), delivery_dir=str(self.ws),
            required_criteria=[{"id": "artifact", "type": "artifact"}],
        )
        self.args = SimpleNamespace(l2_max=2, timeout_sec=7, l2_model="flash", l2_project_id="project")

    def make_coordinator(self):
        return SupervisorCoordinator(
            self.run_dir, self.ws, self.ws, self.baseline, None, "agy", self.args, None,
        )

    def audit_events(self):
        path = self.run_dir / "l2_audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_off_mode_never_calls_jev(self):
        with decision_mode("off"), patch("afk_supervisor.decisions.shadow.JevProvider") as fake_provider, \
                patch("afk_supervisor.coordinator._dispatch_l2",
                      return_value=("PROCEED", "选择主题甲", self.run_dir / "l.log", None)):
            result = self.make_coordinator().handle_interaction("【决策请求】选择主题", 1)
        fake_provider.assert_not_called()
        self.assertEqual(result[0], "PROCEED")
        self.assertNotIn("DECISION_HEAD_SHADOW", [e["event"] for e in self.audit_events()])

    def test_default_env_off_never_calls_jev(self):
        with decision_mode(None), patch("afk_supervisor.decisions.shadow.JevProvider") as fake_provider, \
                patch("afk_supervisor.coordinator._dispatch_l2",
                      return_value=("PROCEED", "选择主题甲", self.run_dir / "l.log", None)):
            self.make_coordinator().handle_interaction("【决策请求】选择主题", 1)
        fake_provider.assert_not_called()

    def test_shadow_mode_calls_jev_and_keeps_behavior_identical(self):
        l2_result = ("PROCEED", "选择主题甲", self.run_dir / "l.log", None)
        with decision_mode("off"), patch("afk_supervisor.coordinator._dispatch_l2", return_value=l2_result):
            baseline_result = self.make_coordinator().handle_interaction("【决策请求】选择主题", 1)

        with decision_mode("shadow"), patch("afk_supervisor.decisions.shadow.JevProvider", StubProvider), \
                patch("afk_supervisor.coordinator._dispatch_l2", return_value=l2_result):
            shadow_result = self.make_coordinator().handle_interaction("【决策请求】选择主题", 2)

        self.assertEqual(baseline_result[0], shadow_result[0])
        self.assertEqual(baseline_result[1], shadow_result[1])
        events = [e for e in self.audit_events() if e["event"] == "DECISION_HEAD_SHADOW"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "OK")
        self.assertEqual(events[0]["top_actions"], {"route": "consult_agy"})
        self.assertTrue(events[0]["request_id"].startswith("req-decide-2"))
        self.assertEqual(
            [e["event"] for e in self.audit_events()],
            ["INTERACTION_START", "INTERACTION_RESULT",
             "INTERACTION_START", "INTERACTION_RESULT", "DECISION_HEAD_SHADOW"],
        )

    def test_shadow_mode_without_token_does_not_break_decide_flow(self):
        """未配置 Token 时 Jev 返回 CONFIG_ERROR; DECIDE 流程照常返回 AGY 结果。"""
        with decision_mode("shadow"), patch("afk_supervisor.coordinator._dispatch_l2",
                                            return_value=("PROCEED", "选择主题乙", self.run_dir / "l.log", None)):
            result = self.make_coordinator().handle_interaction("【决策请求】选择主题", 3)
        self.assertEqual(result[0], "PROCEED")
        events = [e for e in self.audit_events() if e["event"] == "DECISION_HEAD_SHADOW"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "CONFIG_ERROR")
        self.assertEqual(events[0]["answers"], {})


if __name__ == "__main__":
    unittest.main()
