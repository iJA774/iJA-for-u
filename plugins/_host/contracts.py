"""plugins/ 下社交平台 Channel 插件与宿主之间的最小协议。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from domain.models import (
    ChatType,
    DeliveryReceipt,
    InboundMessage,
    MessageComponent,
    OutboundMessage,
    ParticipantRole,
    SessionView,
)
from ports import ChannelRuntimeContext

if TYPE_CHECKING:
    from application.service import IngressResult

IngressHandler = Callable[["InboundEnvelope"], Awaitable["IngressResult"]]
TypingHandler = Callable[["TypingEvent"], Awaitable[None]]
ImageStore = Callable[[str, str, bytes], MessageComponent]
EventAdmission = Callable[[], AbstractAsyncContextManager[bool]]


class PluginUnavailableError(RuntimeError):
    """插件因配置或可选依赖缺失而不能启用。"""


@dataclass(frozen=True, slots=True)
class MessageReference:
    """插件解析平台引用时可读取的最小消息身份，不暴露聊天正文。"""

    message_id: str
    session_id: str
    sender_id: str
    sender_name: str


@dataclass(frozen=True, slots=True)
class InboundEnvelope:
    """插件交给宿主的入站消息及创建会话所需的最小元数据。"""

    message: InboundMessage
    session_display_name: str
    participant_role: ParticipantRole = ParticipantRole.MEMBER
    schedule_turn: bool = True
    schedule_duplicate: bool = False
    debounce_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class TypingEvent:
    """Channel 上报的用户输入状态；它不是消息，不能进入权威聊天历史。"""

    platform: str
    account_id: str
    external_chat_id: str
    chat_type: ChatType
    sender_id: str
    status_text: str = ""
    event_type: int | None = None


async def ignore_typing(_: TypingEvent) -> None:
    """默认丢弃没有消费者的输入状态事件。"""


@asynccontextmanager
async def allow_event() -> AsyncIterator[bool]:
    """供独立插件测试使用的默认事件准入；宿主运行时会替换为 generation 租约。"""

    yield True


@dataclass(frozen=True, slots=True)
class HttpCallbackRequest:
    """与 Web 框架解耦的插件 HTTP 回调请求。"""

    method: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class HttpCallbackResponse:
    """插件 HTTP 回调的确定性响应。"""

    status_code: int
    body: bytes
    media_type: str = "text/plain; charset=utf-8"
    headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def text(cls, text: str, *, status_code: int = 200) -> HttpCallbackResponse:
        return cls(status_code=status_code, body=text.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class PluginContext:
    """宿主授予单个插件的窄能力集合。

    ``admit_event`` 必须覆盖一条平台事件从解析、媒体落盘到核心 ingest 的完整
    处理边界。候选 generation 在发布前会返回 ``False``，插件不得继续处理。
    """

    plugin_id: str
    plugin_root: Path
    options: Mapping[str, Any]
    ingest: IngressHandler
    resolve_external_message_id: Callable[[str], Awaitable[str | None]]
    resolve_message_reference: Callable[
        [str, str, str], Awaitable[MessageReference | None]
    ]
    typing: TypingHandler = ignore_typing
    store_image: ImageStore | None = None
    admit_event: EventAdmission = allow_event
    generation: int = 0


@dataclass(frozen=True, slots=True)
class IngressPluginContext:
    """宿主授予入站中间件插件的只读配置。"""

    plugin_id: str
    plugin_root: Path
    options: Mapping[str, Any]


class PhasedPluginLifecycle(Protocol):
    """可选的显式 generation 生命周期；旧插件由宿主适配 start/stop。"""

    async def prepare(self) -> None:
        """只做纯校验，不得联网、启动任务或写正式状态。"""

    async def activate(self) -> None:
        """建立运行资源；失败时宿主仍会调用 deactivate 做部分启动补偿。"""

    async def ready(self) -> None:
        """等待插件具备可发布条件，失败则当前 generation 不会发布。"""

    async def deactivate(self) -> None:
        """停止任务与连接，允许未来重新 activate。"""


class ChannelPlugin(Protocol):
    """可被统一路由、启停并接收 HTTP 回调的 Channel 插件。"""

    plugin_id: str
    platform: str
    account_id: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, session: SessionView, message: OutboundMessage) -> DeliveryReceipt: ...

    async def handle_http(self, request: HttpCallbackRequest) -> HttpCallbackResponse: ...


class GroupManagementChannel(Protocol):
    """可选的群管理扩展；不要求所有 Channel 实现。"""

    async def manage_group(
        self,
        session: SessionView,
        action: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]: ...


class RuntimeContextChannel(Protocol):
    """可选的会话运行态扩展；数据只用于构造本轮临时上下文。"""

    async def runtime_context(self, session: SessionView) -> ChannelRuntimeContext: ...


class IngressPlugin(Protocol):
    """可拦截 Channel 消息和输入状态的入站插件。"""

    plugin_id: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def ingest(
        self,
        envelope: InboundEnvelope,
        call_next: IngressHandler,
    ) -> IngressResult: ...

    async def typing(
        self,
        event: TypingEvent,
        call_next: TypingHandler,
    ) -> None: ...
