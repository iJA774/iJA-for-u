"""按插件清单策略发现、装载并隔离运行时插件。"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import math
import re
import sys
import types
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from adapters.persistence import DatabaseStore
from domain.errors import NotFoundError
from domain.models import DeliveryReceipt, MessageComponent, OutboundMessage, SessionView
from ports import ChannelAdapter, ChannelRuntimeContext, ModelProvider
from ports.egress import EgressEnvelope, EgressHandler, EgressPlugin, EgressPluginContext
from ports.tool_guard import ToolGuardPlugin, ToolGuardPluginContext

from .capabilities import ChannelCapabilityRegistry
from .contracts import (
    ChannelPlugin,
    GroupManagementChannel,
    HttpCallbackRequest,
    HttpCallbackResponse,
    ImageStore,
    InboundEnvelope,
    IngressHandler,
    IngressPlugin,
    IngressPluginContext,
    MessageReference,
    PluginContext,
    PluginUnavailableError,
    RuntimeContextChannel,
    TypingEvent,
    TypingHandler,
    ignore_typing,
)
from .contributions import PluginContributionCatalog, RuntimePluginManifest
from .ingress import PlatformIngress
from .router import ChannelRouter

logger = logging.getLogger(__name__)

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class PluginCleanupError(RuntimeError):
    """插件资源未完整释放；失败实例仍由 generation snapshot 持有。"""

    def __init__(
        self,
        message: str,
        failures: list[tuple[str, BaseException]],
    ) -> None:
        self.failures = tuple(failures)
        stages = "、".join(stage for stage, _ in failures)
        super().__init__(f"{message}（{len(failures)} 项）：{stages}")


@dataclass(slots=True)
class PluginRuntimeSnapshot:
    """一个不可变发布语义的运行时 generation；准入锁保护切代与在途调用。"""

    generation: int
    plugins: dict[str, ChannelPlugin]
    ingress_plugins: dict[str, IngressPlugin]
    egress_plugins: dict[str, EgressPlugin]
    tool_guard_plugins: dict[str, ToolGuardPlugin]
    ingest_handler: IngressHandler
    typing_handler: TypingHandler
    egress_handler: EgressHandler
    started: list[ChannelPlugin | IngressPlugin | EgressPlugin | ToolGuardPlugin]
    channel_capabilities: ChannelCapabilityRegistry
    phased_plugins: set[int] = field(default_factory=set)
    owner: ChannelPluginManager | None = None
    lease_count: int = 0
    prepared: bool = False
    ready: bool = False
    accepting: bool = False
    running: bool = False
    drained: asyncio.Event = field(default_factory=asyncio.Event)
    admission_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.drained.set()


@dataclass(slots=True)
class _EventAdmissionLease:
    """事件租约能力；退出边界后，派生 Task 继承的 ContextVar 也会立即失效。"""

    snapshot: PluginRuntimeSnapshot
    active: bool = True


class _LeasedChannelAdapter:
    """路由中的稳定代理；每次发送绑定当时的插件 generation。"""

    def __init__(self, manager: ChannelPluginManager, plugin_id: str) -> None:
        self.manager = manager
        self.plugin_id = plugin_id

    async def send(
        self, session: SessionView, message: OutboundMessage
    ) -> DeliveryReceipt:
        return await self.manager._dispatch_channel(
            self.plugin_id, session, message
        )

    async def runtime_context(
        self,
        session: SessionView,
    ) -> ChannelRuntimeContext:
        """在租约内读取同一 Channel generation 的临时会话状态。"""

        return await self.manager._dispatch_runtime_context(
            self.plugin_id,
            session,
        )

    async def manage_group(
        self,
        session: SessionView,
        action: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """在租约内把群管理动作交给同一 Channel generation。"""

        return await self.manager._dispatch_group_management(
            self.plugin_id,
            session,
            action,
            parameters,
        )


class ChannelPluginManager:
    """插件发现、入站管线、生命周期和 HTTP 回调分发的唯一 owner。"""

    def __init__(
        self,
        *,
        project_root: Path,
        enabled: list[str],
        options: Mapping[str, Mapping[str, Any]],
        router: ChannelRouter,
        ingress: PlatformIngress,
        store: DatabaseStore,
        image_store: ImageStore | None = None,
        detection_model: ModelProvider | None = None,
        disabled: list[str] | None = None,
        catalog: PluginContributionCatalog | None = None,
        capability_registry: ChannelCapabilityRegistry | None = None,
        _generation: int = 1,
        _candidate_mode: bool = False,
    ) -> None:
        self.project_root = project_root
        self.enabled = tuple(enabled)
        self.disabled = frozenset(disabled or ())
        self.options = options
        self.router = router
        self.ingress = ingress
        self.store = store
        self._image_store = image_store
        self.catalog = catalog or PluginContributionCatalog(
            project_root,
            include_bundled=True,
        )
        self.channel_capabilities = (
            capability_registry or self.catalog.channel_capabilities
        )
        if self.catalog.project_root != project_root.resolve():
            raise ValueError("插件目录与宿主 project_root 不一致")
        self._include_bundled = len(self.catalog.plugins_roots) > 1
        self._detection_model: ModelProvider | None = detection_model
        self._generation = _generation
        self._candidate_mode = _candidate_mode
        self._publish_lock = asyncio.Lock()
        self._lease_lock = asyncio.Lock()
        self._retire_tasks: set[asyncio.Task[None]] = set()
        self._route_keys: set[tuple[str, str]] = set()
        self._history: dict[int, PluginRuntimeSnapshot] = {}
        self.plugins: dict[str, ChannelPlugin] = {}
        self.ingress_plugins: dict[str, IngressPlugin] = {}
        self.egress_plugins: dict[str, EgressPlugin] = {}
        self.tool_guard_plugins: dict[str, ToolGuardPlugin] = {}
        self._started: list[ChannelPlugin | IngressPlugin | EgressPlugin | ToolGuardPlugin] = []
        self._owned_snapshot: PluginRuntimeSnapshot | None = None
        self._admitted_event: ContextVar[_EventAdmissionLease | None] = ContextVar(
            f"plugin_generation_{_generation}_event",
            default=None,
        )
        self._ingest_handler: IngressHandler = self.ingress.accept
        self._typing_handler: TypingHandler = ignore_typing
        self._egress_handler: EgressHandler = self._default_egress
        self._load_enabled()
        self._snapshot = PluginRuntimeSnapshot(
            generation=self._generation,
            plugins=self.plugins,
            ingress_plugins=self.ingress_plugins,
            egress_plugins=self.egress_plugins,
            tool_guard_plugins=self.tool_guard_plugins,
            ingest_handler=self._ingest_handler,
            typing_handler=self._typing_handler,
            egress_handler=self._egress_handler,
            started=self._started,
            channel_capabilities=self.catalog.channel_capabilities,
            owner=self,
        )
        self._owned_snapshot = self._snapshot
        self._history[self._generation] = self._snapshot

    def replace_options(self, options: Mapping[str, Mapping[str, Any]]) -> None:
        """替换后续热重载使用的插件配置；现有 generation 保持冻结快照。"""

        self.options = {plugin_id: dict(values) for plugin_id, values in options.items()}

    def _load_enabled(self) -> None:
        manifests: list[RuntimePluginManifest] = []
        loaded: set[str] = set()
        visiting: set[str] = set()

        def load_with_dependencies(plugin_id: str) -> bool:
            """按依赖优先顺序解析清单，并拒绝循环依赖。"""

            if plugin_id in loaded:
                return True
            if plugin_id in visiting:
                raise ValueError(f"插件存在循环依赖: {plugin_id}")
            if not _PLUGIN_ID.fullmatch(plugin_id):
                raise ValueError(f"无效的插件 ID: {plugin_id}")
            if plugin_id in self.disabled:
                logger.info(
                    "插件已禁用 plugin_id=%s",
                    plugin_id,
                    extra={
                        "session_id": "-",
                        "turn_id": "-",
                        "plugin_id": plugin_id,
                    },
                )
                return False
            visiting.add(plugin_id)
            manifest = self.catalog.runtime_manifest(plugin_id)
            if manifest is None:
                logger.warning(
                    "已配置插件不存在或没有运行时入口，跳过",
                    extra={"session_id": "-", "turn_id": "-", "plugin_id": plugin_id},
                )
                visiting.remove(plugin_id)
                return False
            dependencies_ready = all(
                load_with_dependencies(dependency_id)
                for dependency_id in manifest.requires
            )
            visiting.remove(plugin_id)
            if not dependencies_ready:
                logger.warning(
                    "插件依赖不存在，跳过",
                    extra={"session_id": "-", "turn_id": "-", "plugin_id": plugin_id},
                )
                return False
            loaded.add(plugin_id)
            manifests.append(manifest)
            return True

        for plugin_id in self.enabled:
            load_with_dependencies(plugin_id)

        # 自动启用策略完全由各插件清单声明，不再按插件名称或 kind 特判。
        for manifest in self.catalog.runtime_manifests:
            if manifest.activation == "automatic":
                load_with_dependencies(manifest.plugin_id)

        # Channel 创建时必须拿到完整的入站管线，所以先装载所有 ingress 插件，
        # 再装载 Channel；配置列表只决定同类 ingress 插件的嵌套顺序。
        unavailable: set[str] = set()
        for manifest in manifests:
            if manifest.kind != "ingress":
                continue
            plugin = self._try_load_plugin(
                manifest,
                self._load_ingress_plugin,
                unavailable,
            )
            if plugin is None:
                continue
            self.ingress_plugins[manifest.plugin_id] = plugin
        self._compose_ingress_pipeline()

        for manifest in manifests:
            if manifest.kind != "egress":
                continue
            plugin = self._try_load_plugin(
                manifest,
                self._load_egress_plugin,
                unavailable,
            )
            if plugin is None:
                continue
            self.egress_plugins[manifest.plugin_id] = plugin
        self._compose_egress_pipeline()

        for manifest in manifests:
            if manifest.kind != "tool_guard":
                continue
            plugin = self._try_load_plugin(manifest, self._load_tool_guard_plugin, unavailable)
            if plugin is not None:
                self.tool_guard_plugins[manifest.plugin_id] = plugin

        for manifest in manifests:
            if manifest.kind != "channel":
                continue
            plugin = self._try_load_plugin(
                manifest,
                self._load_channel_plugin,
                unavailable,
            )
            if plugin is None:
                continue
            key = self.router._key(plugin.platform, plugin.account_id)
            self.router.register(
                plugin.platform,
                plugin.account_id,
                _LeasedChannelAdapter(self, manifest.plugin_id),
            )
            self._route_keys.add(key)
            self.plugins[manifest.plugin_id] = plugin

    def _try_load_plugin(
        self,
        manifest: RuntimePluginManifest,
        loader: Any,
        unavailable: set[str],
    ) -> Any | None:
        """只降级明确的配置/可选依赖缺失，并传播内部契约错误。"""

        missing_dependencies = [
            dependency
            for dependency in manifest.requires
            if dependency in unavailable
        ]
        if missing_dependencies:
            unavailable.add(manifest.plugin_id)
            logger.warning(
                "插件依赖不可用，跳过 plugin_id=%s dependencies=%s",
                manifest.plugin_id,
                ",".join(missing_dependencies),
                extra={
                    "session_id": "-",
                    "turn_id": "-",
                    "plugin_id": manifest.plugin_id,
                },
            )
            return None
        try:
            return loader(manifest)
        except (PluginUnavailableError, ModuleNotFoundError) as exc:
            unavailable.add(manifest.plugin_id)
            logger.warning(
                "插件配置或可选依赖不完整，跳过 plugin_id=%s reason_type=%s",
                manifest.plugin_id,
                type(exc).__name__,
                extra={
                    "session_id": "-",
                    "turn_id": "-",
                    "plugin_id": manifest.plugin_id,
                },
            )
            return None

    def _load_factory(self, manifest: RuntimePluginManifest) -> Any:
        plugin_id = manifest.plugin_id
        entrypoint = manifest.entrypoint
        module_name, factory_name = entrypoint.split(":", maxsplit=1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module_name):
            raise ValueError(f"插件入口模块名无效: {plugin_id}")
        module = self._load_module(
            plugin_id,
            manifest.plugin_root / f"{module_name}.py",
            self._generation,
        )
        factory = getattr(module, factory_name, None)
        if factory is None or not callable(factory):
            raise ValueError(f"插件工厂不存在: {entrypoint}")
        return factory

    def _load_channel_plugin(self, manifest: RuntimePluginManifest) -> ChannelPlugin:
        factory = self._load_factory(manifest)
        context = PluginContext(
            plugin_id=manifest.plugin_id,
            plugin_root=manifest.plugin_root,
            options=dict(self.options.get(manifest.plugin_id, {})),
            ingest=self._dispatch_ingress,
            resolve_external_message_id=self._resolve_external_message_id,
            resolve_message_reference=self._resolve_message_reference,
            typing=self._dispatch_typing,
            store_image=(
                self._store_image_for_admitted_event
                if self._image_store is not None
                else None
            ),
            admit_event=self._admit_generation_event,
            generation=self._generation,
        )
        plugin = factory(context)
        if inspect.isawaitable(plugin):
            raise TypeError("Channel 插件工厂必须是同步函数")
        if (
            getattr(plugin, "plugin_id", None) != manifest.plugin_id
            or not getattr(plugin, "platform", "")
            or not getattr(plugin, "account_id", "")
        ):
            raise ValueError(f"插件工厂返回的身份无效: {manifest.plugin_id}")
        return cast(ChannelPlugin, plugin)

    def _load_ingress_plugin(self, manifest: RuntimePluginManifest) -> IngressPlugin:
        factory = self._load_factory(manifest)
        context = IngressPluginContext(
            plugin_id=manifest.plugin_id,
            plugin_root=manifest.plugin_root,
            options=dict(self.options.get(manifest.plugin_id, {})),
        )
        plugin = factory(context)
        if inspect.isawaitable(plugin):
            raise TypeError("Ingress 插件工厂必须是同步函数")
        if getattr(plugin, "plugin_id", None) != manifest.plugin_id:
            raise ValueError(f"插件工厂返回的身份无效: {manifest.plugin_id}")
        return cast(IngressPlugin, plugin)

    def _load_egress_plugin(self, manifest: RuntimePluginManifest) -> EgressPlugin:
        factory = self._load_factory(manifest)
        context = EgressPluginContext(
            plugin_id=manifest.plugin_id,
            plugin_root=manifest.plugin_root,
            options=dict(self.options.get(manifest.plugin_id, {})),
            detection_model_resolver=self._resolve_detection_model,
        )
        plugin = factory(context)
        if inspect.isawaitable(plugin):
            raise TypeError("Egress 插件工厂必须是同步函数")
        if getattr(plugin, "plugin_id", None) != manifest.plugin_id:
            raise ValueError(f"插件工厂返回的身份无效: {manifest.plugin_id}")
        return cast(EgressPlugin, plugin)

    def _compose_ingress_pipeline(self) -> None:
        ingest: IngressHandler = self.ingress.accept
        typing: TypingHandler = ignore_typing
        for plugin in reversed(list(self.ingress_plugins.values())):
            ingest = partial(self._call_ingress_plugin, plugin, call_next=ingest)
            typing = partial(self._call_typing_plugin, plugin, call_next=typing)
        self._ingest_handler = ingest
        self._typing_handler = typing

    def _load_tool_guard_plugin(self, manifest: RuntimePluginManifest) -> ToolGuardPlugin:
        """使用通用清单加载守卫，不授予外部副作用能力。"""

        plugin = self._load_factory(manifest)(ToolGuardPluginContext(
            plugin_id=manifest.plugin_id,
            plugin_root=manifest.plugin_root,
            options=dict(self.options.get(manifest.plugin_id, {})),
        ))
        if inspect.isawaitable(plugin):
            raise TypeError("ToolGuard 插件工厂必须是同步函数")
        if getattr(plugin, "plugin_id", None) != manifest.plugin_id:
            raise ValueError(f"插件工厂返回的身份无效: {manifest.plugin_id}")
        if not all(callable(getattr(plugin, name, None)) for name in ("start", "stop", "create_guard")):
            raise TypeError(f"执行守卫插件契约无效: {manifest.plugin_id}")
        return cast(ToolGuardPlugin, plugin)

    @asynccontextmanager
    async def tool_guards(self):
        """整个任务租用同一代插件；reload 仅影响下一次任务。"""

        async with self._lease_current() as snapshot:
            guards = []
            for plugin_id, plugin in snapshot.tool_guard_plugins.items():
                guard = plugin.create_guard()
                timeout = getattr(guard, "timeout_seconds", None)
                if (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout)
                    or timeout <= 0
                    or not callable(getattr(guard, "observe", None))
                ):
                    raise TypeError(f"执行守卫状态契约无效: {plugin_id}")
                guards.append((plugin_id, guard))
            yield tuple(guards)

    def _compose_egress_pipeline(self) -> None:
        """按配置逆序嵌套输出过滤插件；无插件时退化为直通透传。"""

        egress: EgressHandler = self._default_egress
        for plugin in reversed(list(self.egress_plugins.values())):
            egress = partial(self._call_egress_plugin, plugin, call_next=egress)
        self._egress_handler = egress

    @staticmethod
    async def _default_egress(envelope: EgressEnvelope) -> str:
        return envelope.text

    @staticmethod
    async def _call_egress_plugin(
        plugin: EgressPlugin,
        envelope: EgressEnvelope,
        *,
        call_next: EgressHandler,
    ) -> str:
        return await plugin.filter(envelope, call_next)

    def _resolve_detection_model(self) -> ModelProvider | None:
        return self._detection_model

    def set_detection_model(self, model: ModelProvider | None) -> None:
        """热切换后续过滤确认使用的检测模型。"""

        self._detection_model = model
        for snapshot in self._history.values():
            if snapshot.owner is not None:
                snapshot.owner._detection_model = model

    @property
    def egress_filter(self) -> EgressHandler:
        """返回稳定分发入口；调用时才绑定当前 generation。"""

        return self._dispatch_egress

    @asynccontextmanager
    async def _lease_current(self):
        """为一次外部调用租用当前快照，切代后旧快照会等待租约归零。"""

        async with self._lease_lock:
            snapshot = self._snapshot
            acquired = await self._acquire_snapshot(snapshot)
        if not acquired:
            raise RuntimeError("插件 generation 尚未发布或已停止接收新调用")
        try:
            yield snapshot
        finally:
            await self._release_snapshot(snapshot)

    @staticmethod
    async def _acquire_snapshot(snapshot: PluginRuntimeSnapshot) -> bool:
        """在同一 generation 的准入锁内检查状态并登记租约。"""

        async with snapshot.admission_lock:
            if not snapshot.accepting:
                return False
            snapshot.lease_count += 1
            snapshot.drained.clear()
            return True

    @staticmethod
    async def _release_snapshot(snapshot: PluginRuntimeSnapshot) -> None:
        """释放 generation 租约；归零后允许退休任务继续。"""

        async with snapshot.admission_lock:
            snapshot.lease_count -= 1
            if snapshot.lease_count < 0:
                raise RuntimeError("插件 generation 租约计数小于零")
            if snapshot.lease_count == 0:
                snapshot.drained.set()

    def _plugin_owned_snapshot(self) -> PluginRuntimeSnapshot:
        """返回本管理器构造插件所绑定的固定 generation，而非当前发布指针。"""

        snapshot = self._owned_snapshot
        if snapshot is None:
            raise RuntimeError("插件 generation 尚未完成构造")
        return snapshot

    @asynccontextmanager
    async def _lease_owned_snapshot(self):
        """为插件上下文租用其固定 generation，防止切代后误入新管线。"""

        snapshot = self._plugin_owned_snapshot()
        acquired = await self._acquire_snapshot(snapshot)
        if not acquired:
            raise RuntimeError("插件 generation 尚未发布或已停止接收新调用")
        try:
            yield snapshot
        finally:
            await self._release_snapshot(snapshot)

    @asynccontextmanager
    async def _admit_generation_event(self):
        """让 Channel 的整条外部事件处理受 generation admission/lease 保护。"""

        snapshot = self._plugin_owned_snapshot()
        acquired = await self._acquire_snapshot(snapshot)
        if not acquired:
            yield False
            return
        lease = _EventAdmissionLease(snapshot=snapshot)
        token = self._admitted_event.set(lease)
        try:
            yield True
        finally:
            lease.active = False
            self._admitted_event.reset(token)
            await self._release_snapshot(snapshot)

    def _has_admitted_event(self, snapshot: PluginRuntimeSnapshot) -> bool:
        """检查当前协程是否仍处在该 generation 的有效整事件租约中。"""

        lease = self._admitted_event.get()
        return (
            lease is not None
            and lease.active
            and lease.snapshot is snapshot
        )

    def _store_image_for_admitted_event(
        self,
        filename: str,
        mime_type: str,
        content: bytes,
    ) -> MessageComponent:
        """只允许已准入事件在租约内写正式附件目录。"""

        snapshot = self._plugin_owned_snapshot()
        if not self._has_admitted_event(snapshot):
            raise RuntimeError("候选插件 generation 未获事件准入，拒绝写正式附件")
        image_store = self._image_store
        if image_store is None:
            raise RuntimeError("宿主未授予图片存储能力")
        return image_store(filename, mime_type, content)

    async def _dispatch_egress(self, envelope: EgressEnvelope) -> str:
        async with self._lease_current() as snapshot:
            return await snapshot.egress_handler(envelope)

    async def _dispatch_ingress(self, envelope: InboundEnvelope):
        snapshot = self._plugin_owned_snapshot()
        if self._has_admitted_event(snapshot):
            return await snapshot.ingest_handler(envelope)
        async with self._lease_owned_snapshot() as snapshot:
            return await snapshot.ingest_handler(envelope)

    async def _dispatch_typing(self, event: TypingEvent) -> None:
        snapshot = self._plugin_owned_snapshot()
        if self._has_admitted_event(snapshot):
            await snapshot.typing_handler(event)
            return
        async with self._lease_owned_snapshot() as snapshot:
            await snapshot.typing_handler(event)

    async def _dispatch_channel(
        self,
        plugin_id: str,
        session: SessionView,
        message: OutboundMessage,
    ) -> DeliveryReceipt:
        async with self._lease_current() as snapshot:
            plugin = snapshot.plugins.get(plugin_id)
            if plugin is None:
                raise NotFoundError("Channel 插件在当前 generation 中不存在")
            return await plugin.send(session, message)

    async def _dispatch_runtime_context(
        self,
        plugin_id: str,
        session: SessionView,
    ) -> ChannelRuntimeContext:
        """租用当前 generation 读取可选的 Channel 会话运行态。"""

        async with self._lease_current() as snapshot:
            plugin = snapshot.plugins.get(plugin_id)
            if plugin is None:
                raise NotFoundError("Channel 插件在当前 generation 中不存在")
            handler = getattr(plugin, "runtime_context", None)
            if not callable(handler):
                return ChannelRuntimeContext()
            result = await cast(RuntimeContextChannel, plugin).runtime_context(session)
            if not isinstance(result, ChannelRuntimeContext):
                raise TypeError("Channel runtime_context 必须返回 ChannelRuntimeContext")
            return result

    async def _dispatch_group_management(
        self,
        plugin_id: str,
        session: SessionView,
        action: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """租用当前 generation 执行可选群管理动作。"""

        async with self._lease_current() as snapshot:
            plugin = snapshot.plugins.get(plugin_id)
            if plugin is None:
                raise NotFoundError("Channel 插件在当前 generation 中不存在")
            handler = getattr(plugin, "manage_group", None)
            if not callable(handler):
                raise NotFoundError("当前 Channel 插件不支持群管理")
            result = await cast(GroupManagementChannel, plugin).manage_group(
                session,
                action,
                parameters,
            )
            if not isinstance(result, dict):
                raise TypeError("Channel 群管理结果必须为 dict")
            return result

    @staticmethod
    async def _call_ingress_plugin(
        plugin: IngressPlugin,
        envelope: InboundEnvelope,
        *,
        call_next: IngressHandler,
    ):
        return await plugin.ingest(envelope, call_next)

    @staticmethod
    async def _call_typing_plugin(
        plugin: IngressPlugin,
        event: TypingEvent,
        *,
        call_next: TypingHandler,
    ) -> None:
        await plugin.typing(event, call_next)

    @staticmethod
    def _load_module(
        plugin_id: str, path: Path, generation: int = 0
    ) -> ModuleType:
        if not path.is_file():
            raise FileNotFoundError(f"插件入口文件不存在: {path}")
        package_name = f"ija_channel_plugin_{plugin_id}_g{generation}"
        package = types.ModuleType(package_name)
        package.__path__ = [str(path.parent)]
        package.__package__ = package_name
        sys.modules[package_name] = package
        qualified_name = f"{package_name}.{path.stem}"
        spec = importlib.util.spec_from_file_location(qualified_name, path)
        if spec is None or spec.loader is None:
            sys.modules.pop(package_name, None)
            raise ImportError(f"无法创建插件模块: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        loaded = False
        try:
            # 热发布必须编译本次候选的确切源码；SourceFileLoader 的时间戳
            # pyc 缓存在“同秒、同大小”改动时可能错误复用旧代。
            source = path.read_bytes()
            code = compile(source, str(path), "exec")
            exec(code, module.__dict__)
            loaded = True
        finally:
            if not loaded:
                sys.modules.pop(qualified_name, None)
                sys.modules.pop(package_name, None)
        return module

    async def start(self) -> None:
        """冷启动当前 generation；候选必须由发布协调器分阶段驱动。"""

        if self._candidate_mode:
            raise RuntimeError("候选 generation 不能直接 start，必须先 prepare 后受控发布")
        await self._prepare_snapshot(self._snapshot)
        await self._activate_snapshot(self._snapshot)
        await self._mark_snapshot_accepting(self._snapshot, True)

    async def prepare(self) -> None:
        """纯校验当前 generation，不调用旧插件 start 或建立外部连接。"""

        await self._prepare_snapshot(self._snapshot)

    @staticmethod
    def _lifecycle_method(
        plugin: ChannelPlugin | IngressPlugin | EgressPlugin | ToolGuardPlugin,
        name: str,
    ) -> Callable[[], Awaitable[None]] | None:
        """取得可选异步生命周期方法；返回值契约在实际 await 时 fail-fast。"""

        method = getattr(plugin, name, None)
        if not callable(method):
            return None
        return cast(Callable[[], Awaitable[None]], method)

    @classmethod
    def _uses_phased_lifecycle(
        cls,
        plugin: ChannelPlugin | IngressPlugin | EgressPlugin | ToolGuardPlugin,
    ) -> bool:
        """显式生命周期必须四阶段齐备，避免激活资源没有对应清理 owner。"""

        phase_names = ("prepare", "activate", "ready", "deactivate")
        present = {
            name
            for name in phase_names
            if cls._lifecycle_method(plugin, name) is not None
        }
        if present and len(present) != len(phase_names):
            missing = ", ".join(name for name in phase_names if name not in present)
            raise TypeError(
                f"插件 {plugin.plugin_id} 的显式生命周期不完整，缺少: {missing}"
            )
        return bool(present)

    async def _prepare_snapshot(self, snapshot: PluginRuntimeSnapshot) -> None:
        """执行可选 prepare；旧插件的 prepare 是纯 no-op 兼容层。"""

        async with snapshot.lifecycle_lock:
            if snapshot.prepared:
                return
            for plugin in (
                *snapshot.ingress_plugins.values(),
                *snapshot.egress_plugins.values(),
                *snapshot.tool_guard_plugins.values(),
                *snapshot.plugins.values(),
            ):
                if not self._uses_phased_lifecycle(plugin):
                    continue
                snapshot.phased_plugins.add(id(plugin))
                prepare = self._lifecycle_method(plugin, "prepare")
                if prepare is None:
                    raise RuntimeError("显式插件生命周期状态不一致")
                await prepare()
            snapshot.prepared = True

    async def _activate_snapshot(self, snapshot: PluginRuntimeSnapshot) -> None:
        """激活并等待 ready；任何失败都逆序补偿，且不改变当前发布代。"""

        async with snapshot.lifecycle_lock:
            if snapshot.running:
                return
            if not snapshot.prepared:
                raise RuntimeError("插件 generation 必须先 prepare 再 activate")
            if snapshot.started:
                raise RuntimeError(
                    "插件 generation 仍有待清理实例，拒绝重复 activate"
                )
            completed = False
            try:
                for plugin in (
                    *snapshot.ingress_plugins.values(),
                    *snapshot.egress_plugins.values(),
                    *snapshot.tool_guard_plugins.values(),
                    *snapshot.plugins.values(),
                ):
                    # 先登记再调用，覆盖“已创建任务/连接后 start 抛错”的部分失败。
                    snapshot.started.append(plugin)
                    if id(plugin) in snapshot.phased_plugins:
                        activate = self._lifecycle_method(plugin, "activate")
                        if activate is None:
                            raise RuntimeError("显式插件生命周期状态不一致")
                        await activate()
                    else:
                        await plugin.start()
                for plugin in snapshot.started:
                    if id(plugin) in snapshot.phased_plugins:
                        ready = self._lifecycle_method(plugin, "ready")
                        if ready is None:
                            raise RuntimeError("显式插件生命周期状态不一致")
                        await ready()
                snapshot.ready = True
                snapshot.running = True
                completed = True
            finally:
                if not completed:
                    await self._deactivate_started(snapshot)
            for plugin in snapshot.started:
                extra = {
                    "session_id": "-",
                    "turn_id": "-",
                    "plugin_id": plugin.plugin_id,
                }
                platform = getattr(plugin, "platform", None)
                account_id = getattr(plugin, "account_id", None)
                if isinstance(platform, str) and isinstance(account_id, str):
                    extra.update(
                        {
                            "platform": platform,
                            "account_id": account_id,
                        }
                    )
                logger.info("插件已就绪 plugin_id=%s", plugin.plugin_id, extra=extra)

    async def stop(self) -> None:
        """停止所有仍存活的 generation；应用关闭时不保留回滚代。"""

        retire_tasks = list(self._retire_tasks)
        self._retire_tasks.clear()
        for task in retire_tasks:
            task.cancel()
        await asyncio.gather(*retire_tasks, return_exceptions=True)
        seen: set[int] = set()
        failures: list[tuple[str, BaseException]] = []
        cancelled: asyncio.CancelledError | None = None
        for snapshot in list(self._history.values()):
            if id(snapshot) in seen:
                continue
            seen.add(id(snapshot))
            await self._mark_snapshot_accepting(snapshot, False)
            await snapshot.drained.wait()
            try:
                await self._stop_snapshot(snapshot)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception as exc:
                failures.append((f"generation={snapshot.generation}", exc))
        if cancelled is not None:
            if failures:
                cancelled.add_note(
                    "取消前仍有插件 generation 清理失败："
                    + "、".join(stage for stage, _ in failures)
                )
            raise cancelled
        if failures:
            raise PluginCleanupError("插件 generation 停止失败", failures)

    async def _stop_snapshot(self, snapshot: PluginRuntimeSnapshot) -> None:
        async with snapshot.lifecycle_lock:
            await self._deactivate_started(snapshot)

    async def _deactivate_started(self, snapshot: PluginRuntimeSnapshot) -> None:
        """逆序关闭所有已进入 activate 的插件，并继续清理其余实例。"""

        plugins = list(reversed(snapshot.started))
        snapshot.started.clear()
        snapshot.ready = False
        snapshot.running = False
        failed: list[ChannelPlugin | IngressPlugin | EgressPlugin | ToolGuardPlugin] = []
        failures: list[tuple[str, BaseException]] = []
        cancelled: asyncio.CancelledError | None = None
        for plugin in plugins:
            try:
                if id(plugin) in snapshot.phased_plugins:
                    deactivate = self._lifecycle_method(plugin, "deactivate")
                    if deactivate is None:
                        raise RuntimeError("显式插件生命周期状态不一致")
                    await deactivate()
                else:
                    await plugin.stop()
            except asyncio.CancelledError as exc:
                # 保留失败实例供外层 stop 重试，但仍继续清理其他已激活插件。
                failed.append(plugin)
                failures.append((plugin.plugin_id, exc))
                cancelled = exc
            except Exception as exc:
                failed.append(plugin)
                failures.append((plugin.plugin_id, exc))
                logger.error(
                    "插件停止失败",
                    exc_info=True,
                    extra={
                        "session_id": "-",
                        "turn_id": "-",
                        "plugin_id": plugin.plugin_id,
                    },
                )
        snapshot.started.extend(reversed(failed))
        if cancelled is not None:
            if len(failures) > 1:
                cancelled.add_note(
                    "取消期间还有插件清理失败："
                    + "、".join(stage for stage, _ in failures)
                )
            raise cancelled
        if failures:
            raise PluginCleanupError("插件实例清理失败", failures)

    async def hot_reload(self) -> int:
        """验证并启动候选 generation，成功后原子发布；失败保留旧代。"""

        async with self._publish_lock:
            generation = max(self._history, default=0) + 1
            candidate_router = ChannelRouter()
            candidate: ChannelPluginManager | None = None
            try:
                catalog = PluginContributionCatalog(
                    self.project_root,
                    include_bundled=self._include_bundled,
                )
                candidate = ChannelPluginManager(
                    project_root=self.project_root,
                    enabled=list(self.enabled),
                    disabled=list(self.disabled),
                    options=self.options,
                    router=candidate_router,
                    ingress=self.ingress,
                    store=self.store,
                    image_store=self._image_store,
                    detection_model=self._detection_model,
                    catalog=catalog,
                    _generation=generation,
                    _candidate_mode=True,
                )
                await candidate.prepare()
                await candidate._activate_snapshot(candidate._snapshot)
                await self._publish_snapshot(candidate._snapshot)
            except BaseException as candidate_error:
                cleanup_error: BaseException | None = None
                if candidate is not None:
                    try:
                        await candidate.stop()
                    except BaseException as exc:
                        cleanup_error = exc
                    if candidate._snapshot.started:
                        # 失败候选仍可能持有网络任务或连接。把 snapshot 转交给
                        # 主 manager，确保后续 stop 能继续重试，而不是随局部变量丢失。
                        self._history[generation] = candidate._snapshot
                        logger.error(
                            "候选插件清理不完整，已保留 generation owner",
                            extra={
                                "session_id": "-",
                                "turn_id": "-",
                                "generation": generation,
                                "cleanup_pending": len(candidate._snapshot.started),
                            },
                        )
                        if cleanup_error is None:
                            cleanup_error = PluginCleanupError(
                                "候选插件清理不完整",
                                [
                                    (
                                        plugin.plugin_id,
                                        RuntimeError("deactivate/stop 未释放插件实例"),
                                    )
                                    for plugin in candidate._snapshot.started
                                ],
                            )
                if isinstance(candidate_error, asyncio.CancelledError):
                    if cleanup_error is not None:
                        candidate_error.add_note(f"候选清理同时失败：{cleanup_error}")
                    raise
                if cleanup_error is not None:
                    raise cleanup_error from candidate_error
                raise
            logger.info(
                "插件 generation 已发布 generation=%s",
                generation,
                extra={"session_id": "-", "turn_id": "-", "generation": generation},
            )
            return generation

    async def rollback(self, generation: int | None = None) -> int:
        """回滚到已验证历史 generation；目标启动失败时当前代保持不变。"""

        async with self._publish_lock:
            candidates = [
                item
                for item in self._history
                if item != self._snapshot.generation
                and not self._history[item].started
                and (generation is None or item == generation)
            ]
            if not candidates:
                raise NotFoundError("没有可回滚的插件 generation")
            target_generation = max(candidates)
            target = self._history[target_generation]
            if not target.running:
                await self._activate_snapshot(target)
            await self._publish_snapshot(target)
            logger.warning(
                "插件 generation 已回滚 generation=%s",
                target_generation,
                extra={
                    "session_id": "-",
                    "turn_id": "-",
                    "generation": target_generation,
                },
            )
            return target_generation

    async def _publish_snapshot(self, snapshot: PluginRuntimeSnapshot) -> None:
        """在统一准入临界区替换路由和当前指针，再异步排空旧代。"""

        if not snapshot.running or not snapshot.ready:
            raise RuntimeError("插件 generation 未 ready，拒绝发布")
        old = self._snapshot
        routes: dict[tuple[str, str], ChannelAdapter] = {}
        for plugin_id, plugin in snapshot.plugins.items():
            key = self.router._key(plugin.platform, plugin.account_id)
            routes[key] = _LeasedChannelAdapter(self, plugin_id)
        new_keys = set(routes)
        async with self._lease_lock:
            admission_snapshots = sorted(
                {id(item): item for item in (old, snapshot)}.values(),
                key=lambda item: item.generation,
            )
            acquired_snapshots: list[PluginRuntimeSnapshot] = []
            try:
                for item in admission_snapshots:
                    await item.admission_lock.acquire()
                    acquired_snapshots.append(item)
                # replace_owned 先在副本上完成冲突校验；失败时不会改变路由或准入。
                self.router.replace_owned(self._route_keys, routes)
                old.accepting = False
                snapshot.accepting = True
                self._snapshot = snapshot
                self._generation = snapshot.generation
                self.plugins = snapshot.plugins
                self.ingress_plugins = snapshot.ingress_plugins
                self.egress_plugins = snapshot.egress_plugins
                self.tool_guard_plugins = snapshot.tool_guard_plugins
                self._ingest_handler = snapshot.ingest_handler
                self._typing_handler = snapshot.typing_handler
                self._egress_handler = snapshot.egress_handler
                self._started = snapshot.started
                self._route_keys = new_keys
                self.channel_capabilities.replace_from(
                    snapshot.channel_capabilities
                )
                self._history[snapshot.generation] = snapshot
            finally:
                for item in reversed(acquired_snapshots):
                    item.admission_lock.release()
        if old is not snapshot:
            task = asyncio.create_task(
                self._retire_after_drain(old),
                name=f"plugin-generation-retire-{old.generation}",
            )
            self._retire_tasks.add(task)
            task.add_done_callback(self._retire_tasks.discard)

    async def _retire_after_drain(self, snapshot: PluginRuntimeSnapshot) -> None:
        await snapshot.drained.wait()
        async with self._publish_lock:
            if snapshot is self._snapshot:
                return
            await self._stop_snapshot(snapshot)

    @staticmethod
    async def _mark_snapshot_accepting(
        snapshot: PluginRuntimeSnapshot,
        accepting: bool,
    ) -> None:
        """切换单代准入；关闭准入后既有租约仍可自然排空。"""

        async with snapshot.admission_lock:
            snapshot.accepting = accepting

    def generation_status(self) -> dict[str, object]:
        """返回控制面可展示的 generation 与租约状态。"""

        return {
            "current_generation": self._snapshot.generation,
            "generations": [
                {
                    "generation": generation,
                    "current": snapshot is self._snapshot,
                    "prepared": snapshot.prepared,
                    "ready": snapshot.ready,
                    "accepting": snapshot.accepting,
                    "running": snapshot.running,
                    "cleanup_pending": bool(snapshot.started) and not snapshot.running,
                    "lease_count": snapshot.lease_count,
                    "plugins": sorted(
                        {
                            *snapshot.plugins,
                            *snapshot.ingress_plugins,
                            *snapshot.egress_plugins,
                            *snapshot.tool_guard_plugins,
                        }
                    ),
                }
                for generation, snapshot in sorted(self._history.items())
            ],
        }

    async def handle_http(
        self, plugin_id: str, request: HttpCallbackRequest
    ) -> HttpCallbackResponse:
        async with self._lease_current() as snapshot:
            plugin = snapshot.plugins.get(plugin_id)
            if plugin is None:
                raise NotFoundError("Channel 插件未启用或不存在")
            return await plugin.handle_http(request)

    async def _resolve_external_message_id(self, stored_message_id: str) -> str | None:
        message = await self.store.get_message(stored_message_id)
        return message.external_message_id if message is not None else None

    async def _resolve_message_reference(
        self,
        platform: str,
        account_id: str,
        external_message_id: str,
    ) -> MessageReference | None:
        """只返回引用判定所需身份，不向 Channel 插件暴露消息正文。"""

        message = await self.store.get_message_by_external_id(
            platform=platform,
            account_id=account_id,
            external_message_id=external_message_id,
        )
        if message is None:
            return None
        return MessageReference(
            message_id=message.id,
            session_id=message.session_id,
            sender_id=message.sender_id,
            sender_name=message.sender_name,
        )
