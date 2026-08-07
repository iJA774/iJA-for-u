"""跨边界可识别的错误类型。"""


class IJAError(Exception):
    """业务错误基类。"""

    code = "internal_error"

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details


class NotFoundError(IJAError):
    code = "not_found"


class ConflictError(IJAError):
    code = "conflict"


class InputValidationError(IJAError):
    code = "invalid_input"


class PortraitAspectRatioError(InputValidationError):
    """上传形象需要用户决定重新选择或自动裁剪。"""

    code = "portrait_aspect_ratio_mismatch"


class ProviderError(IJAError):
    code = "provider_error"


class ProviderAuthError(ProviderError):
    code = "provider_auth_error"


class ProviderRequestError(ProviderError):
    """上游明确拒绝请求，修改端点、模型 ID 或请求参数前不应重试。"""

    code = "provider_request_error"


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limit"


class ProviderQuotaExceededError(ProviderError):
    """Provider 套餐或账户额度耗尽，重试不会在当前额度周期内恢复。"""

    code = "provider_quota_exceeded"


class ModelHardTimeoutError(ProviderError):
    """任务级 hard timeout；与 HTTP 客户端内部超时分开观测。"""

    code = "model_hard_timeout"


class InvalidModelResponseError(ProviderError):
    code = "invalid_model_response"


class DeliveryError(IJAError):
    code = "delivery_failed"


class ToolExecutionError(IJAError):
    code = "tool_execution_failed"


class ToolLimitError(ToolExecutionError):
    code = "tool_limit_exceeded"
