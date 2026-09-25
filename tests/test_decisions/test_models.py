"""P1 决策层抽象接口测试: DecisionRequest / DecisionResult / DecisionProvider / ProviderError。

覆盖: 数据结构构造、默认值、request_id 传递、schema 校验、无效问题结构、
与 AGY L2 类型的隔离（status 与 verdict 词汇表零重叠）。
"""

import unittest
from dataclasses import fields

from afk_supervisor.decisions import (
    DECISION_SCHEMA,
    RESULT_STATUSES,
    DecisionProvider,
    DecisionRequest,
    DecisionResult,
    InvalidDecisionRequestError,
    InvalidDecisionResultError,
    ProviderError,
)


def make_request(**overrides):
    base = dict(
        request_id="req-test-0001",
        state={"worker_alive": True, "tests_failed": False},
        questions={
            "route": {
                "type": "choice",
                "instructions": "选择下一步路由",
                "criteria": {"consult_agy": "交给 AGY 决策", "auto_answer": "低风险直接代答"},
            },
        },
        metadata={"task_stage": "finite_choice"},
    )
    base.update(overrides)
    return DecisionRequest(**base)


def make_result(**overrides):
    base = dict(
        provider="jev",
        model="jev",
        request_id="req-test-0001",
        answers={"route": {"choice": "consult_agy"}},
        latency_ms=123,
        status="OK",
    )
    base.update(overrides)
    return DecisionResult(**base)


class DecisionRequestTests(unittest.TestCase):
    def test_valid_request_constructs_with_explicit_schema(self):
        req = make_request(schema=DECISION_SCHEMA)
        self.assertEqual(req.schema, "doloris.decision.v1")
        self.assertEqual(req.request_id, "req-test-0001")

    def test_schema_defaults_to_decision_v1(self):
        req = DecisionRequest(request_id="req-x", state={}, questions={"q": {"type": "noul"}})
        self.assertEqual(req.schema, DECISION_SCHEMA)

    def test_wrong_schema_is_rejected(self):
        for bad in ("doloris.decision.v2", "agy.l2.v1", "", None):
            with self.assertRaises(InvalidDecisionRequestError):
                make_request(schema=bad)

    def test_request_id_must_be_nonempty_trackable_string(self):
        for bad in ("", "   ", None, 123):
            with self.assertRaises(InvalidDecisionRequestError):
                make_request(request_id=bad)

    def test_state_must_be_dict(self):
        with self.assertRaises(InvalidDecisionRequestError):
            make_request(state="worker alive")

    def test_metadata_defaults_to_empty_dict_and_must_be_dict(self):
        req = DecisionRequest(request_id="req-x", state={}, questions={"q": {"type": "noul"}})
        self.assertEqual(req.metadata, {})
        with self.assertRaises(InvalidDecisionRequestError):
            make_request(metadata=[("k", "v")])

    def test_to_dict_round_trips_all_fields(self):
        req = make_request()
        data = req.to_dict()
        self.assertEqual(set(data), {"schema", "request_id", "state", "questions", "metadata"})
        self.assertEqual(data["request_id"], "req-test-0001")
        self.assertEqual(data["schema"], DECISION_SCHEMA)

    def test_questions_must_be_nonempty_dict(self):
        with self.assertRaises(InvalidDecisionRequestError):
            make_request(questions={})
        with self.assertRaises(InvalidDecisionRequestError):
            make_request(questions=["route"])

    def test_invalid_question_structures_are_rejected(self):
        cases = [
            ({1: {"type": "noul"}}, "非字符串 key"),
            ({"": {"type": "noul"}}, "空 key"),
            ({"route": "choice"}, "问题定义不是 dict"),
            ({"route": {}}, "缺少 type"),
            ({"route": {"type": "boolean"}}, "未知 type"),
            ({"route": {"type": None}}, "type 为 None"),
            ({"route": {"type": "choice"}}, "choice 缺少 criteria"),
            ({"route": {"type": "choice", "criteria": {}}}, "choice criteria 为空"),
            ({"route": {"type": "choice", "criteria": {"a": 1}}}, "choice criteria 值不是字符串"),
            ({"route": {"type": "choice", "criteria": {2: "x"}}}, "choice criteria key 不是字符串"),
            ({"risk": {"type": "score"}}, "score 缺少等级定义"),
            ({"risk": {"type": "score", "criteria": []}}, "score 等级定义不是 dict"),
        ]
        for questions, label in cases:
            with self.subTest(case=label):
                with self.assertRaises(InvalidDecisionRequestError):
                    make_request(questions=questions)

    def test_valid_question_types_accept_noul_choice_score(self):
        req = make_request(questions={
            "route": {"type": "choice", "criteria": {"a": "选项A", "b": "选项B"}},
            "risk": {"type": "score", "criteria": {"1": "低", "2": "高"}},
            "can_auto_answer": {"type": "noul", "instructions": "是否可自动回答"},
        })
        self.assertEqual(len(req.questions), 3)


class DecisionResultTests(unittest.TestCase):
    def test_valid_result_constructs(self):
        res = make_result()
        self.assertEqual(res.provider, "jev")
        self.assertEqual(res.status, "OK")
        self.assertEqual(res.error_code, "")
        self.assertEqual(res.raw_metadata, {})

    def test_error_result_keeps_empty_answers_and_error_code(self):
        res = make_result(answers={}, status="TIMEOUT", error_code="decision_api_timeout")
        self.assertEqual(res.answers, {})
        self.assertEqual(res.status, "TIMEOUT")
        self.assertEqual(res.error_code, "decision_api_timeout")

    def test_status_vocabulary_is_fixed(self):
        for status in ("OK", "TIMEOUT", "AUTH_ERROR", "HTTP_ERROR", "INVALID_RESPONSE", "CONFIG_ERROR", "NETWORK_ERROR"):
            with self.subTest(status=status):
                self.assertEqual(make_result(status=status).status, status)

    def test_agy_l2_verdicts_are_never_valid_statuses(self):
        """决策层结果与 AGY L2 结果必须保持类型隔离: AGY verdict 一律不得作为 status。"""
        for agy_verdict in ("PROCEED", "PASS", "FAIL", "REPAIRED", "STOP", "COMPLETED",
                            "INCONCLUSIVE", "UNRESOLVED", "NO-VERDICT", "NO-BRIDGE", "PROTOCOL_ERROR"):
            with self.subTest(verdict=agy_verdict):
                with self.assertRaises(InvalidDecisionResultError):
                    make_result(status=agy_verdict)
                self.assertNotIn(agy_verdict, RESULT_STATUSES)

    def test_result_field_structure_matches_pipeline_contract(self):
        names = {f.name for f in fields(DecisionResult)}
        self.assertLessEqual(
            {"provider", "model", "request_id", "answers", "latency_ms", "status", "error_code", "raw_metadata"},
            names,
        )

    def test_invalid_result_constructions_are_rejected(self):
        cases = [
            (dict(provider=""), "provider 为空"),
            (dict(provider=None), "provider 缺失"),
            (dict(model=""), "model 为空"),
            (dict(request_id=""), "request_id 为空"),
            (dict(answers=None), "answers 不是 dict"),
            (dict(latency_ms=-1), "latency 为负"),
            (dict(latency_ms="12"), "latency 不是数字"),
            (dict(latency_ms=True), "latency 是布尔"),
            (dict(raw_metadata=None), "raw_metadata 不是 dict"),
        ]
        for overrides, label in cases:
            with self.subTest(case=label):
                with self.assertRaises(InvalidDecisionResultError):
                    make_result(**overrides)

    def test_request_id_passes_from_request_to_result(self):
        """request_id 必须可追踪: 请求与结果的 request_id 一一对应。"""
        req = make_request(request_id="req-trace-42")
        res = make_result(request_id=req.request_id)
        self.assertEqual(res.request_id, "req-trace-42")


class DecisionProviderProtocolTests(unittest.TestCase):
    def test_fake_provider_satisfies_protocol_without_network(self):
        class FakeProvider:
            provider_name = "fake"

            def decide(self, request: DecisionRequest) -> DecisionResult:
                return make_result(provider="fake", request_id=request.request_id,
                                   answers={"route": {"choice": request.questions["route"]["criteria"]
                                                      and list(request.questions["route"]["criteria"])[0]}})

        provider = FakeProvider()
        self.assertIsInstance(provider, DecisionProvider)
        res = provider.decide(make_request(request_id="req-fake-1"))
        self.assertEqual(res.request_id, "req-fake-1")
        self.assertEqual(res.provider, "fake")

    def test_object_without_decide_does_not_satisfy_protocol(self):
        class NotAProvider:
            provider_name = "bad"

        self.assertNotIsInstance(NotAProvider(), DecisionProvider)

    def test_provider_error_carries_stable_error_code(self):
        err = ProviderError("调用失败", error_code="decision_api_timeout")
        self.assertEqual(err.error_code, "decision_api_timeout")
        self.assertEqual(str(err), "调用失败")
        self.assertEqual(ProviderError("x").error_code, "provider_error")


if __name__ == "__main__":
    unittest.main()
