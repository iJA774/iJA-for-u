"""发现插件声明的 Skill 根目录和受管外部服务。"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from domain.errors import InputValidationError

from .capabilities import (
    WEB_SIMULATOR_CAPABILITIES,
    ChannelCapabilityMatrix,
    ChannelCapabilityRegistry,
)

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_AUTHORIZATION_SCOPE = re.compile(
    r"^[a-z][a-z0-9-]*(?::[a-z][a-z0-9-]*)+$"
)
_ENTRYPOINT_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDERS = {"{python}", "{plugin_root}", "{project_root}"}
_RUNTIME_KINDS = frozenset({"channel", "ingress", "egress", "tool_guard"})
_CONTRIBUTION_KINDS = frozenset({"extension"})
_ACTIVATION_MODES = frozenset({"automatic", "explicit"})


@dataclass(frozen=True, slots=True)
class ManagedServiceSpec:
    """经边界校验的插件外部服务声明。"""

    plugin_id: str
    plugin_root: Path
    project_root: Path
    name: str
    command: tuple[str, ...]
    working_directory: Path
    capabilities: tuple[str, ...]
    startup_timeout_seconds: float
    request_timeout_seconds: float
    shutdown_timeout_seconds: float

    @property
    def service_id(self) -> str:
        """返回用于日志和协议握手的稳定服务标识。"""

        return f"{self.plugin_id}:{self.name}"

    def resolved_command(self) -> tuple[str, ...]:
        """替换宿主提供的可信路径占位符，不通过 shell 解释参数。"""

        replacements = {
            "{python}": sys.executable,
            "{plugin_root}": str(self.plugin_root),
            "{project_root}": str(self.project_root),
        }
        return tuple(replacements.get(item, item) for item in self.command)


@dataclass(frozen=True, slots=True)
class RuntimePluginManifest:
    """插件宿主可直接装载的运行时声明。"""

    plugin_id: str
    kind: str
    plugin_root: Path
    entrypoint: str
    requires: tuple[str, ...]
    activation: str
    capabilities: ChannelCapabilityMatrix | None


@dataclass(frozen=True, slots=True)
class PluginContribution:
    """一个插件向宿主声明的可移植能力。"""

    plugin_id: str
    plugin_root: Path
    skill_roots: tuple[Path, ...]
    required_scopes: frozenset[str]
    managed_services: tuple[ManagedServiceSpec, ...]
    runtime: RuntimePluginManifest | None


class PluginContributionCatalog:
    """自动扫描并统一校验插件贡献与运行时清单。"""

    def __init__(self, project_root: Path, *, include_bundled: bool = False) -> None:
        self.project_root = project_root.resolve()
        project_plugins_root = self._validate_plugins_root(
            self.project_root,
            self.project_root / "plugins",
        )
        bundled_plugins_root = Path(__file__).resolve().parents[1]
        roots = [project_plugins_root]
        if include_bundled:
            bundled_owner = bundled_plugins_root.parent.resolve()
            validated_bundled_root = self._validate_plugins_root(
                bundled_owner,
                bundled_plugins_root,
            )
            # 内置插件先扫描，项目插件可用同 ID 显式覆盖，便于本地修复和开发。
            roots = list(
                dict.fromkeys((validated_bundled_root, project_plugins_root))
            )
        self.plugins_root = project_plugins_root
        self.plugins_roots = tuple(roots)
        # 延续既有的数据边界：内置目录只作为运行时插件回退来源，Skill 与
        # 受管服务仍必须位于当前项目的 plugins/ 中。
        self._contribution_roots = frozenset({project_plugins_root})
        self._contributions = self._scan()
        self._runtime_manifests = tuple(
            contribution.runtime
            for contribution in self._contributions
            if contribution.runtime is not None
        )

    @staticmethod
    def _validate_plugins_root(owner_root: Path, plugins_candidate: Path) -> Path:
        """校验一个插件根目录没有通过重解析点逃逸其 owner。"""

        is_junction = getattr(plugins_candidate, "is_junction", lambda: False)
        if plugins_candidate.is_symlink() or is_junction():
            raise InputValidationError(
                "plugins 根目录禁止使用符号链接或 junction"
            )
        plugins_root = plugins_candidate.resolve()
        if plugins_root.parent != owner_root:
            raise InputValidationError("plugins 根目录越出项目边界")
        return plugins_root

    @property
    def skill_roots(self) -> tuple[Path, ...]:
        """返回所有插件贡献的普通 Skill 根目录。"""

        return tuple(
            root
            for contribution in self._contributions
            if contribution.plugin_root.parent in self._contribution_roots
            for root in contribution.skill_roots
        )

    @property
    def managed_services(self) -> tuple[ManagedServiceSpec, ...]:
        """返回所有插件声明的受管服务。"""

        return tuple(
            service
            for contribution in self._contributions
            if contribution.plugin_root.parent in self._contribution_roots
            for service in contribution.managed_services
        )

    @property
    def skill_required_scopes(self) -> dict[Path, frozenset[str]]:
        """返回每个插件 Skill 根目录所需的宿主授权 scope。"""

        return {
            root: contribution.required_scopes
            for contribution in self._contributions
            if contribution.plugin_root.parent in self._contribution_roots
            for root in contribution.skill_roots
        }

    @property
    def runtime_manifests(self) -> tuple[RuntimePluginManifest, ...]:
        """返回所有受支持运行时扩展点的清单。"""

        return self._runtime_manifests

    def runtime_manifest(self, plugin_id: str) -> RuntimePluginManifest | None:
        """按稳定 ID 查找运行时清单。"""

        return next(
            (
                manifest
                for manifest in self._runtime_manifests
                if manifest.plugin_id == plugin_id
            ),
            None,
        )

    def _scan(self) -> tuple[PluginContribution, ...]:
        contributions_by_id: dict[str, PluginContribution] = {}
        for plugins_root in self.plugins_roots:
            if not plugins_root.exists():
                continue
            if not plugins_root.is_dir():
                raise InputValidationError(f"插件根路径不是目录: {plugins_root}")
            for plugin_root in sorted(
                plugins_root.iterdir(), key=lambda item: item.name
            ):
                if plugin_root.name.startswith("_"):
                    continue
                is_junction = getattr(plugin_root, "is_junction", lambda: False)
                if plugin_root.is_symlink() or is_junction():
                    raise InputValidationError(
                        f"插件目录禁止使用符号链接或 junction: {plugin_root.name}"
                    )
                if not plugin_root.is_dir():
                    continue
                resolved_plugin_root = plugin_root.resolve()
                if resolved_plugin_root.parent != plugins_root:
                    raise InputValidationError(
                        f"插件目录不是 plugins 的直接子目录: {plugin_root.name}"
                    )
                manifest_path = plugin_root / "plugin.toml"
                if not manifest_path.is_file():
                    continue
                contribution = self._read_manifest(
                    resolved_plugin_root,
                    manifest_path,
                )
                contributions_by_id[contribution.plugin_id] = contribution
        return tuple(
            contributions_by_id[plugin_id]
            for plugin_id in sorted(contributions_by_id)
        )

    def _read_manifest(
        self, plugin_root: Path, manifest_path: Path
    ) -> PluginContribution:
        try:
            with manifest_path.open("rb") as file:
                raw = tomllib.load(file)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise InputValidationError(f"插件清单损坏: {plugin_root.name}") from exc
        plugin_id = raw.get("id")
        if (
            not isinstance(plugin_id, str)
            or not _PLUGIN_ID.fullmatch(plugin_id)
            or plugin_id != plugin_root.name
        ):
            raise InputValidationError(f"插件清单身份无效: {plugin_root.name}")
        allowed_fields = {
            "id",
            "name",
            "kind",
            "version",
            "entrypoint",
            "requires",
            "activation",
            "contributes",
            "managed_services",
            "capabilities",
        }
        unknown_fields = set(raw) - allowed_fields
        if unknown_fields:
            raise InputValidationError(
                f"插件清单包含未知字段: {plugin_id}: {sorted(unknown_fields)}"
            )
        skill_roots, required_scopes = self._read_skill_contribution(
            plugin_id,
            plugin_root,
            raw.get("contributes"),
        )
        services = self._read_services(plugin_id, plugin_root, raw.get("managed_services", []))
        runtime = self._read_runtime_manifest(plugin_id, plugin_root, raw)
        return PluginContribution(
            plugin_id=plugin_id,
            plugin_root=plugin_root,
            skill_roots=skill_roots,
            required_scopes=required_scopes,
            managed_services=services,
            runtime=runtime,
        )

    @staticmethod
    def _read_runtime_manifest(
        plugin_id: str,
        plugin_root: Path,
        raw: dict[str, object],
    ) -> RuntimePluginManifest | None:
        """读取标准运行时扩展声明；其他 kind 只参与通用贡献。"""

        kind = raw.get("kind")
        if kind is None or kind in _CONTRIBUTION_KINDS:
            return None
        if not isinstance(kind, str) or kind not in _RUNTIME_KINDS:
            raise InputValidationError(f"插件 kind 无效: {plugin_id}")
        entrypoint = raw.get("entrypoint")
        if not isinstance(entrypoint, str) or entrypoint.count(":") != 1:
            raise InputValidationError(f"插件 entrypoint 无效: {plugin_id}")
        module_name, factory_name = entrypoint.split(":", maxsplit=1)
        if not _ENTRYPOINT_PART.fullmatch(
            module_name
        ) or not _ENTRYPOINT_PART.fullmatch(factory_name):
            raise InputValidationError(f"插件 entrypoint 无效: {plugin_id}")
        if not (plugin_root / f"{module_name}.py").is_file():
            raise InputValidationError(f"插件入口文件不存在: {plugin_id}")
        raw_requires = raw.get("requires", [])
        if (
            not isinstance(raw_requires, list)
            or any(
                not isinstance(item, str)
                or _PLUGIN_ID.fullmatch(item) is None
                or item == plugin_id
                for item in raw_requires
            )
            or len(raw_requires) != len(set(raw_requires))
        ):
            raise InputValidationError(f"插件 requires 无效: {plugin_id}")
        activation = raw.get("activation", "explicit")
        if not isinstance(activation, str) or activation not in _ACTIVATION_MODES:
            raise InputValidationError(f"插件 activation 无效: {plugin_id}")
        capabilities = PluginContributionCatalog._read_channel_capabilities(
            plugin_id,
            kind,
            raw.get("capabilities"),
        )
        return RuntimePluginManifest(
            plugin_id=plugin_id,
            kind=kind,
            plugin_root=plugin_root,
            entrypoint=entrypoint,
            requires=tuple(raw_requires),
            activation=activation,
            capabilities=capabilities,
        )

    @staticmethod
    def _read_channel_capabilities(
        plugin_id: str,
        kind: str,
        raw: object,
    ) -> ChannelCapabilityMatrix | None:
        """Channel 必须完整声明矩阵，其他扩展点禁止夹带该字段。"""

        if kind != "channel":
            if raw is not None:
                raise InputValidationError(
                    f"只有 channel 插件可以声明 capabilities: {plugin_id}"
                )
            return None
        if not isinstance(raw, dict):
            raise InputValidationError(
                f"channel 插件缺少 capabilities: {plugin_id}"
            )
        try:
            return ChannelCapabilityMatrix.model_validate(raw)
        except ValidationError as exc:
            raise InputValidationError(
                f"channel capabilities 无效: {plugin_id}: {exc.errors()[0]['msg']}"
            ) from exc

    @property
    def channel_capabilities(self) -> ChannelCapabilityRegistry:
        """合并内置 Web 模拟器与所有 Channel manifest。"""

        return ChannelCapabilityRegistry(
            [
                (None, WEB_SIMULATOR_CAPABILITIES),
                *[
                    (manifest.plugin_id, manifest.capabilities)
                    for manifest in self._runtime_manifests
                    if manifest.kind == "channel"
                    and manifest.capabilities is not None
                ],
            ]
        )

    @staticmethod
    def _inside(root: Path, relative: str, *, label: str) -> Path:
        if not relative or Path(relative).is_absolute():
            raise InputValidationError(f"{label} 必须是插件内相对路径")
        resolved = (root / relative).resolve()
        if resolved != root and root not in resolved.parents:
            raise InputValidationError(f"{label} 路径越界")
        return resolved

    def _read_skill_contribution(
        self, plugin_id: str, plugin_root: Path, raw: object
    ) -> tuple[tuple[Path, ...], frozenset[str]]:
        if raw is None:
            return (), frozenset()
        if (
            not isinstance(raw, dict)
            or "skill_roots" not in raw
            or set(raw) - {"skill_roots", "required_scopes"}
        ):
            raise InputValidationError(f"插件 contributes 字段无效: {plugin_id}")
        values = raw.get("skill_roots")
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(item, str) for item in values)
            or len(values) != len(set(values))
        ):
            raise InputValidationError(f"插件 skill_roots 无效: {plugin_id}")
        roots: list[Path] = []
        for value in values:
            root = self._inside(plugin_root, value, label=f"插件 {plugin_id} skill_root")
            if not root.is_dir():
                raise InputValidationError(f"插件 Skill 根目录不存在: {root}")
            roots.append(root)
        raw_scopes = raw.get("required_scopes", [])
        if (
            not isinstance(raw_scopes, list)
            or any(
                not isinstance(scope, str)
                or not _AUTHORIZATION_SCOPE.fullmatch(scope)
                or "*" in scope
                for scope in raw_scopes
            )
            or len(raw_scopes) != len(set(raw_scopes))
        ):
            raise InputValidationError(f"插件 required_scopes 无效: {plugin_id}")
        return tuple(roots), frozenset(raw_scopes)

    def _read_services(
        self, plugin_id: str, plugin_root: Path, raw: object
    ) -> tuple[ManagedServiceSpec, ...]:
        if not isinstance(raw, list):
            raise InputValidationError(f"插件 managed_services 必须是数组: {plugin_id}")
        services: list[ManagedServiceSpec] = []
        seen_names: set[str] = set()
        seen_capabilities: set[str] = set()
        allowed = {
            "name",
            "command",
            "working_directory",
            "capabilities",
            "startup_timeout_seconds",
            "request_timeout_seconds",
            "shutdown_timeout_seconds",
        }
        for item in raw:
            if not isinstance(item, dict) or set(item) - allowed:
                raise InputValidationError(f"插件 managed_service 字段无效: {plugin_id}")
            name = item.get("name")
            command = item.get("command")
            capabilities = item.get("capabilities")
            if (
                not isinstance(name, str)
                or not _SERVICE_NAME.fullmatch(name)
                or name in seen_names
            ):
                raise InputValidationError(f"插件 managed_service name 无效: {plugin_id}")
            if (
                not isinstance(command, list)
                or not command
                or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command)
            ):
                raise InputValidationError(f"插件 managed_service command 无效: {plugin_id}:{name}")
            if any("{" in arg or "}" in arg for arg in command if arg not in _PLACEHOLDERS):
                raise InputValidationError(f"插件 managed_service 含未知占位符: {plugin_id}:{name}")
            if (
                not isinstance(capabilities, list)
                or not capabilities
                or any(
                    not isinstance(value, str)
                    or not _CAPABILITY_NAME.fullmatch(value)
                    or value in seen_capabilities
                    for value in capabilities
                )
                or len(capabilities) != len(set(capabilities))
            ):
                raise InputValidationError(
                    f"插件 managed_service capabilities 无效: {plugin_id}:{name}"
                )
            working = item.get("working_directory", ".")
            if not isinstance(working, str):
                raise InputValidationError(
                    f"插件 managed_service working_directory 无效: {plugin_id}:{name}"
                )
            working_directory = self._inside(
                plugin_root,
                working,
                label=f"插件 {plugin_id}:{name} working_directory",
            )
            if not working_directory.is_dir():
                raise InputValidationError(
                    f"插件 managed_service 工作目录不存在: {plugin_id}:{name}"
                )
            self._validate_command(plugin_id, name, plugin_root, command)
            timeouts = {
                field: self._timeout(item, field, default)
                for field, default in (
                    ("startup_timeout_seconds", 5.0),
                    ("request_timeout_seconds", 30.0),
                    ("shutdown_timeout_seconds", 5.0),
                )
            }
            services.append(
                ManagedServiceSpec(
                    plugin_id=plugin_id,
                    plugin_root=plugin_root,
                    project_root=self.project_root,
                    name=name,
                    command=tuple(command),
                    working_directory=working_directory,
                    capabilities=tuple(capabilities),
                    **timeouts,
                )
            )
            seen_names.add(name)
            seen_capabilities.update(capabilities)
        return tuple(services)

    def _validate_command(
        self, plugin_id: str, name: str, plugin_root: Path, command: list[str]
    ) -> None:
        executable = command[0]
        if executable == "{python}":
            if len(command) < 2 or command[1] in _PLACEHOLDERS:
                raise InputValidationError(
                    f"插件 Python managed_service 缺少脚本: {plugin_id}:{name}"
                )
            script = self._inside(
                plugin_root,
                command[1],
                label=f"插件 {plugin_id}:{name} Python 脚本",
            )
            if script.suffix != ".py" or not script.is_file():
                raise InputValidationError(
                    f"插件 managed_service Python 脚本不存在: {plugin_id}:{name}"
                )
            # 子进程 cwd 可以是插件内任意子目录；校验后必须固化绝对路径，
            # 不能让启动结果继续依赖 cwd 的偶然取值。
            command[1] = str(script)
            return
        if executable in {"{plugin_root}", "{project_root}"}:
            raise InputValidationError(f"插件 managed_service 可执行文件无效: {plugin_id}:{name}")
        executable_path = self._inside(
            plugin_root,
            executable,
            label=f"插件 {plugin_id}:{name} 可执行文件",
        )
        if not executable_path.is_file():
            raise InputValidationError(f"插件 managed_service 可执行文件不存在: {plugin_id}:{name}")
        command[0] = str(executable_path)

    @staticmethod
    def _timeout(item: dict[str, object], field: str, default: float) -> float:
        value = item.get(field, default)
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0.1 <= value <= 300:
            raise InputValidationError(f"插件 managed_service {field} 必须在 0.1 到 300 秒")
        return float(value)
