"""应用依赖的唯一装配入口。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from adapters.model import (
    FakeModelProvider,
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleImageProvider,
    OpenAICompatibleProvider,
)
from adapters.persistence import DatabaseStore, PersonaStore
from adapters.persistence.migration import upgrade_database
from adapters.web_simulator import AttachmentStore, WebSimulatorChannel
from application.blacklist import BlacklistService
from application.events import EventHub
from application.expressions import ExpressionService
from application.memory import MemoryService
from application.outbound import OutboundCoordinator
from application.qq_group_management import QQGroupManagementGateway
from application.service import ChatService
from application.social_learning import SocialLearningService
from application.vision import VisionUnderstandingService
from config import (
    AppSettings,
    DetectionModelSettings,
    EmbeddingSettings,
    ImageModelSettings,
    ModelSettings,
    ModelTaskProfile,
    VisionModelSettings,
)
from domain.errors import ConflictError
from observability import (
    LogHub,
    ModelAttemptObserver,
    ModelRequestGate,
    ObservedEmbeddingProvider,
    ObservedImageModelProvider,
    ObservedModelProvider,
    configure_logging,
    pricing_from_settings,
    provider_identity,
    retire_provider,
)
from plugins._host import (
    ChannelCapabilityRegistry,
    ChannelPluginManager,
    ChannelRouter,
    ManagedServiceManager,
    PlatformIngress,
    PluginContributionCatalog,
)
from ports import EmbeddingProvider, ImageModelProvider, ModelProvider
from proactive import (
    DriftScheduler,
    EngagementService,
    ProactiveScheduler,
    RssFeedClient,
)
from proactive.rss import CandidateSource
from prompting import PromptAssembler
from scheduling import ScheduleService
from scheduling.scheduler import ScheduleScheduler
from skill_runtime import SkillCatalog, SkillPluginManager
from tools import (
    ToolRegistry,
    WeatherClient,
    build_feed_tools,
    build_history_tools,
    build_information_tools,
    build_memory_tools,
    build_messaging_tools,
    build_profile_tools,
    build_schedule_tools,
    build_skill_tools,
)
from workspace_lock import WorkspaceLock, WorkspaceLockError

logger = logging.getLogger(__name__)


class RuntimeShutdownError(RuntimeError):
    """Runtime 已尝试全部关闭步骤，但仍有一个或多个资源关闭失败。"""

    code = "runtime_shutdown_failed"

    def __init__(self, failures: list[tuple[str, Exception]]) -> None:
        self.failures = tuple(failures)
        stages = "、".join(stage for stage, _ in failures)
        super().__init__(f"iJA Runtime 关闭失败（{len(failures)} 项）：{stages}")


class _AsyncClosable(Protocol):
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _LifecycleStep:
    """一个有名称、可独立失败的异步生命周期清理步骤。"""

    name: str
    callback: Callable[[], Awaitable[None]]
    blocks_lock_handoff: bool = False


def _observe_model(
    provider: ModelProvider,
    *,
    observer: ModelAttemptObserver,
    settings: ModelSettings,
    profile: str,
    default_task: str,
    retry: bool,
    task_profiles: Mapping[str, ModelTaskProfile] | None = None,
    request_gate: ModelRequestGate | None = None,
) -> ObservedModelProvider:
    provider_name, model_name = provider_identity(
        provider,
        fallback_model=settings.name or "fake",
    )
    return ObservedModelProvider(
        provider,
        observer=observer,
        provider_name=provider_name,
        profile=profile,
        model=model_name,
        default_task=default_task,
        pricing=pricing_from_settings(settings),
        max_attempts=2 if retry else 1,
        task_profiles=(
            task_profiles
            if task_profiles is not None
            else settings.task_profiles
        ),
        default_hard_timeout_seconds=settings.timeout_seconds,
        request_gate=request_gate,
    )


def _observe_image_model(
    provider: ImageModelProvider,
    *,
    observer: ModelAttemptObserver,
    settings: ImageModelSettings,
    task_profiles: Mapping[str, ModelTaskProfile],
) -> ObservedImageModelProvider:
    provider_name, model_name = provider_identity(
        provider,
        fallback_model=settings.name or "image",
    )
    return ObservedImageModelProvider(
        provider,
        observer=observer,
        provider_name=provider_name,
        model=model_name,
        pricing=pricing_from_settings(settings),
        task_profiles=task_profiles,
        default_hard_timeout_seconds=settings.timeout_seconds,
    )


def _observe_embedding(
    provider: EmbeddingProvider,
    *,
    observer: ModelAttemptObserver,
    settings: EmbeddingSettings,
    retry: bool,
    task_profiles: Mapping[str, ModelTaskProfile],
) -> ObservedEmbeddingProvider:
    provider_name, model_name = provider_identity(
        provider,
        fallback_model=settings.name or "embedding",
    )
    return ObservedEmbeddingProvider(
        provider,
        observer=observer,
        provider_name=provider_name,
        model=model_name,
        pricing=pricing_from_settings(settings),
        max_attempts=2 if retry else 1,
        task_profiles=task_profiles,
        default_hard_timeout_seconds=settings.timeout_seconds,
    )


def _detection_observation_settings(
    settings: DetectionModelSettings,
    *,
    task_profiles: Mapping[str, ModelTaskProfile],
) -> ModelSettings:
    """把独立检测配置投影为无工具权限的任务路由配置。"""

    return ModelSettings(
        mode="openai",
        base_url=settings.base_url,
        api_key=settings.api_key,
        name=settings.name,
        timeout_seconds=settings.timeout_seconds,
        max_tokens=200,
        temperature=0,
        supports_json_object=True,
        supports_tools=False,
        task_profiles=dict(task_profiles),
        input_price_usd_per_million_tokens=(
            settings.input_price_usd_per_million_tokens
        ),
        output_price_usd_per_million_tokens=(
            settings.output_price_usd_per_million_tokens
        ),
    )


@dataclass(slots=True)
class Runtime:
    """显式持有进程级资源及其关闭顺序。"""

    settings: AppSettings
    workspace_lock: WorkspaceLock
    store: DatabaseStore
    model_attempt_observer: ModelAttemptObserver
    model_task_profiles: dict[str, ModelTaskProfile]
    model_request_gate: ModelRequestGate
    model: ModelProvider
    profile_model: ModelProvider
    image_model: ImageModelProvider | None
    vision_model: ModelProvider | None
    detection_model: ModelProvider | None
    embedding: EmbeddingProvider | None
    channel: ChannelRouter
    platform_plugins: ChannelPluginManager
    channel_capabilities: ChannelCapabilityRegistry
    managed_services: ManagedServiceManager
    attachments: AttachmentStore
    personas: PersonaStore
    expressions: ExpressionService
    vision: VisionUnderstandingService
    blacklist: BlacklistService
    qq_group_management: QQGroupManagementGateway
    outbound: OutboundCoordinator
    memory: MemoryService
    social_learning: SocialLearningService
    skills: SkillCatalog
    skill_plugins: SkillPluginManager
    events: EventHub
    log_hub: LogHub
    chat: ChatService
    schedules: ScheduleService
    scheduler: ScheduleScheduler
    engagement: EngagementService
    proactive_scheduler: ProactiveScheduler
    drift_scheduler: DriftScheduler
    rss: CandidateSource
    weather: WeatherClient
    retired_models: list[ModelProvider]
    retired_image_models: list[ImageModelProvider]
    retired_vision_models: list[ModelProvider]
    retired_detection_models: list[ModelProvider]
    retired_embeddings: list[EmbeddingProvider]
    model_config_lock: asyncio.Lock
    image_model_config_lock: asyncio.Lock
    vision_model_config_lock: asyncio.Lock
    detection_model_config_lock: asyncio.Lock
    embedding_config_lock: asyncio.Lock
    platform_plugin_config_lock: asyncio.Lock
    persona_lock: asyncio.Lock
    provider_retirement_tasks: set[asyncio.Task[None]] = field(
        init=False,
        default_factory=set,
    )
    lifecycle_lock: asyncio.Lock = field(
        init=False,
        default_factory=asyncio.Lock,
    )
    lifecycle_state: str = field(init=False, default="created")

    async def start(self) -> None:
        """串行化启动与停止/热重配，避免生命周期快照被并发改写。"""

        async with self.lifecycle_lock:
            if self.lifecycle_state != "created":
                raise ConflictError(
                    f"Runtime 当前状态不允许启动：{self.lifecycle_state}"
                )
            await self._start_unlocked()

    async def _start_unlocked(self) -> None:
        self.lifecycle_state = "starting"
        try:
            self.workspace_lock.acquire()
        except WorkspaceLockError:
            # 锁竞争表示当前实例从未成为 workspace owner，可在原 owner 退出后
            # 使用同一 Runtime 安全重试，而不是把瞬时占用固化为内部失败。
            self.lifecycle_state = "created"
            raise
        except BaseException:
            self.lifecycle_state = "failed"
            raise
        compensations = [
            _LifecycleStep("工作区锁", self._release_workspace_lock),
            _LifecycleStep("数据库", self.store.close),
            *self._closable_resource_steps(),
        ]
        try:
            logger.info(
                "iJA 服务启动",
                extra={
                    "session_id": "-",
                    "turn_id": "-",
                    "model_mode": self.settings.model.mode,
                    "supports_tools": self.settings.model.supports_tools,
                },
            )
            self.personas.initialize()
            await asyncio.to_thread(
                upgrade_database,
                self.settings.project_root,
                self.settings.storage.database_path,
            )
            await self.store.initialize()
            await self._start_stage(
                compensations,
                name="受管服务",
                start=self.managed_services.start,
                stop=self.managed_services.stop,
                blocks_lock_handoff=True,
            )
            # 平台一旦 activate 就可能收到入站事件；先登记所有内部任务 owner 的
            # 逆操作，即使它们自己的 start 尚未完成，也能清理由 ingress 触发的任务。
            compensations.extend(
                (
                    _LifecycleStep("长期记忆", self.memory.stop, True),
                    _LifecycleStep("社交学习", self.social_learning.stop, True),
                    _LifecycleStep("视觉理解", self.vision.stop, True),
                    _LifecycleStep("响应式聊天", self.chat.stop, True),
                )
            )
            await self._start_stage(
                compensations,
                name="平台插件",
                start=self.platform_plugins.start,
                stop=self.platform_plugins.stop,
                blocks_lock_handoff=True,
            )
            await self.outbound.recover()
            await self.memory.start()
            await self.social_learning.start()
            await self.chat.start()
            await self._start_stage(
                compensations,
                name="周期任务调度器",
                start=self.scheduler.start,
                stop=self.scheduler.stop,
                blocks_lock_handoff=True,
            )
            await self._start_stage(
                compensations,
                name="Proactive 调度器",
                start=self.proactive_scheduler.start,
                stop=self.proactive_scheduler.stop,
                blocks_lock_handoff=True,
            )
            await self._start_stage(
                compensations,
                name="Drift 调度器",
                start=self.drift_scheduler.start,
                stop=self.drift_scheduler.stop,
                blocks_lock_handoff=True,
            )
            logger.info(
                "iJA 服务就绪",
                extra={
                    "session_id": "-",
                    "turn_id": "-",
                    "host": self.settings.server.host,
                    "port": self.settings.server.port,
                },
            )
            self.lifecycle_state = "ready"
        except BaseException as startup_error:
            self.lifecycle_state = "failed"
            failures = await self._run_lifecycle_steps(
                compensations,
                phase="启动补偿",
                reverse=True,
            )
            if failures:
                logger.error(
                    "iJA 启动失败后的资源补偿不完整",
                    extra={
                        "session_id": "-",
                        "turn_id": "-",
                        "failure_count": len(failures),
                        "failed_stages": [stage for stage, _ in failures],
                    },
                )
                startup_error.add_note("启动补偿失败阶段：" + "、".join(stage for stage, _ in failures))
            raise

    async def stop(self) -> None:
        """冻结热重配后关闭同一份资源快照；重复停止保持幂等。"""

        async with self.lifecycle_lock:
            if self.lifecycle_state == "stopped":
                return
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        self.lifecycle_state = "stopping"
        logger.info("iJA 服务停止中", extra={"session_id": "-", "turn_id": "-"})
        steps = [
            _LifecycleStep("Drift 调度器", self.drift_scheduler.stop, True),
            _LifecycleStep("Proactive 调度器", self.proactive_scheduler.stop, True),
            _LifecycleStep("周期任务调度器", self.scheduler.stop, True),
            # 先冻结外部 ingress，再取消响应式 Turn 和内部后台任务。
            _LifecycleStep("平台插件", self.platform_plugins.stop, True),
            _LifecycleStep("响应式聊天", self.chat.stop, True),
            _LifecycleStep("视觉理解", self.vision.stop, True),
            _LifecycleStep("社交学习", self.social_learning.stop, True),
            _LifecycleStep("受管服务", self.managed_services.stop, True),
            _LifecycleStep("长期记忆", self.memory.stop, True),
            _LifecycleStep(
                "退役 Provider 排空",
                self._drain_provider_retirements,
            ),
            *self._closable_resource_steps(),
            _LifecycleStep("数据库", self.store.close),
            _LifecycleStep("工作区锁", self._release_workspace_lock),
        ]
        failures = await self._run_lifecycle_steps(steps, phase="停止")
        if failures:
            self.lifecycle_state = "failed"
            logger.error(
                "iJA 服务停止完成，但部分资源关闭失败",
                extra={
                    "session_id": "-",
                    "turn_id": "-",
                    "failure_count": len(failures),
                    "failed_stages": [stage for stage, _ in failures],
                },
            )
            raise RuntimeShutdownError(failures)
        self.lifecycle_state = "stopped"
        logger.info("iJA 服务已停止", extra={"session_id": "-", "turn_id": "-"})

    async def readiness_snapshot(self) -> dict[str, object]:
        """返回不含路径、插件身份和异常正文的运行依赖快照。"""

        runtime_ready = self.lifecycle_state == "ready"
        database_ready = False
        if runtime_ready:
            try:
                database_ready = await self.store.readiness_check()
            except Exception:
                logger.warning(
                    "readiness 数据库检查失败",
                    exc_info=True,
                    extra={
                        "session_id": "-",
                        "turn_id": "-",
                        "operation": "readiness",
                    },
                )
        generation_status = self.platform_plugins.generation_status()
        raw_generations = generation_status["generations"]
        generations = raw_generations if isinstance(raw_generations, list) else []
        current_generation = next(
            (
                generation
                for generation in generations
                if isinstance(generation, dict)
                and generation.get("current") is True
            ),
            None,
        )
        plugin_generation_ready = bool(
            isinstance(current_generation, dict)
            and current_generation.get("running") is True
            and current_generation.get("ready") is True
            and current_generation.get("accepting") is True
        )
        checks = {
            "runtime_state": runtime_ready,
            "workspace_lock": self.workspace_lock.acquired,
            "database": database_ready,
            "schedule_scheduler": self.scheduler.running,
            "proactive_scheduler": self.proactive_scheduler.running,
            "drift_scheduler": self.drift_scheduler.running,
            "managed_services": self.managed_services.ready,
            "plugin_generation": plugin_generation_ready,
        }
        return {
            "ready": all(checks.values()),
            "state": self.lifecycle_state,
            "checks": checks,
            "current_plugin_generation": generation_status[
                "current_generation"
            ],
        }

    async def _start_stage(
        self,
        compensations: list[_LifecycleStep],
        *,
        name: str,
        start: Callable[[], Awaitable[None]],
        stop: Callable[[], Awaitable[None]],
        blocks_lock_handoff: bool = False,
    ) -> None:
        """在启动前登记逆操作，使阶段自身部分失败时也会尝试清理。"""

        compensations.append(
            _LifecycleStep(
                name,
                stop,
                blocks_lock_handoff=blocks_lock_handoff,
            )
        )
        await start()

    async def _run_lifecycle_steps(
        self,
        steps: list[_LifecycleStep],
        *,
        phase: str,
        reverse: bool = False,
    ) -> list[tuple[str, Exception]]:
        """逐一执行清理；一个步骤失败不能阻止后续资源获得关闭机会。"""

        failures: list[tuple[str, Exception]] = []
        lock_handoff_blocked = False
        ordered_steps = reversed(steps) if reverse else iter(steps)
        for step in ordered_steps:
            if step.name == "工作区锁" and lock_handoff_blocked:
                logger.error(
                    "关键 writer 未完整停止，保留工作区锁等待后续清理重试",
                    extra={
                        "session_id": "-",
                        "turn_id": "-",
                        "lifecycle_phase": phase,
                    },
                )
                continue
            try:
                await step.callback()
            except Exception as exc:
                failures.append((step.name, exc))
                lock_handoff_blocked = (
                    lock_handoff_blocked or step.blocks_lock_handoff
                )
                logger.exception(
                    "Runtime 生命周期步骤失败",
                    extra={
                        "session_id": "-",
                        "turn_id": "-",
                        "lifecycle_phase": phase,
                        "resource_stage": step.name,
                        "error_type": type(exc).__name__,
                    },
                )
        return failures

    async def _release_workspace_lock(self) -> None:
        self.workspace_lock.release()

    async def _drain_provider_retirements(self) -> None:
        """停止前等待被取消配置请求留下的后台排空任务。"""

        while self.provider_retirement_tasks:
            pending = tuple(self.provider_retirement_tasks)
            await asyncio.gather(
                *(asyncio.shield(task) for task in pending),
                return_exceptions=True,
            )
            self.provider_retirement_tasks.difference_update(
                task for task in pending if task.done()
            )

    def _closable_resource_steps(self) -> list[_LifecycleStep]:
        """返回按实例身份去重的网络客户端与模型 Provider 关闭步骤。"""

        steps: list[_LifecycleStep] = []
        seen: set[int] = set()

        def add(name: str, resource: _AsyncClosable | None) -> None:
            if resource is None:
                return
            identity = getattr(resource, "wrapped_provider", resource)
            if id(identity) in seen:
                return
            seen.add(id(identity))
            steps.append(_LifecycleStep(name, resource.close))

        add("RSS 客户端", self.rss)
        add("天气客户端", self.weather)
        add("聊天模型", self.model)
        add("画像模型", self.profile_model)
        add("图片模型", self.image_model)
        add("视觉模型", self.vision_model)
        add("检测模型", self.detection_model)
        add("Embedding 模型", self.embedding)
        for index, model in enumerate(self.retired_models, start=1):
            add(f"退役聊天模型 #{index}", model)
        for index, model in enumerate(self.retired_image_models, start=1):
            add(f"退役图片模型 #{index}", model)
        for index, model in enumerate(self.retired_vision_models, start=1):
            add(f"退役视觉模型 #{index}", model)
        for index, model in enumerate(self.retired_detection_models, start=1):
            add(f"退役检测模型 #{index}", model)
        for index, embedding in enumerate(self.retired_embeddings, start=1):
            add(f"退役 Embedding 模型 #{index}", embedding)
        return steps

    async def _retire_replaced(
        self,
        resources: Iterable[_AsyncClosable | None],
        *,
        resource_kind: str,
    ) -> None:
        """关闭已从所有 owner 解绑的旧 Provider，不让关闭失败回滚新配置。"""

        seen: set[int] = set()
        retirement_tasks: list[asyncio.Task[None]] = []
        for resource in resources:
            if resource is None:
                continue
            identity = getattr(resource, "wrapped_provider", resource)
            if id(identity) in seen:
                continue
            seen.add(id(identity))
            task = asyncio.create_task(retire_provider(resource))
            self.provider_retirement_tasks.add(task)

            def retirement_done(
                completed: asyncio.Task[None],
                *,
                retired_identity: object = identity,
            ) -> None:
                self.provider_retirement_tasks.discard(completed)
                try:
                    completed.result()
                except asyncio.CancelledError:
                    logger.error(
                        "退役 Provider 关闭任务被取消",
                        extra={
                            "session_id": "-",
                            "turn_id": "-",
                            "resource_kind": resource_kind,
                            "provider_type": type(
                                retired_identity
                            ).__name__,
                            "error_type": "provider_retirement_cancelled",
                        },
                    )
                except Exception as exc:
                    logger.error(
                        "退役 Provider 关闭失败",
                        exc_info=(type(exc), exc, exc.__traceback__),
                        extra={
                            "session_id": "-",
                            "turn_id": "-",
                            "resource_kind": resource_kind,
                            "provider_type": type(
                                retired_identity
                            ).__name__,
                            "error_type": (
                                "provider_retirement_close_error"
                            ),
                        },
                    )

            task.add_done_callback(retirement_done)
            retirement_tasks.append(task)
        if not retirement_tasks:
            return
        # 配置请求取消时，所有已经解绑的旧资源仍必须独立排空。
        await asyncio.gather(
            *(asyncio.shield(task) for task in retirement_tasks),
            return_exceptions=True,
        )

    def _ensure_reconfiguration_allowed(self) -> None:
        """只允许未启动或已就绪 Runtime 接受新的 Provider 配置。"""

        if self.lifecycle_state not in {"created", "ready"}:
            raise ConflictError(
                "Runtime 正在启动、停止或已经失效，不能热切换模型"
            )

    async def reconfigure_model(self, model_settings: ModelSettings) -> None:
        """在统一生命周期门禁内原子切换聊天与画像 Provider。"""

        async with self.lifecycle_lock:
            self._ensure_reconfiguration_allowed()
            await self._reconfigure_model_unlocked(model_settings)

    async def _reconfigure_model_unlocked(
        self,
        model_settings: ModelSettings,
    ) -> None:
        """原子切换入口；旧 Provider 等待在途 lease 归零后立即关闭。"""

        next_model_request_gate = ModelRequestGate(
            max_concurrency=model_settings.max_concurrency,
            min_interval_seconds=model_settings.min_request_interval_seconds,
        )
        raw_next_model: ModelProvider = (
            FakeModelProvider()
            if model_settings.mode == "fake"
            else OpenAICompatibleProvider(model_settings, max_attempts=1)
        )
        next_model: ModelProvider = _observe_model(
            raw_next_model,
            observer=self.model_attempt_observer,
            settings=model_settings,
            profile="chat",
            default_task="chat.reply",
            retry=model_settings.mode == "openai",
            task_profiles=self.model_task_profiles,
            request_gate=next_model_request_gate,
        )
        next_profile_model: ModelProvider = (
            next_model
            if model_settings.mode == "fake"
            else _observe_model(
                OpenAICompatibleProvider(
                    model_settings.for_profile(),
                    max_attempts=1,
                ),
                observer=self.model_attempt_observer,
                settings=model_settings.for_profile(),
                profile="profile",
                default_task="profile.extract",
                retry=True,
                task_profiles=self.model_task_profiles,
                request_gate=next_model_request_gate,
            )
        )
        previous = [self.model, self.profile_model]
        # 所有模态共享同一份只读路由快照；原地替换保证图片、视觉、
        # 检测和 Embedding 不会继续使用旧 task profile。
        self.model_task_profiles.clear()
        self.model_task_profiles.update(model_settings.task_profiles)
        self.model_request_gate = next_model_request_gate
        self.model = next_model
        self.profile_model = next_profile_model
        self.settings.model = model_settings
        self.chat.model = next_model
        self.chat.profile_model = next_profile_model
        self.chat.settings = self.settings
        self.engagement.set_model(next_model)
        self.engagement.settings = self.settings
        self.memory.set_model(next_profile_model)
        self.memory.settings = self.settings
        self.social_learning.set_model(next_profile_model)
        self.social_learning.settings = self.settings
        self.vision.set_main_model(next_model)
        await self._retire_replaced(previous, resource_kind="text")

    async def reconfigure_image_model(self, settings: ImageModelSettings) -> None:
        """在统一生命周期门禁内切换图片 Provider。"""

        async with self.lifecycle_lock:
            self._ensure_reconfiguration_allowed()
            await self._reconfigure_image_model_unlocked(settings)

    async def _reconfigure_image_model_unlocked(
        self,
        settings: ImageModelSettings,
    ) -> None:
        """切换后续表情生成使用的图片 Provider。"""

        next_model: ImageModelProvider | None = (
            _observe_image_model(
                OpenAICompatibleImageProvider(settings),
                observer=self.model_attempt_observer,
                settings=settings,
                task_profiles=self.model_task_profiles,
            )
            if settings.enabled
            else None
        )
        previous = self.image_model
        self.image_model = next_model
        self.settings.image_model = settings
        self.expressions.set_image_provider(next_model)
        await self._retire_replaced([previous], resource_kind="image")

    async def reconfigure_vision_model(self, settings: VisionModelSettings) -> None:
        """在统一生命周期门禁内切换视觉 Provider。"""

        async with self.lifecycle_lock:
            self._ensure_reconfiguration_allowed()
            await self._reconfigure_vision_model_unlocked(settings)

    async def _reconfigure_vision_model_unlocked(
        self,
        settings: VisionModelSettings,
    ) -> None:
        """切换图片理解 Provider；主模型模式不创建额外连接。"""

        observation_settings = (
            settings.as_model_settings(
                task_profiles=self.model_task_profiles,
            )
            if settings.mode == "external"
            else None
        )
        next_model: ModelProvider | None = (
            _observe_model(
                OpenAICompatibleProvider(
                    observation_settings,
                    max_attempts=1,
                ),
                observer=self.model_attempt_observer,
                settings=observation_settings,
                profile="vision",
                default_task="vision.analyze",
                retry=True,
                task_profiles=self.model_task_profiles,
            )
            if observation_settings is not None
            else None
        )
        previous = self.vision_model
        self.vision_model = next_model
        self.settings.vision_model = settings
        self.vision.settings = self.settings
        self.vision.set_external_model(next_model)
        await self._retire_replaced([previous], resource_kind="vision")

    async def reconfigure_detection_model(self, settings: DetectionModelSettings) -> None:
        """在统一生命周期门禁内切换检测 Provider。"""

        async with self.lifecycle_lock:
            self._ensure_reconfiguration_allowed()
            await self._reconfigure_detection_model_unlocked(settings)

    async def _reconfigure_detection_model_unlocked(
        self,
        settings: DetectionModelSettings,
    ) -> None:
        """切换后续输出过滤确认使用的检测模型 Provider。"""

        observation_settings = (
            _detection_observation_settings(
                settings,
                task_profiles=self.model_task_profiles,
            )
            if settings.enabled
            else None
        )
        next_model: ModelProvider | None = (
            _observe_model(
                OpenAICompatibleProvider(
                    observation_settings,
                    max_attempts=1,
                ),
                observer=self.model_attempt_observer,
                settings=observation_settings,
                profile="detection",
                default_task="detection.check",
                retry=True,
                task_profiles=self.model_task_profiles,
            )
            if observation_settings is not None
            else None
        )
        previous = self.detection_model
        self.detection_model = next_model
        self.settings.detection_model = settings
        self.platform_plugins.set_detection_model(next_model)
        await self._retire_replaced([previous], resource_kind="detection")

    async def reconfigure_embedding(self, settings: EmbeddingSettings) -> None:
        """在统一生命周期门禁内切换 Embedding Provider。"""

        async with self.lifecycle_lock:
            self._ensure_reconfiguration_allowed()
            await self._reconfigure_embedding_unlocked(settings)

    async def _reconfigure_embedding_unlocked(
        self,
        settings: EmbeddingSettings,
    ) -> None:
        """切换独立 embedding Provider，并等待旧请求完成后关闭。"""

        next_embedding: EmbeddingProvider | None = (
            _observe_embedding(
                OpenAICompatibleEmbeddingProvider(settings),
                observer=self.model_attempt_observer,
                settings=settings,
                retry=True,
                task_profiles=self.model_task_profiles,
            )
            if settings.available
            else None
        )
        previous = self.embedding
        self.embedding = next_embedding
        self.settings.embedding = settings
        self.memory.set_embedding_provider(next_embedding)
        self.social_learning.set_embedding_provider(next_embedding)
        await self._retire_replaced([previous], resource_kind="embedding")


def build_runtime(
    settings: AppSettings,
    model_override: ModelProvider | None = None,
    profile_model_override: ModelProvider | None = None,
    image_model_override: ImageModelProvider | None = None,
    vision_model_override: ModelProvider | None = None,
    embedding_override: EmbeddingProvider | None = None,
    rss_override: CandidateSource | None = None,
) -> Runtime:
    """根据已校验配置构造完整运行时。"""

    events = EventHub(
        history_capacity=settings.server.event_history_capacity,
        subscriber_queue_capacity=settings.server.event_subscriber_queue_capacity,
    )
    log_hub = LogHub(events)
    configure_logging(settings.storage.data_dir, log_hub)
    store = DatabaseStore(settings.storage.database_path)
    model_attempt_observer = ModelAttemptObserver(store)
    model_task_profiles = dict(settings.model.task_profiles)
    model_request_gate = ModelRequestGate(
        max_concurrency=settings.model.max_concurrency,
        min_interval_seconds=settings.model.min_request_interval_seconds,
    )
    raw_model = model_override or (
        FakeModelProvider()
        if settings.model.mode == "fake"
        else OpenAICompatibleProvider(settings.model, max_attempts=1)
    )
    model: ModelProvider = _observe_model(
        raw_model,
        observer=model_attempt_observer,
        settings=settings.model,
        profile="chat",
        default_task="chat.reply",
        retry=model_override is None and settings.model.mode == "openai",
        task_profiles=model_task_profiles,
        request_gate=model_request_gate,
    )
    raw_profile_model = profile_model_override or (
        raw_model
        if model_override is not None or settings.model.mode == "fake"
        else OpenAICompatibleProvider(
            settings.model.for_profile(),
            max_attempts=1,
        )
    )
    profile_model: ModelProvider = (
        model
        if raw_profile_model is raw_model
        else _observe_model(
            raw_profile_model,
            observer=model_attempt_observer,
            settings=settings.model.for_profile(),
            profile="profile",
            default_task="profile.extract",
            retry=profile_model_override is None,
            task_profiles=model_task_profiles,
            request_gate=model_request_gate,
        )
    )
    raw_image_model = image_model_override or (
        OpenAICompatibleImageProvider(settings.image_model)
        if settings.image_model.enabled
        else None
    )
    image_model: ImageModelProvider | None = (
        _observe_image_model(
            raw_image_model,
            observer=model_attempt_observer,
            settings=settings.image_model,
            task_profiles=model_task_profiles,
        )
        if raw_image_model is not None
        else None
    )
    if settings.detection_model.enabled:
        detection_observation_settings = _detection_observation_settings(
            settings.detection_model,
            task_profiles=model_task_profiles,
        )
        raw_detection_model = OpenAICompatibleProvider(
            detection_observation_settings,
            max_attempts=1,
        )
        detection_model: ModelProvider | None = _observe_model(
            raw_detection_model,
            observer=model_attempt_observer,
            settings=detection_observation_settings,
            profile="detection",
            default_task="detection.check",
            retry=True,
            task_profiles=model_task_profiles,
        )
    else:
        detection_model = None
    raw_embedding = embedding_override or (
        OpenAICompatibleEmbeddingProvider(settings.embedding)
        if settings.embedding.available
        else None
    )
    embedding: EmbeddingProvider | None = (
        _observe_embedding(
            raw_embedding,
            observer=model_attempt_observer,
            settings=settings.embedding,
            retry=embedding_override is None,
            task_profiles=model_task_profiles,
        )
        if raw_embedding is not None
        else None
    )
    channel = ChannelRouter()
    channel.register("web-simulator", "ija-local", WebSimulatorChannel())
    attachments = AttachmentStore(
        uploads_root=settings.storage.data_dir / "uploads",
        personas_root=settings.storage.data_dir / "personas",
        media_root=settings.storage.data_dir / "media",
        max_bytes=settings.chat.max_attachment_bytes,
    )
    personas = PersonaStore(
        project_root=settings.project_root,
        data_root=settings.storage.data_dir,
        active_character_id=settings.persona.active_character_id,
    )
    plugin_contributions = PluginContributionCatalog(
        settings.project_root,
        include_bundled=True,
    )
    channel_capabilities = plugin_contributions.channel_capabilities
    prompting = PromptAssembler(
        settings.project_root / "prompts",
        image_reader=attachments.read_image_ref,
        channel_capabilities=channel_capabilities,
    )
    skills = SkillCatalog(
        [
            settings.project_root / "skills",
            *plugin_contributions.skill_roots,
        ],
        required_scopes=plugin_contributions.skill_required_scopes,
    )
    weather = WeatherClient(settings)
    schedules = ScheduleService(settings, store, events)
    memory = MemoryService(
        settings=settings,
        store=store,
        model=profile_model,
        embedding=embedding,
        prompting=prompting,
        events=events,
    )
    external_vision_settings = (
        settings.vision_model.as_model_settings(
            task_profiles=model_task_profiles,
        )
        if settings.vision_model.mode == "external"
        else None
    )
    vision_observation_settings = external_vision_settings or settings.model
    raw_vision_model = vision_model_override or (
        OpenAICompatibleProvider(
            external_vision_settings,
            max_attempts=1,
        )
        if external_vision_settings is not None
        else None
    )
    vision_model: ModelProvider | None = (
        _observe_model(
            raw_vision_model,
            observer=model_attempt_observer,
            settings=vision_observation_settings,
            profile="vision",
            default_task="vision.analyze",
            retry=vision_model_override is None,
            task_profiles=model_task_profiles,
        )
        if raw_vision_model is not None
        else None
    )
    social_learning = SocialLearningService(
        settings=settings,
        store=store,
        model=profile_model,
        embedding=embedding,
        prompting=prompting,
        events=events,
    )
    persona_lock = asyncio.Lock()
    expressions = ExpressionService(
        store=store,
        personas=personas,
        attachments=attachments,
        image_provider=image_model,
        persona_lock=persona_lock,
        selection_settings=settings.expression_selection,
    )
    vision = VisionUnderstandingService(
        settings=settings,
        store=store,
        attachments=attachments,
        main_model=model,
        external_model=vision_model,
        expressions=expressions,
    )
    expressions.set_visual_selector(vision)
    blacklist = BlacklistService(store=store, events=events)
    qq_group_management = QQGroupManagementGateway(store=store, channel=channel)
    managed_services = ManagedServiceManager(plugin_contributions.managed_services)
    skill_plugins = SkillPluginManager(
        skills,
        capabilities={
            "expression-library": expressions,
            "local-blacklist": blacklist,
            "qq-group-management": qq_group_management,
            **managed_services.capabilities,
        },
    )
    tool_registry = ToolRegistry(build_information_tools(weather))
    for tool in build_messaging_tools(store, attachments):
        tool_registry.register(tool)
    for tool in build_schedule_tools(schedules):
        tool_registry.register(tool)
    for tool in build_memory_tools(memory):
        tool_registry.register(tool)
    for tool in build_history_tools(store):
        tool_registry.register(tool)
    for tool in build_profile_tools(store):
        tool_registry.register(tool)
    for tool in build_skill_tools(skills, skill_plugins):
        tool_registry.register(tool)
    outbound = OutboundCoordinator(
        store=store,
        channel=channel,
        personas=personas,
        events=events,
    )
    chat = ChatService(
        settings=settings,
        store=store,
        model=model,
        profile_model=profile_model,
        channel=channel,
        attachment_validator=attachments,
        events=events,
        tool_registry=tool_registry,
        schedule_service=schedules,
        personas=personas,
        prompting=prompting,
        expressions=expressions,
        vision=vision,
        memory=memory,
        social_learning=social_learning,
        outbound=outbound,
        skills=skills,
        skill_plugins=skill_plugins,
        channel_capabilities=channel_capabilities,
    )
    platform_ingress = PlatformIngress(store, chat, channel_capabilities)
    platform_plugins = ChannelPluginManager(
        project_root=settings.project_root,
        enabled=settings.platform_plugins.enabled,
        disabled=settings.platform_plugins.disabled,
        options=settings.platform_plugins.options,
        router=channel,
        ingress=platform_ingress,
        store=store,
        image_store=attachments.save_image,
        detection_model=detection_model,
        catalog=plugin_contributions,
        capability_registry=channel_capabilities,
    )
    chat.set_egress_filter(platform_plugins.egress_filter)
    scheduler = ScheduleScheduler(
        settings=settings,
        store=store,
        chat=chat,
        events=events,
    )
    rss: CandidateSource = rss_override or RssFeedClient(
        timeout_seconds=settings.proactive.feed_timeout_seconds,
        max_response_bytes=settings.proactive.feed_max_response_bytes,
        max_redirects=settings.proactive.feed_max_redirects,
    )
    engagement = EngagementService(
        settings=settings,
        store=store,
        model=model,
        personas=personas,
        prompting=prompting,
        outbound=outbound,
        events=events,
        rss=rss,
        session_lock=chat.session_lock,
        memory=memory,
    )
    engagement.set_egress_filter(platform_plugins.egress_filter)
    proactive_scheduler = ProactiveScheduler(settings, engagement)
    drift_scheduler = DriftScheduler(settings, engagement)
    for tool in build_feed_tools(engagement, store, proactive_scheduler.wake):
        tool_registry.register(tool)
    schedules.set_change_notifier(scheduler.wake)
    return Runtime(
        settings=settings,
        workspace_lock=WorkspaceLock(settings.storage.data_dir),
        store=store,
        model_attempt_observer=model_attempt_observer,
        model_task_profiles=model_task_profiles,
        model_request_gate=model_request_gate,
        model=model,
        profile_model=profile_model,
        image_model=image_model,
        vision_model=vision_model,
        detection_model=detection_model,
        embedding=embedding,
        channel=channel,
        platform_plugins=platform_plugins,
        channel_capabilities=channel_capabilities,
        managed_services=managed_services,
        attachments=attachments,
        personas=personas,
        expressions=expressions,
        vision=vision,
        blacklist=blacklist,
        qq_group_management=qq_group_management,
        outbound=outbound,
        memory=memory,
        social_learning=social_learning,
        skills=skills,
        skill_plugins=skill_plugins,
        events=events,
        log_hub=log_hub,
        chat=chat,
        schedules=schedules,
        scheduler=scheduler,
        engagement=engagement,
        proactive_scheduler=proactive_scheduler,
        drift_scheduler=drift_scheduler,
        rss=rss,
        weather=weather,
        retired_models=[],
        retired_image_models=[],
        retired_vision_models=[],
        retired_detection_models=[],
        retired_embeddings=[],
        model_config_lock=asyncio.Lock(),
        image_model_config_lock=asyncio.Lock(),
        vision_model_config_lock=asyncio.Lock(),
        detection_model_config_lock=asyncio.Lock(),
        embedding_config_lock=asyncio.Lock(),
        platform_plugin_config_lock=asyncio.Lock(),
        persona_lock=persona_lock,
    )
