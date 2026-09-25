"""Shadow 对照报告的配对/漂移/汇总逻辑测试 (全部基于合成审计事件, 只读)。"""

import json
import unittest

from afk_supervisor.decisions.report import (
    build_gates,
    format_run_report,
    format_summary,
    parse_audit_events,
    summarize,
)


def start(ts, n, snippet):
    return {"ts": ts, "event": "INTERACTION_START", "n": n, "snippet": snippet}


def result(ts, n, request_id, verdict="PROCEED", answer="已拍板主题甲"):
    return {"ts": ts, "event": "INTERACTION_RESULT", "n": n, "request_id": request_id,
            "verdict": verdict, "answer": answer}


def shadow(ts, request_id, route="consult_agy", margin=0.6, confidence=0.8,
           can_auto=0.3, risk=2.0, status="OK", latency=2000, request_hash="sha256:aaa"):
    event = {
        "ts": ts, "event": "DECISION_HEAD_SHADOW", "provider": "jev", "model": "jev",
        "request_id": request_id, "status": status, "error_code": "" if status == "OK" else "decision_api_timeout",
        "latency_ms": latency, "request_hash": request_hash,
        "answers": {},
    }
    if status == "OK":
        # 观察器契约: 只有成功调用才携带答案
        event["answers"] = {
            "route": {"type": "choice", "choice": route, "margin": margin, "confidence": confidence},
            "can_auto_answer": {"type": "noul", "probabilities": {"true": can_auto, "false": round(1 - can_auto, 6)}},
            "risk": {"type": "score", "score": risk},
        }
    return event


SNIPPET_THEME = "【决策请求】请选择全站视觉主题 甲/乙"


class BuildGatesTests(unittest.TestCase):
    def test_gate_with_retry_and_route_drift_is_grouped_and_flagged(self):
        events = [
            start("2026-09-25T14:42:39", 1, SNIPPET_THEME),
            result("2026-09-25T14:43:01", 1, "req-decide-1-a1", verdict="PROTOCOL_ERROR"),
            shadow("2026-09-25T14:43:03", "req-decide-1-a1", route="consult_agy",
                   margin=0.05, confidence=0.30, can_auto=0.29, risk=2.64, request_hash="sha256:k1"),
            start("2026-09-25T14:43:08", 1, SNIPPET_THEME),
            result("2026-09-25T14:43:21", 1, "req-decide-1-b2"),
            shadow("2026-09-25T14:43:24", "req-decide-1-b2", route="auto_answer",
                   margin=0.43, confidence=0.46, can_auto=0.51, risk=2.62, request_hash="sha256:k1"),
        ]
        gates = build_gates(events)
        self.assertEqual(len(gates), 1)
        gate = gates[0]
        self.assertEqual(len(gate.attempts), 2)
        self.assertTrue(gate.drifted)
        self.assertEqual(gate.attempts[0].jev_route, "consult_agy")
        self.assertEqual(gate.attempts[1].jev_route, "auto_answer")
        self.assertEqual(gate.attempts[0].agy_verdict, "PROTOCOL_ERROR")
        # 同题漂移判定依据: 两次尝试的 request_hash 相同
        self.assertEqual(gate.attempts[0].request_hash, gate.attempts[1].request_hash)

    def test_separate_gates_stay_separate(self):
        events = [
            start("2026-09-25T14:42:39", 1, SNIPPET_THEME),
            result("2026-09-25T14:43:01", 1, "req-decide-1-a1"),
            shadow("2026-09-25T14:43:03", "req-decide-1-a1", route="consult_agy"),
            start("2026-09-25T14:51:21", 2, "【决策请求】请选择文案语言 中文/英文"),
            result("2026-09-25T14:51:27", 2, "req-decide-2-c3"),
            shadow("2026-09-25T14:51:29", "req-decide-2-c3", route="auto_answer"),
        ]
        gates = build_gates(events)
        self.assertEqual(len(gates), 2)
        self.assertFalse(gates[0].drifted)
        self.assertFalse(gates[1].drifted)
        self.assertEqual([g.attempts[0].jev_route for g in gates], ["consult_agy", "auto_answer"])

    def test_mode_off_run_yields_unobserved_attempts(self):
        events = [
            start("2026-09-25T10:00:00", 1, SNIPPET_THEME),
            result("2026-09-25T10:00:20", 1, "req-decide-1-x"),
        ]
        gates = build_gates(events)
        self.assertEqual(len(gates), 1)
        self.assertFalse(gates[0].attempts[0].observed)
        summary = summarize(gates)
        self.assertEqual(summary["observed"], 0)
        self.assertIsNone(summary["agreement_rate"])

    def test_shadow_failure_status_is_preserved(self):
        events = [
            start("2026-09-25T10:00:00", 1, SNIPPET_THEME),
            result("2026-09-25T10:00:20", 1, "req-decide-1-x"),
            shadow("2026-09-25T10:00:23", "req-decide-1-x", status="TIMEOUT"),
        ]
        gates = build_gates(events)
        attempt = gates[0].attempts[0]
        self.assertTrue(attempt.observed)
        self.assertEqual(attempt.jev_status, "TIMEOUT")
        self.assertEqual(attempt.jev_error_code, "decision_api_timeout")
        self.assertEqual(attempt.jev_route, "")

    def test_malformed_json_lines_are_skipped(self):
        import tempfile
        from pathlib import Path
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as handle:
            handle.write(json.dumps(start("2026-09-25T10:00:00", 1, SNIPPET_THEME)) + "\n")
            handle.write("not-json-line\n")
            handle.write(json.dumps(result("2026-09-25T10:00:20", 1, "req-x")) + "\n")
            path = Path(handle.name)
        try:
            parsed = parse_audit_events(path)
            self.assertEqual(len(parsed), 2)
            self.assertEqual(len(build_gates(parsed)), 1)
        finally:
            path.unlink()


class SummaryTests(unittest.TestCase):
    def make_gates(self):
        events = [
            start("2026-09-25T14:42:39", 1, SNIPPET_THEME),
            result("2026-09-25T14:43:01", 1, "r1", verdict="PROTOCOL_ERROR"),
            shadow("2026-09-25T14:43:03", "r1", route="consult_agy", margin=0.05,
                   confidence=0.30, latency=2547, request_hash="sha256:k"),
            result("2026-09-25T14:43:21", 1, "r2"),
            shadow("2026-09-25T14:43:24", "r2", route="auto_answer", margin=0.43,
                   confidence=0.46, latency=2641, request_hash="sha256:k"),
            start("2026-09-25T14:43:54", 2, "【决策请求】语言选择"),
            result("2026-09-25T14:44:04", 2, "r3"),
            shadow("2026-09-25T14:44:05", "r3", route="auto_answer", margin=0.37,
                   confidence=0.47, latency=1828, request_hash="sha256:other"),
        ]
        return build_gates(events)

    def test_summary_counts(self):
        summary = summarize(self.make_gates())
        self.assertEqual(summary["gates"], 2)
        self.assertEqual(summary["attempts"], 3)
        self.assertEqual(summary["shadow_ok"], 3)
        self.assertEqual(summary["routes"], {"consult_agy": 1, "auto_answer": 2})
        self.assertEqual(summary["agree_consult"], 1)
        self.assertEqual(summary["differ_auto_answer"], 2)
        self.assertEqual(summary["drift_gates"], 1)
        self.assertEqual(summary["avg_latency_ms"], 2339)
        self.assertAlmostEqual(summary["avg_confidence"], 0.410, places=3)
        self.assertAlmostEqual(summary["agreement_rate"], 0.333, places=3)
        self.assertAlmostEqual(summary["drift_rate"], 0.5, places=3)

    def test_summary_below_sample_threshold_recommends_accumulation(self):
        text = format_summary(summarize(self.make_gates()))
        self.assertIn("未达 active 门槛评估所需样本量", text)
        self.assertIn("继续影子积累", text)

    def test_summary_with_sufficient_samples_applies_thresholds(self):
        gates = self.make_gates()
        import copy
        gates = gates + [copy.deepcopy(g) for g in gates for _ in range(10)]
        for gate in gates:
            for attempt in gate.attempts:
                attempt.ts = attempt.ts  # 保留
        text = format_summary(summarize(gates))
        self.assertIn("样本充足", text)

    def test_run_report_renders_gate_details(self):
        text = format_run_report(self.make_gates(), "run-x")
        self.assertIn("=== Shadow 对照报告: run-x ===", text)
        self.assertIn("AGY=PROTOCOL_ERROR", text)
        self.assertIn("一致 — Jev 也建议走 AGY", text)
        self.assertIn("分歧 — Jev 认为可代答, 实际走了 AGY", text)
        self.assertIn("同题漂移", text)

    def test_run_report_without_gates_is_explicit(self):
        text = format_run_report([], "empty-run")
        self.assertIn("无决策门", text)


if __name__ == "__main__":
    unittest.main()
