"""通用 Skill 加载工具；具体工具由 Skill 包声明。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from domain.errors import InputValidationError
from tools.registry import RegisteredTool, ToolContext

if TYPE_CHECKING:
    from skill_runtime.catalog import SkillCatalog
    from skill_runtime.plugins import SkillPluginManager


class LoadSkillArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def build_skill_tools(catalog: SkillCatalog, plugins: SkillPluginManager) -> list[RegisteredTool]:
    """构造通用加载工具，并附加各 Skill 包自己的工具。"""

    async def load_skill(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        args = LoadSkillArguments.model_validate(arguments)
        if args.skill not in context.available_skills:
            raise InputValidationError(f"本轮不可使用 Skill: {args.skill}")
        record = catalog.get(args.skill)
        missing_scopes = record.required_scopes - context.authorization_scopes
        if missing_scopes:
            raise InputValidationError(
                f"未授权加载 Skill {args.skill}，缺少 scope: "
                + ", ".join(sorted(missing_scopes))
            )
        result: dict[str, object] = {"skill": record.name, "instructions": record.body}
        runtime = await plugins.load(record.name, context)
        if runtime is not None:
            result["runtime"] = runtime
        context.loaded_skills.add(record.name)
        return result

    return [
        RegisteredTool(
            name="load_skill",
            description="读取本轮可用 Skill 的完整说明。决定使用某个 Skill 后必须先调用此工具。",
            arguments_model=LoadSkillArguments,
            handler=load_skill,
        ),
        *plugins.registered_tools(),
    ]
