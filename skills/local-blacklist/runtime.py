"""local-blacklist 的可移植运行时插件入口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

SKILL_NAME = "local-blacklist"
CAPABILITY_NAME = "local-blacklist"


class BlacklistHost(Protocol):
    """宿主必须实现的最小黑名单能力；Skill 不依赖具体数据库或 Channel。"""

    async def is_available(self) -> bool: ...

    async def block_current_user(
        self,
        *,
        context: Any,
        reason: str,
        caption: str | None,
    ) -> dict[str, Any]: ...


class BlockUserArguments(BaseModel):
    """终态工具的稳定参数契约。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
    caption: str | None = Field(default=None, max_length=1000)


@dataclass(frozen=True, slots=True)
class PortableTool:
    """由宿主通用装载器读取的工具描述。"""

    name: str
    description: str
    arguments_model: type[BaseModel]
    handler: Callable[[BaseModel, Any], Awaitable[dict[str, Any]]]
    terminal: bool = False


class BlacklistPlugin:
    """拥有工具契约、加载状态与终态回复规则的独立插件。"""

    name = SKILL_NAME

    def __init__(self, host: BlacklistHost) -> None:
        self.host = host

    async def is_available(self) -> bool:
        return await self.host.is_available()

    async def load(self, context: Any) -> dict[str, Any]:
        # 拉黑是宿主本地副作用，无需预读外部状态；仅声明就绪。
        return {"ready": True}

    def tools(self) -> list[PortableTool]:
        return [
            PortableTool(
                name="block_user",
                description=(
                    "在确认对方屡次严重违反社会主义核心价值观、公序良俗或涉嫌违法犯罪且提醒不改后，"
                    "将对方拉入本地黑名单；成功即结束本轮回复并以告别语收尾，"
                    "之后对方消息不再受理。不能与其他工具并列调用。"
                ),
                arguments_model=BlockUserArguments,
                handler=self._block_user,
                terminal=True,
            )
        ]

    async def _block_user(
        self, arguments: BaseModel, context: Any
    ) -> dict[str, Any]:
        args = BlockUserArguments.model_validate(arguments)
        if self.name not in context.loaded_skills:
            raise ValueError("必须先调用 load_skill 读取 local-blacklist")
        if "block_user" in context.attempted_terminal_tools:
            raise ValueError("本轮已经尝试过拉黑回复，请改用普通文字完成回复")
        context.attempted_terminal_tools.add("block_user")
        # 宿主负责解析当前 Turn 的发送者、写入黑名单并构造终态回复草稿。
        return await self.host.block_current_user(
            context=context,
            reason=args.reason,
            caption=args.caption,
        )


def create_plugin(capabilities: dict[str, Any]) -> BlacklistPlugin:
    """创建插件；复制本目录后只需由宿主提供同名能力即可装载。"""

    try:
        host = capabilities[CAPABILITY_NAME]
    except KeyError as exc:
        raise RuntimeError(f"宿主缺少能力: {CAPABILITY_NAME}") from exc
    return BlacklistPlugin(host)
