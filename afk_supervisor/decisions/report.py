"""afk_supervisor.decisions.report — Shadow Mode 影子数据对照报告 (只读分析工具)
================================================================================
把 runs/<id>/l2_audit.jsonl 中的 DECISION_HEAD_SHADOW 与 INTERACTION_RESULT 按
决策门配对, 量化「Jev 影子判断 vs AGY 实际路径」的一致率与同题漂移率,
为未来 active 模式的量化启用门槛提供数据依据。

硬性边界: 只读取审计文件、只输出文本; 不读取/修改监管状态, 不影响任何运行行为。
当前阶段 (Shadow) 实际路径恒为 AGY, 因此「一致」= Jev 也建议走 AGY (consult_agy),
「分歧」= Jev 认为可代答 (auto_answer) 但实际走了 AGY。
"""

import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SHADOW_EVENT = "DECISION_HEAD_SHADOW"
INTERACTION_START = "INTERACTION_START"
INTERACTION_RESULT = "INTERACTION_RESULT"

GATE_SNIPPET_CHARS = 100
ANSWER_SNIPPET_CHARS = 80

ACTIVE_MIN_SAMPLES = 20
ACTIVE_MIN_AGREEMENT_RATE = 0.8
ACTIVE_MAX_DRIFT_RATE = 0.1


@dataclass
class GateAttempt:
    """单个决策门的一次尝试 (AGY 调用 + 对应的 Jev 影子观察, 若启用)。"""

    ts: str = ""
    n: int = 0
    request_id: str = ""
    snippet: str = ""
    agy_verdict: str = ""
    agy_answer: str = ""
    jev_status: str = ""  # 空 = 该次未被影子观察 (mode=off)
    jev_error_code: str = ""
    jev_route: str = ""
    jev_margin: Optional[float] = None
    jev_confidence: Optional[float] = None
    jev_can_auto_true: Optional[float] = None
    jev_risk: Optional[float] = None
    jev_latency_ms: Optional[int] = None
    request_hash: str = ""

    @property
    def observed(self) -> bool:
        return bool(self.jev_status)


@dataclass
class Gate:
    """一个决策门 (可能含多次尝试, 例如 AGY 协议错误后的重试)。"""

    key: str
    attempts: List[GateAttempt] = field(default_factory=list)

    @property
    def snippet(self) -> str:
        return self.attempts[0].snippet if self.attempts else ""

    @property
    def observed_routes(self) -> List[str]:
        return [a.jev_route for a in self.attempts if a.observed and a.jev_route]

    @property
    def drifted(self) -> bool:
        """同题漂移: 同一决策门的多次尝试中 Jev 路由判断不一致。"""
        return len(set(self.observed_routes)) > 1


def parse_audit_events(path: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _attach_jev(attempt: GateAttempt, shadow: Dict[str, Any]) -> None:
    attempt.jev_status = str(shadow.get("status", ""))
    attempt.jev_error_code = str(shadow.get("error_code", "") or "")
    attempt.jev_latency_ms = shadow.get("latency_ms") if isinstance(shadow.get("latency_ms"), int) else None
    attempt.request_hash = str(shadow.get("request_hash", "") or "")
    if attempt.jev_status != "OK":
        # 失败调用不产生可信答案 (观察器契约: 失败时 answers 为空)。
        return
    answers = shadow.get("answers") if isinstance(shadow.get("answers"), dict) else {}
    route = answers.get("route") if isinstance(answers.get("route"), dict) else {}
    attempt.jev_route = str(route.get("choice", "")) if route else ""
    margin = route.get("margin")
    attempt.jev_margin = float(margin) if isinstance(margin, (int, float)) else None
    confidence = route.get("confidence")
    attempt.jev_confidence = float(confidence) if isinstance(confidence, (int, float)) else None
    noul = answers.get("can_auto_answer") if isinstance(answers.get("can_auto_answer"), dict) else {}
    probabilities = noul.get("probabilities") if isinstance(noul.get("probabilities"), dict) else {}
    true_prob = probabilities.get("true")
    attempt.jev_can_auto_true = float(true_prob) if isinstance(true_prob, (int, float)) else None
    risk = answers.get("risk") if isinstance(answers.get("risk"), dict) else {}
    score = risk.get("score")
    attempt.jev_risk = float(score) if isinstance(score, (int, float)) else None


def build_gates(events: List[Dict[str, Any]]) -> List[Gate]:
    """按时间序扫描事件流, 把 START/RESULT/SHADOW 组装成决策门与其尝试。"""
    shadows_by_rid = {
        e.get("request_id"): e for e in events
        if e.get("event") == SHADOW_EVENT and e.get("request_id")
    }
    gates: Dict[str, Gate] = {}
    order: List[str] = []
    last_start_snippet: Dict[int, str] = {}

    for event in sorted(events, key=lambda e: str(e.get("ts", ""))):
        kind = event.get("event")
        if kind == INTERACTION_START:
            try:
                n = int(event.get("n", 0))
            except (TypeError, ValueError):
                n = 0
            last_start_snippet[n] = str(event.get("snippet", "") or "")
            continue
        if kind != INTERACTION_RESULT:
            continue
        request_id = event.get("request_id")
        if not request_id:
            continue
        try:
            n = int(event.get("n", 0))
        except (TypeError, ValueError):
            n = 0
        snippet = last_start_snippet.get(n, "")
        gate_key = f"n{n}|{snippet[:GATE_SNIPPET_CHARS]}"
        if gate_key not in gates:
            gates[gate_key] = Gate(key=gate_key)
            order.append(gate_key)
        attempt = GateAttempt(
            ts=str(event.get("ts", "")),
            n=n,
            request_id=str(request_id),
            snippet=snippet,
            agy_verdict=str(event.get("verdict", "")),
            agy_answer=str(event.get("answer", "") or ""),
        )
        shadow = shadows_by_rid.get(request_id)
        if shadow is not None:
            _attach_jev(attempt, shadow)
        gates[gate_key].attempts.append(attempt)

    for gate in gates.values():
        gate.attempts.sort(key=lambda a: a.ts)
    return [gates[key] for key in order]


def summarize(gates: List[Gate]) -> Dict[str, Any]:
    attempts = [a for gate in gates for a in gate.attempts]
    observed = [a for a in attempts if a.observed]
    ok = [a for a in observed if a.jev_status == "OK"]
    failed = [a for a in observed if a.jev_status != "OK"]
    routes = Counter(a.jev_route for a in ok)
    agree = [a for a in ok if a.jev_route == "consult_agy"]
    differ = [a for a in ok if a.jev_route == "auto_answer"]
    other = [a for a in ok if a.jev_route not in ("consult_agy", "auto_answer", "")]
    drift_gates = [g for g in gates if g.drifted]
    latencies = [a.jev_latency_ms for a in ok if a.jev_latency_ms is not None]
    confidences = [a.jev_confidence for a in ok if a.jev_confidence is not None]
    return {
        "gates": len(gates),
        "attempts": len(attempts),
        "observed": len(observed),
        "shadow_ok": len(ok),
        "shadow_failed": len(failed),
        "routes": dict(routes),
        "agree_consult": len(agree),
        "differ_auto_answer": len(differ),
        "other_routes": len(other),
        "drift_gates": len(drift_gates),
        "avg_latency_ms": round(statistics.mean(latencies)) if latencies else None,
        "avg_confidence": round(statistics.mean(confidences), 3) if confidences else None,
        "agreement_rate": round(len(agree) / len(ok), 3) if ok else None,
        "drift_rate": round(len(drift_gates) / len(gates), 3) if gates else None,
    }


def _fmt_attempt(attempt: GateAttempt, index: int) -> List[str]:
    verdict = attempt.agy_verdict or "?"
    lines = []
    lines.append(f"  尝试{index} [{attempt.ts}] AGY={verdict}")
    if attempt.agy_answer:
        lines.append(f"    AGY 决议: {attempt.agy_answer.replace(chr(10), ' ')[:ANSWER_SNIPPET_CHARS]}")
    if not attempt.observed:
        lines.append("    Jev  : (未观察 — mode=off 或历史 run)")
        return lines
    if attempt.jev_status != "OK":
        lines.append(
            f"    Jev  : 调用失败 status={attempt.jev_status} error={attempt.jev_error_code} "
            f"({attempt.jev_latency_ms}ms)")
        return lines
    margin = f"{attempt.jev_margin:.2f}" if attempt.jev_margin is not None else "?"
    conf = f"{attempt.jev_confidence:.2f}" if attempt.jev_confidence is not None else "?"
    can_auto = f"{attempt.jev_can_auto_true:.2f}" if attempt.jev_can_auto_true is not None else "?"
    risk = f"{attempt.jev_risk:.2f}" if attempt.jev_risk is not None else "?"
    lines.append(
        f"    Jev  : route={attempt.jev_route} (margin={margin}, conf={conf}) "
        f"can_auto_true={can_auto} risk={risk} [{attempt.jev_latency_ms}ms]")
    if attempt.jev_route == "consult_agy":
        lines.append("    对照 : 一致 — Jev 也建议走 AGY")
    elif attempt.jev_route == "auto_answer":
        lines.append("    对照 : 分歧 — Jev 认为可代答, 实际走了 AGY")
    elif attempt.jev_route:
        lines.append(f"    对照 : 其他信号 — Jev 建议 {attempt.jev_route}")
    return lines


def format_run_report(gates: List[Gate], run_id: str) -> str:
    lines = [f"=== Shadow 对照报告: {run_id} ==="]
    if not gates:
        lines.append("(该 run 无决策门 — 没有 DECIDE 交互, Shadow 无观察点)")
        return "\n".join(lines)
    for index, gate in enumerate(gates, 1):
        mark = "  ⚠ 同题漂移" if gate.drifted else ""
        lines.append(f"门 #{index} ({len(gate.attempts)} 次尝试){mark}")
        lines.append(f"  问题: {gate.snippet.replace(chr(10), ' ')[:GATE_SNIPPET_CHARS]}")
        for attempt_index, attempt in enumerate(gate.attempts, 1):
            lines.extend(_fmt_attempt(attempt, attempt_index))
    return "\n".join(lines)


def format_summary(summary: Dict[str, Any]) -> str:
    lines = ["=== 汇总 ==="]
    lines.append(
        f"决策门: {summary['gates']} | 尝试: {summary['attempts']} | "
        f"Jev 调用: {summary['shadow_ok']} OK / {summary['shadow_failed']} 失败"
        + (f" (平均 {summary['avg_latency_ms']}ms)" if summary["avg_latency_ms"] is not None else ""))
    if summary["observed"] == 0:
        lines.append("(无影子观察 — DOLORIS_DECISION_MODE 未启用或无决策门)")
        return "\n".join(lines)
    lines.append(f"路由分布: {summary['routes']}")
    lines.append(
        f"一致 (建议 AGY): {summary['agree_consult']} | "
        f"分歧 (建议代答): {summary['differ_auto_answer']} | 其他: {summary['other_routes']}")
    lines.append(
        f"一致率: {summary['agreement_rate']} | 同题漂移门: {summary['drift_gates']} "
        f"(漂移率 {summary['drift_rate']}) | 平均置信度: {summary['avg_confidence']}")
    if summary["attempts"] < ACTIVE_MIN_SAMPLES:
        lines.append(
            f"评估: 样本 {summary['attempts']} 次, 未达 active 门槛评估所需样本量 "
            f"(≥{ACTIVE_MIN_SAMPLES}), 继续影子积累。")
    else:
        agreement_ok = (summary["agreement_rate"] or 0) >= ACTIVE_MIN_AGREEMENT_RATE
        drift_ok = (summary["drift_rate"] or 1) <= ACTIVE_MAX_DRIFT_RATE
        lines.append(
            f"评估: 样本充足。一致率门槛(≥{ACTIVE_MIN_AGREEMENT_RATE}): "
            f"{'达标' if agreement_ok else '未达标'}; 漂移率门槛(≤{ACTIVE_MAX_DRIFT_RATE}): "
            f"{'达标' if drift_ok else '未达标'}。"
            + ("可进入 active 模式评审。" if agreement_ok and drift_ok else "继续影子积累。"))
    return "\n".join(lines)
