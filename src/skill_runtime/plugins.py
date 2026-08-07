"""项目无关的 Skill 运行时插件发现、装载与工具适配。"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import logging
import re
import sys
import tomllib
import types
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from domain.errors import InputValidationError
from tools import RegisteredTool, ToolContext, ToolOutcome

from .catalog import SkillCatalog

logger = logging.getLogger(__name__)

TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class SkillRuntimeManifest:
    """经校验的 Skill 插件入口与宿主能力声明。"""

    api_version: int
    module_path: Path
    factory_name: str
    capabilities: tuple[str, ...]


class SkillPluginManager:
    """只依赖声明式能力装载 Skill 包，不包含任何具体 Skill 名称分支。"""

    API_VERSION = 1

    def __init__(
        self,
        catalog: SkillCatalog,
        *,
        capabilities: Mapping[str, object] | None = None,
    ) -> None:
        self.catalog = catalog
        self.capabilities = dict(capabilities or {})
        self._plugins: dict[str, Any] = {}
        self._runtime_skills: set[str] = set()
        self._tools_by_skill: dict[str, list[RegisteredTool]] = {}
        self._load_scopes_by_skill: dict[str, frozenset[str]] = {}
        self._load_plugins()

    @property
    def plugin_names(self) -> set[str]:
        return set(self._plugins)

    async def available_skills(
        self,
        authorization_scopes: frozenset[str] = frozenset(),
        *,
        context: ToolContext | None = None,
    ) -> set[str]:
        """返回说明型 Skill，并按当前工具上下文过滤运行态 Skill。"""

        # 声明了 runtime 却因 capability 缺失而未装载的 Skill 不能伪装成
        # 纯说明 Skill，否则模型会看到指令却永远拿不到其承诺的工具。
        available = {
            name
            for name in self.catalog.names - self._runtime_skills
            if self._required_scopes(name) <= authorization_scopes
        }
        for name, plugin in self._plugins.items():
            if not self._required_scopes(name) <= authorization_scopes:
                continue
            if context is not None:
                tools = self._tools_by_skill.get(name, [])
                if tools and not any(
                    tool.supports_context_egress(
                        context.supported_egress_components
                    )
                    for tool in tools
                ):
                    continue
            checker = getattr(plugin, "is_available", None)
            if checker is None:
                available.add(name)
                continue
            parameters = inspect.signature(checker).parameters
            is_available = (
                await checker(context=context)
                if "context" in parameters
                else await checker()
            )
            if bool(is_available):
                available.add(name)
        return available

    def tool_names(
        self,
        skills: set[str],
        authorization_scopes: frozenset[str] = frozenset(),
    ) -> set[str]:
        """返回指定 Skill 声明的工具名。"""

        return {
            tool.name
            for skill in skills
            for tool in self._tools_by_skill.get(skill, [])
            if tool.required_scopes <= authorization_scopes
        }

    def registered_tools(self) -> list[RegisteredTool]:
        """返回全部插件工具，供宿主统一注册审计。"""

        return [tool for tools in self._tools_by_skill.values() for tool in tools]

    async def load(self, name: str, context: ToolContext) -> dict[str, Any] | None:
        """执行 Skill 自己的加载钩子并返回模型可见运行态。"""

        missing_scopes = self._required_scopes(name) - context.authorization_scopes
        if missing_scopes:
            raise InputValidationError(
                f"未授权加载 Skill {name}，缺少 scope: "
                + ", ".join(sorted(missing_scopes))
            )
        plugin = self._plugins.get(name)
        if plugin is None:
            return None
        loader = getattr(plugin, "load", None)
        if loader is None:
            return None
        try:
            result = await loader(context)
        except ValueError as exc:
            raise InputValidationError(str(exc)) from exc
        if not isinstance(result, dict):
            raise InputValidationError(f"Skill load 必须返回 dict: {name}")
        return result

    async def recover_schedule(
        self,
        run_id: str,
        executions: list[Any],
        authorization_scopes: frozenset[str] = frozenset(),
    ) -> Any | None:
        """让声明恢复钩子的 Skill 重建已完成但未 PREPARED 的草稿。"""

        recovered: list[Any] = []
        for name, plugin in self._plugins.items():
            if not self.catalog.get(name).required_scopes <= authorization_scopes:
                continue
            hook = getattr(plugin, "recover_schedule", None)
            if hook is None:
                continue
            try:
                draft = await hook(run_id, executions)
            except ValueError as exc:
                raise InputValidationError(str(exc)) from exc
            if draft is not None:
                recovered.append(draft)
        if len(recovered) > 1:
            raise InputValidationError("多个 Skill 同时声明可恢复同一周期回复")
        return recovered[0] if recovered else None

    def _load_plugins(self) -> None:
        seen_tools: set[str] = set()
        logger.info(
            "开始加载 Skill 插件",
            extra={"session_id": "-", "turn_id": "-", "skill_count": len(self.catalog.names)},
        )
        for name in sorted(self.catalog.names):
            record = self.catalog.get(name)
            manifest_path = record.path.parent / "runtime.toml"
            if not manifest_path.is_file():
                continue
            self._runtime_skills.add(name)
            manifest = self._read_manifest(manifest_path, record.path.parent)
            missing = [
                capability
                for capability in manifest.capabilities
                if capability not in self.capabilities
            ]
            if missing:
                logger.info(
                    "Skill 插件缺少宿主能力，跳过",
                    extra={
                        "session_id": "-",
                        "turn_id": "-",
                        "skill": name,
                        "missing_capabilities": ",".join(missing),
                    },
                )
                continue
            module = self._load_module(name, record.path.parent, manifest.module_path)
            factory = getattr(module, manifest.factory_name, None)
            if factory is None or not callable(factory):
                raise InputValidationError(f"Skill runtime 工厂不存在: {name}")
            selected_capabilities = {
                item: self.capabilities[item] for item in manifest.capabilities
            }
            plugin = factory(selected_capabilities)
            if inspect.isawaitable(plugin):
                raise InputValidationError(f"Skill runtime 工厂不得是异步函数: {name}")
            if getattr(plugin, "name", None) != name:
                raise InputValidationError(f"Skill runtime name 与目录不一致: {name}")
            load_required_scopes = getattr(
                plugin,
                "load_required_scopes",
                frozenset(),
            )
            if (
                not isinstance(load_required_scopes, (set, frozenset))
                or any(not isinstance(scope, str) for scope in load_required_scopes)
            ):
                raise InputValidationError(
                    f"Skill load_required_scopes 不符合 API v1: {name}"
                )
            self._load_scopes_by_skill[name] = frozenset(load_required_scopes)
            tools = self._adapt_tools(
                name,
                plugin,
                seen_tools,
                record.required_scopes,
            )
            self._plugins[name] = plugin
            self._tools_by_skill[name] = tools
            logger.info(
                "Skill 插件已加载",
                extra={
                    "session_id": "-",
                    "turn_id": "-",
                    "skill": name,
                    "tool_count": len(tools),
                    "capabilities": ",".join(manifest.capabilities),
                },
            )

    def _required_scopes(self, name: str) -> frozenset[str]:
        """合并贡献根目录与 Skill runtime 自声明的加载权限。"""

        return (
            self.catalog.get(name).required_scopes
            | self._load_scopes_by_skill.get(name, frozenset())
        )

    def _adapt_tools(
        self,
        skill_name: str,
        plugin: Any,
        seen_tools: set[str],
        skill_required_scopes: frozenset[str],
    ) -> list[RegisteredTool]:
        source_tools = plugin.tools()
        if not isinstance(source_tools, list):
            raise InputValidationError(f"Skill tools() 必须返回 list: {skill_name}")
        tools: list[RegisteredTool] = []
        for source in source_tools:
            name = getattr(source, "name", None)
            description = getattr(source, "description", None)
            arguments_model = getattr(source, "arguments_model", None)
            handler = getattr(source, "handler", None)
            terminal = getattr(source, "terminal", False)
            source_required_scopes = getattr(
                source,
                "required_scopes",
                frozenset(),
            )
            required_any_egress_components = getattr(
                source,
                "required_any_egress_components",
                frozenset(),
            )
            if not isinstance(name, str) or not TOOL_NAME_PATTERN.fullmatch(name):
                raise InputValidationError(f"Skill 工具名无效: {skill_name}")
            if name in seen_tools:
                raise InputValidationError(f"Skill 工具名称重复: {name}")
            if not isinstance(description, str) or not description.strip():
                raise InputValidationError(f"Skill 工具说明为空: {name}")
            if (
                not isinstance(arguments_model, type)
                or not issubclass(arguments_model, BaseModel)
                or not callable(handler)
                or not isinstance(terminal, bool)
                or not isinstance(source_required_scopes, (set, frozenset))
                or any(not isinstance(scope, str) for scope in source_required_scopes)
                or not isinstance(
                    required_any_egress_components, (set, frozenset)
                )
                or any(
                    not isinstance(component, str)
                    for component in required_any_egress_components
                )
            ):
                raise InputValidationError(f"Skill 工具描述不符合 API v1: {name}")

            async def adapted_handler(
                arguments: BaseModel,
                context: ToolContext,
                *,
                portable_handler: Any = handler,
                tool_name: str = name,
            ) -> ToolOutcome:
                try:
                    result = await portable_handler(arguments, context)
                except ValueError as exc:
                    raise InputValidationError(str(exc)) from exc
                if not isinstance(result, dict) or not isinstance(
                    result.get("value"), dict
                ):
                    raise InputValidationError(
                        f"Skill 工具必须返回 value dict: {tool_name}"
                    )
                return ToolOutcome(
                    value=result["value"],
                    reply_draft=result.get("reply_draft"),
                )

            tools.append(
                RegisteredTool(
                    name=name,
                    description=description.strip(),
                    arguments_model=arguments_model,
                    handler=adapted_handler,
                    terminal=terminal,
                    required_scopes=(
                        skill_required_scopes
                        | frozenset(source_required_scopes)
                    ),
                    required_skill=skill_name,
                    required_any_egress_components=frozenset(
                        required_any_egress_components
                    ),
                )
            )
            seen_tools.add(name)
        return tools

    @classmethod
    def _read_manifest(cls, path: Path, skill_root: Path) -> SkillRuntimeManifest:
        try:
            with path.open("rb") as file:
                raw = tomllib.load(file)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise InputValidationError(f"Skill runtime.toml 损坏: {skill_root.name}") from exc
        if set(raw) != {"api_version", "entrypoint", "capabilities"}:
            raise InputValidationError(
                f"Skill runtime.toml 字段必须为 api_version/entrypoint/capabilities: {skill_root.name}"
            )
        api_version = raw.get("api_version")
        entrypoint = raw.get("entrypoint")
        capabilities = raw.get("capabilities")
        if api_version != cls.API_VERSION:
            raise InputValidationError(f"不支持的 Skill runtime API: {api_version}")
        if not isinstance(entrypoint, str) or entrypoint.count(":") != 1:
            raise InputValidationError(f"Skill entrypoint 格式无效: {skill_root.name}")
        module_name, factory_name = entrypoint.split(":", 1)
        module_path = (skill_root / module_name).resolve()
        if (
            skill_root.resolve() not in module_path.parents
            or module_path.suffix != ".py"
            or not module_path.is_file()
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", factory_name)
        ):
            raise InputValidationError(f"Skill entrypoint 越界或不存在: {skill_root.name}")
        if (
            not isinstance(capabilities, list)
            or not all(
                isinstance(item, str)
                and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", item)
                for item in capabilities
            )
            or len(set(capabilities)) != len(capabilities)
        ):
            raise InputValidationError(f"Skill capabilities 无效: {skill_root.name}")
        return SkillRuntimeManifest(
            api_version=api_version,
            module_path=module_path,
            factory_name=factory_name,
            capabilities=tuple(capabilities),
        )

    @staticmethod
    def _load_module(skill_name: str, skill_root: Path, module_path: Path) -> types.ModuleType:
        digest = hashlib.sha256(str(skill_root).encode()).hexdigest()[:12]
        package_name = f"_ija_skill_{skill_name.replace('-', '_')}_{digest}"
        package = types.ModuleType(package_name)
        package.__path__ = [str(skill_root)]
        package.__package__ = package_name
        sys.modules[package_name] = package
        module_name = f"{package_name}.runtime"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise InputValidationError(f"无法创建 Skill runtime 模块: {skill_name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            sys.modules.pop(package_name, None)
            raise InputValidationError(f"Skill runtime 装载失败: {skill_name}: {exc}") from exc
        return module
