"""任务执行守卫的只读观测契约；守卫不能执行工具或修改任务事实。"""

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ToolObservation:
    """一次已审计调用的不可变投影，正文仅供内存检测使用。"""

    name: str
    arguments: str
    result: str
    succeeded: bool


@dataclass(frozen=True, slots=True)
class GuardViolation:
    """可公开的终止原因；禁止放入参数、结果或聊天正文。"""

    reason: str
    message: str


@dataclass(frozen=True, slots=True)
class ToolGuardPluginContext:
    """守卫插件仅获得路径和管理员配置，不获得模型、存储或发送能力。"""

    plugin_id: str
    plugin_root: Path
    options: Mapping[str, Any]


class ToolGuard(Protocol):
    """单次 run 独占的检测状态；超时由宿主统一执行。"""

    timeout_seconds: float

    def observe(self, observation: ToolObservation) -> GuardViolation | None:
        """消费已完成调用；返回终止原因或允许继续。"""
        ...


class ToolGuardPlugin(Protocol):
    """支持 generation 生命周期的守卫工厂。"""

    plugin_id: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def create_guard(self) -> ToolGuard:
        """每次执行创建新状态，禁止跨会话共享计数。"""
        ...


ToolGuardLease = Callable[
    [], AbstractAsyncContextManager[tuple[tuple[str, ToolGuard], ...]]
]
