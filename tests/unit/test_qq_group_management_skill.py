from __future__ import annotations

import asyncio
from typing import Any

import pytest

from application.qq_group_management import QQGroupManagementGateway
from domain.errors import InputValidationError
from domain.models import (
    ChatType,
    MessageComponent,
    MessageRole,
    SessionView,
    StoredMessage,
    utc_now,
)
from skill_runtime import SkillCatalog, SkillPluginManager
from tools import ToolContext, ToolRegistry, build_skill_tools

MANAGE_SCOPE = "channel:qq-group:manage"


class FakeStore:
    """为无策略宿主桥接提供当前会话与消息。"""

    def __init__(
        self,
        session: SessionView,
        message: StoredMessage | None = None,
    ) -> None:
        self.session = session
        self.message = message

    async def get_session(self, session_id: str):
        return self.session if session_id == self.session.id else None

    async def get_recallable_message(self, session_id: str, message_id: str):
        if self.message is not None and session_id == self.session.id and message_id == self.message.id:
            return self.message
        return None


class FakeChannel:
    """模拟 OneBot 实时角色查询并记录最终副作用。"""

    def __init__(self, roles: dict[str, str]) -> None:
        self.roles = roles
        self.inspections: list[str] = []
        self.actions: list[tuple[str, dict[str, Any]]] = []

    def has_group_management_route(self, platform: str) -> bool:
        return platform == "onebot"

    async def manage_group(
        self,
        session: SessionView,
        action: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        del session
        if action == "inspect_member":
            user_id = str(parameters["user_id"])
            self.inspections.append(user_id)
            return {
                "user_id": user_id,
                "display_name": f"成员{user_id[-2:]}",
                "role": self.roles[user_id],
            }
        self.actions.append((action, parameters))
        return {"success": True, "action": action}


def _group_session() -> SessionView:
    now = utc_now()
    return SessionView(
        id="session-group",
        platform="onebot",
        account_id="10001",
        external_chat_id="20001",
        chat_type=ChatType.GROUP,
        display_name="测试群",
        created_at=now,
        updated_at=now,
    )


def _context(
    *,
    scopes: frozenset[str] = frozenset({MANAGE_SCOPE}),
) -> ToolContext:
    return ToolContext(
        session_id="session-group",
        actor_id="30001",
        available_skills={"qq-group-management"},
        authorization_scopes=scopes,
    )


def _runtime(
    settings,
    *,
    roles: dict[str, str],
    message: StoredMessage | None = None,
) -> tuple[SkillPluginManager, ToolRegistry, FakeChannel]:
    channel = FakeChannel(roles)
    gateway = QQGroupManagementGateway(
        FakeStore(_group_session(), message),  # type: ignore[arg-type]
        channel,  # type: ignore[arg-type]
    )
    catalog = SkillCatalog(settings.project_root / "skills")
    plugins = SkillPluginManager(
        catalog,
        capabilities={"qq-group-management": gateway},
    )
    return plugins, ToolRegistry(build_skill_tools(catalog, plugins)), channel


def test_skill_missing_capability_is_not_available(settings) -> None:
    catalog = SkillCatalog(settings.project_root / "skills")
    plugins = SkillPluginManager(catalog)

    assert "qq-group-management" in catalog.names
    assert "qq-group-management" not in asyncio.run(plugins.available_skills())


@pytest.mark.asyncio
async def test_availability_does_not_query_roles_but_load_rejects_member(
    settings,
) -> None:
    plugins, registry, channel = _runtime(
        settings,
        roles={"10001": "member", "30001": "admin"},
    )
    context = _context()

    available = await plugins.available_skills(
        frozenset({MANAGE_SCOPE}),
        context=context,
    )

    assert "qq-group-management" in available
    assert channel.inspections == []
    with pytest.raises(InputValidationError, match="iJA 不是 QQ 群主或管理员"):
        await registry.execute(
            "load_skill",
            {"skill": "qq-group-management"},
            context,
        )
    assert channel.inspections == ["30001", "10001"]
    assert channel.actions == []


@pytest.mark.asyncio
async def test_runtime_rechecks_roles_before_each_action_and_limits_turn(
    settings,
) -> None:
    _, registry, channel = _runtime(
        settings,
        roles={
            "10001": "owner",
            "30001": "admin",
            "40001": "member",
        },
    )
    context = _context()
    await registry.execute(
        "load_skill",
        {"skill": "qq-group-management"},
        context,
    )
    inspections_after_load = len(channel.inspections)

    result = await registry.execute(
        "set_qq_group_member_mute",
        {
            "target_user_id": "40001",
            "duration_seconds": 600,
            "reason": "管理员明确要求临时禁言",
        },
        context,
    )

    assert result.value["action"] == "mute_member"
    assert len(channel.inspections) == inspections_after_load + 3
    assert channel.actions == [
        (
            "set_member_mute",
            {"user_id": "40001", "duration": 600},
        )
    ]
    with pytest.raises(InputValidationError, match="已经尝试"):
        await registry.execute(
            "set_qq_group_whole_mute",
            {"enable": True, "reason": "再次操作"},
            context,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed_user_id", "error_message"),
    [
        ("30001", "当前发起者不是 QQ 群主或管理员"),
        ("10001", "iJA 不是 QQ 群主或管理员"),
    ],
)
async def test_runtime_rejects_role_changes_after_load(
    settings,
    changed_user_id: str,
    error_message: str,
) -> None:
    _, registry, channel = _runtime(
        settings,
        roles={"10001": "admin", "30001": "owner"},
    )
    context = _context()
    await registry.execute(
        "load_skill",
        {"skill": "qq-group-management"},
        context,
    )
    channel.roles[changed_user_id] = "member"

    with pytest.raises(
        InputValidationError,
        match=error_message,
    ):
        await registry.execute(
            "set_qq_group_whole_mute",
            {"enable": True, "reason": "权限已经变化"},
            context,
        )
    assert channel.actions == []


@pytest.mark.asyncio
async def test_runtime_rejects_equal_role_and_missing_scope(settings) -> None:
    plugins, registry, channel = _runtime(
        settings,
        roles={
            "10001": "owner",
            "30001": "admin",
            "40001": "admin",
        },
    )
    context = _context()
    await registry.execute(
        "load_skill",
        {"skill": "qq-group-management"},
        context,
    )

    with pytest.raises(InputValidationError, match="同级或更高"):
        await registry.execute(
            "kick_qq_group_member",
            {
                "target_user_id": "40001",
                "reject_add_request": False,
                "confirmation": "确认移出",
                "reason": "越权请求",
            },
            context,
        )
    assert channel.actions == []

    unauthorized = _context(scopes=frozenset())
    assert "qq-group-management" not in await plugins.available_skills()
    with pytest.raises(InputValidationError, match="缺少 scope"):
        await registry.execute(
            "load_skill",
            {"skill": "qq-group-management"},
            unauthorized,
        )


@pytest.mark.asyncio
async def test_runtime_recall_uses_authoritative_current_session_message(
    settings,
) -> None:
    session = _group_session()
    message = StoredMessage(
        id="msg-internal",
        session_id=session.id,
        role=MessageRole.USER,
        sender_id="40001",
        sender_name="普通成员",
        external_message_id="onebot:v1:MTIzNDU",
        components=[MessageComponent.text_component("待撤回")],
        created_at=utc_now(),
    )
    _, registry, channel = _runtime(
        settings,
        roles={
            "10001": "owner",
            "30001": "admin",
            "40001": "member",
        },
        message=message,
    )
    context = _context()
    await registry.execute(
        "load_skill",
        {"skill": "qq-group-management"},
        context,
    )

    result = await registry.execute(
        "recall_qq_group_message",
        {
            "message_id": "message:msg-internal",
            "reason": "管理员明确要求撤回",
        },
        context,
    )

    assert result.value["message_id"] == "msg-internal"
    assert channel.actions == [
        (
            "recall_message",
            {"external_message_id": "onebot:v1:MTIzNDU"},
        )
    ]
