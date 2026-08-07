import base64
from io import BytesIO

import httpx
import pytest
from PIL import Image

from adapters.model import OpenAICompatibleImageProvider
from config import ImageModelSettings
from domain.errors import (
    InvalidModelResponseError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
)
from ports import ImageGenerationRequest


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (90, 160), "#eb799f").save(output, format="PNG")
    return output.getvalue()


async def provider_with(handler: httpx.MockTransport) -> OpenAICompatibleImageProvider:
    settings = ImageModelSettings(
        enabled=True,
        base_url="https://images.example/v1",
        api_key="secret-for-test-only",
        name="image-test-model",
    )
    provider = OpenAICompatibleImageProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(base_url=settings.base_url, transport=handler)
    return provider


def generation_request() -> ImageGenerationRequest:
    return ImageGenerationRequest(
        base_image=png_bytes(),
        prompt="开心挥手",
        filename="base_image.png",
    )


@pytest.mark.asyncio
async def test_images_edits_uses_minimal_multipart_and_strict_base64() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]},
        )

    provider = await provider_with(httpx.MockTransport(handler))
    result = await provider.generate(generation_request())
    assert result.content.startswith(b"\x89PNG")
    assert len(calls) == 1
    assert calls[0].url.path == "/v1/images/edits"
    body = calls[0].content
    assert b'name="image"; filename="base_image.png"' in body
    assert b'name="model"' in body and b"image-test-model" in body
    assert b'name="size"' in body and b"1024x1536" in body
    assert b'name="output_format"' in body and b"png" in body
    await provider.close()


@pytest.mark.asyncio
async def test_images_edits_honors_explicit_routed_model() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(
                            png_bytes()
                        ).decode()
                    }
                ]
            },
        )

    provider = await provider_with(httpx.MockTransport(handler))
    request = generation_request()
    request = ImageGenerationRequest(
        base_image=request.base_image,
        prompt=request.prompt,
        filename=request.filename,
        model="image-fallback",
    )
    await provider.generate(request)

    assert b"image-fallback" in calls[0].content
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, ProviderAuthError),
        (429, ProviderRateLimitError),
        (503, ProviderError),
    ],
)
async def test_paid_generation_failures_are_not_retried(
    status: int, error_type: type[ProviderError]
) -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(status, json={"error": {"message": "failed"}})

    provider = await provider_with(httpx.MockTransport(handler))
    with pytest.raises(error_type):
        await provider.generate(generation_request())
    assert call_count == 1
    await provider.close()


@pytest.mark.asyncio
async def test_timeout_is_not_retried() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.ReadTimeout("slow", request=request)

    provider = await provider_with(httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="请求失败"):
        await provider.generate(generation_request())
    assert call_count == 1
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"url": "https://attacker.example/image.png"}]},
        {"data": [{"b64_json": "not valid base64%%%"}]},
        {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]},
    ],
)
async def test_url_invalid_base64_and_forged_images_are_rejected(payload: dict) -> None:
    provider = await provider_with(
        httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    with pytest.raises(InvalidModelResponseError):
        await provider.generate(generation_request())
    await provider.close()


@pytest.mark.asyncio
async def test_oversized_images_response_is_rejected_before_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = await provider_with(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"b64_json": "A" * 200}]})
        )
    )
    monkeypatch.setattr(OpenAICompatibleImageProvider, "MAX_RESPONSE_BYTES", 100)
    with pytest.raises(InvalidModelResponseError, match="响应超过大小上限"):
        await provider.generate(generation_request())
    await provider.close()
