"""上游 usage 必须按协议读取，缺失时保持 unknown。"""

from __future__ import annotations

import json
from typing import Literal

import httpx
import pytest

from adapters.model import (
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleProvider,
)
from config import EmbeddingSettings, ModelSettings
from ports import ModelMessage, ModelRequest


def _request(model: str = "usage-model") -> ModelRequest:
    return ModelRequest(
        messages=[ModelMessage(role="user", content="测试 usage")],
        model=model,
        temperature=0,
        max_tokens=32,
    )


async def _provider(
    settings: ModelSettings,
    body: dict,
) -> OpenAICompatibleProvider:
    provider = OpenAICompatibleProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=body)
        ),
    )
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protocol", "body", "expected"),
    [
        (
            "openai_chat",
            {
                "choices": [
                    {
                        "message": {"content": "chat"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            },
            (11, 7, 18),
        ),
        (
            "openai_responses",
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "responses"}
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 13,
                    "output_tokens": 5,
                    "total_tokens": 18,
                },
            },
            (13, 5, 18),
        ),
        (
            "anthropic_messages",
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "anthropic"}],
                "usage": {"input_tokens": 17, "output_tokens": 3},
            },
            (17, 3, 20),
        ),
    ],
)
async def test_text_protocols_parse_provider_usage(
    protocol: Literal[
        "openai_chat",
        "openai_responses",
        "anthropic_messages",
    ],
    body: dict,
    expected: tuple[int, int, int],
) -> None:
    settings = ModelSettings(
        mode="openai",
        protocol=protocol,
        base_url="https://usage.example/v1",
        api_key="test-only",
        name="usage-model",
    )
    provider = await _provider(settings, body)
    try:
        usage = (await provider.complete(_request())).usage
        assert usage is not None
        assert (
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
        ) == expected
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_tool_call_keeps_usage_and_missing_usage_stays_none() -> None:
    settings = ModelSettings(
        mode="openai",
        base_url="https://usage.example/v1",
        api_key="test-only",
        name="usage-model",
    )
    provider = await _provider(
        settings,
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "lookup",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 9,
                "completion_tokens": 2,
                "total_tokens": 11,
            },
        },
    )
    try:
        result = await provider.complete(_request())
        assert result.tool_calls[0].name == "lookup"
        assert result.usage is not None
        assert result.usage.total_tokens == 11
    finally:
        await provider.close()

    without_usage = await _provider(
        settings,
        {"choices": [{"message": {"content": "没有 usage"}}]},
    )
    try:
        assert (await without_usage.complete(_request())).usage is None
    finally:
        await without_usage.close()


@pytest.mark.asyncio
async def test_streaming_terminal_usage_is_exposed_without_fake_estimate() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            content=(
                'data: {"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":6,'
                '"completion_tokens":1,"total_tokens":7}}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
            headers={"content-type": "text/event-stream"},
        )

    settings = ModelSettings(
        mode="openai",
        base_url="https://usage.example/v1",
        api_key="test-only",
        name="usage-model",
        supports_streaming=True,
    )
    provider = OpenAICompatibleProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        events = [event async for event in provider.stream(_request())]
        usage_events = [event for event in events if event.usage is not None]
        assert usage_events[-1].usage is not None
        assert usage_events[-1].usage.total_tokens == 7
        payload = json.loads(seen[0].content)
        assert payload["stream_options"] == {"include_usage": True}
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_embedding_usage_is_attached_to_list_compatible_result() -> None:
    settings = EmbeddingSettings(
        enabled=True,
        base_url="https://usage.example/v1",
        api_key="test-only",
        name="embedding-model",
    )
    provider = OpenAICompatibleEmbeddingProvider(settings)
    await provider.client.aclose()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [1.0, 0.0]}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
        )

    provider.client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        vectors = await provider.embed(
            ["向量文本"],
            model="embedding-fallback",
        )
        assert vectors == [[1.0, 0.0]]
        assert vectors.usage is not None
        assert vectors.usage.input_tokens == 4
        assert vectors.usage.output_tokens == 0
        assert json.loads(requests[0].content)["model"] == "embedding-fallback"
    finally:
        await provider.close()
