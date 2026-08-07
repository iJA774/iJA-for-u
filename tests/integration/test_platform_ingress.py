import asyncio
import hashlib
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from adapters.model import FakeModelProvider
from api import create_app
from bootstrap import build_runtime
from domain.models import ParticipantRole
from plugins._host import PlatformIngress
from plugins.filter.runtime import _TEMPLATE
from plugins.qq.runtime import normalize_event
from ports import ModelMessage
from ports.egress import EgressEnvelope


def _wechat_enabled(settings) -> None:
    settings.platform_plugins.enabled = ["wechat"]
    settings.platform_plugins.options = {
        "wechat": {
            "app_id": "wx-test",
            "app_secret": "secret",
            "token": "token",
            "message_mode": "plaintext",
        }
    }


@pytest.mark.asyncio
async def test_runtime_loads_enabled_plugin_from_plugins_directory(settings) -> None:
    _wechat_enabled(settings)
    runtime = build_runtime(settings)
    assert set(runtime.platform_plugins.plugins) == {"wechat"}
    assert runtime.platform_plugins.plugins["wechat"].account_id == "wx-test"
    await runtime.platform_plugins.start()
    await runtime.platform_plugins.stop()


@pytest.mark.asyncio
async def test_runtime_auto_discovers_filter_egress_plugin(settings) -> None:
    """Egress 插件不依赖平台启用列表，放入插件目录后应在冷启动时生效。"""

    model = FakeModelProvider("中国共产党")
    runtime = build_runtime(settings, model_override=model)

    assert settings.platform_plugins.enabled == []
    assert set(runtime.platform_plugins.egress_plugins) == {"filter"}
    await runtime.platform_plugins.start()
    try:
        filtered = await runtime.platform_plugins.egress_filter(
            EgressEnvelope(
                session_id="session-filter",
                turn_id="turn-filter",
                text="中国共产党",
                prompt_messages=[ModelMessage(role="user", content="测试过滤")],
                model=model,
                model_name="fake",
                temperature=0,
                max_tokens=100,
            )
        )
    finally:
        await runtime.platform_plugins.stop()

    assert filtered == _TEMPLATE
    assert len(model.requests) == 3


@pytest.mark.asyncio
async def test_runtime_loads_ingress_plugin_separately_from_channels(settings) -> None:
    settings.platform_plugins.enabled = ["delayed_reply"]
    settings.platform_plugins.options = {
        "delayed_reply": {
            "message_delay_seconds": 5,
            "typing_delay_seconds": 10,
        }
    }
    runtime = build_runtime(settings)

    assert set(runtime.platform_plugins.ingress_plugins) == {"delayed_reply"}
    assert runtime.platform_plugins.plugins == {}
    await runtime.platform_plugins.start()
    await runtime.platform_plugins.stop()


@pytest.mark.asyncio
async def test_onebot_manifest_always_loads_delayed_reply_dependency(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.platform_plugins.enabled = ["onebot"]
    settings.platform_plugins.options = {
        "onebot": {
            "ws_url": "ws://127.0.0.1:3001",
            "http_base_url": "http://127.0.0.1:3000",
            "bot_uin": "999",
        }
    }
    runtime = build_runtime(settings)

    assert set(runtime.platform_plugins.plugins) == {"onebot"}
    assert set(runtime.platform_plugins.ingress_plugins) == {"delayed_reply"}
    onebot = cast(Any, runtime.platform_plugins.plugins["onebot"])

    async def ready_without_network() -> None:
        onebot._ready_event.set()
        await onebot._stop_event.wait()

    monkeypatch.setattr(onebot, "_run_loop", ready_without_network)
    await runtime.platform_plugins.start()
    try:
        assert [plugin.plugin_id for plugin in runtime.platform_plugins._started] == [
            "delayed_reply",
            "filter",
            "onebot",
        ]
    finally:
        await runtime.platform_plugins.stop()


def test_public_callback_route_dispatches_to_enabled_plugin(settings) -> None:
    _wechat_enabled(settings)
    timestamp = "1721790000"
    nonce = "nonce"
    signature = hashlib.sha1("".join(sorted(("token", timestamp, nonce))).encode()).hexdigest()
    with TestClient(create_app(settings=settings)) as client:
        response = client.get(
            "/platform-plugins/wechat/callback",
            params={
                "timestamp": timestamp,
                "nonce": nonce,
                "signature": signature,
                "echostr": "verified",
            },
        )
    assert response.status_code == 200
    assert response.text == "verified"


@pytest.mark.asyncio
async def test_platform_ingress_creates_route_and_adds_group_members(settings) -> None:
    runtime = build_runtime(settings)
    runtime.personas.initialize()
    await runtime.store.initialize()
    ingress = PlatformIngress(
        runtime.store,
        runtime.chat,
        runtime.channel_capabilities,
    )

    first = normalize_event(
        "bot-app",
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "qq-1",
            "group_openid": "group-1",
            "author": {
                "id": "member-1",
                "member_openid": "member-1",
                "member_role": "member",
                "username": "成员",
                "bot": False,
            },
            "content": "第一条",
        },
    )
    assert first is not None
    first_result = await ingress.accept(first)
    session = await runtime.store.get_session_by_route("qq", "bot-app", "group-1", first.message.chat_type)
    assert first_result.accepted
    assert session is not None
    assert session.participants[0].role == ParticipantRole.OWNER

    repeated_member_event = normalize_event(
        "bot-app",
        "GROUP_MESSAGE_CREATE",
        {
            "id": "qq-repeat",
            "group_openid": "group-1",
            "author": {
                "id": "member-1",
                "member_openid": "member-1",
                "member_role": "member",
                "username": "成员",
                "bot": False,
            },
            "content": "仍是普通成员标记",
        },
    )
    assert repeated_member_event is not None
    await ingress.accept(repeated_member_event)
    still_owned = await runtime.store.get_session(session.id)
    assert still_owned is not None
    assert still_owned.participants[0].role == ParticipantRole.OWNER

    owner = normalize_event(
        "bot-app",
        "GROUP_MESSAGE_CREATE",
        {
            "id": "qq-2",
            "group_openid": "group-1",
            "author": {
                "id": "owner-1",
                "member_openid": "owner-1",
                "member_role": "owner",
                "username": "群主",
                "bot": False,
            },
            "content": "第二条",
        },
    )
    assert owner is not None
    await ingress.accept(owner)
    updated = await runtime.store.get_session(session.id)
    assert updated is not None
    roles = {item.external_user_id: item.role for item in updated.participants}
    assert roles == {
        "member-1": ParticipantRole.MEMBER,
        "owner-1": ParticipantRole.OWNER,
    }

    for task in runtime.chat._debounce_tasks.values():
        task.cancel()
    await asyncio.gather(*runtime.chat._debounce_tasks.values(), return_exceptions=True)
    await runtime.chat.stop()
    await runtime.store.close()
