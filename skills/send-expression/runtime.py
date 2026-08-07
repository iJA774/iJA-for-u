"""send-expression 的可移植运行时插件入口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .scripts.expression_core import build_generation_prompt, normalize_expression_name

SKILL_NAME = "send-expression"
CAPABILITY_NAME = "expression-library"


class ExpressionLibraryHost(Protocol):
    """宿主必须实现的最小表情库能力；Skill 不依赖具体数据库或 Channel。"""

    async def availability(self, *, context: Any | None = None) -> dict[str, Any]: ...

    async def is_available(self, *, context: Any | None = None) -> bool: ...

    async def get_by_generation_key(self, generation_key: str) -> Any | None: ...

    async def select_for_reply(self, **kwargs: Any) -> dict[str, Any]: ...

    async def prepare_reply(self, **kwargs: Any) -> Any: ...


class SendExpressionArguments(BaseModel):
    """终态工具的稳定参数契约。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["select", "reuse", "generate"]
    name: str | None = Field(default=None, min_length=1, max_length=40)
    emotion: str = Field(min_length=1, max_length=120)
    selection_query: str | None = Field(default=None, max_length=500)
    image_prompt: str | None = Field(default=None, max_length=500)
    caption: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_action_fields(self) -> SendExpressionArguments:
        if self.action in {"reuse", "generate"} and not (self.name or "").strip():
            raise ValueError("reuse 和 generate 必须提供 name")
        if self.action == "select" and not (self.selection_query or "").strip():
            raise ValueError("select 必须提供 selection_query")
        if self.action == "select" and self.name is not None:
            raise ValueError("select 由宿主选择素材，不得提供 name")
        if self.action == "generate" and not (self.image_prompt or "").strip():
            raise ValueError("generate 必须提供 image_prompt")
        if self.action != "generate" and self.image_prompt is not None:
            raise ValueError("只有 generate 可以提供 image_prompt")
        if self.action != "select" and self.selection_query is not None:
            raise ValueError("只有 select 可以提供 selection_query")
        return self


@dataclass(frozen=True, slots=True)
class PortableTool:
    """由宿主通用装载器读取的工具描述。"""

    name: str
    description: str
    arguments_model: type[BaseModel]
    handler: Callable[[BaseModel, Any], Awaitable[dict[str, Any]]]
    terminal: bool = False
    required_any_egress_components: frozenset[str] = frozenset()


class SendExpressionPlugin:
    """拥有工具契约、加载状态和周期恢复规则的独立插件。"""

    name = SKILL_NAME

    def __init__(self, host: ExpressionLibraryHost) -> None:
        self.host = host

    async def is_available(self, *, context: Any | None = None) -> bool:
        return await self.host.is_available(context=context)

    async def load(self, context: Any) -> dict[str, Any]:
        runtime = await self.host.availability(context=context)
        context.loaded_skill_versions[self.name] = (
            str(runtime["character_id"]),
            (str(runtime["base_image_sha256"]) if runtime.get("base_image_sha256") is not None else None),
        )
        return runtime

    def tools(self) -> list[PortableTool]:
        return [
            PortableTool(
                name="send_expression",
                description=(
                    "在加载 send-expression 后，以混合选择、精确复用或生成方式准备一张角色表情及可选文字；"
                    "成功即结束本轮回复，不能与其他工具并列调用。"
                ),
                arguments_model=SendExpressionArguments,
                handler=self._send_expression,
                terminal=True,
                required_any_egress_components=frozenset({"image_ref"}),
            )
        ]

    async def _send_expression(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        args = SendExpressionArguments.model_validate(arguments)
        if self.name not in context.loaded_skills:
            raise ValueError("必须先调用 load_skill 读取 send-expression")
        if "send_expression" in context.attempted_terminal_tools:
            raise ValueError("本轮已经尝试过表情回复，请改用普通文字完成回复")
        loaded_version = context.loaded_skill_versions.get(self.name)
        if loaded_version is None:
            raise ValueError("send-expression 的加载快照缺失，请重新加载")
        if args.action == "select" and context.schedule_run_id is not None:
            raise ValueError("周期任务必须使用精确 reuse 或 generate，不能动态 select")
        context.attempted_terminal_tools.add("send_expression")
        loaded_character_id, loaded_portrait_sha256 = loaded_version
        selection: dict[str, Any] | None = None
        selected_name = args.name
        if args.action == "select":
            selection = await self.host.select_for_reply(
                query=f"{args.emotion}。{args.selection_query or ''}",
                expected_character_id=loaded_character_id,
                expected_portrait_sha256=loaded_portrait_sha256,
            )
            selected_name = str(selection["name"])
        if selected_name is None:
            raise ValueError("表情名称缺失")
        expression_name = normalize_expression_name(selected_name)
        model_prompt = (
            build_generation_prompt(args.emotion, args.image_prompt or "")
            if args.action == "generate"
            else None
        )
        draft = await self.host.prepare_reply(
            action="reuse" if args.action == "select" else args.action,
            name=selected_name,
            normalized_name=expression_name.normalized,
            filename=expression_name.filename,
            emotion=args.emotion,
            image_prompt=args.image_prompt,
            model_prompt=model_prompt,
            caption=args.caption,
            expected_character_id=loaded_character_id,
            expected_portrait_sha256=loaded_portrait_sha256,
            expected_expression_id=(
                str(selection["expression_id"]) if selection is not None else None
            ),
            generation_key=context.schedule_run_id,
        )
        return {
            "value": {
                "prepared": True,
                "action": args.action,
                "name": expression_name.visible,
                "expression_id": getattr(draft, "expression_id", None),
                "has_caption": bool((args.caption or "").strip()),
                **({"selection": selection["selection"]} if selection else {}),
            },
            "reply_draft": draft,
        }

    async def recover_schedule(self, run_id: str, executions: list[Any]) -> Any | None:
        """恢复已经进入工具审计的周期表情，绝不重试状态不明的付费调用。"""

        expression_call = next((item for item in executions if item.tool_name == "send_expression"), None)
        if expression_call is None:
            return None
        skill_load = next(
            (
                item
                for item in executions
                if item.tool_name == "load_skill"
                and isinstance(item.result, dict)
                and item.result.get("skill") == self.name
            ),
            None,
        )
        runtime_state = (
            skill_load.result.get("runtime")
            if skill_load is not None and isinstance(skill_load.result, dict)
            else None
        )
        expected_character_id = (
            str(runtime_state.get("character_id"))
            if isinstance(runtime_state, dict) and runtime_state.get("character_id")
            else None
        )
        expected_portrait_sha256 = (
            str(runtime_state.get("base_image_sha256"))
            if isinstance(runtime_state, dict) and runtime_state.get("base_image_sha256")
            else None
        )
        args = SendExpressionArguments.model_validate(expression_call.arguments)
        if args.action == "select":
            raise RuntimeError("周期任务不支持动态表情选择，无法安全恢复")
        if args.name is None:
            raise RuntimeError("周期表情名称缺失")
        if args.action == "generate":
            generated = await self.host.get_by_generation_key(run_id)
            if generated is None:
                raise RuntimeError("上次周期表情生成是否扣费无法确认，本次不自动重试图片模型")
            if generated.name != args.name:
                raise RuntimeError("周期表情生成幂等键与工具参数不一致")
        expression_name = normalize_expression_name(args.name)
        return await self.host.prepare_reply(
            action="reuse",
            name=args.name,
            normalized_name=expression_name.normalized,
            filename=expression_name.filename,
            emotion=args.emotion,
            image_prompt=None,
            model_prompt=None,
            caption=args.caption,
            expected_character_id=expected_character_id,
            expected_portrait_sha256=expected_portrait_sha256,
            expected_expression_id=None,
            generation_key=None,
        )


def create_plugin(capabilities: dict[str, Any]) -> SendExpressionPlugin:
    """创建插件；复制本目录后只需由宿主提供同名能力即可装载。"""

    try:
        host = capabilities[CAPABILITY_NAME]
    except KeyError as exc:
        raise RuntimeError(f"宿主缺少能力: {CAPABILITY_NAME}") from exc
    return SendExpressionPlugin(host)
