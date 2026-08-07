from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from application.service import IngressResult
from domain.models import ChatType, ParticipantRole, SessionView, utc_now
from plugins._host import ChannelRouter, PluginContext
from plugins.onebot.runtime import (
    OneBotGroupConfig,
    OneBotPlugin,
    OneBotSettings,
    encode_reply_id,
)
from ports import ChannelRuntimeContext


async def _ignore_ingress(_):
    return IngressResult(accepted=True)


async def _resolve_external_message_id(_):
    return None


async def _resolve_message_reference(_, __, ___):
    return None


def _context(tmp_path: Path) -> PluginContext:
    return PluginContext(
        plugin_id="onebot",
        plugin_root=tmp_path,
        options={},
        ingest=_ignore_ingress,
        resolve_external_message_id=_resolve_external_message_id,
        resolve_message_reference=_resolve_message_reference,
    )


def _settings() -> OneBotSettings:
    return OneBotSettings(
        ws_url="ws://127.0.0.1:3001",
        http_base_url="http://127.0.0.1:3000",
        access_token="test-token",
        bot_uin="10001",
        groups=[OneBotGroupConfig(group_id="20001", require_at=False)],
    )


def _session() -> SessionView:
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


@pytest.mark.asyncio
async def test_channel_router_forwards_optional_group_management() -> None:
    class ManagementAdapter:
        async def runtime_context(self, session):
            del session
            return ChannelRuntimeContext(agent_group_role=ParticipantRole.ADMIN)

        async def manage_group(self, session, action, parameters):
            return {
                "session_id": session.id,
                "action": action,
                "parameters": parameters,
            }

    router = ChannelRouter()
    router.register("onebot", "10001", ManagementAdapter())  # type: ignore[arg-type]

    assert router.has_group_management_route("ONEBOT") is True
    assert await router.runtime_context(_session()) == ChannelRuntimeContext(
        agent_group_role=ParticipantRole.ADMIN
    )
    assert await router.manage_group(
        _session(),
        "set_whole_mute",
        {"enable": True},
    ) == {
        "session_id": "session-group",
        "action": "set_whole_mute",
        "parameters": {"enable": True},
    }


@pytest.mark.asyncio
async def test_onebot_inspects_live_group_member_role(tmp_path: Path) -> None:
    captured: list[tuple[str, dict, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                request.url.path,
                json.loads(request.content),
                request.headers.get("authorization"),
            )
        )
        return httpx.Response(
            200,
            json={
                "retcode": 0,
                "data": {
                    "group_id": 20001,
                    "user_id": 30001,
                    "nickname": "管理员",
                    "card": "群管",
                    "role": "admin",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(_settings(), _context(tmp_path), client=client)

    result = await plugin.manage_group(
        _session(),
        "inspect_member",
        {"user_id": "30001"},
    )

    assert result == {
        "user_id": "30001",
        "display_name": "群管",
        "role": "admin",
    }
    assert captured == [
        (
            "/get_group_member_info",
            {"group_id": 20001, "user_id": 30001, "no_cache": True},
            "Bearer test-token",
        )
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_onebot_runtime_context_reads_bot_role_without_cache(
    tmp_path: Path,
) -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "retcode": 0,
                "data": {
                    "group_id": 20001,
                    "user_id": 10001,
                    "nickname": "iJA",
                    "role": "member",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(_settings(), _context(tmp_path), client=client)

    context = await plugin.runtime_context(_session())

    assert context == ChannelRuntimeContext(
        agent_group_role=ParticipantRole.MEMBER
    )
    assert captured == [
        {"group_id": 20001, "user_id": 10001, "no_cache": True}
    ]
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "parameters", "endpoint", "body"),
    [
        (
            "recall_message",
            {"external_message_id": encode_reply_id("12345", None)},
            "/delete_msg",
            {"message_id": 12345},
        ),
        (
            "set_member_mute",
            {"user_id": "40001", "duration": 600},
            "/set_group_ban",
            {"group_id": 20001, "user_id": 40001, "duration": 600},
        ),
        (
            "set_whole_mute",
            {"enable": True},
            "/set_group_whole_ban",
            {"group_id": 20001, "enable": True},
        ),
        (
            "kick_member",
            {"user_id": "40001", "reject_add_request": False},
            "/set_group_kick",
            {
                "group_id": 20001,
                "user_id": 40001,
                "reject_add_request": False,
            },
        ),
    ],
)
async def test_onebot_group_management_action_mapping(
    tmp_path: Path,
    action: str,
    parameters: dict,
    endpoint: str,
    body: dict,
) -> None:
    captured: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(_settings(), _context(tmp_path), client=client)

    result = await plugin.manage_group(_session(), action, parameters)

    assert result == {"success": True, "action": action}
    assert captured == [(endpoint, body)]
    await client.aclose()


@pytest.mark.asyncio
async def test_onebot_group_management_reports_protocol_failure(
    tmp_path: Path,
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "status": "failed",
                    "retcode": 1200,
                    "wording": "权限不足",
                },
            )
        )
    )
    plugin = OneBotPlugin(_settings(), _context(tmp_path), client=client)

    with pytest.raises(RuntimeError, match="权限不足"):
        await plugin.manage_group(
            _session(),
            "set_member_mute",
            {"user_id": "40001", "duration": 60},
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_onebot_group_management_rejects_ambiguous_success_response(
    tmp_path: Path,
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"status": "ok", "data": {}})
        )
    )
    plugin = OneBotPlugin(_settings(), _context(tmp_path), client=client)

    with pytest.raises(RuntimeError, match="结果未知"):
        await plugin.manage_group(
            _session(),
            "set_whole_mute",
            {"enable": True},
        )
    await client.aclose()
