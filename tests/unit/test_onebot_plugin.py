import asyncio
import base64
import hashlib
import json
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from adapters.web_simulator.uploads import AttachmentStore
from application.service import IngressResult
from domain.models import (
    ChatType,
    ComponentType,
    DeliveryStatus,
    MessageComponent,
    OutboundMessage,
    ParticipantRole,
    SessionView,
    utc_now,
)
from plugins._host import InboundEnvelope, MessageReference, PluginContext, TypingEvent
from plugins.onebot.runtime import (
    OneBotGroupConfig,
    OneBotPlugin,
    OneBotSettings,
    _parse_cq_string,
    _render_segments,
    decode_reply_id,
    encode_reply_id,
    normalize_event,
    normalize_typing_event,
)


class PluginHarness:
    def __init__(self) -> None:
        self.inbound: list[InboundEnvelope] = []
        self.typing_events: list[TypingEvent] = []
        self.reply_external_id: str | None = None
        self.references: dict[str, MessageReference] = {}

    async def ingest(self, envelope: InboundEnvelope) -> IngressResult:
        self.inbound.append(envelope)
        return IngressResult(accepted=True)

    async def resolve(self, _: str) -> str | None:
        return self.reply_external_id

    async def resolve_reference(
        self, platform: str, account_id: str, external_id: str
    ) -> MessageReference | None:
        del platform, account_id
        return self.references.get(external_id)

    async def typing(self, event: TypingEvent) -> None:
        self.typing_events.append(event)

    def context(self, *, store_image=None, admit_event=None) -> PluginContext:
        context_options = {
            "plugin_id": "onebot",
            "plugin_root": Path("plugins") / "onebot",
            "options": {},
            "ingest": self.ingest,
            "resolve_external_message_id": self.resolve,
            "resolve_message_reference": self.resolve_reference,
            "typing": self.typing,
            "store_image": store_image,
        }
        if admit_event is not None:
            context_options["admit_event"] = admit_event
        return PluginContext(
            **context_options,
        )


def _settings(**overrides: object) -> OneBotSettings:
    base: dict[str, object] = {
        "ws_url": "ws://127.0.0.1:3001",
        "http_base_url": "http://127.0.0.1:3000",
        "bot_uin": "999",
    }
    base.update(overrides)
    return OneBotSettings.model_validate(base)


def _session(chat_type: ChatType, external_chat_id: str) -> SessionView:
    now = utc_now()
    return SessionView(
        id="session_test",
        platform="onebot",
        account_id="999",
        external_chat_id=external_chat_id,
        chat_type=chat_type,
        display_name="测试会话",
        created_at=now,
        updated_at=now,
    )


def _private_event(message: object, *, user_id: str = "111", message_id: str = "m1") -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "user_id": int(user_id),
        "self_id": 999,
        "message_id": int(message_id) if message_id.isdigit() else message_id,
        "time": 1721790000,
        "sender": {"user_id": int(user_id), "nickname": "小明"},
        "message": message,
    }


def _group_event(
    message: object,
    *,
    group_id: str = "222",
    user_id: str = "111",
    role: str = "member",
    nickname: str = "群员",
) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "group_id": int(group_id),
        "user_id": int(user_id),
        "self_id": 999,
        "message_id": 100,
        "time": 1721790000,
        "sender": {"user_id": int(user_id), "nickname": nickname, "role": role},
        "message": message,
    }


def test_normalize_napcat_private_typing_extension() -> None:
    event = normalize_typing_event(
        "999",
        {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "input_status",
            "self_id": 999,
            "user_id": 111,
            "group_id": 0,
            "status_text": "",
            "event_type": 2,
        },
        {"111"},
        {},
        "999",
    )

    assert event == TypingEvent(
        platform="onebot",
        account_id="999",
        external_chat_id="111",
        chat_type=ChatType.PRIVATE,
        sender_id="111",
        status_text="",
        event_type=2,
    )


def test_normalize_typing_rejects_unauthorized_private_and_unconfigured_group() -> None:
    base = {
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "input_status",
        "self_id": 999,
        "user_id": 111,
        "event_type": 2,
    }
    assert normalize_typing_event("999", {**base, "group_id": 0}, {"222"}, {}, "999") is None
    assert normalize_typing_event("999", {**base, "group_id": 333}, set(), {}, "999") is None


@pytest.mark.asyncio
async def test_serve_publishes_typing_without_ingesting_message() -> None:
    class FakeWebSocket:
        def __aiter__(self):
            event = {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "input_status",
                "self_id": 999,
                "user_id": 111,
                "group_id": 0,
                "event_type": 2,
            }

            async def events():
                yield json.dumps(event)

            return events()

    harness = PluginHarness()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    plugin = OneBotPlugin(_settings(), harness.context(), client=client)

    await plugin._serve(FakeWebSocket())

    assert len(harness.typing_events) == 1
    assert harness.inbound == []
    await client.aclose()


@pytest.mark.asyncio
async def test_candidate_event_is_rejected_before_download_or_attachment_write() -> None:
    class FakeWebSocket:
        def __aiter__(self):
            async def events():
                yield json.dumps(
                    _private_event(
                        [
                            {
                                "type": "image",
                                "data": {
                                    "file": "candidate.png",
                                    "url": "https://gchat.qpic.cn/candidate.png",
                                },
                            }
                        ]
                    )
                )

            return events()

    admission_calls = 0
    attachment_writes = 0
    requested_urls: list[str] = []

    @asynccontextmanager
    async def reject_event():
        nonlocal admission_calls
        admission_calls += 1
        yield False

    def store_image(filename: str, mime_type: str, content: bytes):
        nonlocal attachment_writes
        del filename, mime_type, content
        attachment_writes += 1
        return MessageComponent.text_component("不应写入")

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, content=b"not-reached")

    harness = PluginHarness()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(
        _settings(),
        harness.context(store_image=store_image, admit_event=reject_event),
        client=client,
    )

    await plugin._serve(FakeWebSocket())

    assert admission_calls == 1
    assert requested_urls == []
    assert attachment_writes == 0
    assert harness.inbound == []
    await client.aclose()


@pytest.mark.asyncio
async def test_onebot_ready_waits_for_websocket_connection_signal() -> None:
    harness = PluginHarness()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    )
    plugin = OneBotPlugin(
        _settings(ws_open_timeout_seconds=0.01),
        harness.context(),
        client=client,
    )
    blocker = asyncio.Event()

    async def wait_forever() -> None:
        await blocker.wait()

    websocket_task = asyncio.create_task(wait_forever())
    plugin._ws_task = websocket_task
    try:
        with pytest.raises(RuntimeError, match="ready 超时"):
            await plugin.ready()
        plugin._ready_event.set()
        await plugin.ready()
    finally:
        websocket_task.cancel()
        await asyncio.gather(websocket_task, return_exceptions=True)
        await client.aclose()


def test_normalize_private_text_and_image_segments() -> None:
    event = _private_event(
        [
            {"type": "text", "data": {"text": "看这张图"}},
            {"type": "image", "data": {"url": "http://cdn.example.com/x.jpg"}},
        ]
    )
    envelope = normalize_event("999", event, set(), {}, "999")
    assert envelope is not None
    assert envelope.message.chat_type == ChatType.PRIVATE
    assert envelope.message.external_chat_id == "111"
    assert envelope.message.sender_name == "小明"
    assert envelope.participant_role == ParticipantRole.OWNER
    types = [c.type for c in envelope.message.components]
    assert types == [ComponentType.TEXT, ComponentType.TEXT]
    assert envelope.message.components[1].text == "[图片读取失败]"
    # message_id 编码为 onebot: 前缀
    assert decode_reply_id(envelope.message.external_message_id) == "m1"


def test_normalize_group_with_at_bot_and_reply() -> None:
    groups = {"222": OneBotGroupConfig(group_id="222", require_at=True)}
    event = _group_event(
        [
            {"type": "at", "data": {"qq": "999", "name": "机器人"}},
            {"type": "reply", "data": {"id": "88"}},
            {"type": "text", "data": {"text": " 帮我查 "}},
            {"type": "face", "data": {"id": "178"}},
        ],
        role="admin",
    )
    envelope = normalize_event("999", event, set(), groups, "999")
    assert envelope is not None
    assert envelope.message.chat_type == ChatType.GROUP
    assert envelope.participant_role == ParticipantRole.ADMIN
    types = [c.type for c in envelope.message.components]
    assert types == [ComponentType.MENTION, ComponentType.TEXT, ComponentType.TEXT]
    # @bot 记录 + reply id 记录
    assert envelope.message.components[0].target_id == "agent"
    assert "[表情:178]" in (envelope.message.components[-1].text or "")
    # external_message_id 同时编码 message_id 与 reply_id
    assert decode_reply_id(envelope.message.external_message_id) == "100"


def test_normalize_cq_string_parses_text_at_image() -> None:
    segments = _parse_cq_string("[CQ:at,qq=123,name=小红]你好&#91;test&#93;[CQ:image,file=abc,url=http://a.com/1.jpg]")
    assert [s["type"] for s in segments] == ["at", "text", "image"]
    assert segments[0]["data"]["qq"] == "123"
    assert segments[0]["data"]["name"] == "小红"
    assert "你好[test]" in segments[1]["data"]["text"]
    assert segments[2]["data"]["url"] == "http://a.com/1.jpg"


def test_normalize_file_record_placeholders() -> None:
    event = _private_event(
        [
            {"type": "file", "data": {"file": "report.pdf", "size": 1024}},
            {"type": "record", "data": {"file": "voice.amr"}},
            {"type": "mface", "data": {"summary": "[得意]"}},
        ]
    )
    envelope = normalize_event("999", event, set(), {}, "999")
    assert envelope is not None
    texts = [c.text for c in envelope.message.components]
    assert texts == ["[文件:report.pdf(1024)]", "[语音消息]", "[表情:[得意]]"]


@pytest.mark.asyncio
async def test_serve_downloads_napcat_image_and_market_face_into_attachment_store(
    tmp_path: Path,
) -> None:
    image = Image.new("RGB", (24, 24), "#e05a75")
    output = BytesIO()
    image.save(output, format="PNG")
    content = output.getvalue()
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(404, json={"retcode": 1404})
        return httpx.Response(
            200,
            headers={"Content-Type": "application/octet-stream"},
            content=content,
        )

    class FakeWebSocket:
        def __aiter__(self):
            event = _private_event(
                [
                    {"type": "text", "data": {"text": "这两个"}},
                    {
                        "type": "image",
                        "data": {
                            "file": "normal.png",
                            "url": "https://gchat.qpic.cn/normal.png",
                        },
                    },
                    {
                        "type": "mface",
                        "data": {
                            "summary": "[得意]",
                            "url": "https://gchat.qpic.cn/expression.png",
                        },
                    },
                    {
                        "type": "image",
                        "data": {
                            "file": "marketface",
                            "summary": "[拍拍]",
                            "url": "https://gchat.qpic.cn/marketface.png",
                        },
                    },
                ]
            )

            async def events():
                yield json.dumps(event)

            return events()

    attachments = AttachmentStore(
        uploads_root=tmp_path / "uploads",
        personas_root=tmp_path / "personas",
        max_bytes=1024 * 1024,
    )
    harness = PluginHarness()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(
        _settings(max_image_bytes=1024 * 1024),
        harness.context(store_image=attachments.save_image),
        client=client,
    )

    await plugin._serve(FakeWebSocket())

    assert len(harness.inbound) == 1
    components = harness.inbound[0].message.components
    assert [item.type for item in components] == [
        ComponentType.TEXT,
        ComponentType.IMAGE_REF,
        ComponentType.IMAGE_REF,
        ComponentType.IMAGE_REF,
    ]
    assert components[1].description == "QQ图片"
    assert components[2].description == "QQ表情包：[得意]"
    assert components[3].description == "QQ表情包：[拍拍]"
    assert components[1].is_expression is False
    assert components[2].is_expression is True
    assert components[3].is_expression is True
    for component in components[1:]:
        attachments.validate_image_ref(component)
        assert await asyncio.to_thread(attachments.read_image_ref, component) == content
    assert requested_paths.count("/normal.png") == 1
    assert requested_paths.count("/expression.png") == 1
    assert requested_paths.count("/marketface.png") == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_serve_resolves_file_only_image_through_napcat_get_image(tmp_path: Path) -> None:
    image = Image.new("RGB", (16, 16), "#4f86c6")
    output = BytesIO()
    image.save(output, format="JPEG")
    content = output.getvalue()
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/get_image":
            assert json.loads(request.content) == {"file": "temporary-file-id"}
            return httpx.Response(
                200,
                json={
                    "retcode": 0,
                    "data": {"url": "http://127.0.0.1:3000/cache/resolved.jpg"},
                },
            )
        return httpx.Response(200, content=content)

    class FakeWebSocket:
        def __aiter__(self):
            async def events():
                yield json.dumps(
                    _private_event(
                        [
                            {
                                "type": "image",
                                "data": {"file": "temporary-file-id"},
                            }
                        ]
                    )
                )

            return events()

    attachments = AttachmentStore(
        uploads_root=tmp_path / "uploads",
        personas_root=tmp_path / "personas",
        max_bytes=1024 * 1024,
    )
    harness = PluginHarness()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(
        _settings(),
        harness.context(store_image=attachments.save_image),
        client=client,
    )

    await plugin._serve(FakeWebSocket())

    component = harness.inbound[0].message.components[0]
    assert component.type == ComponentType.IMAGE_REF
    assert component.mime_type == "image/jpeg"
    assert await asyncio.to_thread(attachments.read_image_ref, component) == content
    assert requests == [
        ("POST", "/get_image"),
        ("GET", "/cache/resolved.jpg"),
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_serve_rejects_unapproved_image_host_without_requesting_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=b"not-reached")

    class FakeWebSocket:
        def __aiter__(self):
            async def events():
                yield json.dumps(
                    _private_event(
                        [
                            {
                                "type": "image",
                                "data": {"url": "http://169.254.169.254/latest/meta-data"},
                            }
                        ]
                    )
                )

            return events()

    attachments = AttachmentStore(
        uploads_root=tmp_path / "uploads",
        personas_root=tmp_path / "personas",
        max_bytes=1024 * 1024,
    )
    harness = PluginHarness()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(
        _settings(),
        harness.context(store_image=attachments.save_image),
        client=client,
    )

    await plugin._serve(FakeWebSocket())

    assert requested == []
    assert harness.inbound[0].message.components[0].text == "[图片读取失败]"
    assert "169.254.169.254" not in caplog.text
    await client.aclose()


def test_group_require_at_filters_message_without_at() -> None:
    groups = {"222": OneBotGroupConfig(group_id="222", require_at=True)}
    event = _group_event([{"type": "text", "data": {"text": "没人@我"}}])
    assert normalize_event("999", event, set(), groups, "999") is None


def test_group_require_at_accepts_resolved_quote_to_agent() -> None:
    groups = {"222": OneBotGroupConfig(group_id="222", require_at=True)}
    event = _group_event(
        [
            {"type": "reply", "data": {"id": "88"}},
            {"type": "text", "data": {"text": "接着说"}},
        ]
    )
    reference = MessageReference(
        message_id="stored-agent-message",
        session_id="session_test",
        sender_id="agent",
        sender_name="小佳",
    )

    envelope = normalize_event(
        "999",
        event,
        set(),
        groups,
        "999",
        resolved_reply=reference,
    )

    assert envelope is not None
    quote = envelope.message.components[0]
    assert quote.type == ComponentType.QUOTE
    assert quote.message_id == "stored-agent-message"
    assert quote.target_id == "agent"


def test_group_require_at_rejects_quote_to_other_member() -> None:
    groups = {"222": OneBotGroupConfig(group_id="222", require_at=True)}
    event = _group_event(
        [
            {"type": "reply", "data": {"id": "77"}},
            {"type": "text", "data": {"text": "你继续"}},
        ]
    )
    reference = MessageReference(
        message_id="stored-user-message",
        session_id="session_test",
        sender_id="other-user",
        sender_name="其他成员",
    )

    assert (
        normalize_event(
            "999",
            event,
            set(),
            groups,
            "999",
            resolved_reply=reference,
        )
        is None
    )


@pytest.mark.asyncio
async def test_serve_resolves_reply_only_group_trigger_to_quote() -> None:
    class FakeWebSocket:
        def __aiter__(self):
            async def events():
                yield json.dumps(
                    _group_event(
                        [
                            {"type": "reply", "data": {"id": "88"}},
                            {"type": "text", "data": {"text": "继续"}},
                        ]
                    )
                )

            return events()

    harness = PluginHarness()
    harness.references["88"] = MessageReference(
        message_id="stored-agent-message",
        session_id="session_test",
        sender_id="agent",
        sender_name="小佳",
    )
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"status": "ok", "retcode": 0})
    )
    client = httpx.AsyncClient(transport=transport)
    plugin = OneBotPlugin(
        _settings(groups=[{"group_id": "222", "require_at": True}]),
        harness.context(),
        client=client,
    )

    await plugin._serve(FakeWebSocket())

    assert len(harness.inbound) == 1
    quote = harness.inbound[0].message.components[0]
    assert quote.type == ComponentType.QUOTE
    assert quote.message_id == "stored-agent-message"
    await client.aclose()


def test_group_not_in_whitelist_returns_none() -> None:
    event = _group_event([{"type": "text", "data": {"text": "hi"}}], group_id="9999")
    assert normalize_event("999", event, set(), {}, "999") is None


def test_private_allow_from_filters_unauthorized() -> None:
    event = _private_event([{"type": "text", "data": {"text": "hi"}}], user_id="777")
    assert normalize_event("999", event, {"111"}, {}, "999") is None


def test_self_message_filtered() -> None:
    event = _private_event([{"type": "text", "data": {"text": "hi"}}], user_id="999")
    assert normalize_event("999", event, set(), {}, "999") is None


def test_non_message_event_ignored() -> None:
    assert normalize_event("999", {"post_type": "notice"}, set(), {}, "999") is None


@pytest.mark.asyncio
async def test_send_text_and_mention_to_group() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(
            {
                "path": request.url.path,
                "body": body,
                "auth": request.headers.get("authorization"),
            }
        )
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"message_id": 555}})

    harness = PluginHarness()
    settings = _settings(access_token="tok")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(settings, harness.context(), client=client)
    receipt = await plugin.send(
        _session(ChatType.GROUP, "222"),
        OutboundMessage(
            session_id="s1",
            components=[
                MessageComponent(type=ComponentType.MENTION, target_id="123", target_name="小红"),
                MessageComponent.text_component("收到"),
            ],
        ),
    )
    assert receipt.status == DeliveryStatus.SENT
    assert receipt.external_message_id == "555"
    assert captured[0]["path"] == "/send_group_msg"
    assert captured[0]["body"]["group_id"] == 222
    assert captured[0]["body"]["message"] == [
        {"type": "at", "data": {"qq": "123"}},
        {"type": "text", "data": {"text": "收到"}},
    ]
    assert captured[0]["auth"] == "Bearer tok"
    await client.aclose()


@pytest.mark.asyncio
async def test_send_image_ref_converts_to_base64(tmp_path: Path) -> None:
    content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    image_file = tmp_path / "pic.png"
    image_file.write_bytes(content)

    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"message_id": 7}})

    harness = PluginHarness()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(_settings(), harness.context(), client=client)
    receipt = await plugin.send(
        _session(ChatType.PRIVATE, "111"),
        OutboundMessage(
            session_id="s1",
            components=[
                MessageComponent(
                    type=ComponentType.IMAGE_REF,
                    attachment_id="att_1",
                    filename="pic.png",
                    mime_type="image/png",
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    storage_path=str(image_file),
                ),
            ],
        ),
    )
    assert receipt.status == DeliveryStatus.SENT
    sent_segments = captured[0]["message"]
    assert sent_segments[0]["type"] == "image"
    file_uri = sent_segments[0]["data"]["file"]
    assert file_uri.startswith("base64://")
    assert base64.b64decode(file_uri[len("base64://"):]) == content
    assert captured[0]["user_id"] == 111
    await client.aclose()


@pytest.mark.asyncio
async def test_send_reply_includes_reply_segment() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9}})

    harness = PluginHarness()
    harness.reply_external_id = encode_reply_id("100", "88")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(_settings(), harness.context(), client=client)
    receipt = await plugin.send(
        _session(ChatType.GROUP, "222"),
        OutboundMessage(
            session_id="s1",
            reply_to_message_id="stored-1",
            components=[MessageComponent.text_component("这是回复")],
        ),
    )
    assert receipt.status == DeliveryStatus.SENT
    segments = captured[0]["message"]
    assert segments[0] == {"type": "reply", "data": {"id": "100"}}
    assert segments[1] == {"type": "text", "data": {"text": "这是回复"}}
    await client.aclose()


@pytest.mark.asyncio
async def test_private_reply_does_not_include_reply_segment() -> None:
    """私聊回复不应渲染 OneBot reply 段。"""

    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"message_id": 10}})

    harness = PluginHarness()
    harness.reply_external_id = encode_reply_id("100", "88")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(_settings(), harness.context(), client=client)
    receipt = await plugin.send(
        _session(ChatType.PRIVATE, "111"),
        OutboundMessage(
            session_id="s1",
            reply_to_message_id="stored-1",
            components=[MessageComponent.text_component("这是私聊回复")],
        ),
    )

    assert receipt.status == DeliveryStatus.SENT
    assert captured[0]["message"] == [{"type": "text", "data": {"text": "这是私聊回复"}}]
    await client.aclose()


@pytest.mark.asyncio
async def test_send_reports_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "failed", "retcode": 100, "msg": "群禁言"})

    harness = PluginHarness()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(_settings(), harness.context(), client=client)
    receipt = await plugin.send(
        _session(ChatType.GROUP, "222"),
        OutboundMessage(session_id="s1", components=[MessageComponent.text_component("hi")]),
    )
    assert receipt.status == DeliveryStatus.FAILED
    assert receipt.error_code == "onebot_100"
    assert "群禁言" in (receipt.error_message or "")
    await client.aclose()


@pytest.mark.asyncio
async def test_send_image_missing_file_fails(tmp_path: Path) -> None:
    harness = PluginHarness()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"retcode": 0}))
    client = httpx.AsyncClient(transport=transport)
    plugin = OneBotPlugin(_settings(), harness.context(), client=client)
    receipt = await plugin.send(
        _session(ChatType.PRIVATE, "111"),
        OutboundMessage(
            session_id="s1",
            components=[
                MessageComponent(
                    type=ComponentType.IMAGE_REF,
                    attachment_id="att_1",
                    filename="x.png",
                    mime_type="image/png",
                    size=10,
                    sha256="a" * 64,
                    storage_path=str(tmp_path / "missing.png"),
                ),
            ],
        ),
    )
    assert receipt.status == DeliveryStatus.FAILED
    assert receipt.error_code == "onebot_media_read_failed"
    await client.aclose()


@pytest.mark.asyncio
async def test_send_audio_ref_converts_to_record_segment(tmp_path: Path) -> None:
    content = b"#!AMR\n" + b"\x00" * 32
    audio_file = tmp_path / "voice.amr"
    audio_file.write_bytes(content)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"retcode": 0, "data": {"message_id": 11}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(_settings(), PluginHarness().context(), client=client)
    receipt = await plugin.send(
        _session(ChatType.PRIVATE, "111"),
        OutboundMessage(
            session_id="s1",
            components=[
                MessageComponent(
                    type=ComponentType.AUDIO_REF,
                    attachment_id="att_audio",
                    filename="voice.amr",
                    mime_type="audio/amr",
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    storage_path=str(audio_file),
                )
            ],
        ),
    )
    assert receipt.status == DeliveryStatus.SENT
    segment = captured[0]["message"][0]
    assert segment["type"] == "record"
    assert base64.b64decode(segment["data"]["file"][len("base64://"):]) == content
    assert "_sha256" not in segment["data"]
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_type", "chat_id", "endpoint", "target_key"),
    [
        (ChatType.PRIVATE, "111", "/upload_private_file", "user_id"),
        (ChatType.GROUP, "222", "/upload_group_file", "group_id"),
    ],
)
async def test_send_file_uses_onebot_upload_action(
    tmp_path: Path,
    chat_type: ChatType,
    chat_id: str,
    endpoint: str,
    target_key: str,
) -> None:
    content = b"%PDF-1.7\ncontent"
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(content)
    captured: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"retcode": 0, "data": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plugin = OneBotPlugin(_settings(), PluginHarness().context(), client=client)
    receipt = await plugin.send(
        _session(chat_type, chat_id),
        OutboundMessage(
            session_id="s1",
            components=[
                MessageComponent(
                    type=ComponentType.FILE_REF,
                    attachment_id="att_file",
                    filename="report.pdf",
                    mime_type="application/pdf",
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    storage_path=str(file_path),
                )
            ],
        ),
    )
    assert receipt.status == DeliveryStatus.SENT
    assert captured[0][0] == endpoint
    assert captured[0][1][target_key] == int(chat_id)
    assert captured[0][1]["name"] == "report.pdf"
    assert base64.b64decode(captured[0][1]["file"][len("base64://"):]) == content
    await client.aclose()


@pytest.mark.asyncio
async def test_send_file_rejects_mixed_components(tmp_path: Path) -> None:
    content = b"file"
    file_path = tmp_path / "x.bin"
    file_path.write_bytes(content)
    plugin = OneBotPlugin(_settings(), PluginHarness().context())
    receipt = await plugin.send(
        _session(ChatType.PRIVATE, "111"),
        OutboundMessage(
            session_id="s1",
            components=[
                MessageComponent.text_component("说明"),
                MessageComponent(
                    type=ComponentType.FILE_REF,
                    attachment_id="att_file",
                    filename="x.bin",
                    mime_type="application/octet-stream",
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    storage_path=str(file_path),
                ),
            ],
        ),
    )
    assert receipt.status == DeliveryStatus.FAILED
    assert receipt.error_code == "onebot_file_must_be_standalone"
    await plugin.stop()


def test_render_segments_marks_unsupported() -> None:
    # QUOTE 组件被跳过（由 reply_to_message_id 处理），不视为 unsupported
    segments, unsupported = _render_segments(
        [
            MessageComponent.text_component("a"),
            MessageComponent(type=ComponentType.QUOTE, message_id="q1", target_id="t", target_name="n"),
        ]
    )
    assert segments == [{"type": "text", "data": {"text": "a"}}]
    assert unsupported == []


def test_encode_decode_reply_id_roundtrip() -> None:
    encoded = encode_reply_id("msg-100", "reply-88")
    assert decode_reply_id(encoded) == "msg-100"
    assert decode_reply_id("onebot:" + base64.urlsafe_b64encode(b"m").decode().rstrip("=")) == "m"
    assert decode_reply_id(None) is None
    assert decode_reply_id("other:xxx") is None
