"""任务级模型路由、attempt 观测与 Provider lease 回归。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pydantic import ValidationError

from config import ModelSettings, ModelTaskProfile, VisionModelSettings
from domain.errors import (
    ModelHardTimeoutError,
    ProviderError,
    ProviderRateLimitError,
)
from domain.models import ModelAttempt
from observability import (
    ModelAttemptObserver,
    ModelPricing,
    ModelRequestGate,
    ObservedEmbeddingProvider,
    ObservedImageModelProvider,
    ObservedModelProvider,
    model_observation_scope,
)
from ports import (
    EmbeddingVectors,
    ImageGenerationRequest,
    ImageGenerationResult,
    ModelMessage,
    ModelRequest,
    ModelResult,
    ModelStreamEvent,
    ModelUsage,
)


class _AttemptSink:
    def __init__(self) -> None:
        self.attempts: list[ModelAttempt] = []

    async def save_model_attempt(self, attempt: ModelAttempt) -> None:
        self.attempts.append(attempt)


class _ScriptedProvider:
    """按给定结果执行 complete，便于验证精确尝试顺序。"""

    def __init__(
        self,
        outcomes: list[ModelResult | BaseException],
    ) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[ModelRequest] = []
        self.close_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        result = await self.complete(request)
        if result.content:
            yield ModelStreamEvent(
                delta=result.content,
                finish_reason=result.finish_reason,
                usage=result.usage,
            )

    async def probe(self) -> dict[str, object]:
        return {"ok": True}

    async def close(self) -> None:
        self.close_calls += 1


class _FallbackStreamProvider:
    def __init__(self, *, fail_after_visible_output: bool = False) -> None:
        self.fail_after_visible_output = fail_after_visible_output
        self.models: list[str] = []
        self.close_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResult:
        raise AssertionError("流式测试不应调用 complete")

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        self.models.append(request.model)
        if request.model == "primary":
            if self.fail_after_visible_output:
                yield ModelStreamEvent(delta="半")
            raise ProviderRateLimitError("测试限流")
        yield ModelStreamEvent(delta="好")
        yield ModelStreamEvent(
            finish_reason="stop",
            usage=ModelUsage(
                input_tokens=6,
                output_tokens=1,
                total_tokens=7,
            ),
        )

    async def probe(self) -> dict[str, object]:
        return {"ok": True}

    async def close(self) -> None:
        self.close_calls += 1


class _BlockingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.close_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.started.set()
        await self.release.wait()
        return ModelResult(content="完成")

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        result = await self.complete(request)
        yield ModelStreamEvent(delta=result.content or "")

    async def probe(self) -> dict[str, object]:
        return {"ok": True}

    async def close(self) -> None:
        self.close_calls += 1


class _NeverReturningProvider(_ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        await asyncio.Event().wait()
        raise AssertionError("hard timeout 应取消该调用")


class _ScriptedEmbeddingProvider:
    """记录实际模型名的 Embedding Provider。"""

    def __init__(
        self,
        outcomes: list[EmbeddingVectors | BaseException],
    ) -> None:
        self.outcomes = list(outcomes)
        self.models: list[str] = []
        self.close_calls = 0

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> EmbeddingVectors:
        self.models.append(model or "embedding-primary")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def close(self) -> None:
        self.close_calls += 1


class _NeverReturningEmbeddingProvider(_ScriptedEmbeddingProvider):
    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> EmbeddingVectors:
        self.models.append(model or "embedding-primary")
        await asyncio.Event().wait()
        raise AssertionError("hard timeout 应取消该调用")


class _ScriptedImageProvider:
    """记录 request.model 的图片 Provider。"""

    def __init__(
        self,
        outcomes: list[ImageGenerationResult | BaseException],
    ) -> None:
        self.outcomes = list(outcomes)
        self.models: list[str | None] = []
        self.close_calls = 0

    async def generate(
        self,
        request: ImageGenerationRequest,
    ) -> ImageGenerationResult:
        self.models.append(request.model)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def close(self) -> None:
        self.close_calls += 1


class _NeverReturningImageProvider(_ScriptedImageProvider):
    async def generate(
        self,
        request: ImageGenerationRequest,
    ) -> ImageGenerationResult:
        self.models.append(request.model)
        await asyncio.Event().wait()
        raise AssertionError("hard timeout 应取消该调用")


def _request(*, model: str = "request-model") -> ModelRequest:
    return ModelRequest(
        messages=[ModelMessage(role="user", content="不应进入观测的正文")],
        model=model,
        temperature=0.9,
        max_tokens=999,
    )


def _observed(
    provider,
    sink: _AttemptSink,
    *,
    max_attempts: int = 2,
    task_profiles: dict[str, ModelTaskProfile] | None = None,
    pricing: ModelPricing | None = None,
    request_gate: ModelRequestGate | None = None,
) -> ObservedModelProvider:
    return ObservedModelProvider(
        provider,
        observer=ModelAttemptObserver(sink),
        provider_name="test-protocol",
        profile="chat",
        model="primary",
        default_task="chat.reply",
        pricing=pricing,
        max_attempts=max_attempts,
        retry_delay_seconds=0,
        task_profiles=task_profiles,
        default_hard_timeout_seconds=1,
        request_gate=request_gate,
    )


def _observed_embedding(
    provider,
    sink: _AttemptSink,
    *,
    task_profiles: dict[str, ModelTaskProfile],
    max_attempts: int = 2,
) -> ObservedEmbeddingProvider:
    return ObservedEmbeddingProvider(
        provider,
        observer=ModelAttemptObserver(sink),
        provider_name="openai-embedding",
        model="embedding-primary",
        pricing=ModelPricing(input_usd_per_million_tokens=2),
        max_attempts=max_attempts,
        retry_delay_seconds=0,
        task_profiles=task_profiles,
        default_hard_timeout_seconds=1,
    )


def _observed_image(
    provider,
    sink: _AttemptSink,
    *,
    task_profiles: dict[str, ModelTaskProfile],
) -> ObservedImageModelProvider:
    return ObservedImageModelProvider(
        provider,
        observer=ModelAttemptObserver(sink),
        provider_name="openai-image",
        model="image-primary",
        pricing=ModelPricing(usd_per_image=0.04),
        task_profiles=task_profiles,
        default_hard_timeout_seconds=1,
    )


@pytest.mark.asyncio
async def test_shared_request_gate_serializes_independent_model_providers() -> None:
    gate = ModelRequestGate(max_concurrency=1)
    first_provider = _BlockingProvider()
    second_provider = _BlockingProvider()
    first = _observed(
        first_provider,
        _AttemptSink(),
        max_attempts=1,
        request_gate=gate,
    )
    second = _observed(
        second_provider,
        _AttemptSink(),
        max_attempts=1,
        request_gate=gate,
    )

    first_task = asyncio.create_task(first.complete(_request()))
    await first_provider.started.wait()
    second_task = asyncio.create_task(second.complete(_request()))
    await asyncio.sleep(0)
    assert not second_provider.started.is_set()

    first_provider.release.set()
    await first_task
    await second_provider.started.wait()
    second_provider.release.set()
    await second_task


@pytest.mark.asyncio
async def test_request_gate_spaces_concurrent_request_starts() -> None:
    gate = ModelRequestGate(max_concurrency=2, min_interval_seconds=0.03)
    starts: list[float] = []

    async def enter_gate() -> None:
        async with gate.slot():
            starts.append(asyncio.get_running_loop().time())

    await asyncio.gather(enter_gate(), enter_gate())

    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.02


@pytest.mark.asyncio
async def test_ordered_fallback_applies_profile_and_records_each_attempt() -> None:
    sink = _AttemptSink()
    provider = _ScriptedProvider(
        [
            ProviderRateLimitError("测试限流"),
            ModelResult(
                content="fallback",
                usage=ModelUsage(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                ),
            ),
        ]
    )
    observed = _observed(
        provider,
        sink,
        task_profiles={
            "memory.consolidate": ModelTaskProfile(
                models=["primary", "fallback"],
                temperature=0.2,
                max_tokens=321,
                hard_timeout_seconds=2,
                selection_policy="ordered_fallback",
            )
        },
        pricing=ModelPricing(
            input_usd_per_million_tokens=2,
            output_usd_per_million_tokens=4,
        ),
    )

    with model_observation_scope(
        task="memory.consolidate",
        profile="profile",
        session_id="session-1",
        run_id="run-1",
    ):
        result = await observed.complete(_request())

    assert result.content == "fallback"
    assert [request.model for request in provider.requests] == [
        "primary",
        "fallback",
    ]
    assert {request.temperature for request in provider.requests} == {0.2}
    assert {request.max_tokens for request in provider.requests} == {321}
    assert [attempt.success for attempt in sink.attempts] == [False, True]
    assert sink.attempts[0].error_code == "provider_rate_limit"
    assert sink.attempts[1].usage_source == "provider"
    assert sink.attempts[1].total_tokens == 15
    # fallback 模型没有单独价格配置，不能套用主模型价格猜算。
    assert sink.attempts[1].cost_microusd is None
    assert sink.attempts[1].session_id == "session-1"
    assert sink.attempts[1].run_id == "run-1"


@pytest.mark.asyncio
async def test_primary_policy_retries_same_model_for_state_changing_task() -> None:
    sink = _AttemptSink()
    provider = _ScriptedProvider(
        [
            ProviderRateLimitError("测试限流"),
            ModelResult(content="成功"),
        ]
    )
    observed = _observed(
        provider,
        sink,
        task_profiles={
            "social.learn": ModelTaskProfile(
                models=["fixed-primary", "must-not-be-selected"],
                selection_policy="primary",
            )
        },
    )

    with model_observation_scope(task="social.learn"):
        await observed.complete(_request())

    assert [request.model for request in provider.requests] == [
        "fixed-primary",
        "fixed-primary",
    ]
    assert [attempt.attempt_number for attempt in sink.attempts] == [1, 2]


@pytest.mark.asyncio
async def test_hard_timeout_is_bounded_and_observed() -> None:
    sink = _AttemptSink()
    provider = _NeverReturningProvider([])
    observed = _observed(
        provider,
        sink,
        max_attempts=1,
        task_profiles={
            "profile.extract": ModelTaskProfile(
                hard_timeout_seconds=0.01,
            )
        },
    )

    with model_observation_scope(task="profile.extract"):
        with pytest.raises(ModelHardTimeoutError):
            await observed.complete(_request())

    assert len(sink.attempts) == 1
    assert sink.attempts[0].success is False
    assert sink.attempts[0].error_code == "model_hard_timeout"


@pytest.mark.asyncio
async def test_stream_falls_back_only_before_visible_output() -> None:
    profile = ModelTaskProfile(
        models=["primary", "fallback"],
        selection_policy="ordered_fallback",
    )
    sink = _AttemptSink()
    provider = _FallbackStreamProvider()
    observed = _observed(
        provider,
        sink,
        task_profiles={"chat.reply": profile},
    )

    events = [event async for event in observed.stream(_request())]

    assert provider.models == ["primary", "fallback"]
    assert "".join(event.delta for event in events) == "好"
    assert [attempt.success for attempt in sink.attempts] == [False, True]
    assert sink.attempts[1].total_tokens == 7

    visible_sink = _AttemptSink()
    visible_provider = _FallbackStreamProvider(
        fail_after_visible_output=True
    )
    visible_observed = _observed(
        visible_provider,
        visible_sink,
        task_profiles={"chat.reply": profile},
    )
    visible_events: list[ModelStreamEvent] = []
    with pytest.raises(ProviderRateLimitError):
        async for event in visible_observed.stream(_request()):
            visible_events.append(event)

    assert "".join(event.delta for event in visible_events) == "半"
    assert visible_provider.models == ["primary"]
    assert len(visible_sink.attempts) == 1
    assert visible_sink.attempts[0].success is False


@pytest.mark.asyncio
async def test_usage_cost_is_known_only_from_provider_facts_and_price() -> None:
    sink = _AttemptSink()
    provider = _ScriptedProvider(
        [
            ModelResult(
                content="known",
                usage=ModelUsage(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                ),
            ),
            ModelResult(content="unknown"),
        ]
    )
    observed = _observed(
        provider,
        sink,
        max_attempts=1,
        pricing=ModelPricing(
            input_usd_per_million_tokens=2,
            output_usd_per_million_tokens=4,
        ),
    )

    await observed.complete(_request(model="primary"))
    await observed.complete(_request(model="primary"))

    assert sink.attempts[0].cost_microusd == 40
    assert sink.attempts[0].usage_source == "provider"
    assert sink.attempts[1].usage_source == "unknown"
    assert sink.attempts[1].input_tokens is None
    assert sink.attempts[1].output_tokens is None
    assert sink.attempts[1].total_tokens is None
    assert sink.attempts[1].cost_microusd is None


@pytest.mark.asyncio
async def test_retirement_rejects_new_leases_drains_inflight_and_closes_once() -> None:
    sink = _AttemptSink()
    provider = _BlockingProvider()
    observed = _observed(provider, sink, max_attempts=1)
    in_flight = asyncio.create_task(observed.complete(_request()))
    await provider.started.wait()

    first_retirement = asyncio.create_task(observed.retire_and_close())
    for _ in range(20):
        if observed.lifecycle_state == "retiring":
            break
        await asyncio.sleep(0)

    assert observed.lifecycle_state == "retiring"
    assert observed.active_leases == 1
    first_retirement.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_retirement
    second_retirement = asyncio.create_task(observed.retire_and_close())
    third_retirement = asyncio.create_task(observed.retire_and_close())
    with pytest.raises(ProviderError, match="已退役"):
        await observed.complete(_request())
    assert provider.close_calls == 0

    provider.release.set()
    assert (await in_flight).content == "完成"
    await asyncio.gather(second_retirement, third_retirement)
    assert observed.active_leases == 0
    assert observed.lifecycle_state == "closed"
    assert provider.close_calls == 1

    await observed.close()
    assert provider.close_calls == 1


@pytest.mark.asyncio
async def test_dedicated_modalities_require_exact_task_profile() -> None:
    """全局 default 是文本默认值，不能把聊天模型名带进独立模态。"""

    default_profile = ModelTaskProfile(
        models=["chat-only"],
        selection_policy="ordered_fallback",
    )
    text_sink = _AttemptSink()
    text_provider = _ScriptedProvider([ModelResult(content="vision")])
    text_observed = _observed(
        text_provider,
        text_sink,
        max_attempts=1,
        task_profiles={"default": default_profile},
    )
    with model_observation_scope(task="vision.analyze"):
        await text_observed.complete(_request(model="vision-primary"))
    assert [item.model for item in text_provider.requests] == [
        "vision-primary"
    ]
    exact_vision_provider = _ScriptedProvider(
        [ModelResult(content="vision")]
    )
    exact_vision_observed = _observed(
        exact_vision_provider,
        _AttemptSink(),
        max_attempts=1,
        task_profiles={
            "default": default_profile,
            "vision.analyze": ModelTaskProfile(
                models=["vision-special"]
            ),
        },
    )
    with model_observation_scope(task="vision.analyze"):
        await exact_vision_observed.complete(
            _request(model="vision-primary")
        )
    assert [item.model for item in exact_vision_provider.requests] == [
        "vision-special"
    ]

    embedding_sink = _AttemptSink()
    embedding_provider = _ScriptedEmbeddingProvider(
        [EmbeddingVectors([[0.1]])]
    )
    embedding_observed = _observed_embedding(
        embedding_provider,
        embedding_sink,
        task_profiles={"default": default_profile},
    )
    with model_observation_scope(task="embedding.memory_query"):
        await embedding_observed.embed(["query"])
    assert embedding_provider.models == ["embedding-primary"]

    image_sink = _AttemptSink()
    image_provider = _ScriptedImageProvider(
        [ImageGenerationResult(content=b"image")]
    )
    image_observed = _observed_image(
        image_provider,
        image_sink,
        task_profiles={"default": default_profile},
    )
    with model_observation_scope(task="image.expression_generate"):
        await image_observed.generate(
            ImageGenerationRequest(base_image=b"base", prompt="prompt")
        )
    assert image_provider.models == ["image-primary"]


@pytest.mark.asyncio
async def test_embedding_and_image_exact_profiles_route_ordered_fallback() -> None:
    embedding_sink = _AttemptSink()
    embedding_provider = _ScriptedEmbeddingProvider(
        [
            ProviderRateLimitError("测试限流"),
            EmbeddingVectors(
                [[0.2]],
                usage=ModelUsage(input_tokens=3, output_tokens=0),
            ),
        ]
    )
    embedding_observed = _observed_embedding(
        embedding_provider,
        embedding_sink,
        task_profiles={
            "embedding.memory_query": ModelTaskProfile(
                models=["embedding-primary", "embedding-fallback"],
                selection_policy="ordered_fallback",
            )
        },
    )
    with model_observation_scope(task="embedding.memory_query"):
        await embedding_observed.embed(["query"])
    assert embedding_provider.models == [
        "embedding-primary",
        "embedding-fallback",
    ]
    assert [item.success for item in embedding_sink.attempts] == [
        False,
        True,
    ]
    assert embedding_sink.attempts[1].cost_microusd is None

    image_sink = _AttemptSink()
    image_provider = _ScriptedImageProvider(
        [
            ProviderRateLimitError("测试限流"),
            ImageGenerationResult(content=b"image"),
        ]
    )
    image_observed = _observed_image(
        image_provider,
        image_sink,
        task_profiles={
            "image.expression_generate": ModelTaskProfile(
                models=["image-primary", "image-fallback"],
                selection_policy="ordered_fallback",
            )
        },
    )
    with model_observation_scope(task="image.expression_generate"):
        await image_observed.generate(
            ImageGenerationRequest(base_image=b"base", prompt="prompt")
        )
    assert image_provider.models == ["image-primary", "image-fallback"]
    assert [item.success for item in image_sink.attempts] == [False, True]
    assert image_sink.attempts[1].cost_microusd is None


@pytest.mark.asyncio
async def test_embedding_and_image_apply_exact_hard_timeout() -> None:
    embedding_sink = _AttemptSink()
    embedding_provider = _NeverReturningEmbeddingProvider([])
    embedding_observed = _observed_embedding(
        embedding_provider,
        embedding_sink,
        max_attempts=1,
        task_profiles={
            "embedding.memory_query": ModelTaskProfile(
                hard_timeout_seconds=0.01
            )
        },
    )
    with model_observation_scope(task="embedding.memory_query"):
        with pytest.raises(ModelHardTimeoutError):
            await embedding_observed.embed(["query"])
    assert embedding_sink.attempts[0].error_code == "model_hard_timeout"

    image_sink = _AttemptSink()
    image_provider = _NeverReturningImageProvider([])
    image_observed = _observed_image(
        image_provider,
        image_sink,
        task_profiles={
            "image.expression_generate": ModelTaskProfile(
                hard_timeout_seconds=0.01
            )
        },
    )
    with model_observation_scope(task="image.expression_generate"):
        with pytest.raises(ModelHardTimeoutError):
            await image_observed.generate(
                ImageGenerationRequest(
                    base_image=b"base",
                    prompt="prompt",
                )
            )
    assert image_sink.attempts[0].error_code == "model_hard_timeout"


def test_external_vision_projection_preserves_task_profiles() -> None:
    profile = ModelTaskProfile(models=["vision-special"])
    projected = VisionModelSettings(
        mode="external",
        api_key="test-key",
        name="vision-primary",
    ).as_model_settings(task_profiles={"vision.analyze": profile})

    assert projected.task_profiles == {"vision.analyze": profile}


def test_task_profile_rejects_budget_outside_context_window() -> None:
    with pytest.raises(
        ValidationError,
        match="task_profiles 的 max_tokens",
    ):
        ModelSettings(
            context_window_tokens=1024,
            max_tokens=500,
            task_profiles={
                "chat.reply": ModelTaskProfile(max_tokens=1024)
            },
        )
