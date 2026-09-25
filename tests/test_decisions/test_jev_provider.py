"""P2 JevProvider 测试: 全部使用 mock HTTP，绝不依赖真实网络或真实 Token。

覆盖: endpoint/token/model/timeout 配置、JSON POST、2xx、非 2xx、超时、
网络错误、非 JSON、缺少 answers、request_id 回显不匹配、配置错误、Token 脱敏。
"""

import json
import unittest
import urllib.error
from unittest.mock import patch

from afk_supervisor.decisions import jev as jev_module
from afk_supervisor.decisions.errors import ProviderError
from afk_supervisor.decisions.jev import JevConfig, JevProvider, resolve_decision_mode
from afk_supervisor.decisions.models import DecisionRequest

TEST_TOKEN = "mock-token-not-a-real-secret"


class FakeResponse:
    def __init__(self, body=b"", status=200):
        self.status = status
        self._body = body

    def read(self, size=-1):
        if size is None or size < 0:
            return self._body
        out, self._body = self._body[:size], self._body[size:]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_urlopen(response=None, error=None):
    calls = []

    def urlopen(request, timeout=None):
        calls.append({"request": request, "timeout": timeout})
        if error is not None:
            raise error
        return response

    urlopen.calls = calls
    return urlopen


def make_request(**overrides):
    base = dict(
        request_id="req-jev-0001",
        state={"worker_alive": True, "last_message_summary": "Worker 在两个主题间等待选择"},
        questions={
            "route": {
                "type": "choice",
                "instructions": "选择下一步路由",
                "criteria": {"consult_agy": "交给 AGY", "auto_answer": "直接代答"},
            },
        },
        metadata={"interaction_type": "finite_choice"},
    )
    base.update(overrides)
    return DecisionRequest(**base)


def ok_body(answers=None, **extra):
    if answers is None:
        answers = {
            "route": {
                "choice": "consult_agy",
                "probabilities": {"consult_agy": 0.8, "auto_answer": 0.2},
                "top1": "consult_agy",
                "margin": 0.6,
            },
        }
    payload = {"answers": answers}
    payload.update(extra)
    return json.dumps(payload).encode("utf-8")


def make_provider(urlopen, **config_overrides):
    config_kwargs = dict(
        endpoint="https://api.mindshub.ai/v1/decisions",
        token=TEST_TOKEN,
        model="jev",
        timeout_sec=20.0,
    )
    config_kwargs.update(config_overrides)
    config = JevConfig(**config_kwargs)
    return JevProvider(config=config, urlopen=urlopen), config


class JevProviderOkPathTests(unittest.TestCase):
    def test_ok_result_normalizes_answers_and_keeps_request_id(self):
        urlopen = make_urlopen(FakeResponse(ok_body()))
        provider, _ = make_provider(urlopen)
        result = provider.decide(make_request())
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.provider, "jev")
        self.assertEqual(result.model, "jev")
        self.assertEqual(result.request_id, "req-jev-0001")
        route = result.answers["route"]
        self.assertEqual(route["type"], "choice")
        self.assertEqual(route["choice"], "consult_agy")
        self.assertEqual(route["top1"], "consult_agy")
        self.assertEqual(route["top2"], "auto_answer")
        self.assertEqual(route["margin"], 0.6)
        self.assertGreaterEqual(result.latency_ms, 0)
        self.assertEqual(result.error_code, "")

    def test_request_is_json_post_with_bearer_token(self):
        urlopen = make_urlopen(FakeResponse(ok_body()))
        provider, config = make_provider(urlopen)
        provider.decide(make_request())
        self.assertEqual(len(urlopen.calls), 1)
        call = urlopen.calls[0]
        request = call["request"]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.headers.get("Content-type"), "application/json")
        self.assertEqual(request.headers.get("Authorization"), f"Bearer {TEST_TOKEN}")
        self.assertEqual(call["timeout"], config.timeout_sec)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "jev")
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertIn("questions", body)
        self.assertIn("state", body)

    def test_response_metadata_captured_into_raw_metadata(self):
        urlopen = make_urlopen(FakeResponse(ok_body(model="jev-1.13.0",
                                                    usage={"input_tokens": 455, "output_tokens": 70})))
        provider, _ = make_provider(urlopen)
        result = provider.decide(make_request())
        self.assertEqual(result.raw_metadata.get("model"), "jev-1.13.0")
        self.assertEqual(result.raw_metadata.get("model_revision"), "jev-1.13.0")
        self.assertEqual(result.raw_metadata.get("usage_input_tokens"), 455)
        self.assertEqual(result.raw_metadata.get("usage_output_tokens"), 70)


class JevProviderFailureTests(unittest.TestCase):
    def assertFailure(self, result, status, error_code):
        self.assertEqual(result.status, status)
        self.assertEqual(result.error_code, error_code)
        self.assertEqual(result.answers, {})
        self.assertGreaterEqual(result.latency_ms, 0)

    def test_http_401_maps_to_auth_error(self):
        urlopen = make_urlopen(error=urllib.error.HTTPError(
            "https://api", 401, "Unauthorized", {}, b"unauthorized"))
        provider, _ = make_provider(urlopen)
        self.assertFailure(provider.decide(make_request()), "AUTH_ERROR", "decision_api_auth")

    def test_http_403_maps_to_auth_error(self):
        urlopen = make_urlopen(error=urllib.error.HTTPError(
            "https://api", 403, "Forbidden", {}, b"forbidden"))
        provider, _ = make_provider(urlopen)
        self.assertFailure(provider.decide(make_request()), "AUTH_ERROR", "decision_api_auth")

    def test_http_500_maps_to_http_error(self):
        urlopen = make_urlopen(error=urllib.error.HTTPError(
            "https://api", 500, "Server Error", {}, b"boom"))
        provider, _ = make_provider(urlopen)
        self.assertFailure(provider.decide(make_request()), "HTTP_ERROR", "decision_api_http_error")

    def test_socket_timeout_maps_to_timeout(self):
        urlopen = make_urlopen(error=TimeoutError("timed out"))
        provider, _ = make_provider(urlopen)
        self.assertFailure(provider.decide(make_request()), "TIMEOUT", "decision_api_timeout")

    def test_url_error_with_timeout_reason_maps_to_timeout(self):
        urlopen = make_urlopen(error=urllib.error.URLError(TimeoutError("timed out")))
        provider, _ = make_provider(urlopen)
        self.assertFailure(provider.decide(make_request()), "TIMEOUT", "decision_api_timeout")

    def test_url_error_maps_to_network_error(self):
        urlopen = make_urlopen(error=urllib.error.URLError(OSError("getaddrinfo failed")))
        provider, _ = make_provider(urlopen)
        self.assertFailure(provider.decide(make_request()), "NETWORK_ERROR", "decision_api_network")

    def test_os_error_maps_to_network_error(self):
        urlopen = make_urlopen(error=OSError("connection refused"))
        provider, _ = make_provider(urlopen)
        self.assertFailure(provider.decide(make_request()), "NETWORK_ERROR", "decision_api_network")

    def test_non_json_body_maps_to_invalid_response(self):
        urlopen = make_urlopen(FakeResponse(b"<html>not json</html>"))
        provider, _ = make_provider(urlopen)
        self.assertFailure(provider.decide(make_request()), "INVALID_RESPONSE", "decision_api_invalid_response")

    def test_missing_answers_maps_to_invalid_response(self):
        urlopen = make_urlopen(FakeResponse(json.dumps({"model": "jev"}).encode("utf-8")))
        provider, _ = make_provider(urlopen)
        self.assertFailure(provider.decide(make_request()), "INVALID_RESPONSE", "decision_api_invalid_response")

    def test_answers_not_dict_maps_to_invalid_response(self):
        urlopen = make_urlopen(FakeResponse(ok_body(answers=["route"])))
        provider, _ = make_provider(urlopen)
        self.assertFailure(provider.decide(make_request()), "INVALID_RESPONSE", "decision_api_invalid_response")

    def test_request_id_echo_mismatch_maps_to_invalid_response(self):
        urlopen = make_urlopen(FakeResponse(ok_body(request_id="req-other")))
        provider, _ = make_provider(urlopen)
        self.assertFailure(provider.decide(make_request()), "INVALID_RESPONSE", "decision_api_invalid_response")

    def test_oversized_response_maps_to_invalid_response(self):
        urlopen = make_urlopen(FakeResponse(b"x" * 4096))
        provider, _ = make_provider(urlopen)
        with patch.object(jev_module, "MAX_RESPONSE_BYTES", 1024):
            self.assertFailure(
                provider.decide(make_request()), "INVALID_RESPONSE", "decision_api_invalid_response")

    def test_non_dict_top_level_response_maps_to_invalid_response(self):
        urlopen = make_urlopen(FakeResponse(b"[1, 2, 3]"))
        provider, _ = make_provider(urlopen)
        self.assertFailure(provider.decide(make_request()), "INVALID_RESPONSE", "decision_api_invalid_response")


class JevProviderConfigTests(unittest.TestCase):
    def test_missing_token_returns_config_error_without_network_call(self):
        urlopen = make_urlopen(FakeResponse(ok_body()))
        provider, _ = make_provider(urlopen, token="")
        result = provider.decide(make_request())
        self.assertEqual(result.status, "CONFIG_ERROR")
        self.assertEqual(result.error_code, "decision_provider_config")
        self.assertEqual(len(urlopen.calls), 0)

    def test_disallowed_endpoint_scheme_returns_config_error(self):
        urlopen = make_urlopen(FakeResponse(ok_body()))
        provider, _ = make_provider(urlopen, endpoint="ftp://example.com/decisions")
        result = provider.decide(make_request())
        self.assertEqual(result.status, "CONFIG_ERROR")
        self.assertEqual(len(urlopen.calls), 0)

    def test_http_endpoint_allowed_only_for_loopback(self):
        urlopen = make_urlopen(FakeResponse(ok_body()))
        provider, _ = make_provider(urlopen, endpoint="http://127.0.0.1:8099/decisions")
        self.assertEqual(provider.decide(make_request()).status, "OK")
        provider2, _ = make_provider(urlopen, endpoint="http://evil.example.com/decisions")
        self.assertEqual(provider2.decide(make_request()).status, "CONFIG_ERROR")

    def test_non_positive_timeout_returns_config_error(self):
        urlopen = make_urlopen(FakeResponse(ok_body()))
        provider, _ = make_provider(urlopen, timeout_sec=0)
        self.assertEqual(provider.decide(make_request()).status, "CONFIG_ERROR")


class JevProviderTokenRedactionTests(unittest.TestCase):
    TOKEN = "super-secret-mock-token-value"

    def make_secrets_provider(self, urlopen):
        config = JevConfig(endpoint="https://api.mindshub.ai/v1/decisions",
                           token=self.TOKEN, model="jev", timeout_sec=5.0)
        return JevProvider(config=config, urlopen=urlopen)

    def test_token_never_in_payload_state(self):
        urlopen = make_urlopen(FakeResponse(ok_body()))
        provider = self.make_secrets_provider(urlopen)
        state = {"api_key": "sk-abc123", "note": f"携带 {self.TOKEN} 的文本",
                 "nested": {"password": "hunter2", "deep": {"cookie": "a=b"}}}
        provider.decide(make_request(state=state))
        body = json.loads(urlopen.calls[0]["request"].data.decode("utf-8"))
        flat = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("sk-abc123", flat)
        self.assertNotIn("hunter2", flat)
        self.assertNotIn("a=b", flat)
        self.assertNotIn(self.TOKEN, flat)
        self.assertEqual(body["state"]["api_key"], "[REDACTED]")
        self.assertEqual(body["state"]["note"], "携带 [REDACTED] 的文本")

    def test_token_never_in_any_error_status_or_code(self):
        scenarios = [
            make_urlopen(error=urllib.error.HTTPError("https://api", 500, "err", {}, b"body")),
            make_urlopen(error=TimeoutError("t")),
            make_urlopen(error=urllib.error.URLError(OSError("net"))),
            make_urlopen(FakeResponse(b"<not-json " + self.TOKEN.encode() + b">")),
        ]
        for urlopen in scenarios:
            with self.subTest(urlen=urlopen):
                provider = self.make_secrets_provider(urlopen)
                result = provider.decide(make_request(state={"blob": self.TOKEN}))
                rendered = f"{result.status}|{result.error_code}|{result.answers}|{result.raw_metadata}"
                self.assertNotIn(self.TOKEN, rendered)
                self.assertNotIn(self.TOKEN, str(result))

    def test_error_message_from_network_reason_is_redacted(self):
        urlopen = make_urlopen(error=urllib.error.URLError(OSError(f"connect failed with {self.TOKEN}")))
        provider = self.make_secrets_provider(urlopen)
        result = provider.decide(make_request())
        self.assertEqual(result.status, "NETWORK_ERROR")
        self.assertNotIn(self.TOKEN, str(result))


class JevConfigEnvTests(unittest.TestCase):
    def test_defaults_when_env_empty(self):
        config = JevConfig.from_env(env={})
        self.assertEqual(config.endpoint, "https://api.mindshub.ai/v1/decisions")
        self.assertEqual(config.model, "jev")
        self.assertEqual(config.timeout_sec, 20.0)
        self.assertEqual(config.token, "")
        self.assertEqual(config.mode, "off")

    def test_env_values_are_read(self):
        config = JevConfig.from_env(env={
            "DOLORIS_JEV_ENDPOINT": "https://jev.example/v2/decisions",
            "DOLORIS_JEV_TOKEN": "tok",
            "DOLORIS_JEV_MODEL": "jev-mini",
            "DOLORIS_JEV_TIMEOUT_SEC": "7.5",
            "DOLORIS_DECISION_MODE": "shadow",
        })
        self.assertEqual(config.endpoint, "https://jev.example/v2/decisions")
        self.assertEqual(config.token, "tok")
        self.assertEqual(config.model, "jev-mini")
        self.assertEqual(config.timeout_sec, 7.5)
        self.assertEqual(config.mode, "shadow")

    def test_invalid_timeout_falls_back_to_default(self):
        config = JevConfig.from_env(env={"DOLORIS_JEV_TIMEOUT_SEC": "not-a-number"})
        self.assertEqual(config.timeout_sec, 20.0)
        config2 = JevConfig.from_env(env={"DOLORIS_JEV_TIMEOUT_SEC": "-3"})
        self.assertEqual(config2.timeout_sec, 20.0)

    def test_resolve_decision_mode_is_fail_closed(self):
        self.assertEqual(resolve_decision_mode({}), "off")
        self.assertEqual(resolve_decision_mode({"DOLORIS_DECISION_MODE": "shadow"}), "shadow")
        self.assertEqual(resolve_decision_mode({"DOLORIS_DECISION_MODE": "SHADOW"}), "shadow")
        self.assertEqual(resolve_decision_mode({"DOLORIS_DECISION_MODE": "active"}), "off")
        self.assertEqual(resolve_decision_mode({"DOLORIS_DECISION_MODE": "garbage"}), "off")


class JevWireFormatTests(unittest.TestCase):
    """线上契约 (2026-09 实测 docs.mindshub.ai): 请求体只含 model/state/questions;
    score 的 criteria 是数组 (位置即分值); 必须携带产品 User-Agent, 否则被
    Cloudflare WAF 以 error 1010 (HTTP 403) 拦截。"""

    def make_score_request(self):
        return make_request(questions={
            "risk": {"type": "score", "instructions": "评估风险",
                     "criteria": {"2": "高", "1": "低", "3": "极高"}},
        })

    def test_wire_payload_contains_only_model_state_questions(self):
        urlopen = make_urlopen(FakeResponse(ok_body()))
        provider, _ = make_provider(urlopen)
        provider.decide(make_request(metadata={"interaction_type": "finite_choice"}))
        body = json.loads(urlopen.calls[0]["request"].data.decode("utf-8"))
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertNotIn("schema", body)
        self.assertNotIn("request_id", body)
        self.assertNotIn("metadata", body)

    def test_score_criteria_sent_as_array_in_numeric_order(self):
        urlopen = make_urlopen(FakeResponse(ok_body()))
        provider, _ = make_provider(urlopen)
        provider.decide(self.make_score_request())
        body = json.loads(urlopen.calls[0]["request"].data.decode("utf-8"))
        self.assertEqual(body["questions"]["risk"]["criteria"], ["低", "高", "极高"])

    def test_choice_criteria_sent_as_name_description_map(self):
        urlopen = make_urlopen(FakeResponse(ok_body()))
        provider, _ = make_provider(urlopen)
        provider.decide(make_request())
        body = json.loads(urlopen.calls[0]["request"].data.decode("utf-8"))
        self.assertEqual(body["questions"]["route"]["criteria"],
                         {"consult_agy": "交给 AGY", "auto_answer": "直接代答"})

    def test_user_agent_header_is_present(self):
        urlopen = make_urlopen(FakeResponse(ok_body()))
        provider, _ = make_provider(urlopen)
        provider.decide(make_request())
        headers = {k.lower(): v for k, v in urlopen.calls[0]["request"].headers.items()}
        self.assertIn("doloris", headers.get("user-agent", "").lower())


class ProviderErrorTaxonomyTests(unittest.TestCase):
    def test_every_provider_error_maps_to_a_valid_status(self):
        from afk_supervisor.decisions.models import RESULT_STATUSES

        for cls in (ProviderError, jev_module.ProviderTimeoutError, jev_module.ProviderAuthError,
                    jev_module.ProviderHTTPError, jev_module.ProviderNetworkError,
                    jev_module.ProviderInvalidResponseError, jev_module.ProviderConfigError):
            with self.subTest(cls=cls.__name__):
                self.assertIn(cls.result_status, RESULT_STATUSES)
                self.assertTrue(issubclass(cls, ProviderError))


if __name__ == "__main__":
    unittest.main()
