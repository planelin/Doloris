"""P3 响应标准化测试: 所有模拟响应均有确定结果; 错误响应绝不被静默接受。

fixtures/ 提供 choice / score / noul / malformed / error 五类模拟 Jev 响应;
本测试同时直接测 normalize 与经 JevProvider(mock HTTP) 的端到端解析。
"""

import json
import unittest
from pathlib import Path

from afk_supervisor.decisions.errors import ProviderInvalidResponseError
from afk_supervisor.decisions.jev import JevConfig, JevProvider
from afk_supervisor.decisions.models import DecisionRequest
from afk_supervisor.decisions.normalize import (
    normalize_answers,
    normalize_choice,
    normalize_noul,
    normalize_score,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


ROUTE_QUESTIONS = {
    "route": {
        "type": "choice",
        "instructions": "选择下一步路由",
        "criteria": {
            "auto_answer": "低风险直接代答",
            "consult_agy": "交给 AGY 决策",
            "gather_evidence": "先采证",
            "stop_blocked": "有依据地停止",
        },
    },
}

RISK_QUESTIONS = {
    "risk": {
        "type": "score",
        "instructions": "评估风险等级",
        "criteria": {"1": "极低", "2": "低", "3": "中", "4": "高", "5": "极高"},
    },
}

NOUL_QUESTIONS = {
    "can_auto_answer": {"type": "noul", "instructions": "是否可自动回答"},
}


class ChoiceNormalizeTests(unittest.TestCase):
    def test_fixture_choice_normalizes_deterministically(self):
        raw = load_fixture("choice.json")
        answers = normalize_answers(ROUTE_QUESTIONS, raw["answers"])
        route = answers["route"]
        self.assertEqual(route["type"], "choice")
        self.assertEqual(route["choice"], "consult_agy")
        self.assertEqual(route["top1"], "consult_agy")
        self.assertEqual(route["top2"], "auto_answer")
        self.assertAlmostEqual(route["margin"], 0.69, places=6)
        self.assertEqual(route["probabilities"]["auto_answer"], 0.12)
        self.assertEqual(route["probabilities"]["consult_agy"], 0.81)
        self.assertEqual(route["probabilities"]["gather_evidence"], 0.05)
        self.assertEqual(route["probabilities"]["stop_blocked"], 0.02)

    def test_top1_top2_margin_computed_from_distribution_when_absent(self):
        raw = {"choice": "consult_agy", "probabilities": {"consult_agy": 0.7, "auto_answer": 0.2,
                                                          "gather_evidence": 0.1}}
        out = normalize_choice(ROUTE_QUESTIONS["route"], raw)
        self.assertEqual(out["top1"], "consult_agy")
        self.assertEqual(out["top2"], "auto_answer")
        self.assertAlmostEqual(out["margin"], 0.5, places=6)

    def test_choice_outside_criteria_is_rejected(self):
        raw = {"choice": "deploy_now", "probabilities": {"consult_agy": 0.8, "auto_answer": 0.2}}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_choice(ROUTE_QUESTIONS["route"], raw)

    def test_choice_probability_for_unknown_option_is_rejected(self):
        raw = {"choice": "consult_agy", "probabilities": {"consult_agy": 0.8, "nuclear_launch": 0.2}}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_choice(ROUTE_QUESTIONS["route"], raw)

    def test_choice_without_probabilities_is_rejected(self):
        raw = {"choice": "consult_agy"}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_choice(ROUTE_QUESTIONS["route"], raw)

    def test_choice_argmax_mismatch_with_claimed_top1_is_rejected(self):
        raw = {"choice": "consult_agy", "probabilities": {"consult_agy": 0.8, "auto_answer": 0.2},
               "top1": "auto_answer"}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_choice(ROUTE_QUESTIONS["route"], raw)

    def test_choice_margin_inconsistent_with_distribution_is_rejected(self):
        raw = {"choice": "consult_agy", "probabilities": {"consult_agy": 0.8, "auto_answer": 0.2},
               "margin": 0.9}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_choice(ROUTE_QUESTIONS["route"], raw)

    def test_final_choice_missing_probability_is_rejected(self):
        raw = {"choice": "consult_agy", "probabilities": {"auto_answer": 1.0}}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_choice(ROUTE_QUESTIONS["route"], raw)


class NoulNormalizeTests(unittest.TestCase):
    def test_fixture_noul_keeps_true_false_probabilities(self):
        raw = load_fixture("noul.json")
        answers = normalize_answers(NOUL_QUESTIONS, raw["answers"])
        noul = answers["can_auto_answer"]
        self.assertEqual(noul["type"], "noul")
        self.assertEqual(noul["probabilities"]["true"], 0.87)
        self.assertEqual(noul["probabilities"]["false"], 0.13)
        self.assertNotIn("value", noul)
        self.assertNotIn("bool", noul)

    def test_noul_is_never_converted_to_boolean(self):
        out = normalize_noul(NOUL_QUESTIONS["can_auto_answer"], {"probabilities": {"true": 1.0, "false": 0.0}})
        self.assertIsInstance(out["probabilities"]["true"], float)
        self.assertNotIsInstance(out.get("choice"), bool)

    def test_noul_accepts_flat_true_false_shape(self):
        out = normalize_noul(NOUL_QUESTIONS["can_auto_answer"], {"true": 0.6, "false": 0.4})
        self.assertEqual(out["probabilities"], {"true": 0.6, "false": 0.4})

    def test_noul_missing_false_probability_is_rejected(self):
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_noul(NOUL_QUESTIONS["can_auto_answer"], {"true": 0.9})

    def test_noul_unknown_probability_key_is_rejected(self):
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_noul(NOUL_QUESTIONS["can_auto_answer"],
                           {"true": 0.6, "false": 0.3, "maybe": 0.1})


class ScoreNormalizeTests(unittest.TestCase):
    def test_fixture_score_keeps_score_probabilities_and_levels(self):
        raw = load_fixture("score.json")
        answers = normalize_answers(RISK_QUESTIONS, raw["answers"])
        score = answers["risk"]
        self.assertEqual(score["type"], "score")
        self.assertEqual(score["score"], 3)
        self.assertEqual(score["probabilities"]["3"], 0.5)
        self.assertEqual(score["levels"], RISK_QUESTIONS["risk"]["criteria"])

    def test_score_above_defined_range_is_rejected(self):
        raw = {"score": 9, "probabilities": {"1": 0.5, "2": 0.5}}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_score(RISK_QUESTIONS["risk"], raw)

    def test_score_below_defined_range_is_rejected(self):
        raw = {"score": 0, "probabilities": {"1": 0.5, "2": 0.5}}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_score(RISK_QUESTIONS["risk"], raw)

    def test_score_without_numeric_level_definition_is_rejected(self):
        question = {"type": "score", "criteria": {"low": "低", "high": "高"}}
        raw = {"score": 1, "probabilities": {"low": 0.6, "high": 0.4}}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_score(question, raw)

    def test_score_non_numeric_value_is_rejected(self):
        raw = {"score": "3", "probabilities": {"1": 0.5, "2": 0.5}}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_score(RISK_QUESTIONS["risk"], raw)

    def test_score_probability_for_unknown_level_is_rejected(self):
        raw = {"score": 2, "probabilities": {"1": 0.5, "9": 0.5}}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_score(RISK_QUESTIONS["risk"], raw)


class RealApiContractTests(unittest.TestCase):
    """2026-09 实测 MindsHub /v1/decisions 真实响应形状的标准化行为。"""

    def test_real_noul_numeric_field_becomes_true_false_probabilities(self):
        out = normalize_noul(NOUL_QUESTIONS["can_auto_answer"], {"type": "noul", "noul": 0.02})
        self.assertEqual(out["probabilities"]["true"], 0.02)
        self.assertEqual(out["probabilities"]["false"], 0.98)

    def test_real_noul_probability_out_of_range_is_rejected(self):
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_noul(NOUL_QUESTIONS["can_auto_answer"], {"type": "noul", "noul": 1.5})

    def test_real_score_index_space_is_mapped_back_to_level_keys(self):
        raw = {"type": "score", "score": 1.0, "confidence": 1.0,
               "legend": {"0": "极低", "1": "低", "2": "中"},
               "probabilities": {"0": 0.0, "1": 1.0, "2": 0.0}}
        out = normalize_score(RISK_QUESTIONS["risk"], raw)
        self.assertEqual(out["score"], 2.0)
        self.assertEqual(out["probabilities"], {"1": 0.0, "2": 1.0, "3": 0.0})
        self.assertEqual(out["levels"], RISK_QUESTIONS["risk"]["criteria"])

    def test_real_choice_shape_from_documented_example(self):
        question = {"type": "choice", "criteria": {
            "packaging": "Damage to the packaging, with the item itself intact.",
            "product": "Damage to the item itself.",
            "other": "A different issue, or not enough information to identify one."}}
        raw = {"type": "choice", "choice": "packaging", "confidence": 1.0,
               "probabilities": {"packaging": 1.0, "other": 0.0, "product": 0.0}}
        out = normalize_choice(question, raw)
        self.assertEqual(out["choice"], "packaging")
        self.assertEqual(out["top1"], "packaging")
        self.assertEqual(out["confidence"], 1.0)

    def test_confidence_is_preserved_and_range_checked(self):
        raw = {"type": "choice", "choice": "consult_agy", "confidence": 0.9,
               "probabilities": {"consult_agy": 0.8, "auto_answer": 0.2}}
        self.assertEqual(normalize_choice(ROUTE_QUESTIONS["route"], raw)["confidence"], 0.9)
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_choice(ROUTE_QUESTIONS["route"],
                             {"type": "choice", "choice": "consult_agy", "confidence": 1.5,
                              "probabilities": {"consult_agy": 0.8, "auto_answer": 0.2}})

    def test_answer_declared_type_mismatch_is_rejected(self):
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_noul(NOUL_QUESTIONS["can_auto_answer"], {"type": "choice", "noul": 0.5})
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_choice(ROUTE_QUESTIONS["route"],
                             {"type": "score", "choice": "consult_agy",
                              "probabilities": {"consult_agy": 0.8, "auto_answer": 0.2}})


class AnswerEnvelopeRejectionTests(unittest.TestCase):
    def test_missing_question_key_is_rejected(self):
        raw = {"route": {"choice": "consult_agy", "probabilities": {"consult_agy": 0.8, "auto_answer": 0.2}},
               "risk": {"score": 2, "probabilities": {"1": 0.5, "2": 0.5}}}
        questions = dict(ROUTE_QUESTIONS, **{"can_auto_answer": NOUL_QUESTIONS["can_auto_answer"]})
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_answers(questions, raw)

    def test_unknown_question_key_is_rejected(self):
        raw = {"route": {"choice": "consult_agy", "probabilities": {"consult_agy": 0.8, "auto_answer": 0.2}},
               "hacker_extra": {"score": 1, "probabilities": {"1": 1.0}}}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_answers(ROUTE_QUESTIONS, raw)

    def test_answer_entry_not_a_dict_is_rejected(self):
        raw = {"route": "consult_agy"}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_answers(ROUTE_QUESTIONS, raw)

    def test_probability_not_a_number_is_rejected(self):
        for bad in ("0.8", True, None, [0.8]):
            with self.subTest(value=bad):
                raw = {"route": {"choice": "consult_agy", "probabilities": {"consult_agy": bad, "auto_answer": 0.2}}}
                with self.assertRaises(ProviderInvalidResponseError):
                    normalize_answers(ROUTE_QUESTIONS, raw)

    def test_probability_above_one_is_rejected(self):
        raw = {"route": {"choice": "consult_agy", "probabilities": {"consult_agy": 1.5, "auto_answer": 0.2}}}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_answers(ROUTE_QUESTIONS, raw)

    def test_probability_below_zero_is_rejected(self):
        raw = {"route": {"choice": "consult_agy", "probabilities": {"consult_agy": 0.8, "auto_answer": -0.2}}}
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_answers(ROUTE_QUESTIONS, raw)

    def test_empty_answers_is_rejected(self):
        with self.assertRaises(ProviderInvalidResponseError):
            normalize_answers(ROUTE_QUESTIONS, {})


class FixtureDrivenProviderTests(unittest.TestCase):
    """fixtures 经 mock HTTP 端到端驱动 JevProvider，模拟响应确定可复现。"""

    def make_provider(self, body_bytes):
        config = JevConfig(endpoint="https://api.mindshub.ai/v1/decisions",
                           token="mock-token", model="jev", timeout_sec=5.0)

        class FakeResponse:
            def __init__(self):
                self._remaining = body_bytes

            def read(self, size=-1):
                if size is None or size < 0:
                    out, self._remaining = self._remaining, b""
                    return out
                out, self._remaining = self._remaining[:size], self._remaining[size:]
                return out

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return JevProvider(config=config, urlopen=lambda request, timeout=None: FakeResponse())

    def make_request(self, questions, request_id):
        return DecisionRequest(request_id=request_id, state={"worker_alive": True}, questions=questions)

    def test_choice_fixture_end_to_end_ok(self):
        raw = load_fixture("choice.json")
        provider = self.make_provider(json.dumps(raw).encode("utf-8"))
        result = provider.decide(self.make_request(ROUTE_QUESTIONS, "req-fixture-choice"))
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.answers["route"]["choice"], "consult_agy")
        self.assertEqual(result.raw_metadata.get("model"), "jev-1.13.0")

    def test_score_fixture_end_to_end_ok(self):
        raw = load_fixture("score.json")
        provider = self.make_provider(json.dumps(raw).encode("utf-8"))
        result = provider.decide(self.make_request(RISK_QUESTIONS, "req-fixture-score"))
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.answers["risk"]["score"], 3)
        self.assertEqual(result.answers["risk"]["levels"], RISK_QUESTIONS["risk"]["criteria"])

    def test_noul_fixture_end_to_end_ok(self):
        raw = load_fixture("noul.json")
        provider = self.make_provider(json.dumps(raw).encode("utf-8"))
        result = provider.decide(self.make_request(NOUL_QUESTIONS, "req-fixture-noul"))
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.answers["can_auto_answer"]["probabilities"]["true"], 0.87)

    def test_malformed_fixture_is_rejected_as_invalid_response(self):
        raw = load_fixture("malformed.json")
        provider = self.make_provider(json.dumps(raw).encode("utf-8"))
        result = provider.decide(self.make_request(ROUTE_QUESTIONS, "req-fixture-malformed"))
        self.assertEqual(result.status, "INVALID_RESPONSE")
        self.assertEqual(result.error_code, "decision_api_invalid_response")
        self.assertEqual(result.answers, {})

    def test_error_fixture_without_answers_is_rejected_not_accepted(self):
        raw = load_fixture("error.json")
        provider = self.make_provider(json.dumps(raw).encode("utf-8"))
        result = provider.decide(self.make_request(ROUTE_QUESTIONS, "req-fixture-error"))
        self.assertEqual(result.status, "INVALID_RESPONSE")
        self.assertEqual(result.answers, {})
        self.assertNotIn("error", result.answers)


if __name__ == "__main__":
    unittest.main()
