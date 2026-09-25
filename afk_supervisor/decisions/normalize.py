"""afk_supervisor.decisions.normalize — Jev 原始响应标准化
==========================================================
把 Jev 返回的 answers 原始结构转换为 Doloris 统一决策答案结构;
对任何不可信结构 fail-closed: 抛出 ProviderInvalidResponseError，绝不静默接受。

MindsHub /v1/decisions 真实响应形状 (2026-09 实测):
- choice: {type, choice, confidence, probabilities: {选项名: 概率}}
- score:  {type, score(0 起index空间的加权平均), confidence, legend, probabilities: {"0": p, ...}}
- noul:   {type, noul: <0..1 概率数值>}

统一输出 (按问题类型):
- choice: {type, choice, probabilities, top1, top2, margin, confidence?}
- noul:   {type, probabilities: {true, false}}   —— 不在此转布尔，阈值由调用方显式决定
- score:  {type, score, probabilities, levels, confidence?}  —— score 映射回内部等级 key 空间

拒绝规则 (pipeline §6 / §P3):
- 缺少问题 key / answers 中出现未知问题 key / 答案项不是 dict
- 答案自带 type 与问题类型不一致
- 概率不是数字 (含布尔) / 概率 < 0 / 概率 > 1
- choice 不在 criteria 中; 声明的 top1 / margin 与概率分布不一致
- score 超出定义范围; score 等级定义必须为数值型 key
"""

from typing import Any, Dict

from afk_supervisor.decisions.errors import ProviderInvalidResponseError

_MARGIN_TOLERANCE = 1e-3


def _reject(reason: str):
    raise ProviderInvalidResponseError(f"Jev 响应标准化失败: {reason}")


def _check_answer_type(raw: Dict[str, Any], expected: str) -> None:
    """真实 API 每个答案自带 type; 若声明类型与问题类型不一致则整体不可信。"""
    declared = raw.get("type")
    if declared is not None and declared != expected:
        _reject(f"答案声明类型 {declared!r} 与问题类型 {expected!r} 不一致")


def _check_probability(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject(f"{label} 概率不是数字: {value!r}")
    prob = float(value)
    if prob < 0.0 or prob > 1.0:
        _reject(f"{label} 概率越界 [0,1]: {prob}")
    return prob


def _optional_confidence(raw: Dict[str, Any], out: Dict[str, Any]) -> None:
    confidence = raw.get("confidence")
    if confidence is None:
        return
    out["confidence"] = _check_probability(confidence, "confidence")


def _validated_probabilities(raw: Any, label: str) -> Dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        _reject(f"{label} 缺少 probabilities 结构")
    return {str(key): _check_probability(value, label) for key, value in raw.items()}


def _ranked(probabilities: Dict[str, float]) -> list:
    """确定性排序: 概率降序、同分按 key 字典序，保证 top1/top2 可复现。"""
    return sorted(probabilities.items(), key=lambda kv: (-kv[1], kv[0]))


def normalize_choice(question: Dict[str, Any], raw: Any) -> Dict[str, Any]:
    criteria = question.get("criteria") or {}
    if not isinstance(raw, dict):
        _reject("choice 答案不是 dict")
    _check_answer_type(raw, "choice")
    choice = raw.get("choice")
    if not isinstance(choice, str) or choice not in criteria:
        _reject(f"choice {choice!r} 不在 criteria 中")
    probabilities = _validated_probabilities(raw.get("probabilities"), "choice")
    for key in probabilities:
        if key not in criteria:
            _reject(f"choice probabilities 含未知选项 {key!r}")
    if choice not in probabilities:
        _reject(f"最终选项 {choice!r} 缺少对应概率")
    ranked = _ranked(probabilities)
    top1, p_top1 = ranked[0]
    if top1 != choice:
        _reject(f"概率分布 argmax ({top1!r}) 与最终选择 ({choice!r}) 不一致")
    top2 = ranked[1][0] if len(ranked) > 1 else ""
    p_top2 = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = round(p_top1 - p_top2, 6)
    claimed_top1 = raw.get("top1")
    if claimed_top1 is not None and claimed_top1 != top1:
        _reject(f"响应声明 top1={claimed_top1!r} 与概率分布不一致")
    claimed_margin = raw.get("margin")
    if isinstance(claimed_margin, (int, float)) and not isinstance(claimed_margin, bool):
        if abs(float(claimed_margin) - margin) > _MARGIN_TOLERANCE:
            _reject(f"响应声明 margin={claimed_margin!r} 与概率分布不一致")
    out = {
        "type": "choice",
        "choice": choice,
        "probabilities": probabilities,
        "top1": top1,
        "top2": top2,
        "margin": margin,
    }
    _optional_confidence(raw, out)
    return out


def normalize_noul(question: Dict[str, Any], raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        _reject("noul 答案不是 dict")
    _check_answer_type(raw, "noul")
    # 真实 API 形状: {"type": "noul", "noul": <P(true) 数值>}
    if "noul" in raw and not isinstance(raw.get("probabilities"), dict):
        value = raw["noul"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _reject(f"noul 概率不是数字: {value!r}")
        probability = float(value)
        if probability < 0.0 or probability > 1.0:
            _reject(f"noul 概率越界 [0,1]: {probability}")
        return {
            "type": "noul",
            "probabilities": {"true": round(probability, 6), "false": round(1.0 - probability, 6)},
        }
    # 兼容形状: probabilities {true, false} 或平铺 true/false
    source = raw.get("probabilities") if isinstance(raw.get("probabilities"), dict) else raw
    probabilities = _validated_probabilities(source, "noul")
    for key in probabilities:
        if key not in ("true", "false"):
            _reject(f"noul probabilities 含未知键 {key!r}")
    missing = {"true", "false"} - set(probabilities)
    if missing:
        _reject(f"noul 缺少 {sorted(missing)} 概率")
    return {"type": "noul", "probabilities": probabilities}


def _numeric_level_keys(criteria: Dict[str, Any]) -> list:
    keys = []
    for key in criteria:
        try:
            keys.append((float(str(key)), str(key)))
        except ValueError:
            _reject(f"score 等级定义 {key!r} 不是数值等级，无法确定定义范围")
    keys.sort()
    return keys


def normalize_score(question: Dict[str, Any], raw: Any) -> Dict[str, Any]:
    criteria = question.get("criteria") or {}
    if not isinstance(raw, dict):
        _reject("score 答案不是 dict")
    _check_answer_type(raw, "score")
    levels = _numeric_level_keys(criteria)
    numeric_values = [value for value, _ in levels]
    level_keys = [key for _, key in levels]
    score = raw.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        _reject(f"score 不是数字: {score!r}")
    probabilities = _validated_probabilities(raw.get("probabilities"), "score")

    probability_keys = set(probabilities)
    if probability_keys and probability_keys <= set(level_keys):
        # 直接模式: 概率已使用内部等级 key (回放/旧形状), score 保持原空间
        converted = float(score)
    elif probability_keys and probability_keys <= {str(i) for i in range(len(level_keys))}:
        # 真实 API 模式: 概率与 score 使用 0 起 index 空间, 映射回内部等级 key 空间
        probabilities = {level_keys[int(key)]: value for key, value in probabilities.items()}
        if len(level_keys) > 1:
            converted = numeric_values[0] + float(score) * (numeric_values[-1] - numeric_values[0]) / (len(level_keys) - 1)
        else:
            converted = numeric_values[0]
    else:
        _reject("score probabilities 含未知等级")

    low, high = numeric_values[0], numeric_values[-1]
    if not (low <= converted <= high):
        _reject(f"score {score!r} 超出定义范围 [{low}, {high}]")
    out = {
        "type": "score",
        "score": converted,
        "probabilities": probabilities,
        "levels": {str(key): value for key, value in criteria.items()},
    }
    _optional_confidence(raw, out)
    return out


_NORMALIZERS = {"choice": normalize_choice, "noul": normalize_noul, "score": normalize_score}


def normalize_answers(questions: Dict[str, Any], raw_answers: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """按问题定义逐项标准化 Jev 原始 answers; 任一问题失败即整体拒绝 (fail-closed)。"""
    if not isinstance(raw_answers, dict) or not raw_answers:
        _reject("answers 缺少或不是非空 dict")
    unknown = [key for key in raw_answers if key not in questions]
    if unknown:
        _reject(f"answers 含未知问题 key: {unknown}")
    missing = [key for key in questions if key not in raw_answers]
    if missing:
        _reject(f"answers 缺少问题 key: {missing}")
    normalized: Dict[str, Dict[str, Any]] = {}
    for key, question in questions.items():
        qtype = question.get("type")
        normalizer = _NORMALIZERS.get(qtype)
        if normalizer is None:
            _reject(f"问题 {key!r} 类型未知: {qtype!r}")
        normalized[str(key)] = normalizer(question, raw_answers[key])
    return normalized
