"""模型调用的统一安全观测与 Provider 在途生命周期。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from time import monotonic_ns
from typing import Any, Protocol

from domain.errors import (
    InvalidModelResponseError,
    ModelHardTimeoutError,
    ProviderAuthError,
    ProviderError,
    ProviderQuotaExceededError,
    ProviderRateLimitError,
    ProviderRequestError,
)
from domain.models import ModelAttempt, new_id, utc_now
from ports import (
    ImageGenerationRequest,
    ImageGenerationResult,
    ModelRequest,
    ModelResult,
    ModelStreamEvent,
    ModelUsage,
)

logger = logging.getLogger(__name__)


class ModelAttemptSink(Protocol):
    """模型观测的最小持久化边界。"""

    async def save_model_attempt(self, attempt: ModelAttempt) -> None: ...


class ModelTaskProfileLike(Protocol):
    """观测层只依赖任务路由的只读字段，避免反向依赖配置实现。"""

    @property
    def models(self) -> Sequence[str]: ...

    @property
    def temperature(self) -> float | None: ...

    @property
    def max_tokens(self) -> int | None: ...

    @property
    def hard_timeout_seconds(self) -> float | None: ...

    @property
    def selection_policy(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ModelObservationContext:
    """由业务 owner 提供的低基数关联，不携带用户正文。"""

    task: str | None = None
    profile: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """显式配置的价格；没有配置就不计算成本。"""

    input_usd_per_million_tokens: float | None = None
    output_usd_per_million_tokens: float | None = None
    usd_per_image: float | None = None


class ModelRequestGate:
    """进程级模型请求门禁：限制并发、平滑启动速率，并共享 429 冷却。"""

    def __init__(
        self,
        *,
        max_concurrency: int,
        min_interval_seconds: float = 0.0,
        max_rate_limit_cooldown_seconds: float = 60.0,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须至少为 1")
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds 不能为负数")
        if max_rate_limit_cooldown_seconds <= 0:
            raise ValueError("max_rate_limit_cooldown_seconds 必须大于 0")
        self.max_concurrency = max_concurrency
        self.min_interval_seconds = min_interval_seconds
        self.max_rate_limit_cooldown_seconds = max_rate_limit_cooldown_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._pace_lock = asyncio.Lock()
        self._next_start_at = 0.0
        self._cooldown_until = 0.0
        self._rate_limit_streak = 0

    async def _wait_for_start(self) -> None:
        """所有调用共享启动节奏；等待期间允许新的 429 延长冷却。"""

        loop = asyncio.get_running_loop()
        while True:
            async with self._pace_lock:
                now = loop.time()
                delay = max(self._next_start_at, self._cooldown_until) - now
                if delay <= 0:
                    self._next_start_at = now + self.min_interval_seconds
                    return
            await asyncio.sleep(delay)

    async def _mark_rate_limited(self, error: ProviderRateLimitError) -> None:
        loop = asyncio.get_running_loop()
        async with self._pace_lock:
            self._rate_limit_streak += 1
            retry_after = None
            if error.details:
                raw_retry_after = error.details.get("retry_after_seconds")
                if isinstance(raw_retry_after, (int, float)) and not isinstance(
                    raw_retry_after, bool
                ):
                    retry_after = max(0.0, float(raw_retry_after))
            fallback = max(1.0, self.min_interval_seconds) * (
                2 ** (self._rate_limit_streak - 1)
            )
            cooldown = min(
                self.max_rate_limit_cooldown_seconds,
                max(fallback, retry_after or 0.0),
            )
            self._cooldown_until = max(
                self._cooldown_until,
                loop.time() + cooldown,
            )

    async def _mark_success(self) -> None:
        async with self._pace_lock:
            self._rate_limit_streak = 0

    @asynccontextmanager
    async def slot(self):
        async with self._semaphore:
            await self._wait_for_start()
            try:
                yield
            except ProviderRateLimitError as exc:
                await self._mark_rate_limited(exc)
                raise
            else:
                await self._mark_success()


_EXACT_PROFILE_TASK_PREFIXES = (
    "vision.",
    "detection.",
    "image.",
    "embedding.",
)


def _resolve_model_route(
    *,
    default_task: str,
    default_model: str,
    task_profiles: Mapping[str, ModelTaskProfileLike],
    primary_attempts: int,
    default_hard_timeout_seconds: float | None,
    allow_default_profile: bool = True,
) -> tuple[
    tuple[str, ...],
    float | None,
    ModelTaskProfileLike | None,
]:
    """解析跨模态共用的确定性模型顺序与任务级硬超时。"""

    context = _OBSERVATION_CONTEXT.get() or ModelObservationContext()
    task = context.task or default_task
    profile = task_profiles.get(task)
    if (
        profile is None
        and allow_default_profile
        and not task.startswith(_EXACT_PROFILE_TASK_PREFIXES)
    ):
        profile = task_profiles.get("default")
    if profile is None:
        return (
            (default_model,) * primary_attempts,
            default_hard_timeout_seconds,
            None,
        )
    configured_models = tuple(profile.models)
    candidates = configured_models or (default_model,)
    if profile.selection_policy == "primary":
        models = candidates[:1] * primary_attempts
    elif profile.selection_policy == "ordered_fallback":
        models = (
            candidates
            if configured_models
            else candidates * primary_attempts
        )
    else:
        raise ValueError(
            f"不支持的模型选择策略：{profile.selection_policy}"
        )
    timeout = (
        profile.hard_timeout_seconds
        if profile.hard_timeout_seconds is not None
        else default_hard_timeout_seconds
    )
    return models, timeout, profile


_OBSERVATION_CONTEXT: ContextVar[ModelObservationContext | None] = ContextVar(
    "ija_model_observation_context",
    default=None,
)


@contextmanager
def model_observation_scope(
    *,
    task: str | None = None,
    profile: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    run_id: str | None = None,
):
    """临时补充一次业务调用链的安全关联字段。"""

    current = _OBSERVATION_CONTEXT.get() or ModelObservationContext()
    token = _OBSERVATION_CONTEXT.set(
        ModelObservationContext(
            task=task if task is not None else current.task,
            profile=profile if profile is not None else current.profile,
            session_id=(
                session_id if session_id is not None else current.session_id
            ),
            turn_id=turn_id if turn_id is not None else current.turn_id,
            run_id=run_id if run_id is not None else current.run_id,
        )
    )
    try:
        yield
    finally:
        _OBSERVATION_CONTEXT.reset(token)


class ModelAttemptObserver:
    """把 Provider 事实转换为隐私安全 attempt；观测失败不改变业务结果。"""

    def __init__(self, sink: ModelAttemptSink) -> None:
        self._sink = sink

    async def record(self, attempt: ModelAttempt) -> None:
        try:
            await self._sink.save_model_attempt(attempt)
        except Exception:
            # 观测属于派生数据，不能把一次已经成功的模型调用改写成业务失败。
            logger.exception(
                "模型 attempt 观测写入失败",
                extra={
                    "session_id": attempt.session_id or "-",
                    "turn_id": attempt.turn_id or "-",
                    "run_id": attempt.run_id or "-",
                    "task": attempt.task,
                    "provider": attempt.provider,
                    "profile": attempt.profile,
                    "model": attempt.model,
                    "attempt_number": attempt.attempt_number,
                    "error_type": "model_attempt_persistence_error",
                },
            )


class _LeasedProvider:
    """退役后拒绝新调用，并在既有调用全部结束后只关闭一次。"""

    def __init__(self, provider: Any) -> None:
        self.wrapped_provider = provider
        self._lease_condition = asyncio.Condition()
        self._active_leases = 0
        self._lifecycle_state = "active"
        self._close_error: BaseException | None = None
        self._retirement_task: asyncio.Task[None] | None = None

    @property
    def lifecycle_state(self) -> str:
        return self._lifecycle_state

    @property
    def active_leases(self) -> int:
        return self._active_leases

    @asynccontextmanager
    async def _lease(self):
        async with self._lease_condition:
            if self._lifecycle_state != "active":
                raise ProviderError("模型 Provider 已退役，请重试当前操作")
            self._active_leases += 1
        try:
            yield
        finally:
            async with self._lease_condition:
                self._active_leases -= 1
                if self._active_leases == 0:
                    self._lease_condition.notify_all()

    async def retire_and_close(self) -> None:
        """冻结新租约、等待在途归零并关闭底层客户端。"""

        async with self._lease_condition:
            if self._lifecycle_state == "closed":
                if self._close_error is not None:
                    raise self._close_error
                return
            if self._retirement_task is None:
                self._lifecycle_state = "retiring"
                self._retirement_task = asyncio.create_task(
                    self._finish_retirement()
                )
                self._retirement_task.add_done_callback(
                    self._consume_retirement_result
                )
            retirement_task = self._retirement_task
        # 调用方（例如断开的配置请求）取消等待，不能取消资源真正的排空。
        await asyncio.shield(retirement_task)

    async def _finish_retirement(self) -> None:
        """由共享后台任务完成唯一一次底层关闭。"""

        close_error: BaseException | None = None
        try:
            async with self._lease_condition:
                while self._active_leases:
                    await self._lease_condition.wait()
                self._lifecycle_state = "closing"
            await self.wrapped_provider.close()
        except BaseException as exc:
            close_error = exc
            raise
        finally:
            async with self._lease_condition:
                self._close_error = close_error
                self._lifecycle_state = "closed"
                self._lease_condition.notify_all()

    @staticmethod
    def _consume_retirement_result(task: asyncio.Task[None]) -> None:
        """取走无人等待时的异常，真正的等待方仍会收到同一异常。"""

        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        await self.retire_and_close()


def _error_code(error: BaseException) -> str | None:
    code = getattr(error, "code", None)
    return str(code)[:100] if isinstance(code, str) and code else None


def _latency_ms(start_ns: int) -> int:
    return max(0, round((monotonic_ns() - start_ns) / 1_000_000))


def _usage_cost(usage: ModelUsage | None, pricing: ModelPricing) -> int | None:
    if usage is None:
        return None
    if usage.input_tokens is None or usage.output_tokens is None:
        return None
    if (
        usage.input_tokens > 0
        and pricing.input_usd_per_million_tokens is None
    ) or (
        usage.output_tokens > 0
        and pricing.output_usd_per_million_tokens is None
    ):
        return None
    # 美元/百万 token 换算到 micro-USD 后，数值正好是 token * 配置单价。
    return max(
        0,
        round(
            usage.input_tokens
            * (pricing.input_usd_per_million_tokens or 0)
            + usage.output_tokens
            * (pricing.output_usd_per_million_tokens or 0)
        ),
    )


class _ObservedProviderBase(_LeasedProvider):
    def __init__(
        self,
        provider: Any,
        *,
        observer: ModelAttemptObserver,
        provider_name: str,
        profile: str,
        model: str,
        default_task: str,
        pricing: ModelPricing | None = None,
    ) -> None:
        super().__init__(provider)
        self._observer = observer
        self.provider_name = provider_name
        self.profile = profile
        self.model_name = model
        self.default_task = default_task
        self.pricing = pricing or ModelPricing()

    def _attempt(
        self,
        *,
        invocation_id: str,
        attempt_number: int,
        started_at,
        start_ns: int,
        model: str | None = None,
        streamed: bool = False,
        usage: ModelUsage | None = None,
        tool_call_count: int = 0,
        success: bool,
        error: BaseException | None = None,
        cost_microusd: int | None = None,
    ) -> ModelAttempt:
        context = _OBSERVATION_CONTEXT.get() or ModelObservationContext()
        completed_at = utc_now()
        return ModelAttempt(
            invocation_id=invocation_id,
            attempt_number=attempt_number,
            task=context.task or self.default_task,
            provider=self.provider_name,
            profile=context.profile or self.profile,
            model=(model or self.model_name)[:300],
            session_id=context.session_id,
            turn_id=context.turn_id,
            run_id=context.run_id,
            streamed=streamed,
            tool_call_count=tool_call_count,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            total_tokens=usage.total_tokens if usage is not None else None,
            usage_source="provider" if usage is not None else "unknown",
            latency_ms=_latency_ms(start_ns),
            success=success,
            error_type=type(error).__name__[:100] if error is not None else None,
            error_code=_error_code(error) if error is not None else None,
            cost_microusd=cost_microusd,
            started_at=started_at,
            completed_at=completed_at,
        )

    async def _record(self, attempt: ModelAttempt) -> None:
        await self._observer.record(attempt)


class ObservedModelProvider(_ObservedProviderBase):
    """文本/视觉/检测模型统一装饰器，按真实尝试记录重试。"""

    def __init__(
        self,
        provider: Any,
        *,
        observer: ModelAttemptObserver,
        provider_name: str,
        profile: str,
        model: str,
        default_task: str,
        pricing: ModelPricing | None = None,
        max_attempts: int = 1,
        retry_delay_seconds: float = 0.5,
        task_profiles: Mapping[str, ModelTaskProfileLike] | None = None,
        default_hard_timeout_seconds: float | None = None,
        request_gate: ModelRequestGate | None = None,
    ) -> None:
        super().__init__(
            provider,
            observer=observer,
            provider_name=provider_name,
            profile=profile,
            model=model,
            default_task=default_task,
            pricing=pricing,
        )
        if max_attempts < 1:
            raise ValueError("max_attempts 必须至少为 1")
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.task_profiles = (
            task_profiles if task_profiles is not None else {}
        )
        self.default_hard_timeout_seconds = default_hard_timeout_seconds
        self.request_gate = request_gate

    @asynccontextmanager
    async def _request_slot(self):
        """让多个模型包装器共享同一套并发、pacing 与限流冷却。"""

        if self.request_gate is None:
            yield
            return
        async with self.request_gate.slot():
            yield

    def _retry_delay(self, error: BaseException, attempt_number: int) -> float:
        """优先尊重上游 Retry-After，否则使用指数退避。"""

        delay = self.retry_delay_seconds * (2 ** max(0, attempt_number - 1))
        if isinstance(error, ProviderRateLimitError) and error.details:
            retry_after = error.details.get("retry_after_seconds")
            if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
                delay = max(delay, float(retry_after))
        return delay

    @staticmethod
    def _is_retryable(error: BaseException) -> bool:
        """只重试瞬时 Provider 故障；鉴权与契约错误必须立即暴露。"""

        return isinstance(error, ProviderError) and not isinstance(
            error,
            (
                ProviderAuthError,
                ProviderQuotaExceededError,
                ProviderRequestError,
                InvalidModelResponseError,
            ),
        )

    def _route_requests(
        self,
        request: ModelRequest,
    ) -> tuple[tuple[ModelRequest, ...], float | None]:
        """按任务选择固定顺序候选；从不随机，也不跨 Provider。"""

        models, timeout, profile = _resolve_model_route(
            default_task=self.default_task,
            default_model=request.model or self.model_name,
            task_profiles=self.task_profiles,
            primary_attempts=self.max_attempts,
            default_hard_timeout_seconds=self.default_hard_timeout_seconds,
        )
        if profile is None:
            return (request,) * self.max_attempts, self.default_hard_timeout_seconds
        temperature = profile.temperature
        max_tokens = profile.max_tokens
        routed = tuple(
            replace(
                request,
                model=model,
                temperature=(
                    request.temperature
                    if temperature is None
                    else temperature
                ),
                max_tokens=(
                    request.max_tokens
                    if max_tokens is None
                    else max_tokens
                ),
            )
            for model in models
        )
        return routed, timeout

    async def _complete_once(
        self,
        request: ModelRequest,
        timeout_seconds: float | None,
    ) -> ModelResult:
        async with self._request_slot():
            try:
                if timeout_seconds is None:
                    return await self.wrapped_provider.complete(request)
                async with asyncio.timeout(timeout_seconds):
                    return await self.wrapped_provider.complete(request)
            except TimeoutError as exc:
                raise ModelHardTimeoutError(
                    "模型任务超过 hard timeout"
                ) from exc

    async def complete(self, request: ModelRequest) -> ModelResult:
        invocation_id = new_id("model_call")
        attempt_requests, timeout_seconds = self._route_requests(request)
        async with self._lease():
            for attempt_number, routed_request in enumerate(
                attempt_requests,
                start=1,
            ):
                started_at = utc_now()
                start_ns = monotonic_ns()
                try:
                    result = await self._complete_once(
                        routed_request,
                        timeout_seconds,
                    )
                except BaseException as exc:
                    await self._record(
                        self._attempt(
                            invocation_id=invocation_id,
                            attempt_number=attempt_number,
                            started_at=started_at,
                            start_ns=start_ns,
                            model=routed_request.model,
                            success=False,
                            error=exc,
                        )
                    )
                    if (
                        self._is_retryable(exc)
                        and attempt_number < len(attempt_requests)
                    ):
                        await asyncio.sleep(self._retry_delay(exc, attempt_number))
                        continue
                    raise
                await self._record(
                    self._attempt(
                        invocation_id=invocation_id,
                        attempt_number=attempt_number,
                        started_at=started_at,
                        start_ns=start_ns,
                        model=routed_request.model,
                        usage=result.usage,
                        tool_call_count=len(result.tool_calls),
                        success=True,
                        cost_microusd=_usage_cost(
                            result.usage,
                            (
                                self.pricing
                                if routed_request.model == self.model_name
                                else ModelPricing()
                            ),
                        ),
                    )
                )
                return result
        raise AssertionError("模型尝试循环不应到达此处")

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        invocation_id = new_id("model_call")
        attempt_requests, timeout_seconds = self._route_requests(request)
        async with self._lease():
            for attempt_number, routed_request in enumerate(
                attempt_requests,
                start=1,
            ):
                started_at = utc_now()
                start_ns = monotonic_ns()
                usage: ModelUsage | None = None
                visible_output_emitted = False
                try:
                    async with self._request_slot():
                        if timeout_seconds is None:
                            async for event in self.wrapped_provider.stream(
                                routed_request
                            ):
                                if event.usage is not None:
                                    usage = event.usage
                                if event.delta:
                                    visible_output_emitted = True
                                yield event
                        else:
                            async with asyncio.timeout(timeout_seconds):
                                async for event in self.wrapped_provider.stream(
                                    routed_request
                                ):
                                    if event.usage is not None:
                                        usage = event.usage
                                    if event.delta:
                                        visible_output_emitted = True
                                    yield event
                except TimeoutError as exc:
                    error: BaseException = ModelHardTimeoutError(
                        "流式模型任务超过 hard timeout"
                    )
                    error.__cause__ = exc
                except BaseException as exc:
                    error = exc
                else:
                    await self._record(
                        self._attempt(
                            invocation_id=invocation_id,
                            attempt_number=attempt_number,
                            started_at=started_at,
                            start_ns=start_ns,
                            model=routed_request.model,
                            streamed=True,
                            usage=usage,
                            success=True,
                            cost_microusd=_usage_cost(
                                usage,
                                (
                                    self.pricing
                                    if routed_request.model == self.model_name
                                    else ModelPricing()
                                ),
                            ),
                        )
                    )
                    return
                await self._record(
                    self._attempt(
                        invocation_id=invocation_id,
                        attempt_number=attempt_number,
                        started_at=started_at,
                        start_ns=start_ns,
                        model=routed_request.model,
                        streamed=True,
                        usage=usage,
                        success=False,
                        error=error,
                    )
                )
                if (
                    not visible_output_emitted
                    and self._is_retryable(error)
                    and attempt_number < len(attempt_requests)
                ):
                    await asyncio.sleep(self._retry_delay(error, attempt_number))
                    continue
                raise error
        raise AssertionError("流式模型尝试循环不应到达此处")

    async def probe(self) -> dict[str, Any]:
        invocation_id = new_id("model_call")
        started_at = utc_now()
        start_ns = monotonic_ns()
        async with self._lease():
            try:
                result = await self.wrapped_provider.probe()
            except BaseException as exc:
                await self._record(
                    self._attempt(
                        invocation_id=invocation_id,
                        attempt_number=1,
                        started_at=started_at,
                        start_ns=start_ns,
                        success=False,
                        error=exc,
                    )
                )
                raise
            await self._record(
                self._attempt(
                    invocation_id=invocation_id,
                    attempt_number=1,
                    started_at=started_at,
                    start_ns=start_ns,
                    success=True,
                )
            )
            return result


class ObservedEmbeddingProvider(_ObservedProviderBase):
    """Embedding 装饰器；按任务路由模型并保持 list/usage 兼容。"""

    def __init__(
        self,
        provider: Any,
        *,
        observer: ModelAttemptObserver,
        provider_name: str,
        model: str,
        pricing: ModelPricing | None = None,
        max_attempts: int = 1,
        retry_delay_seconds: float = 0.5,
        task_profiles: Mapping[str, ModelTaskProfileLike] | None = None,
        default_hard_timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(
            provider,
            observer=observer,
            provider_name=provider_name,
            profile="embedding",
            model=model,
            default_task="embedding.embed",
            pricing=pricing,
        )
        if max_attempts < 1:
            raise ValueError("max_attempts 必须至少为 1")
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.task_profiles = (
            task_profiles if task_profiles is not None else {}
        )
        self.default_hard_timeout_seconds = default_hard_timeout_seconds

    async def _embed_once(
        self,
        texts: list[str],
        *,
        model: str,
        timeout_seconds: float | None,
    ) -> list[list[float]]:
        """执行一次明确模型的向量请求，并施加独立 hard timeout。"""

        async def invoke() -> list[list[float]]:
            if model == self.model_name:
                # 兼容尚未实现可选 model 参数的本地测试/注入 Provider；
                # 只有默认模型才允许走这个窄兼容分支，避免伪装已完成路由。
                return await self.wrapped_provider.embed(texts)
            return await self.wrapped_provider.embed(texts, model=model)

        try:
            if timeout_seconds is None:
                return await invoke()
            async with asyncio.timeout(timeout_seconds):
                return await invoke()
        except TimeoutError as exc:
            raise ModelHardTimeoutError(
                "Embedding 任务超过 hard timeout"
            ) from exc

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[list[float]]:
        invocation_id = new_id("model_call")
        models, timeout_seconds, _ = _resolve_model_route(
            default_task=self.default_task,
            default_model=model or self.model_name,
            task_profiles=self.task_profiles,
            primary_attempts=self.max_attempts,
            default_hard_timeout_seconds=self.default_hard_timeout_seconds,
            allow_default_profile=False,
        )
        async with self._lease():
            for attempt_number, routed_model in enumerate(models, start=1):
                started_at = utc_now()
                start_ns = monotonic_ns()
                try:
                    vectors = await self._embed_once(
                        texts,
                        model=routed_model,
                        timeout_seconds=timeout_seconds,
                    )
                except BaseException as exc:
                    await self._record(
                        self._attempt(
                            invocation_id=invocation_id,
                            attempt_number=attempt_number,
                            started_at=started_at,
                            start_ns=start_ns,
                            model=routed_model,
                            success=False,
                            error=exc,
                        )
                    )
                    if (
                        ObservedModelProvider._is_retryable(exc)
                        and attempt_number < len(models)
                    ):
                        await asyncio.sleep(self.retry_delay_seconds)
                        continue
                    raise
                usage = getattr(vectors, "usage", None)
                await self._record(
                    self._attempt(
                        invocation_id=invocation_id,
                        attempt_number=attempt_number,
                        started_at=started_at,
                        start_ns=start_ns,
                        model=routed_model,
                        usage=usage if isinstance(usage, ModelUsage) else None,
                        success=True,
                        cost_microusd=_usage_cost(
                            usage if isinstance(usage, ModelUsage) else None,
                            (
                                self.pricing
                                if routed_model == self.model_name
                                else ModelPricing()
                            ),
                        ),
                    )
                )
                return vectors
        raise AssertionError("Embedding 尝试循环不应到达此处")


class ObservedImageModelProvider(_ObservedProviderBase):
    """图片生成装饰器；支持显式有序路由且不做隐式付费重试。"""

    def __init__(
        self,
        provider: Any,
        *,
        observer: ModelAttemptObserver,
        provider_name: str,
        model: str,
        pricing: ModelPricing | None = None,
        task_profiles: Mapping[str, ModelTaskProfileLike] | None = None,
        default_hard_timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(
            provider,
            observer=observer,
            provider_name=provider_name,
            profile="image",
            model=model,
            default_task="image.generate",
            pricing=pricing,
        )
        self.task_profiles = (
            task_profiles if task_profiles is not None else {}
        )
        self.default_hard_timeout_seconds = default_hard_timeout_seconds

    async def _generate_once(
        self,
        request: ImageGenerationRequest,
        *,
        model: str,
        timeout_seconds: float | None,
    ) -> ImageGenerationResult:
        """执行一次明确模型的图片请求，并施加独立 hard timeout。"""

        routed_request = replace(request, model=model)
        try:
            if timeout_seconds is None:
                return await self.wrapped_provider.generate(routed_request)
            async with asyncio.timeout(timeout_seconds):
                return await self.wrapped_provider.generate(routed_request)
        except TimeoutError as exc:
            raise ModelHardTimeoutError(
                "图片生成任务超过 hard timeout"
            ) from exc

    async def generate(
        self,
        request: ImageGenerationRequest,
    ) -> ImageGenerationResult:
        invocation_id = new_id("model_call")
        models, timeout_seconds, _ = _resolve_model_route(
            default_task=self.default_task,
            default_model=request.model or self.model_name,
            task_profiles=self.task_profiles,
            # 图片调用可能已经产生费用，未显式配置 fallback 时绝不自动重试。
            primary_attempts=1,
            default_hard_timeout_seconds=self.default_hard_timeout_seconds,
            allow_default_profile=False,
        )
        async with self._lease():
            for attempt_number, routed_model in enumerate(models, start=1):
                started_at = utc_now()
                start_ns = monotonic_ns()
                try:
                    result = await self._generate_once(
                        request,
                        model=routed_model,
                        timeout_seconds=timeout_seconds,
                    )
                except BaseException as exc:
                    await self._record(
                        self._attempt(
                            invocation_id=invocation_id,
                            attempt_number=attempt_number,
                            started_at=started_at,
                            start_ns=start_ns,
                            model=routed_model,
                            success=False,
                            error=exc,
                        )
                    )
                    if (
                        ObservedModelProvider._is_retryable(exc)
                        and attempt_number < len(models)
                    ):
                        continue
                    raise
                cost = (
                    round(self.pricing.usd_per_image * 1_000_000)
                    if (
                        routed_model == self.model_name
                        and self.pricing.usd_per_image is not None
                    )
                    else None
                )
                await self._record(
                    self._attempt(
                        invocation_id=invocation_id,
                        attempt_number=attempt_number,
                        started_at=started_at,
                        start_ns=start_ns,
                        model=routed_model,
                        usage=result.usage,
                        success=True,
                        cost_microusd=cost,
                    )
                )
                return result
        raise AssertionError("图片模型尝试循环不应到达此处")


def provider_identity(provider: Any, *, fallback_model: str) -> tuple[str, str]:
    """只提取协议和模型名，永不把 endpoint 或密钥当作标识。"""

    settings = getattr(provider, "settings", None)
    protocol = getattr(settings, "protocol", None)
    model = getattr(settings, "name", None)
    if not isinstance(protocol, str) or not protocol:
        protocol = (
            "fake"
            if provider.__class__.__name__.casefold().startswith("fake")
            else provider.__class__.__name__
        )
    if not isinstance(model, str) or not model:
        model = fallback_model
    return protocol[:100], model[:300]


def pricing_from_settings(
    settings: Any,
    *,
    input_field: str = "input_price_usd_per_million_tokens",
    output_field: str = "output_price_usd_per_million_tokens",
    image_field: str = "price_usd_per_image",
) -> ModelPricing:
    """读取显式价格配置；字段不存在或为 None 时保持成本未知。"""

    return ModelPricing(
        input_usd_per_million_tokens=getattr(settings, input_field, None),
        output_usd_per_million_tokens=getattr(settings, output_field, None),
        usd_per_image=getattr(settings, image_field, None),
    )


def is_leased_provider(provider: Any) -> bool:
    return isinstance(provider, _LeasedProvider)


async def retire_provider(provider: Any) -> None:
    """热切换的统一关闭入口；兼容尚未包装的测试 Provider。"""

    if isinstance(provider, _LeasedProvider):
        await provider.retire_and_close()
    else:
        await provider.close()
