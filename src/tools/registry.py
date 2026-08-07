"""小而明确的工具注册与参数校验边界。"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from domain.errors import InputValidationError, NotFoundError
from domain.models import ReplyDraft
from ports import ToolDefinition


@dataclass(frozen=True, slots=True)
class ToolContext:
    """由应用层注入、模型不能伪造的调用上下文。"""

    session_id: str
    actor_id: str | None
    turn_id: str | None = None
    schedule_run_id: str | None = None
    source_text: str | None = None
    character_id: str | None = None
    available_skills: set[str] = field(default_factory=set)
    authorization_scopes: frozenset[str] = frozenset()
    loaded_skills: set[str] = field(default_factory=set)
    loaded_skill_versions: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    attempted_terminal_tools: set[str] = field(default_factory=set)
    supported_egress_components: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """工具的模型可见结果及可选终态回复。"""

    value: dict[str, Any]
    reply_draft: ReplyDraft | None = None


ToolHandler = Callable[[BaseModel, ToolContext], Awaitable[dict[str, Any] | ToolOutcome]]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    name: str
    description: str
    arguments_model: type[BaseModel]
    handler: ToolHandler
    terminal: bool = False
    required_scopes: frozenset[str] = frozenset()
    required_skill: str | None = None
    required_any_egress_components: frozenset[str] = frozenset()

    def supports_context_egress(self, components: frozenset[str]) -> bool:
        """工具声明了出站依赖时，至少一个依赖必须由当前 Channel 支持。"""

        return (
            not self.required_any_egress_components
            or bool(self.required_any_egress_components & components)
        )

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.arguments_model.model_json_schema(),
        )


class ToolRegistry:
    """只暴露调用点显式选择的白名单工具。"""

    def __init__(self, tools: list[RegisteredTool] | None = None) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: RegisteredTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        if any(
            not isinstance(scope, str)
            or re.fullmatch(
                r"[a-z][a-z0-9-]*(?::[a-z][a-z0-9-]*)+",
                scope,
            )
            is None
            for scope in tool.required_scopes
        ):
            raise ValueError(f"工具 required_scopes 无效: {tool.name}")
        self._tools[tool.name] = tool

    def definitions(
        self,
        names: set[str] | None = None,
        *,
        authorization_scopes: frozenset[str] = frozenset(),
        loaded_skills: set[str] | None = None,
    ) -> list[ToolDefinition]:
        """返回授权工具；提供 loaded_skills 时隐藏尚未加载的 Skill 工具。"""

        selected = (
            self._tools.values()
            if names is None
            else (self._tools[name] for name in sorted(names) if name in self._tools)
        )
        return [
            tool.definition()
            for tool in selected
            if tool.required_scopes <= authorization_scopes
            and (
                loaded_skills is None
                or tool.required_skill is None
                or tool.required_skill in loaded_skills
            )
        ]

    async def execute(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        tool = self._tools.get(name)
        if tool is None:
            raise NotFoundError(f"未知工具: {name}")
        missing_scopes = tool.required_scopes - context.authorization_scopes
        if missing_scopes:
            raise InputValidationError(
                f"未授权使用工具 {name}，缺少 scope: {', '.join(sorted(missing_scopes))}"
            )
        if tool.required_skill is not None and tool.required_skill not in context.loaded_skills:
            raise InputValidationError(
                f"必须先调用 load_skill 读取 {tool.required_skill}"
            )
        if not tool.supports_context_egress(context.supported_egress_components):
            raise InputValidationError(
                f"当前平台不具备工具 {name} 所需的真实出站能力"
            )
        try:
            validated = tool.arguments_model.model_validate(arguments)
        except ValidationError as exc:
            raise InputValidationError(f"工具 {name} 参数无效: {exc}") from exc
        result = await tool.handler(validated, context)
        return result if isinstance(result, ToolOutcome) else ToolOutcome(value=result)

    def is_terminal(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.terminal)

    @property
    def names(self) -> set[str]:
        return set(self._tools)
