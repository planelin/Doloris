"""afk_supervisor.decisions.errors — 决策层错误类型
==================================================
决策层拥有独立的错误类型体系，与 AGY L2 的 verdict 空间保持类型隔离。
ProviderError 携带稳定 error_code 供结构化判定；消息文本严禁包含 Token 等密钥。
"""


class DecisionError(Exception):
    """决策层错误基类。"""

    error_code = "decision_error"


class InvalidDecisionRequestError(DecisionError, ValueError):
    """DecisionRequest 结构不合法（schema 错误、request_id 缺失、问题结构无效等）。"""

    error_code = "invalid_decision_request"


class InvalidDecisionResultError(DecisionError, ValueError):
    """DecisionResult 结构不合法（status 越界、provider/model 缺失等）。"""

    error_code = "invalid_decision_result"


class ProviderError(DecisionError):
    """决策 Provider 调用失败的结构化错误基类。

    error_code 必须稳定可判别（如 decision_api_timeout），
    异常文本仅描述失败类别，不得包含密钥或完整请求内容。
    result_status 是该错误映射到 DecisionResult.status 的固定类别；
    未知 Provider 失败按 INVALID_RESPONSE 处理（fail-closed）。
    """

    error_code = "provider_error"
    result_status = "INVALID_RESPONSE"

    def __init__(self, message: str = "", error_code: str = ""):
        super().__init__(message)
        if error_code:
            self.error_code = error_code


class ProviderTimeoutError(ProviderError):
    error_code = "decision_api_timeout"
    result_status = "TIMEOUT"


class ProviderAuthError(ProviderError):
    error_code = "decision_api_auth"
    result_status = "AUTH_ERROR"


class ProviderHTTPError(ProviderError):
    error_code = "decision_api_http_error"
    result_status = "HTTP_ERROR"


class ProviderNetworkError(ProviderError):
    error_code = "decision_api_network"
    result_status = "NETWORK_ERROR"


class ProviderInvalidResponseError(ProviderError):
    error_code = "decision_api_invalid_response"
    result_status = "INVALID_RESPONSE"


class ProviderConfigError(ProviderError):
    error_code = "decision_provider_config"
    result_status = "CONFIG_ERROR"
