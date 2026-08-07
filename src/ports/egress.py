"""输出过滤（egress）插件协议，独立于 ingress 以避免循环导入。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ports import ModelMessage, ModelProvider

EgressHandler = Callable[["EgressEnvelope"], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class EgressEnvelope:
    """模型输出待过滤的文本及重新生成所需的最小上下文。

    ``text`` 是模型最终面向用户的可见文本（已从工具结果或 JSON 中提取）。
    ``prompt_messages`` 是产生该文本时的原始 prompt，用于在原文命中敏感词后
    追加纠正指令并重新调用模型。
    """

    session_id: str
    turn_id: str
    text: str
    prompt_messages: list[ModelMessage]
    model: ModelProvider
    model_name: str
    temperature: float
    max_tokens: int


@dataclass(frozen=True, slots=True)
class EgressPluginContext:
    """宿主授予输出过滤插件的只读配置与检测模型解析器。"""

    plugin_id: str
    plugin_root: Path
    options: Mapping[str, Any]
    detection_model_resolver: Callable[[], ModelProvider | None]


class EgressPlugin(Protocol):
    """可拦截模型输出文本的出站过滤插件。"""

    plugin_id: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def filter(self, envelope: EgressEnvelope, call_next: EgressHandler) -> str: ...
