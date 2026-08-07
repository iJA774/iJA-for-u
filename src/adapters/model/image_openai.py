"""OpenAI-compatible Images Edits 图片模型适配器。"""

from __future__ import annotations

import base64
import binascii
from typing import Any

import httpx

from config import ImageModelSettings
from domain.errors import (
    InputValidationError,
    InvalidModelResponseError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
)
from imaging import inspect_dimensions
from ports import ImageGenerationRequest, ImageGenerationResult, ModelUsage


class OpenAICompatibleImageProvider:
    """只实现单图 multipart edit 与 base64 返回的安全公共子集。"""

    MAX_RESPONSE_BYTES = 20 * 1024 * 1024

    def __init__(self, settings: ImageModelSettings) -> None:
        if not settings.enabled:
            raise ValueError("不能为已禁用配置创建图片模型 Provider")
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.timeout_seconds),
            headers={"Authorization": f"Bearer {settings.api_key}"},
        )

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        """执行一次付费图片编辑；失败时不自动重试。"""

        try:
            response = await self.client.post(
                "/images/edits",
                data={
                    "model": request.model or self.settings.name,
                    "prompt": request.prompt,
                    "n": "1",
                    "size": "1024x1536",
                    "output_format": "png",
                },
                files={"image": (request.filename, request.base_image, "image/png")},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(f"图片模型请求失败: {exc}") from exc
        if response.status_code in {401, 403}:
            raise ProviderAuthError("图片模型服务鉴权失败")
        if response.status_code == 429:
            raise ProviderRateLimitError("图片模型服务触发限流")
        if response.status_code >= 500:
            raise ProviderError(f"图片模型服务暂时不可用，HTTP {response.status_code}")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"图片模型请求被拒绝，HTTP {response.status_code}") from exc
        if len(response.content) > self.MAX_RESPONSE_BYTES:
            raise InvalidModelResponseError("图片模型响应超过大小上限")
        try:
            body: dict[str, Any] = response.json()
            encoded = body["data"][0]["b64_json"]
            if not isinstance(encoded, str) or not encoded:
                raise TypeError("b64_json 不是非空字符串")
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, KeyError, IndexError, TypeError, binascii.Error) as exc:
            raise InvalidModelResponseError(
                f"图片模型响应不符合 Images Edits 契约: {exc}"
            ) from exc
        if not content:
            raise InvalidModelResponseError("图片模型返回了空图片")
        try:
            inspect_dimensions(content)
        except InputValidationError as exc:
            raise InvalidModelResponseError("图片模型返回的内容不是安全、可解码的图片") from exc
        usage: ModelUsage | None = None
        raw_usage = body.get("usage")
        if isinstance(raw_usage, dict):
            values: dict[str, int | None] = {}
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                value = raw_usage.get(field)
                values[field] = (
                    value
                    if isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                    else None
                )
            if any(value is not None for value in values.values()):
                usage = ModelUsage(**values)
        return ImageGenerationResult(content=content, usage=usage)

    async def close(self) -> None:
        await self.client.aclose()
