"""OpenAI-compatible Embeddings Provider。"""

from __future__ import annotations

import math

import httpx

from config import EmbeddingSettings
from domain.errors import InvalidModelResponseError, ProviderAuthError, ProviderError, ProviderRateLimitError
from ports import EmbeddingVectors, ModelUsage


class OpenAICompatibleEmbeddingProvider:
    """发送已授权的长期记忆或表达学习文本，支持批量 embedding。"""

    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + "/",
            timeout=settings.timeout_seconds,
            headers={"Authorization": f"Bearer {settings.api_key}"},
        )

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> EmbeddingVectors:
        if not texts:
            return EmbeddingVectors([])
        response = await self.client.post(
            "embeddings",
            json={"model": model or self.settings.name, "input": texts},
        )
        if response.status_code in {401, 403}:
            raise ProviderAuthError("Embedding Provider 身份认证失败")
        if response.status_code == 429:
            raise ProviderRateLimitError("Embedding Provider 触发限流")
        if response.is_error:
            raise ProviderError(f"Embedding Provider 返回 HTTP {response.status_code}")
        try:
            body = response.json()
            data = body["data"]
            ordered = sorted(data, key=lambda item: int(item["index"]))
            vectors = [[float(value) for value in item["embedding"]] for item in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidModelResponseError("Embedding Provider 返回格式无效") from exc
        if len(vectors) != len(texts) or not vectors or len({len(item) for item in vectors}) != 1:
            raise InvalidModelResponseError("Embedding Provider 返回的向量数量或维度不一致")
        if any(not math.isfinite(value) for vector in vectors for value in vector):
            raise InvalidModelResponseError("Embedding Provider 返回了非有限向量值")
        usage: ModelUsage | None = None
        raw_usage = body.get("usage")
        if isinstance(raw_usage, dict):
            input_tokens = raw_usage.get("prompt_tokens")
            total_tokens = raw_usage.get("total_tokens")
            if not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or input_tokens < 0:
                input_tokens = None
            if not isinstance(total_tokens, int) or isinstance(total_tokens, bool) or total_tokens < 0:
                total_tokens = None
            if input_tokens is not None or total_tokens is not None:
                usage = ModelUsage(
                    input_tokens=input_tokens,
                    output_tokens=0,
                    total_tokens=total_tokens,
                )
        return EmbeddingVectors(vectors, usage=usage)

    async def close(self) -> None:
        await self.client.aclose()
