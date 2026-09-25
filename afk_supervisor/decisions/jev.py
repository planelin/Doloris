"""afk_supervisor.decisions.jev — MindsHub Jev 决策 Provider
============================================================
通过 HTTP 结构化决策请求调用 MindsHub Jev API；本阶段仅用于 Shadow Mode
观察记录，绝不直接影响任务行为。

约束:
- 仅使用 Python 标准库 (urllib)，不新增第三方依赖;
- 所有配置来自环境变量或显式 JevConfig，Token 不得写入源码/日志/异常文本;
- 所有失败均返回带错误 status 的 DecisionResult，绝不把失败伪造成默认同意;
- Jev 失败永不阻塞原有 AGY 流程 (调用方负责 fail-open 继续原有路径)。
"""

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from afk_supervisor.decisions.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    ProviderInvalidResponseError,
    ProviderNetworkError,
    ProviderTimeoutError,
)
from afk_supervisor.decisions.models import DecisionRequest, DecisionResult
from afk_supervisor.decisions.normalize import normalize_answers
from afk_supervisor.decisions.redaction import redact_state, redact_text

DEFAULT_JEV_ENDPOINT = "https://api.mindshub.ai/v1/decisions"
DEFAULT_JEV_MODEL = "jev"
DEFAULT_JEV_TIMEOUT_SEC = 20.0
DEFAULT_DECISION_MODE = "off"

# 第一阶段只允许 off / shadow; active 属于未来阶段，显式 fail-closed。
SUPPORTED_DECISION_MODES = ("off", "shadow")

MAX_RESPONSE_BYTES = 1_048_576

ENV_JEV_ENDPOINT = "DOLORIS_JEV_ENDPOINT"
ENV_JEV_TOKEN = "DOLORIS_JEV_TOKEN"
ENV_JEV_MODEL = "DOLORIS_JEV_MODEL"
ENV_JEV_TIMEOUT_SEC = "DOLORIS_JEV_TIMEOUT_SEC"
ENV_DECISION_MODE = "DOLORIS_DECISION_MODE"

# Cloudflare WAF 会按客户端签名封禁默认的 Python-urllib UA (error 1010, HTTP 403),
# 必须显式携带产品 UA 才能到达 API。
_USER_AGENT = "Doloris/1.0 (+https://github.com/planelin/Doloris)"

# 线上 /v1/decisions 请求体只接受 model / state / questions (2026-09 实测;
# 多余字段返回 400 api_usage_error)。schema 与 request_id 仅存在于 Doloris 内部。
_RESPONSE_META_FIELDS = ("model",)


def resolve_decision_mode(env: Optional[Dict[str, str]] = None) -> str:
    """解析 DOLORIS_DECISION_MODE; 任何未支持值 (含 active) 一律按 off 处理 (fail-closed)。"""
    source = os.environ if env is None else env
    mode = (source.get(ENV_DECISION_MODE) or "").strip().lower()
    return mode if mode in SUPPORTED_DECISION_MODES else DEFAULT_DECISION_MODE


@dataclass
class JevConfig:
    """Jev Provider 配置; from_env 只读环境变量，绝不持久化 Token。"""

    endpoint: str = DEFAULT_JEV_ENDPOINT
    token: str = ""
    model: str = DEFAULT_JEV_MODEL
    timeout_sec: float = DEFAULT_JEV_TIMEOUT_SEC
    mode: str = DEFAULT_DECISION_MODE

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "JevConfig":
        source = os.environ if env is None else env
        timeout = DEFAULT_JEV_TIMEOUT_SEC
        raw_timeout = (source.get(ENV_JEV_TIMEOUT_SEC) or "").strip()
        if raw_timeout:
            try:
                parsed = float(raw_timeout)
                if parsed > 0:
                    timeout = parsed
            except ValueError:
                pass
        return cls(
            endpoint=(source.get(ENV_JEV_ENDPOINT) or "").strip() or DEFAULT_JEV_ENDPOINT,
            token=source.get(ENV_JEV_TOKEN) or "",
            model=(source.get(ENV_JEV_MODEL) or "").strip() or DEFAULT_JEV_MODEL,
            timeout_sec=timeout,
            mode=resolve_decision_mode(source),
        )


def _endpoint_is_allowed(endpoint: str) -> bool:
    lowered = endpoint.lower()
    if lowered.startswith("https://"):
        return True
    # 明文 http 仅允许本地回环，便于未来本地模拟桥调试
    for host in ("http://localhost", "http://127.0.0.1", "http://[::1]"):
        if lowered.startswith(host):
            return True
    return False


class JevProvider:
    """Jev HTTP 决策 Provider。

    urlopen 参数仅供测试注入 mock HTTP; 生产路径使用 urllib.request.urlopen。
    """

    provider_name = "jev"

    def __init__(self, config: Optional[JevConfig] = None, urlopen: Optional[Callable] = None):
        self.config = config or JevConfig.from_env()
        self._urlopen = urlopen or urllib.request.urlopen

    def decide(self, request: DecisionRequest) -> DecisionResult:
        started = time.monotonic()
        try:
            answers, raw_metadata = self._call(request)
        except ProviderError as error:
            return DecisionResult(
                provider=self.provider_name,
                model=self.config.model,
                request_id=request.request_id,
                answers={},
                latency_ms=self._elapsed_ms(started),
                status=error.result_status,
                error_code=error.error_code,
                raw_metadata={},
            )
        return DecisionResult(
            provider=self.provider_name,
            model=self.config.model,
            request_id=request.request_id,
            answers=answers,
            latency_ms=self._elapsed_ms(started),
            status="OK",
            raw_metadata=raw_metadata,
        )

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    def _call(self, request: DecisionRequest) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        self._check_config()
        payload = {
            "model": self.config.model,
            "state": redact_state(request.state, secrets=(self.config.token,)),
            "questions": self._wire_questions(request.questions),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        raw = self._http_post(body)
        response = self._parse_json(raw)
        return self._extract_answers(request, response)

    @staticmethod
    def _wire_questions(questions: Dict[str, Any]) -> Dict[str, Any]:
        """把 Doloris 内部问题定义转换为 MindsHub 线上格式。

        choice: criteria 保持 name→description 映射;
        score:  线上 criteria 必须是数组, 数组位置即分值 (0 起), 按内部数值 key 排序展开;
        noul:   仅 type + instructions。
        """
        wire = {}
        for key, question in questions.items():
            qtype = question.get("type")
            entry = {"type": qtype}
            if question.get("instructions"):
                entry["instructions"] = question["instructions"]
            if qtype == "choice":
                entry["criteria"] = dict(question.get("criteria") or {})
            elif qtype == "score":
                criteria = question.get("criteria") or {}
                ordered = sorted(criteria.items(), key=lambda kv: float(str(kv[0])))
                entry["criteria"] = [description for _, description in ordered]
            wire[str(key)] = entry
        return wire

    def _check_config(self) -> None:
        cfg = self.config
        if not isinstance(cfg.endpoint, str) or not cfg.endpoint.strip():
            raise ProviderConfigError("缺少 Jev endpoint 配置")
        if not _endpoint_is_allowed(cfg.endpoint):
            raise ProviderConfigError("Jev endpoint 必须是 https 或本地回环地址")
        if not cfg.token:
            raise ProviderConfigError("缺少 DOLORIS_JEV_TOKEN 配置")
        if not cfg.model:
            raise ProviderConfigError("缺少 Jev model 配置")
        if not isinstance(cfg.timeout_sec, (int, float)) or cfg.timeout_sec <= 0:
            raise ProviderConfigError("Jev timeout 必须是正数")

    def _http_post(self, body: bytes) -> bytes:
        request = urllib.request.Request(
            self.config.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.token}",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            with self._urlopen(request, timeout=self.config.timeout_sec) as response:
                return self._read_limited(response)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise ProviderAuthError(f"Jev API 鉴权失败 (HTTP {error.code})")
            raise ProviderHTTPError(f"Jev API 返回非 2xx 状态 (HTTP {error.code})")
        except (TimeoutError, socket.timeout):
            raise ProviderTimeoutError("Jev API 调用超时")
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise ProviderTimeoutError("Jev API 调用超时")
            raise ProviderNetworkError(f"Jev API 网络错误: {redact_text(str(reason or error))}")
        except OSError as error:
            raise ProviderNetworkError(f"Jev API 网络错误: {redact_text(str(error))}")

    def _read_limited(self, response: Any) -> bytes:
        chunks = []
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ProviderInvalidResponseError("Jev 响应超过大小限制")
            chunks.append(chunk)
        return b"".join(chunks)

    def _parse_json(self, raw: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            raise ProviderInvalidResponseError("Jev 响应不是合法 JSON")
        if not isinstance(payload, dict):
            raise ProviderInvalidResponseError("Jev 响应顶层必须是 JSON 对象")
        return payload

    def _extract_answers(
        self, request: DecisionRequest, response: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        answers = response.get("answers")
        if not isinstance(answers, dict) or not answers:
            raise ProviderInvalidResponseError("Jev 响应缺少有效 answers 结构")
        echoed = response.get("request_id")
        if isinstance(echoed, str) and echoed and echoed != request.request_id:
            raise ProviderInvalidResponseError("Jev 响应 request_id 与请求不匹配")
        normalized = normalize_answers(request.questions, answers)
        metadata = {
            key: response[key]
            for key in _RESPONSE_META_FIELDS
            if key in response and isinstance(response[key], (str, int, float, bool))
        }
        # 响应中的 model 是实际服务版本 (如 jev-1.13.0), 按管线要求保存为 model_revision
        if isinstance(metadata.get("model"), str):
            metadata["model_revision"] = metadata["model"]
        usage = response.get("usage")
        if isinstance(usage, dict):
            for field in ("input_tokens", "output_tokens"):
                value = usage.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    metadata[f"usage_{field}"] = value
        return normalized, metadata
