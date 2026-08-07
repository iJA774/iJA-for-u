"""应用层依赖的端口协议。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from domain.models import (
    ComponentType,
    DeliveryReceipt,
    MessageComponent,
    OutboundMessage,
    ParticipantRole,
    SessionView,
)


@dataclass(frozen=True, slots=True)
class ImageGenerationRequest:
    """图片编辑 Provider 的最小厂商无关请求。"""

    base_image: bytes
    prompt: str
    filename: str = "base_image.png"
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ImageGenerationResult:
    """图片模型返回的原始文件字节。"""

    content: bytes
    usage: ModelUsage | None = None


class ModelUsage(BaseModel):
    """上游明确返回的 token 用量；缺失时不得用字符数伪造。"""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class EmbeddingVectors(list[list[float]]):
    """兼容 list 的向量结果，并携带上游可选 usage 事实。"""

    def __init__(
        self,
        vectors: list[list[float]],
        *,
        usage: ModelUsage | None = None,
    ) -> None:
        super().__init__(vectors)
        self.usage = usage


class ModelToolCall(BaseModel):
    """模型请求执行一个本地函数工具。"""

    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    arguments: str


class ModelImage(BaseModel):
    """交给视觉聊天模型的已验证内联图片。"""

    mime_type: Literal["image/jpeg", "image/png", "image/gif", "image/webp"]
    base64_data: str = Field(min_length=1)
    detail: Literal["auto", "low", "high"] = "auto"


class ModelMessage(BaseModel):
    """Chat Completions 共同消息子集。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    images: list[ModelImage] = Field(default_factory=list, max_length=4)
    tool_calls: list[ModelToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class ToolDefinition(BaseModel):
    """交给模型的函数工具定义。"""

    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    description: str = Field(min_length=1, max_length=1000)
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """不绑定厂商 SDK的模型请求。"""

    messages: list[ModelMessage]
    model: str
    temperature: float
    max_tokens: int
    json_mode: bool = False
    tools: list[ToolDefinition] | None = None
    tool_choice: Literal["auto", "none", "required"] | None = None


class ModelResult(BaseModel):
    """模型文本或原生工具调用结果。"""

    content: str | None = None
    tool_calls: list[ModelToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: ModelUsage | None = None


class ModelStreamEvent(BaseModel):
    """模型流式输出中的一个可见文本增量。"""

    delta: str = ""
    finish_reason: str | None = None
    usage: ModelUsage | None = None


class EmbeddingProvider(Protocol):
    """独立向量化端口；输入只应是获授权的记忆或学习派生文本。"""

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[list[float]]: ...

    async def close(self) -> None: ...


class ModelProvider(Protocol):
    """聊天和结构化提取共用的模型端口。"""

    async def complete(self, request: ModelRequest) -> ModelResult: ...

    async def probe(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class StreamingModelProvider(Protocol):
    """可选的文本流式能力；不改变仅支持 complete 的既有 Provider 契约。"""

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]: ...


class ImageModelProvider(Protocol):
    """独立图片编辑模型端口。"""

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult: ...

    async def close(self) -> None: ...


class ChannelAdapter(Protocol):
    """未来微信、QQ 与当前 Web 模拟器共享的出站端口。"""

    async def send(self, session: SessionView, message: OutboundMessage) -> DeliveryReceipt: ...


@dataclass(frozen=True, slots=True)
class ChannelRuntimeContext:
    """Channel 对当前会话提供的、经过类型校验的临时运行态。"""

    agent_group_role: ParticipantRole | None = None
    supported_processing: frozenset[str] = frozenset()
    supported_prompt_projections: frozenset[str] = frozenset()
    supported_egress_components: frozenset[str] = frozenset()
    capability_summary: str = ""


class ChannelCapabilityProvider(Protocol):
    """按平台提供经 manifest 校验的端到端模态能力。"""

    def supports_ingress(
        self, platform: str, component_type: ComponentType | str
    ) -> bool: ...

    def supports_processing(self, platform: str, capability: str) -> bool: ...

    def supported_prompt_projections(self, platform: str) -> frozenset[str]: ...

    def supported_egress_components(self, platform: str) -> frozenset[str]: ...

    def prompt_summary(self, platform: str) -> str: ...


class ChannelRuntimeContextProvider(Protocol):
    """可选的 Channel 会话运行态扩展，不改变基础出站协议。"""

    async def runtime_context(self, session: SessionView) -> ChannelRuntimeContext: ...


class AttachmentValidator(Protocol):
    """校验 Channel 规范化后的附件引用确实指向受控内容。"""

    def validate_image_ref(self, component: MessageComponent) -> None: ...

    def read_image_ref(self, component: MessageComponent) -> bytes: ...

    def validate_attachment_ref(self, component: MessageComponent) -> None: ...

    def garbage_collect_history_files(
        self,
        candidate_paths: set[str],
        referenced_paths: set[str],
    ) -> dict[str, int]: ...

    def purge_all_user_media(self) -> dict[str, int]: ...

    def read_attachment_ref(self, component: MessageComponent) -> bytes: ...
