from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

import bootstrap as bootstrap_module
from adapters.model import FakeImageModelProvider, FakeModelProvider
from bootstrap import RuntimeShutdownError, build_runtime
from config import AppSettings, ModelTaskProfile
from domain.errors import ConflictError
from observability import (
    ObservedEmbeddingProvider,
    ObservedImageModelProvider,
    ObservedModelProvider,
)
from ports import ModelRequest, ModelResult, ModelStreamEvent


class _EmbeddingProvider:
    """生命周期测试使用的最小 Embedding Provider。"""

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[list[float]]:
        return [[0.0] for _ in texts]

    async def close(self) -> None:
        return None


class _TrackedModelProvider:
    """记录底层 close 次数，验证多次热切换不会积累客户端。"""

    def __init__(self) -> None:
        self.close_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResult:
        return ModelResult(content="tracked")

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        yield ModelStreamEvent(delta="tracked")

    async def probe(self) -> dict[str, object]:
        return {"ok": True}

    async def close(self) -> None:
        self.close_calls += 1


def _async_step(
    calls: list[str],
    name: str,
    *,
    error: Exception | None = None,
) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        calls.append(name)
        if error is not None:
            raise error

    return run


@pytest.mark.asyncio
async def test_start_failure_runs_reverse_compensation_without_masking_primary_error(
    settings: AppSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中段启动失败应逆序清理，且补偿失败不能遮蔽原始错误。"""

    runtime = build_runtime(settings)
    calls: list[str] = []
    detection_model = FakeModelProvider()
    runtime.detection_model = detection_model

    monkeypatch.setattr(
        runtime.personas,
        "initialize",
        lambda: calls.append("persona.initialize"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "upgrade_database",
        lambda *_: calls.append("database.migrate"),
    )
    monkeypatch.setattr(runtime.store, "initialize", _async_step(calls, "database.start"))
    monkeypatch.setattr(runtime.store, "close", _async_step(calls, "database.stop"))
    monkeypatch.setattr(runtime.rss, "close", _async_step(calls, "rss.close"))
    monkeypatch.setattr(runtime.weather, "close", _async_step(calls, "weather.close"))
    monkeypatch.setattr(runtime.model, "close", _async_step(calls, "model.close"))
    monkeypatch.setattr(
        detection_model,
        "close",
        _async_step(calls, "detection.close"),
    )
    monkeypatch.setattr(
        runtime.managed_services,
        "start",
        _async_step(calls, "managed.start"),
    )
    monkeypatch.setattr(
        runtime.managed_services,
        "stop",
        _async_step(calls, "managed.stop"),
    )
    monkeypatch.setattr(
        runtime.platform_plugins,
        "start",
        _async_step(calls, "plugins.start"),
    )
    monkeypatch.setattr(
        runtime.platform_plugins,
        "stop",
        _async_step(
            calls,
            "plugins.stop",
            error=RuntimeError("模拟插件补偿失败"),
        ),
    )
    monkeypatch.setattr(
        runtime.outbound,
        "recover",
        _async_step(calls, "outbound.recover"),
    )
    monkeypatch.setattr(runtime.memory, "start", _async_step(calls, "memory.start"))
    monkeypatch.setattr(runtime.memory, "stop", _async_step(calls, "memory.stop"))
    monkeypatch.setattr(
        runtime.social_learning,
        "start",
        _async_step(
            calls,
            "social.start",
            error=RuntimeError("模拟社交学习启动失败"),
        ),
    )
    monkeypatch.setattr(
        runtime.social_learning,
        "stop",
        _async_step(calls, "social.stop"),
    )
    monkeypatch.setattr(runtime.chat, "stop", _async_step(calls, "chat.stop"))
    monkeypatch.setattr(runtime.vision, "stop", _async_step(calls, "vision.stop"))
    for name, target in (
        ("chat.start", runtime.chat),
        ("scheduler.start", runtime.scheduler),
        ("proactive.start", runtime.proactive_scheduler),
        ("drift.start", runtime.drift_scheduler),
    ):
        monkeypatch.setattr(target, "start", _async_step(calls, name))

    with pytest.raises(RuntimeError, match="模拟社交学习启动失败") as exc_info:
        await runtime.start()

    assert exc_info.value.__notes__ == ["启动补偿失败阶段：平台插件"]
    assert runtime.workspace_lock.acquired
    cleanup_start = calls.index("social.start") + 1
    assert calls[cleanup_start:] == [
        "plugins.stop",
        "chat.stop",
        "vision.stop",
        "social.stop",
        "memory.stop",
        "managed.stop",
        "detection.close",
        "model.close",
        "weather.close",
        "rss.close",
        "database.stop",
    ]
    runtime.workspace_lock.release()
    assert not {
        "chat.start",
        "scheduler.start",
        "proactive.start",
        "drift.start",
    } & set(calls)


@pytest.mark.asyncio
async def test_repeated_model_reconfigure_closes_each_replaced_provider_once(
    settings: AppSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每次切换都立即关闭上一代，最后一代只在自身关闭时释放。"""

    created: list[_TrackedModelProvider] = []

    def create_provider() -> _TrackedModelProvider:
        provider = _TrackedModelProvider()
        created.append(provider)
        return provider

    monkeypatch.setattr(
        bootstrap_module,
        "FakeModelProvider",
        create_provider,
    )
    runtime = build_runtime(settings)
    for _ in range(3):
        await runtime.reconfigure_model(settings.model)

    assert len(created) == 4
    assert [provider.close_calls for provider in created] == [1, 1, 1, 0]
    assert runtime.retired_models == []

    await runtime.model.close()
    await runtime.profile_model.close()
    assert [provider.close_calls for provider in created] == [1, 1, 1, 1]


@pytest.mark.asyncio
async def test_stop_attempts_every_resource_and_aggregates_failures(
    settings: AppSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单个 stop/close 失败不能短路后续资源，当前检测模型也必须关闭。"""

    model = FakeModelProvider()
    profile_model = FakeModelProvider()
    image_model = FakeImageModelProvider()
    vision_model = FakeModelProvider()
    embedding_model = _EmbeddingProvider()
    runtime = build_runtime(
        settings,
        model_override=model,
        profile_model_override=profile_model,
        image_model_override=image_model,
        vision_model_override=vision_model,
        embedding_override=embedding_model,
    )
    detection_model = FakeModelProvider()
    retired_model = FakeModelProvider()
    runtime.detection_model = detection_model
    # 同一实例可能同时出现在 current/retired 视图中，关闭时必须按身份去重。
    runtime.retired_models.extend((model, retired_model))
    runtime.retired_detection_models.append(detection_model)
    runtime.workspace_lock.acquire()
    calls: list[str] = []

    stop_targets = (
        ("drift.stop", runtime.drift_scheduler),
        ("proactive.stop", runtime.proactive_scheduler),
        ("scheduler.stop", runtime.scheduler),
        ("plugins.stop", runtime.platform_plugins),
        ("chat.stop", runtime.chat),
        ("vision-service.stop", runtime.vision),
        ("social.stop", runtime.social_learning),
        ("managed.stop", runtime.managed_services),
        ("memory.stop", runtime.memory),
    )
    for name, target in stop_targets:
        monkeypatch.setattr(
            target,
            "stop",
            _async_step(
                calls,
                name,
                error=RuntimeError("模拟 Drift 关闭失败")
                if name == "drift.stop"
                else None,
            ),
        )

    monkeypatch.setattr(runtime.rss, "close", _async_step(calls, "rss.close"))
    monkeypatch.setattr(runtime.weather, "close", _async_step(calls, "weather.close"))
    monkeypatch.setattr(
        model,
        "close",
        _async_step(calls, "model.close", error=RuntimeError("模拟模型关闭失败")),
    )
    monkeypatch.setattr(profile_model, "close", _async_step(calls, "profile.close"))
    monkeypatch.setattr(image_model, "close", _async_step(calls, "image.close"))
    monkeypatch.setattr(vision_model, "close", _async_step(calls, "vision-model.close"))
    monkeypatch.setattr(
        detection_model,
        "close",
        _async_step(calls, "detection.close"),
    )
    monkeypatch.setattr(
        embedding_model,
        "close",
        _async_step(calls, "embedding.close"),
    )
    monkeypatch.setattr(retired_model, "close", _async_step(calls, "retired.close"))
    monkeypatch.setattr(runtime.store, "close", _async_step(calls, "database.close"))

    with pytest.raises(RuntimeShutdownError) as exc_info:
        await runtime.stop()

    assert [stage for stage, _ in exc_info.value.failures] == [
        "Drift 调度器",
        "聊天模型",
    ]
    assert calls == [
        "drift.stop",
        "proactive.stop",
        "scheduler.stop",
        "plugins.stop",
        "chat.stop",
        "vision-service.stop",
        "social.stop",
        "managed.stop",
        "memory.stop",
        "rss.close",
        "weather.close",
        "model.close",
        "profile.close",
        "image.close",
        "vision-model.close",
        "detection.close",
        "embedding.close",
        "retired.close",
        "database.close",
    ]
    assert runtime.workspace_lock.acquired
    runtime.workspace_lock.release()


@pytest.mark.asyncio
async def test_stop_retries_failed_writer_before_releasing_workspace_lock(
    settings: AppSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """writer 首次关闭失败时保留 owner，后续成功重试才允许交接工作区。"""

    runtime = build_runtime(settings)
    runtime.workspace_lock.acquire()
    stop_calls = 0

    async def stop_plugins() -> None:
        nonlocal stop_calls
        stop_calls += 1
        if stop_calls == 1:
            raise RuntimeError("模拟平台 writer 关闭失败")

    monkeypatch.setattr(runtime.platform_plugins, "stop", stop_plugins)

    with pytest.raises(RuntimeShutdownError) as exc_info:
        await runtime.stop()

    assert [stage for stage, _ in exc_info.value.failures] == ["平台插件"]
    assert runtime.workspace_lock.acquired
    assert runtime.lifecycle_state == "failed"

    await runtime.stop()
    assert stop_calls == 2
    assert not runtime.workspace_lock.acquired
    assert runtime.lifecycle_state == "stopped"


@pytest.mark.asyncio
async def test_stop_atomically_blocks_reconfigure_and_leaves_no_new_provider(
    settings: AppSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop 快照形成后，竞态配置请求不得创建逃逸 Provider。"""

    created: list[_TrackedModelProvider] = []

    def create_provider() -> _TrackedModelProvider:
        provider = _TrackedModelProvider()
        created.append(provider)
        return provider

    monkeypatch.setattr(bootstrap_module, "FakeModelProvider", create_provider)
    runtime = build_runtime(settings)
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    async def blocking_stop() -> None:
        stop_entered.set()
        await release_stop.wait()

    monkeypatch.setattr(runtime.drift_scheduler, "stop", blocking_stop)
    stop_task = asyncio.create_task(runtime.stop())
    await stop_entered.wait()
    reconfigure_task = asyncio.create_task(
        runtime.reconfigure_model(settings.model)
    )
    await asyncio.sleep(0)

    assert not reconfigure_task.done()
    assert len(created) == 1

    release_stop.set()
    await stop_task
    with pytest.raises(ConflictError, match="不能热切换模型"):
        await reconfigure_task

    assert runtime.lifecycle_state == "stopped"
    assert [provider.close_calls for provider in created] == [1]
    reconfigure_calls = (
        lambda: runtime.reconfigure_model(settings.model),
        lambda: runtime.reconfigure_image_model(settings.image_model),
        lambda: runtime.reconfigure_vision_model(settings.vision_model),
        lambda: runtime.reconfigure_detection_model(
            settings.detection_model
        ),
        lambda: runtime.reconfigure_embedding(settings.embedding),
    )
    for reconfigure in reconfigure_calls:
        with pytest.raises(ConflictError, match="不能热切换模型"):
            await reconfigure()
    assert len(created) == 1


@pytest.mark.asyncio
async def test_model_reconfigure_updates_shared_cross_modal_task_catalog(
    settings: AppSettings,
) -> None:
    """主配置更新后，现存跨模态包装器必须同时看到新精确 profile。"""

    initial_profile = ModelTaskProfile(models=["chat-default"])
    settings.model = settings.model.model_copy(
        update={"task_profiles": {"default": initial_profile}}
    )
    runtime = build_runtime(
        settings,
        image_model_override=FakeImageModelProvider(),
        vision_model_override=FakeModelProvider(),
        embedding_override=_EmbeddingProvider(),
    )
    for provider in (
        runtime.model,
        runtime.image_model,
        runtime.vision_model,
        runtime.embedding,
    ):
        assert isinstance(
            provider,
            (
                ObservedModelProvider,
                ObservedImageModelProvider,
                ObservedEmbeddingProvider,
            ),
        )
        assert provider.task_profiles is runtime.model_task_profiles

    exact_profile = ModelTaskProfile(models=["embedding-special"])
    next_settings = settings.model.model_copy(
        update={
            "task_profiles": {
                "embedding.memory_query": exact_profile,
            }
        }
    )
    await runtime.reconfigure_model(next_settings)

    assert runtime.model_task_profiles == {
        "embedding.memory_query": exact_profile
    }
    for provider in (
        runtime.model,
        runtime.image_model,
        runtime.vision_model,
        runtime.embedding,
    ):
        assert isinstance(
            provider,
            (
                ObservedModelProvider,
                ObservedImageModelProvider,
                ObservedEmbeddingProvider,
            ),
        )
        assert provider.task_profiles == {
            "embedding.memory_query": exact_profile
        }

    await runtime.stop()
