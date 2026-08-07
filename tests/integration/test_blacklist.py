import asyncio

import pytest
from fastapi.testclient import TestClient

from adapters.persistence import DatabaseStore
from api import create_app
from bootstrap import build_runtime
from domain.errors import NotFoundError
from domain.models import (
    BlacklistEntry,
    BlacklistSource,
    ChatType,
    InboundMessage,
    MessageComponent,
    Participant,
    ParticipantRole,
)
from plugins._host import PlatformIngress
from plugins._host.contracts import InboundEnvelope
from tools import ToolContext


async def _cancel_debounce(chat) -> None:
    for task in chat._debounce_tasks.values():
        task.cancel()
    await asyncio.gather(*chat._debounce_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_database_blacklist_crud_is_idempotent(settings) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    try:
        entry = await store.add_blacklist(
            BlacklistEntry(
                platform="web-simulator",
                account_id="ija-local",
                external_user_id="bad-user",
                display_name="违规者",
                reason="发布违法言论",
                source=BlacklistSource.MANUAL,
            )
        )
        assert entry.id
        assert await store.is_blacklisted("web-simulator", "ija-local", "bad-user")
        assert not await store.is_blacklisted("web-simulator", "ija-local", "other-user")

        # 重复拉黑同一用户应更新原因而非报错
        updated = await store.add_blacklist(
            BlacklistEntry(
                platform="web-simulator",
                account_id="ija-local",
                external_user_id="bad-user",
                display_name="违规者改名",
                reason="更新后的原因",
                source=BlacklistSource.AGENT,
            )
        )
        assert updated.id == entry.id
        assert updated.reason == "更新后的原因"
        assert updated.display_name == "违规者改名"

        listed = await store.list_blacklist()
        assert len(listed) == 1
        assert listed[0].external_user_id == "bad-user"

        fetched = await store.get_blacklist(entry.id)
        assert fetched is not None
        assert fetched.reason == "更新后的原因"

        removed = await store.remove_blacklist(entry.id)
        assert removed.external_user_id == "bad-user"
        assert not await store.is_blacklisted("web-simulator", "ija-local", "bad-user")

        with pytest.raises(NotFoundError, match="黑名单条目不存在"):
            await store.remove_blacklist(entry.id)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_chat_service_rejects_blacklisted_user_ingest(settings) -> None:
    runtime = build_runtime(settings)
    await runtime.store.initialize()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="私聊",
            external_chat_id="bad-user",
            participants=[Participant(external_user_id="bad-user", display_name="违规者")],
        )
        await runtime.blacklist.block(
            platform=session.platform,
            account_id=session.account_id,
            external_user_id="bad-user",
            display_name="违规者",
            reason="屡次违规",
            source=BlacklistSource.MANUAL,
        )
        result = await runtime.chat.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="m1",
                external_chat_id=session.external_chat_id,
                sender_id="bad-user",
                sender_name="违规者",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component("又来发消息")],
            )
        )
        assert not result.accepted
        assert result.control
        # 消息未入库、未调度 Turn
        assert await runtime.store.list_messages(session.id, 100) == []
    finally:
        await _cancel_debounce(runtime.chat)
        await runtime.chat.stop()
        await runtime.store.close()


@pytest.mark.asyncio
async def test_platform_ingress_rejects_blacklisted_before_session_creation(settings) -> None:
    runtime = build_runtime(settings)
    await runtime.store.initialize()
    try:
        await runtime.blacklist.block(
            platform="qq",
            account_id="bot-app",
            external_user_id="bad-member",
            display_name="违规成员",
            reason="群内发布违法言论",
            source=BlacklistSource.MANUAL,
        )
        ingress = PlatformIngress(
            runtime.store,
            runtime.chat,
            runtime.channel_capabilities,
        )
        envelope = InboundEnvelope(
            message=InboundMessage(
                platform="qq",
                account_id="bot-app",
                external_message_id="qq-1",
                external_chat_id="group-1",
                sender_id="bad-member",
                sender_name="违规成员",
                chat_type=ChatType.GROUP,
                components=[MessageComponent.text_component("违规")],
            ),
            session_display_name="群聊",
            participant_role=ParticipantRole.MEMBER,
        )
        result = await ingress.accept(envelope)
        assert not result.accepted
        assert result.control
        # 拦截发生在创建会话前，不留下任何会话副作用
        session = await runtime.store.get_session_by_route(
            "qq", "bot-app", "group-1", ChatType.GROUP
        )
        assert session is None
    finally:
        await _cancel_debounce(runtime.chat)
        await runtime.chat.stop()
        await runtime.store.close()


def test_api_blacklist_management_and_ingress_blocking(settings) -> None:
    with TestClient(create_app(settings=settings)) as client:
        # 初始黑名单为空
        assert client.get("/api/blacklist").json() == []

        # 创建模拟会话
        session = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "private",
                "display_name": "私聊",
                "external_chat_id": "bad-user",
                "participants": [
                    {"external_user_id": "bad-user", "display_name": "违规者"}
                ],
            },
        ).json()
        session_id = session["id"]

        # 被拉黑前消息正常受理
        ok = client.post(
            f"/api/simulations/sessions/{session_id}/messages",
            json={
                "sender_id": "bad-user",
                "sender_name": "违规者",
                "components": [{"type": "text", "text": "你好"}],
            },
        ).json()
        assert ok["accepted"] is True

        # 手动拉黑
        entry = client.post(
            "/api/blacklist",
            json={
                "platform": session["platform"],
                "account_id": session["account_id"],
                "external_user_id": "bad-user",
                "display_name": "违规者",
                "reason": "发布违法言论",
            },
        ).json()
        assert entry["source"] == "manual"
        assert entry["external_user_id"] == "bad-user"

        # 列表可见
        listed = client.get("/api/blacklist").json()
        assert len(listed) == 1

        # 被拉黑后消息被拦截、不入库
        blocked = client.post(
            f"/api/simulations/sessions/{session_id}/messages",
            json={
                "sender_id": "bad-user",
                "sender_name": "违规者",
                "components": [{"type": "text", "text": "又来"}],
            },
        ).json()
        assert blocked["accepted"] is False
        assert blocked["control"] is True

        # 解除拉黑后恢复受理
        client.delete(f"/api/blacklist/{entry['id']}")
        assert client.get("/api/blacklist").json() == []
        recovered = client.post(
            f"/api/simulations/sessions/{session_id}/messages",
            json={
                "sender_id": "bad-user",
                "sender_name": "违规者",
                "components": [{"type": "text", "text": "回来"}],
            },
        ).json()
        assert recovered["accepted"] is True


@pytest.mark.asyncio
async def test_block_user_skill_via_runtime_blocks_current_user(settings) -> None:
    runtime = build_runtime(settings)
    await runtime.store.initialize()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="私聊",
            external_chat_id="bad-user",
            participants=[Participant(external_user_id="bad-user", display_name="违规者")],
        )
        registry = runtime.chat.tool_registry
        context = ToolContext(
            session_id=session.id,
            actor_id="bad-user",
            available_skills={"local-blacklist"},
        )
        await registry.execute("load_skill", {"skill": "local-blacklist"}, context)
        result = await registry.execute(
            "block_user",
            {"reason": "屡次发布违法言论", "caption": "你已被拉黑"},
            context,
        )
        assert result.reply_draft is not None
        assert result.value["blocked"] is True
        assert result.value["external_user_id"] == "bad-user"

        # 用户已被写入本地黑名单
        assert await runtime.store.is_blacklisted(
            session.platform, session.account_id, "bad-user"
        )
        # 后续入站消息被拦截
        result_ingest = await runtime.chat.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="m-after",
                external_chat_id=session.external_chat_id,
                sender_id="bad-user",
                sender_name="违规者",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component("再发一条")],
            )
        )
        assert not result_ingest.accepted
    finally:
        await _cancel_debounce(runtime.chat)
        await runtime.chat.stop()
        await runtime.store.close()
