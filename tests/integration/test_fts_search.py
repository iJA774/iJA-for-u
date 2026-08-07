from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config

from adapters.model import FakeModelProvider
from adapters.persistence import DatabaseStore
from adapters.persistence.migration import upgrade_database
from application.memory import memory_content_hash
from bootstrap import build_runtime
from domain.errors import NotFoundError
from domain.models import (
    ChatType,
    ComponentType,
    InboundMessage,
    MemoryKind,
    MemoryRecord,
    MemorySourceChain,
    MemoryStatus,
    MessageComponent,
    Participant,
    scope_key_for,
)


def test_fts_migration_backfills_existing_message_and_memory(settings) -> None:
    """从旧 head 升级时，历史权威数据应完整进入可重建索引。"""

    database_path = settings.storage.database_path
    database_path.parent.mkdir(parents=True, exist_ok=True)
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(settings.project_root / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "0017_session_data_epoch")
    with sqlite3.connect(database_path) as connection:
        # 0001 会读取当前 metadata；主动移除新列，真实模拟 0017 时代的已有库。
        connection.execute("ALTER TABLE messages DROP COLUMN search_text")
        connection.execute(
            """
            INSERT INTO sessions(
                id, platform, account_id, external_chat_id, chat_type, display_name,
                revision, data_epoch, memory_cleared_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "session-before-fts",
                "benchmark",
                "ija-test",
                "before-fts",
                "private",
                "迁移前会话",
                1,
                1,
                None,
                "2026-01-01 00:00:00",
                "2026-01-01 00:00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO messages(
                id, session_id, platform, account_id, external_message_id, role,
                sender_id, sender_name, components_json, processed_turn_id,
                origin, origin_run_id, source_refs_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "message-before-fts",
                "session-before-fts",
                "benchmark",
                "ija-test",
                "before-fts-message",
                "user",
                "user-before-fts",
                "小明",
                json.dumps(
                    [{"type": "text", "text": "迁移前的赛博火锅暗号"}],
                    ensure_ascii=False,
                ),
                None,
                None,
                None,
                None,
                "2026-01-01 00:00:00",
            ),
        )
        content = "用户计划下周末去杭州参加独立音乐节"
        connection.execute(
            """
            INSERT INTO memory_records(
                id, session_id, scope_key, subject_id, kind, content, content_hash,
                confidence, importance, status, source_chain, source_run_id,
                source_message_ids_json, source_refs_json, supersedes_id, happened_at,
                reinforcement, recall_count, last_recalled_at, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                "memory-before-fts",
                "session-before-fts",
                "private:session-before-fts",
                "user-before-fts",
                "event",
                content,
                memory_content_hash(content),
                0.9,
                0.8,
                "active",
                "manual",
                None,
                "[]",
                "[]",
                None,
                None,
                1,
                0,
                None,
                "2026-01-01 00:00:00",
                "2026-01-01 00:00:00",
            ),
        )
        connection.commit()

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT search_text FROM messages WHERE id = 'message-before-fts'"
        ).fetchone() == ("迁移前的赛博火锅暗号",)
        assert connection.execute(
            """
            SELECT message_id FROM message_search_fts
            WHERE message_search_fts MATCH '"赛博火锅暗号"'
            """
        ).fetchall() == [("message-before-fts",)]
        assert connection.execute(
            """
            SELECT memory_id FROM memory_search_fts
            WHERE memory_search_fts MATCH '"独立音乐节"'
            """
        ).fetchall() == [("memory-before-fts",)]


async def _private_session(
    store: DatabaseStore,
    suffix: str,
):
    return await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id=f"fts-{suffix}",
        chat_type=ChatType.PRIVATE,
        display_name=f"FTS 测试-{suffix}",
        participants=[Participant(external_user_id=f"user-{suffix}", display_name="小明")],
    )

async def test_session_message_search_matches_sender_and_content_without_crossing_session(
    settings,
) -> None:
    """控制台搜索应匹配发送者或可见正文，并严格限制在当前 Session。"""

    upgrade_database(settings.project_root, settings.storage.database_path)
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()

    session = await _private_session(store, "console-search")
    other = await _private_session(store, "console-search-other")

    content_target, _ = await store.append_inbound(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="console-search-content",
            external_chat_id=session.external_chat_id,
            chat_type=session.chat_type,
            sender_id="user-content",
            sender_name="小明",
            components=[MessageComponent.text_component("今晚一起去吃赛博火锅")],
        )
    )
    sender_target, _ = await store.append_inbound(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="console-search-sender",
            external_chat_id=session.external_chat_id,
            chat_type=session.chat_type,
            sender_id="user-sender",
            sender_name="阿澄",
            components=[MessageComponent.text_component("收到，我稍后出发")],
        )
    )
    await store.append_inbound(
        InboundMessage(
            platform=other.platform,
            account_id=other.account_id,
            external_message_id="console-search-other-session",
            external_chat_id=other.external_chat_id,
            chat_type=other.chat_type,
            sender_id="user-other",
            sender_name="阿澄",
            components=[MessageComponent.text_component("另一个会话里的赛博火锅")],
        )
    )

    content_matches = await store.search_session_messages(
        session.id,
        query_text="赛博火锅",
        limit=20,
    )
    sender_matches = await store.search_session_messages(
        session.id,
        query_text="阿澄",
        limit=20,
    )

    assert [message.id for message in content_matches] == [content_target.id]
    assert [message.id for message in sender_matches] == [sender_target.id]

    await store.close()

async def test_session_message_window_is_centered_and_scoped(settings) -> None:
    """消息窗口应包含目标前后内容，并拒绝读取其他 Session 的目标。"""

    upgrade_database(settings.project_root, settings.storage.database_path)
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()

    session = await _private_session(store, "message-window")
    other = await _private_session(store, "message-window-other")
    base_time = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    messages = []
    for index in range(6):
        message, _ = await store.append_inbound(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id=f"message-window-{index}",
                external_chat_id=session.external_chat_id,
                chat_type=session.chat_type,
                sender_id="window-user",
                sender_name="小明",
                components=[
                    MessageComponent.text_component(f"当前会话消息 {index}")
                ],
                received_at=base_time + timedelta(minutes=index),
            )
        )
        messages.append(message)

    other_message, _ = await store.append_inbound(
        InboundMessage(
            platform=other.platform,
            account_id=other.account_id,
            external_message_id="message-window-other",
            external_chat_id=other.external_chat_id,
            chat_type=other.chat_type,
            sender_id="other-window-user",
            sender_name="小红",
            components=[MessageComponent.text_component("其他会话消息")],
            received_at=base_time + timedelta(minutes=3),
        )
    )

    window = await store.get_session_message_window(
        session.id,
        messages[3].id,
        before=2,
        after=1,
    )

    assert [message.id for message in window] == [
        messages[1].id,
        messages[2].id,
        messages[3].id,
        messages[4].id,
    ]

    with pytest.raises(NotFoundError, match="消息不存在"):
        await store.get_session_message_window(
            session.id,
            other_message.id,
            before=2,
            after=1,
        )

    await store.close()

async def test_message_fts_uses_visible_text_and_keeps_attachment_lookup_exact(
    settings,
) -> None:
    """全文候选不能把附件内部 ID 当聊天正文，也不能跨 Session 泄漏。"""

    upgrade_database(settings.project_root, settings.storage.database_path)
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    assert store.fts_search_enabled
    session = await _private_session(store, "message")
    other = await _private_session(store, "message-other")

    target, _ = await store.append_inbound(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="fts-message-1",
            external_chat_id=session.external_chat_id,
            chat_type=session.chat_type,
            sender_id="user-message",
            sender_name="小明",
            components=[MessageComponent.text_component("今晚赛博火锅暗号启动")],
        )
    )
    await store.append_inbound(
        InboundMessage(
            platform=other.platform,
            account_id=other.account_id,
            external_message_id="fts-message-other",
            external_chat_id=other.external_chat_id,
            chat_type=other.chat_type,
            sender_id="user-message-other",
            sender_name="小红",
            components=[MessageComponent.text_component("今晚赛博火锅暗号启动")],
        )
    )
    attachment_id = "attachment-secret-key"
    attachment, _ = await store.append_inbound(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="fts-message-attachment",
            external_chat_id=session.external_chat_id,
            chat_type=session.chat_type,
            sender_id="user-message",
            sender_name="小明",
            components=[
                MessageComponent(
                    type=ComponentType.IMAGE_REF,
                    attachment_id=attachment_id,
                    filename="cat.png",
                    mime_type="image/png",
                    size=1,
                    sha256="0" * 64,
                    storage_path="attachments/cat.png",
                    description="一张猫猫表情包",
                    is_expression=True,
                )
            ],
        )
    )

    matches = await store.page_recallable_messages(
        session.id,
        limit=10,
        query_text="赛博火锅暗号",
    )
    short_matches = await store.page_recallable_messages(
        session.id,
        limit=10,
        query_text="火锅",
    )
    hidden_metadata = await store.page_recallable_messages(
        session.id,
        limit=10,
        query_text=attachment_id,
    )

    assert [item.id for item in matches] == [target.id]
    assert [item.id for item in short_matches] == [target.id]
    assert hidden_metadata == []
    exact_attachment = await store.get_session_attachment(session.id, attachment_id)
    assert exact_attachment is not None
    assert exact_attachment.attachment_id == attachment_id
    assert attachment.id != target.id
    await store.close()


async def test_memory_fts_candidates_are_scoped_active_and_trigger_updated(
    settings,
) -> None:
    """记忆 FTS 只负责同域活跃候选，并随权威内容/状态事务更新。"""

    upgrade_database(settings.project_root, settings.storage.database_path)
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await _private_session(store, "memory")
    other = await _private_session(store, "memory-other")
    scope = scope_key_for(session)
    target = MemoryRecord(
        session_id=session.id,
        scope_key=scope,
        subject_id="user-memory",
        kind=MemoryKind.EVENT,
        content="用户计划下周末去杭州参加独立音乐节",
        content_hash=memory_content_hash("用户计划下周末去杭州参加独立音乐节"),
        source_chain=MemorySourceChain.MANUAL,
    )
    await store.save_memory(target)
    await store.save_memory(
        MemoryRecord(
            session_id=other.id,
            scope_key=scope_key_for(other),
            subject_id="user-memory-other",
            kind=MemoryKind.EVENT,
            content=target.content,
            content_hash=target.content_hash,
            source_chain=MemorySourceChain.MANUAL,
        )
    )
    await store.save_memory(
        MemoryRecord(
            session_id=session.id,
            scope_key=scope,
            subject_id="user-memory",
            kind=MemoryKind.EVENT,
            content="用户计划下周末去杭州参加独立音乐节",
            content_hash=target.content_hash,
            status=MemoryStatus.RETRACTED,
            source_chain=MemorySourceChain.MANUAL,
        )
    )

    matches = await store.search_active_memory_candidates(
        scope_key=scope,
        query_text="下周末杭州独立音乐节安排",
    )
    assert matches is not None
    assert [item.id for item in matches] == [target.id]
    assert (
        await store.search_active_memory_candidates(
            scope_key=scope,
            query_text="杭州",
        )
        is None
    )

    target.content = "用户决定在家整理机械键盘收藏"
    target.content_hash = memory_content_hash(target.content)
    await store.save_memory(target)
    assert await store.search_active_memory_candidates(
        scope_key=scope,
        query_text="下周末杭州独立音乐节安排",
    ) == []
    updated = await store.search_active_memory_candidates(
        scope_key=scope,
        query_text="在家整理机械键盘收藏计划",
    )
    assert updated is not None
    assert [item.id for item in updated] == [target.id]

    target.status = MemoryStatus.RETRACTED
    await store.save_memory(target)
    assert await store.search_active_memory_candidates(
        scope_key=scope,
        query_text="在家整理机械键盘收藏计划",
    ) == []
    await store.close()


async def test_memory_search_without_fts_requests_full_scan_fallback(settings) -> None:
    """未执行迁移或 SQLite 不支持 FTS 时，store 明确要求应用层完整降级。"""

    store = DatabaseStore(settings.storage.data_dir / "without-fts.sqlite3")
    await store.initialize()
    assert not store.fts_search_enabled
    assert (
        await store.search_active_memory_candidates(
            scope_key="private:test",
            query_text="这是一个足够长的检索问题",
        )
        is None
    )
    await store.close()


async def test_initialize_rebuilds_fts_when_trigger_is_missing(settings) -> None:
    """启动时缺失同步 trigger 必须重建，不能把会继续漂移的索引标为可用。"""

    upgrade_database(settings.project_root, settings.storage.database_path)
    with sqlite3.connect(settings.storage.database_path) as connection:
        connection.execute("DROP TRIGGER memory_search_ai")
        connection.commit()

    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    assert store.fts_search_enabled
    session = await _private_session(store, "rebuild-trigger")
    scope = scope_key_for(session)
    target = MemoryRecord(
        session_id=session.id,
        scope_key=scope,
        subject_id="user-rebuild-trigger",
        kind=MemoryKind.EVENT,
        content="用户准备周末参加城市夜跑训练活动",
        content_hash=memory_content_hash("用户准备周末参加城市夜跑训练活动"),
        source_chain=MemorySourceChain.MANUAL,
    )
    await store.save_memory(target)

    matches = await store.search_active_memory_candidates(
        scope_key=scope,
        query_text="周末城市夜跑训练活动",
    )
    assert matches is not None
    assert [item.id for item in matches] == [target.id]
    await store.close()


async def test_initialize_rebuilds_fts_when_index_row_is_missing(settings) -> None:
    """启动时索引行不一致必须从权威表重建，不能把漏行误判成无结果。"""

    upgrade_database(settings.project_root, settings.storage.database_path)
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await _private_session(store, "rebuild-row")
    scope = scope_key_for(session)
    target = MemoryRecord(
        session_id=session.id,
        scope_key=scope,
        subject_id="user-rebuild-row",
        kind=MemoryKind.EVENT,
        content="用户收藏了一家深夜营业的手工面包店",
        content_hash=memory_content_hash("用户收藏了一家深夜营业的手工面包店"),
        source_chain=MemorySourceChain.MANUAL,
    )
    await store.save_memory(target)
    await store.close()
    with sqlite3.connect(settings.storage.database_path) as connection:
        connection.execute(
            "DELETE FROM memory_search_fts WHERE memory_id = ?",
            (target.id,),
        )
        connection.commit()

    repaired_store = DatabaseStore(settings.storage.database_path)
    await repaired_store.initialize()
    matches = await repaired_store.search_active_memory_candidates(
        scope_key=scope,
        query_text="深夜营业手工面包店",
    )
    assert matches is not None
    assert [item.id for item in matches] == [target.id]
    await repaired_store.close()


async def test_memory_service_uses_fts_then_falls_back_for_short_query(
    settings,
    monkeypatch,
) -> None:
    """长查询不应全量分页；短中文概念仍沿用完整 BM25 语义。"""

    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="记忆预筛链路",
            external_chat_id="fts-memory-service",
            participants=[
                Participant(external_user_id="user-memory-service", display_name="小明")
            ],
        )
        target = await runtime.memory.remember(
            session=session,
            content="用户计划下周末去杭州参加独立音乐节",
            kind=MemoryKind.EVENT,
            subject_id="user-memory-service",
            source_chain=MemorySourceChain.MANUAL,
        )
        await runtime.memory.remember(
            session=session,
            content="用户在工作日早晨喝红茶",
            kind=MemoryKind.PREFERENCE,
            subject_id="user-memory-service",
            source_chain=MemorySourceChain.MANUAL,
        )
        original_page = runtime.store.page_memories
        page_calls = 0

        async def counting_page(*args, **kwargs):
            nonlocal page_calls
            page_calls += 1
            return await original_page(*args, **kwargs)

        monkeypatch.setattr(runtime.store, "page_memories", counting_page)
        long_query = await runtime.memory.retrieve(
            session=session,
            query="下周末杭州独立音乐节安排",
            record_recall=False,
        )
        assert [item.id for item in long_query] == [target.id]
        assert page_calls == 0

        short_query = await runtime.memory.retrieve(
            session=session,
            query="杭州",
            record_recall=False,
        )
        assert [item.id for item in short_query] == [target.id]
        assert page_calls > 0
    finally:
        await runtime.stop()
