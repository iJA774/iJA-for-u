import asyncio

import pytest

from domain.errors import InputValidationError
from domain.models import ComponentType, MessageComponent, ReplyDraft
from skill_runtime import SkillCatalog, SkillPluginManager
from tools import ToolContext, ToolRegistry, build_skill_tools


class FakeBlacklistHost:
    """模拟宿主黑名单能力，记录调用并返回终态回复草稿。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def is_available(self) -> bool:
        return True

    async def block_current_user(self, *, context, reason, caption):
        self.calls.append(
            {
                "session_id": context.session_id,
                "actor_id": context.actor_id,
                "reason": reason,
                "caption": caption,
            }
        )
        return {
            "value": {
                "blocked": True,
                "external_user_id": context.actor_id,
                "display_name": "违规者",
            },
            "reply_draft": ReplyDraft(
                components=[MessageComponent.text_component(caption or "再见")]
            ),
        }


def test_local_blacklist_skill_missing_capability_is_not_exposed(settings) -> None:
    catalog = SkillCatalog(settings.project_root / "skills")
    plugins = SkillPluginManager(catalog)

    # 缺少 local-blacklist capability 时不得伪装成可用 Skill
    assert "local-blacklist" in catalog.names
    assert "local-blacklist" not in asyncio.run(plugins.available_skills())
    assert plugins.tool_names({"local-blacklist"}) == set()


@pytest.mark.asyncio
async def test_local_blacklist_skill_block_user_requires_load(settings) -> None:
    host = FakeBlacklistHost()
    catalog = SkillCatalog(settings.project_root / "skills")
    plugins = SkillPluginManager(catalog, capabilities={"local-blacklist": host})
    assert plugins.plugin_names == {"local-blacklist"}
    assert plugins.tool_names({"local-blacklist"}) == {"block_user"}
    registry = ToolRegistry(build_skill_tools(catalog, plugins))

    context = ToolContext(
        session_id="session-test",
        actor_id="u1",
        available_skills={"local-blacklist"},
    )
    with pytest.raises(InputValidationError, match="必须先调用"):
        await registry.execute("block_user", {"reason": "违规"}, context)


@pytest.mark.asyncio
async def test_local_blacklist_skill_block_user_returns_terminal_reply(settings) -> None:
    host = FakeBlacklistHost()
    catalog = SkillCatalog(settings.project_root / "skills")
    plugins = SkillPluginManager(catalog, capabilities={"local-blacklist": host})
    registry = ToolRegistry(build_skill_tools(catalog, plugins))

    context = ToolContext(
        session_id="session-test",
        actor_id="u1",
        available_skills={"local-blacklist"},
    )
    await registry.execute("load_skill", {"skill": "local-blacklist"}, context)
    result = await registry.execute(
        "block_user",
        {"reason": "多次发布违法言论", "caption": "你已被拉黑"},
        context,
    )
    assert result.reply_draft is not None
    assert [c.type for c in result.reply_draft.components] == [ComponentType.TEXT]
    assert result.reply_draft.components[0].text == "你已被拉黑"
    assert result.value == {
        "blocked": True,
        "external_user_id": "u1",
        "display_name": "违规者",
    }
    assert host.calls[0]["actor_id"] == "u1"
    assert host.calls[0]["reason"] == "多次发布违法言论"

    # 终态工具：同一轮不得重复调用
    with pytest.raises(InputValidationError, match="已经尝试"):
        await registry.execute("block_user", {"reason": "再次"}, context)
