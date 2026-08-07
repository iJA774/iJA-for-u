import asyncio
import base64
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from application.service import IngressResult
from domain.models import (
    ChatType,
    ComponentType,
    DeliveryStatus,
    MessageComponent,
    OutboundMessage,
    SessionView,
    utc_now,
)
from plugins._host import (
    HttpCallbackRequest,
    InboundEnvelope,
    PluginContext,
)
from plugins.qq.runtime import (
    QQPlugin,
    QQSettings,
    decode_inbound_message_id,
    encode_inbound_message_id,
    normalize_event,
)
from plugins.wechat.runtime import (
    WeChatCryptor,
    WeChatPlugin,
    WeChatSettings,
)


class PluginHarness:
    def __init__(self) -> None:
        self.inbound: list[InboundEnvelope] = []
        self.reply_external_id: str | None = None

    async def ingest(self, envelope: InboundEnvelope) -> IngressResult:
        self.inbound.append(envelope)
        return IngressResult(accepted=True)

    async def resolve(self, _: str) -> str | None:
        return self.reply_external_id

    async def resolve_reference(self, platform: str, account_id: str, external_id: str):
        del platform, account_id, external_id
        return None

    def context(self, plugin_id: str, *, admit_event=None) -> PluginContext:
        context_options = {
            "plugin_id": plugin_id,
            "plugin_root": Path("plugins") / plugin_id,
            "options": {},
            "ingest": self.ingest,
            "resolve_external_message_id": self.resolve,
            "resolve_message_reference": self.resolve_reference,
        }
        if admit_event is not None:
            context_options["admit_event"] = admit_event
        return PluginContext(
            **context_options,
        )


def _session(*, platform: str, account_id: str, external_chat_id: str, chat_type: ChatType) -> SessionView:
    now = utc_now()
    return SessionView(
        id="session_test",
        platform=platform,
        account_id=account_id,
        external_chat_id=external_chat_id,
        chat_type=chat_type,
        display_name="测试会话",
        created_at=now,
        updated_at=now,
    )


def test_qq_normalizes_c2c_and_group_events_with_stable_reply_id() -> None:
    private = normalize_event(
        "bot-app",
        "C2C_MESSAGE_CREATE",
        {
            "id": "qq-message-1",
            "author": {
                "id": "user-1",
                "user_openid": "user-1",
                "username": "小明",
                "bot": False,
            },
            "content": "你好",
            "timestamp": "2026-07-24T10:00:00+08:00",
            "message_scene": {"ext": ["msg_idx=idx-1"]},
        },
    )
    assert private is not None
    assert private.message.chat_type == ChatType.PRIVATE
    assert private.message.external_chat_id == "user-1"
    assert decode_inbound_message_id(private.message.external_message_id) == "qq-message-1"

    group = normalize_event(
        "bot-app",
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "qq-message-2",
            "group_openid": "group-1",
            "author": {
                "id": "member-1",
                "member_openid": "member-1",
                "member_role": "admin",
                "username": "管理员",
                "bot": False,
            },
            "content": " 查天气 ",
            "mentions": [{"id": "member-2", "username": "小红"}],
            "attachments": [{"content_type": "image/png", "filename": "weather.png"}],
        },
    )
    assert group is not None
    assert group.message.chat_type == ChatType.GROUP
    assert group.message.external_chat_id == "group-1"
    assert [item.type for item in group.message.components] == [
        ComponentType.MENTION,
        ComponentType.TEXT,
        ComponentType.MENTION,
        ComponentType.TEXT,
    ]
    assert group.message.components[0].target_id == "agent"


@pytest.mark.asyncio
async def test_qq_send_uses_official_token_and_passive_reply_id() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(
                200,
                json={"access_token": "qq-access", "expires_in": "7200"},
            )
        return httpx.Response(
            200,
            json={"id": "qq-outbound-1", "timestamp": "2026-07-24T10:00:00+08:00"},
        )

    harness = PluginHarness()
    harness.reply_external_id = encode_inbound_message_id("qq-inbound-1", "idx-1")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = QQPlugin(
        QQSettings(app_id="bot-app", app_secret="secret"),
        harness.context("qq"),
        client=client,
    )
    message = OutboundMessage(
        session_id="session_test",
        reply_to_message_id="stored-1",
        components=[MessageComponent.text_component("收到")],
    )
    receipt = await plugin.send(
        _session(
            platform="qq",
            account_id="bot-app",
            external_chat_id="group-openid",
            chat_type=ChatType.GROUP,
        ),
        message,
    )
    assert receipt.status == DeliveryStatus.SENT
    assert requests[1].headers["authorization"] == "QQBot qq-access"
    assert requests[1].url.path == "/v2/groups/group-openid/messages"
    assert json.loads(requests[1].content) == {
        "content": "收到",
        "msg_type": 0,
        "msg_id": "qq-inbound-1",
        "msg_seq": 1,
    }
    await client.aclose()


@pytest.mark.asyncio
async def test_qq_gateway_requires_admission_before_ingesting_message() -> None:
    class FakeWebSocket:
        async def recv(self) -> str:
            return json.dumps({"op": 10, "d": {"heartbeat_interval": 60_000}})

        async def send(self, _: str) -> None:
            return None

        def __aiter__(self):
            async def events():
                yield json.dumps(
                    {
                        "op": 0,
                        "t": "READY",
                        "s": 1,
                        "d": {"session_id": "gateway-session"},
                    }
                )
                yield json.dumps(
                    {
                        "op": 0,
                        "t": "C2C_MESSAGE_CREATE",
                        "s": 2,
                        "d": {
                            "id": "candidate-message",
                            "author": {
                                "id": "user-1",
                                "user_openid": "user-1",
                                "username": "小明",
                                "bot": False,
                            },
                            "content": "候选代不应入站",
                        },
                    }
                )

            return events()

    admission_calls = 0

    @asynccontextmanager
    async def reject_event():
        nonlocal admission_calls
        admission_calls += 1
        yield False

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/getAppAccessToken"
        return httpx.Response(
            200,
            json={"access_token": "qq-access", "expires_in": "7200"},
        )

    harness = PluginHarness()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = QQPlugin(
        QQSettings(app_id="bot-app", app_secret="secret"),
        harness.context("qq", admit_event=reject_event),
        client=client,
    )

    await plugin._gateway_session(FakeWebSocket())

    assert plugin._ready_event.is_set()
    assert admission_calls == 1
    assert harness.inbound == []
    await client.aclose()


@pytest.mark.asyncio
async def test_qq_ready_waits_for_gateway_ready_signal() -> None:
    harness = PluginHarness()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    )
    plugin = QQPlugin(
        QQSettings(
            app_id="bot-app",
            app_secret="secret",
            gateway_open_timeout_seconds=0.01,
        ),
        harness.context("qq"),
        client=client,
    )
    blocker = asyncio.Event()

    async def wait_forever() -> None:
        await blocker.wait()

    gateway_task = asyncio.create_task(wait_forever())
    plugin._gateway_task = gateway_task
    try:
        with pytest.raises(RuntimeError, match="READY 超时"):
            await plugin.ready()
        plugin._ready_event.set()
        await plugin.ready()
    finally:
        gateway_task.cancel()
        await asyncio.gather(gateway_task, return_exceptions=True)
        await client.aclose()


def _wechat_signature(token: str, *parts: str) -> str:
    return hashlib.sha1("".join(sorted((token, *parts))).encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_wechat_plaintext_callback_verifies_and_queues_message() -> None:
    harness = PluginHarness()
    plugin = WeChatPlugin(
        WeChatSettings(
            app_id="wx-app",
            app_secret="secret",
            token="callback-token",
            message_mode="plaintext",
        ),
        harness.context("wechat"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(500, json={"errcode": -1}))
        ),
    )
    timestamp = "1721790000"
    nonce = "nonce-1"
    signature = _wechat_signature("callback-token", timestamp, nonce)
    verification = await plugin.handle_http(
        HttpCallbackRequest(
            method="GET",
            query={
                "timestamp": timestamp,
                "nonce": nonce,
                "signature": signature,
                "echostr": "hello",
            },
            headers={},
        )
    )
    assert verification.status_code == 200
    assert verification.body == b"hello"

    body = b"""<xml>
<ToUserName><![CDATA[gh_service]]></ToUserName>
<FromUserName><![CDATA[user-openid]]></FromUserName>
<CreateTime>1721790000</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[\xe4\xbd\xa0\xe5\xa5\xbd]]></Content>
<MsgId>123456</MsgId>
</xml>"""
    result = await plugin.handle_http(
        HttpCallbackRequest(
            method="POST",
            query={
                "timestamp": timestamp,
                "nonce": nonce,
                "signature": signature,
            },
            headers={},
            body=body,
        )
    )
    assert result.body == b"success"
    assert harness.inbound[0].message.external_message_id == "wechat:123456"
    assert harness.inbound[0].message.plain_text == "你好"
    await plugin.stop()


@pytest.mark.asyncio
async def test_wechat_safe_mode_decrypts_and_checks_app_id() -> None:
    key = base64.b64encode(bytes(range(32))).decode("ascii").rstrip("=")
    harness = PluginHarness()
    settings = WeChatSettings(
        app_id="wx-safe-app",
        app_secret="secret",
        token="safe-token",
        message_mode="safe",
        encoding_aes_key=key,
    )
    plugin = WeChatPlugin(
        settings,
        harness.context("wechat"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(500, json={"errcode": -1}))
        ),
    )
    inner = b"""<xml>
<ToUserName><![CDATA[gh_service]]></ToUserName>
<FromUserName><![CDATA[safe-user]]></FromUserName>
<CreateTime>1721790000</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[safe hello]]></Content>
<MsgId>safe-1</MsgId>
</xml>"""
    encrypted = WeChatCryptor(settings.app_id, settings.encoding_aes_key).encrypt(
        inner, random_bytes=b"0123456789abcdef"
    )
    timestamp = "1721790000"
    nonce = "safe-nonce"
    signature = _wechat_signature(settings.token, timestamp, nonce, encrypted)
    outer = (f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>").encode()
    result = await plugin.handle_http(
        HttpCallbackRequest(
            method="POST",
            query={
                "timestamp": timestamp,
                "nonce": nonce,
                "msg_signature": signature,
            },
            headers={},
            body=outer,
        )
    )
    assert result.body == b"success"
    assert harness.inbound[0].message.plain_text == "safe hello"
    await plugin.stop()


@pytest.mark.asyncio
async def test_wechat_customer_message_reports_platform_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/token":
            return httpx.Response(
                200,
                json={"access_token": "wx-access", "expires_in": 7200},
            )
        return httpx.Response(200, json={"errcode": 45015, "errmsg": "response out of time limit"})

    harness = PluginHarness()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = WeChatPlugin(
        WeChatSettings(
            app_id="wx-app",
            app_secret="secret",
            token="token",
            message_mode="plaintext",
        ),
        harness.context("wechat"),
        client=client,
    )
    receipt = await plugin.send(
        _session(
            platform="wechat-service-account",
            account_id="wx-app",
            external_chat_id="user-openid",
            chat_type=ChatType.PRIVATE,
        ),
        OutboundMessage(
            session_id="session_test",
            components=[MessageComponent.text_component("超时回复")],
        ),
    )
    assert receipt.status == DeliveryStatus.FAILED
    assert receipt.error_code == "wechat_45015"
    await client.aclose()
