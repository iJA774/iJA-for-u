import json

import httpx
import pytest

from adapters.model import OpenAICompatibleEmbeddingProvider, OpenAICompatibleProvider
from config.settings import EmbeddingSettings, ModelSettings
from domain.errors import (
    ProviderAuthError,
    ProviderQuotaExceededError,
    ProviderRateLimitError,
    ProviderRequestError,
)
from ports import ModelImage, ModelMessage, ModelRequest, ModelToolCall, ToolDefinition


def request(*, json_mode: bool = False) -> ModelRequest:
    return ModelRequest(
        messages=[ModelMessage(role="user", content="你好")],
        model="compatible-model",
        temperature=0.2,
        max_tokens=64,
        json_mode=json_mode,
    )


@pytest.mark.asyncio
async def test_chat_completions_common_wire_contract_and_limited_retry() -> None:
    calls: list[httpx.Request] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "limited"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "兼容回复"}}]})

    settings = ModelSettings(
        mode="openai",
        base_url="https://model.example/v1",
        api_key="secret-for-test-only",
        name="compatible-model",
    )
    provider = OpenAICompatibleProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handler),
    )
    assert (await provider.complete(request(json_mode=True))).content == "兼容回复"
    assert len(calls) == 2
    assert calls[1].url.path == "/v1/chat/completions"
    payload = json.loads(calls[1].content)
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["messages"] == [{"role": "user", "content": "你好"}]
    await provider.close()


def test_rate_limit_error_preserves_numeric_retry_after() -> None:
    response = httpx.Response(429, headers={"Retry-After": "1.25"})

    with pytest.raises(ProviderRateLimitError) as caught:
        OpenAICompatibleProvider._check_response_status(response)

    assert caught.value.details == {"retry_after_seconds": 1.25}


@pytest.mark.asyncio
async def test_account_quota_exceeded_fails_without_retry() -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            429,
            json={
                "error": {
                    "code": "AccountQuotaExceeded",
                    "type": "TooManyRequests",
                    "message": "monthly quota exceeded",
                }
            },
        )

    settings = ModelSettings(
        mode="openai",
        base_url="https://model.example/v1",
        api_key="secret-for-test-only",
        name="compatible-model",
    )
    provider = OpenAICompatibleProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderQuotaExceededError) as caught:
        await provider.complete(request())

    assert caught.value.details == {
        "provider_error_code": "AccountQuotaExceeded"
    }
    assert call_count == 1
    await provider.close()


@pytest.mark.asyncio
async def test_authentication_error_fails_immediately() -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(401, json={"error": {"message": "unauthorized"}})

    settings = ModelSettings(
        mode="openai",
        base_url="https://model.example/v1",
        api_key="secret-for-test-only",
        name="compatible-model",
    )
    provider = OpenAICompatibleProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderAuthError):
        await provider.complete(request())
    assert call_count == 1
    await provider.close()


@pytest.mark.asyncio
async def test_provider_request_error_preserves_404_details_without_retry() -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": "ModelNotFound",
                    "message": "The specified model does not exist",
                },
                "request_id": "req-test-404",
            },
        )

    settings = ModelSettings(
        mode="openai",
        base_url="https://model.example/v1",
        api_key="secret-for-test-only",
        name="wrong-display-name",
    )
    provider = OpenAICompatibleProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderRequestError) as caught:
        await provider.complete(request())

    assert call_count == 1
    assert "HTTP 404" in str(caught.value)
    assert caught.value.details == {
        "http_status": 404,
        "provider_error_code": "ModelNotFound",
        "provider_message": "The specified model does not exist",
        "provider_request_id": "req-test-404",
    }
    await provider.close()


@pytest.mark.asyncio
async def test_streaming_chat_completions_forwards_sse_text_deltas() -> None:
    calls: list[httpx.Request] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        return httpx.Response(
            200,
            content=(
                'data: {"choices":[{"delta":{"content":"你"},"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
            headers={"content-type": "text/event-stream"},
        )

    settings = ModelSettings(
        mode="openai",
        base_url="https://model.example/v1",
        api_key="secret-for-test-only",
        name="compatible-model",
        supports_streaming=True,
    )
    provider = OpenAICompatibleProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handler),
    )
    events = [event async for event in provider.stream(request())]

    assert "".join(event.delta for event in events) == "你好"
    assert events[-1].finish_reason == "stop"
    assert json.loads(calls[0].content)["stream"] is True
    await provider.close()


@pytest.mark.asyncio
async def test_embeddings_uses_batch_input_and_preserves_response_order() -> None:
    seen: list[httpx.Request] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        seen.append(incoming)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    settings = EmbeddingSettings(
        enabled=True,
        base_url="https://embedding.example/v1",
        api_key="secret-for-test-only",
        name="embedding-model",
    )
    provider = OpenAICompatibleEmbeddingProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handler),
    )

    assert await provider.embed(["first", "second"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert seen[0].url.path == "/v1/embeddings"
    assert json.loads(seen[0].content) == {"model": "embedding-model", "input": ["first", "second"]}
    await provider.close()


def test_vision_message_uses_chat_completions_image_url_parts() -> None:
    message = ModelMessage(
        role="user",
        content="看看这张图",
        images=[
            ModelImage(
                mime_type="image/png",
                base64_data="aW1hZ2U=",
                detail="auto",
            )
        ],
    )

    assert OpenAICompatibleProvider._message_payload(message) == {
        "role": "user",
        "content": [
            {"type": "text", "text": "看看这张图"},
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64,aW1hZ2U=",
                    "detail": "auto",
                },
            },
        ],
    }


@pytest.mark.asyncio
async def test_native_tool_calls_and_tool_result_wire_shape() -> None:
    calls: list[httpx.Request] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_time",
                                    "type": "function",
                                    "function": {
                                        "name": "get_current_time",
                                        "arguments": '{"timezone":"Asia/Shanghai"}',
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    settings = ModelSettings(
        mode="openai",
        base_url="https://model.example/v1",
        api_key="secret-for-test-only",
        name="compatible-model",
        supports_tools=True,
    )
    provider = OpenAICompatibleProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handler),
    )
    result = await provider.complete(
        ModelRequest(
            messages=[
                ModelMessage(role="user", content="现在几点"),
                ModelMessage(
                    role="assistant",
                    tool_calls=[
                        ModelToolCall(
                            id="old_call",
                            name="get_current_time",
                            arguments='{"timezone":"UTC"}',
                        )
                    ],
                ),
                ModelMessage(role="tool", content='{"ok":true}', tool_call_id="old_call"),
            ],
            model="compatible-model",
            temperature=0,
            max_tokens=64,
            tools=[
                ToolDefinition(
                    name="get_current_time",
                    description="查询时间",
                    parameters={"type": "object", "properties": {}},
                )
            ],
        )
    )
    assert result.tool_calls[0].name == "get_current_time"
    payload = json.loads(calls[0].content)
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["function"]["name"] == "get_current_time"
    assert payload["messages"][1]["tool_calls"][0]["id"] == "old_call"
    assert payload["messages"][2]["tool_call_id"] == "old_call"
    await provider.close()


@pytest.mark.asyncio
async def test_openai_responses_wire_contract_and_result_mapping() -> None:
    calls: list[httpx.Request] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "先查询"}],
                    },
                    {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "lookup",
                        "arguments": '{"q":"天气"}',
                    },
                ],
            },
        )

    settings = ModelSettings(
        mode="openai",
        protocol="openai_responses",
        base_url="https://model.example/v1",
        api_key="secret-for-test-only",
        name="responses-model",
        supports_tools=True,
    )
    provider = OpenAICompatibleProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handler),
    )
    result = await provider.complete(
        ModelRequest(
            messages=[
                ModelMessage(role="user", content="查天气"),
                ModelMessage(
                    role="assistant",
                    tool_calls=[
                        ModelToolCall(id="old", name="lookup", arguments='{"q":"时间"}')
                    ],
                ),
                ModelMessage(role="tool", content='{"ok":true}', tool_call_id="old"),
            ],
            model="responses-model",
            temperature=0,
            max_tokens=64,
            tools=[
                ToolDefinition(
                    name="lookup",
                    description="查询",
                    parameters={"type": "object", "properties": {}},
                )
            ],
            tool_choice="required",
        )
    )

    assert result.content == "先查询"
    assert result.tool_calls == [
        ModelToolCall(id="call_1", name="lookup", arguments='{"q":"天气"}')
    ]
    assert calls[0].url.path == "/v1/responses"
    payload = json.loads(calls[0].content)
    assert payload["max_output_tokens"] == 64
    assert payload["input"][-1] == {
        "type": "function_call_output",
        "call_id": "old",
        "output": '{"ok":true}',
    }
    assert payload["tools"][0]["name"] == "lookup"
    await provider.close()


@pytest.mark.asyncio
async def test_anthropic_messages_wire_contract_headers_and_result_mapping() -> None:
    calls: list[httpx.Request] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        return httpx.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "我来查"},
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "lookup",
                        "input": {"q": "天气"},
                    },
                ],
            },
        )

    settings = ModelSettings(
        mode="openai",
        protocol="anthropic_messages",
        base_url="https://anthropic.example/v1",
        api_key="anthropic-secret-for-test-only",
        name="claude-compatible",
        supports_tools=True,
    )
    provider = OpenAICompatibleProvider(settings)
    headers = provider.client.headers
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=settings.base_url,
        headers=headers,
        transport=httpx.MockTransport(handler),
    )
    result = await provider.complete(
        ModelRequest(
            messages=[
                ModelMessage(role="system", content="你是助手"),
                ModelMessage(role="user", content="查天气"),
                ModelMessage(
                    role="assistant",
                    tool_calls=[
                        ModelToolCall(id="old", name="lookup", arguments='{"q":"时间"}')
                    ],
                ),
                ModelMessage(role="tool", content='{"ok":true}', tool_call_id="old"),
            ],
            model="claude-compatible",
            temperature=0,
            max_tokens=64,
            tools=[
                ToolDefinition(
                    name="lookup",
                    description="查询",
                    parameters={"type": "object", "properties": {}},
                )
            ],
            tool_choice="required",
        )
    )

    assert result.content == "我来查"
    assert result.tool_calls == [
        ModelToolCall(id="tool_1", name="lookup", arguments='{"q":"天气"}')
    ]
    assert calls[0].url.path == "/v1/messages"
    assert calls[0].headers["x-api-key"] == "anthropic-secret-for-test-only"
    assert calls[0].headers["anthropic-version"] == "2023-06-01"
    payload = json.loads(calls[0].content)
    assert payload["system"] == "你是助手"
    assert payload["messages"][-1]["content"][0]["type"] == "tool_result"
    assert payload["tools"][0]["input_schema"]["type"] == "object"
    assert payload["tool_choice"] == {"type": "any"}
    await provider.close()


def test_profile_model_can_inherit_or_override_protocol_endpoint_and_secret() -> None:
    inherited = ModelSettings(
        mode="openai",
        protocol="openai_responses",
        base_url="https://chat.example/v1",
        api_key="chat-secret-for-test-only",
        name="chat-model",
    ).for_profile()
    assert inherited.protocol == "openai_responses"
    assert inherited.base_url == "https://chat.example/v1"
    assert inherited.api_key == "chat-secret-for-test-only"

    overridden = ModelSettings(
        mode="openai",
        protocol="openai_chat",
        base_url="https://chat.example/v1",
        api_key="chat-secret-for-test-only",
        name="chat-model",
        profile_protocol="anthropic_messages",
        profile_base_url="https://profile.example/v1",
        profile_api_key="profile-secret-for-test-only",
        profile_name="profile-model",
    ).for_profile()
    assert overridden.protocol == "anthropic_messages"
    assert overridden.base_url == "https://profile.example/v1"
    assert overridden.api_key == "profile-secret-for-test-only"
    assert overridden.name == "profile-model"
    assert not overridden.supports_tools


@pytest.mark.asyncio
async def test_capability_probe_reports_each_configured_capability() -> None:
    def handler(incoming: httpx.Request) -> httpx.Response:
        payload = json.loads(incoming.content)
        if payload.get("tools"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "probe_call",
                                        "type": "function",
                                        "function": {
                                            "name": "probe_tool_support",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        content = '{"ok":true}' if payload.get("response_format") else "OK"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    settings = ModelSettings(
        mode="openai",
        base_url="https://model.example/v1",
        api_key="secret-for-test-only",
        name="probe-model",
        supports_json_object=True,
        supports_tools=True,
    )
    provider = OpenAICompatibleProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handler),
    )

    result = await provider.probe()

    assert result["compatible"] is True
    assert result["capabilities"]["text"]["detected"] is True
    assert result["capabilities"]["json_object"]["detected"] is True
    assert result["capabilities"]["tools"]["detected"] is True
    assert result["capabilities"]["streaming"]["configured"] is False
    await provider.close()
