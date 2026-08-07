import asyncio
from pathlib import Path

import pytest
from anyio import Path as AsyncPath
from fastapi.testclient import TestClient
from sqlalchemy import text

from adapters.model import FakeModelProvider
from api.app import create_app
from bootstrap import build_runtime
from domain.errors import ConflictError
from domain.models import (
    BlacklistSource,
    ChatType,
    InboundMessage,
    JargonTerm,
    LearnedItemStatus,
    MemoryKind,
    MemoryRecord,
    MemorySourceChain,
    MemoryStatus,
    MessageComponent,
    ModelAttempt,
    Participant,
    ProfileFact,
    scope_key_for,
    utc_now,
)


async def _private(runtime, suffix: str):
    """在隔离测试工作区创建一个私聊 Session。"""

    return await runtime.chat.create_session(
        chat_type=ChatType.PRIVATE,
        display_name=f"数据生命周期-{suffix}",
        external_chat_id=f"data-lifecycle-{suffix}",
        participants=[
            Participant(
                external_user_id=f"user-{suffix}",
                display_name=f"用户-{suffix}",
            )
        ],
    )


def _model_attempt(*, session_id: str, suffix: str) -> ModelAttempt:
    now = utc_now()
    return ModelAttempt(
        invocation_id=f"invocation-{suffix}",
        task="chat_reply",
        provider="fake",
        profile="chat",
        model="fake",
        session_id=session_id,
        turn_id=f"turn-{suffix}",
        run_id=f"run-{suffix}",
        usage_source="unknown",
        latency_ms=1,
        success=True,
        started_at=now,
        completed_at=now,
    )


@pytest.mark.asyncio
async def test_clear_chat_preserves_semantics_scrubs_sources_and_gcs_shared_media(
    settings,
) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        first = await _private(runtime, "first")
        second = await _private(runtime, "second")
        shared = runtime.attachments.save_attachment(
            "shared.txt",
            "text/plain",
            b"shared-private-attachment",
        )
        shared_path = Path(shared.storage_path or "")

        first_message, _ = await runtime.store.append_inbound(
            InboundMessage(
                platform=first.platform,
                account_id=first.account_id,
                external_message_id="shared-first",
                external_chat_id=first.external_chat_id,
                sender_id="user-first",
                sender_name="用户-first",
                chat_type=first.chat_type,
                components=[
                    MessageComponent.text_component("第一段私聊正文"),
                    shared,
                ],
            )
        )
        await runtime.store.append_inbound(
            InboundMessage(
                platform=second.platform,
                account_id=second.account_id,
                external_message_id="shared-second",
                external_chat_id=second.external_chat_id,
                sender_id="user-second",
                sender_name="用户-second",
                chat_type=second.chat_type,
                components=[
                    MessageComponent.text_component("第二段私聊正文"),
                    shared,
                ],
            )
        )
        memory = await runtime.memory.remember(
            session=first,
            content="用户偏好简短回答",
            kind=MemoryKind.PREFERENCE,
            subject_id="user-first",
            source_chain=MemorySourceChain.REACTIVE,
            source_run_id="run-first",
            source_message_ids=[first_message.id],
            source_refs=["message:first"],
        )
        await runtime.store.save_fact(
            ProfileFact(
                subject_id="user-first",
                scope_key=scope_key_for(first),
                category="表达偏好",
                content="偏好简短回答",
                confidence=0.9,
                source_message_ids=[first_message.id],
            )
        )
        jargon = await runtime.store.save_jargon_candidate(
            JargonTerm(
                session_id=first.id,
                term="秒回",
                normalized_term="秒回",
                meaning="快速回复",
                status=LearnedItemStatus.ACTIVE,
                confidence=0.9,
                evidence_message_ids=[first_message.id],
            )
        )
        attempt = _model_attempt(session_id=first.id, suffix="first")
        await runtime.store.save_model_attempt(attempt)

        result = await runtime.chat.clear_chat(first.id)

        assert result["operation"] == "clear_chat_content"
        assert result["message_count"] == 1
        assert result["media_file_count"] == 0
        assert await runtime.store.list_messages(first.id) == []
        assert await AsyncPath(shared_path).is_file()

        retained_memory = await runtime.store.get_memory(memory.id)
        assert retained_memory is not None
        assert retained_memory.content == memory.content
        assert retained_memory.source_message_ids == []
        assert retained_memory.source_refs == []
        assert retained_memory.source_run_id is None
        retained_facts = await runtime.store.list_facts(scope_key_for(first))
        assert len(retained_facts) == 1
        assert retained_facts[0].source_message_ids == []
        retained_jargons = await runtime.store.list_jargons(first.id)
        assert [item.id for item in retained_jargons] == [jargon.id]
        assert retained_jargons[0].evidence_message_ids == []
        retained_attempts = await runtime.store.list_model_attempts(
            session_id=first.id,
            limit=1000,
        )
        retained_attempt = next(item for item in retained_attempts if item.id == attempt.id)
        assert retained_attempt.turn_id is None
        assert retained_attempt.run_id is None

        second_result = await runtime.chat.clear_chat(second.id)
        assert second_result["media_file_count"] == 1
        assert not await AsyncPath(shared_path).exists()
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_stale_manual_memory_writes_cannot_cross_lifecycle_epoch(
    settings,
) -> None:
    """手工新增、撤回和纠正都不得用 clear 前冻结的 Session 复活状态。"""

    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        stale_before_chat_clear = await _private(runtime, "stale-memory")
        original = await runtime.memory.remember(
            session=stale_before_chat_clear,
            content="用户喜欢先给结论",
            kind=MemoryKind.PREFERENCE,
            source_chain=MemorySourceChain.MANUAL,
        )

        await runtime.chat.clear_chat(stale_before_chat_clear.id)

        with pytest.raises(ConflictError, match="数据版本已失效"):
            await runtime.memory.remember(
                session=stale_before_chat_clear,
                content="迟到的新记忆",
                kind=MemoryKind.PROFILE,
                source_chain=MemorySourceChain.MANUAL,
            )
        with pytest.raises(ConflictError, match="数据版本已失效"):
            await runtime.memory.forget(
                session=stale_before_chat_clear,
                memory_id=original.id,
                reason="迟到撤回",
            )
        with pytest.raises(ConflictError, match="数据版本已失效"):
            await runtime.memory.correct(
                session=stale_before_chat_clear,
                memory_id=original.id,
                corrected_content="用户喜欢先看证据",
                reason="迟到纠正",
            )
        retained = await runtime.store.get_memory(original.id)
        assert retained is not None
        assert retained.status == MemoryStatus.ACTIVE
        assert retained.content == "用户喜欢先给结论"

        stale_before_memory_clear = await runtime.store.get_session(
            stale_before_chat_clear.id
        )
        assert stale_before_memory_clear is not None
        await runtime.chat.clear_memory(stale_before_chat_clear.id)

        with pytest.raises(ConflictError, match="数据版本已失效"):
            await runtime.memory.remember(
                session=stale_before_memory_clear,
                content="清空后迟到复活",
                kind=MemoryKind.PROFILE,
                source_chain=MemorySourceChain.MANUAL,
            )
        assert (
            await runtime.store.list_memories(
                session_id=stale_before_chat_clear.id
            )
            == []
        )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_manual_remember_rechecks_epoch_at_commit_after_concurrent_clear(
    settings,
    monkeypatch,
) -> None:
    """预检后并发 clear_chat 推进 epoch，最终事务仍必须拒绝迟到提交。"""

    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await _private(runtime, "concurrent-memory")
        entered_find = asyncio.Event()
        resume_write = asyncio.Event()
        original_find = runtime.store.find_active_memory

        async def pause_after_epoch_precheck(
            *,
            scope_key: str,
            kind: MemoryKind,
            subject_id: str | None,
            content_hash: str,
        ) -> MemoryRecord | None:
            entered_find.set()
            await resume_write.wait()
            return await original_find(
                scope_key=scope_key,
                kind=kind,
                subject_id=subject_id,
                content_hash=content_hash,
            )

        monkeypatch.setattr(
            runtime.store,
            "find_active_memory",
            pause_after_epoch_precheck,
        )
        late_write = asyncio.create_task(
            runtime.memory.remember(
                session=session,
                content="并发清空边界后的迟到记忆",
                kind=MemoryKind.PROFILE,
                source_chain=MemorySourceChain.MANUAL,
            )
        )
        await asyncio.wait_for(entered_find.wait(), timeout=2)
        try:
            await runtime.chat.clear_chat(session.id)
        finally:
            resume_write.set()

        with pytest.raises(ConflictError, match="数据版本已失效"):
            await late_write
        assert await runtime.store.list_memories(session_id=session.id) == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_delete_session_detaches_blacklist_deletes_orphan_identity_and_late_attempt(
    settings,
) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await _private(runtime, "delete")
        entry = await runtime.blacklist.block(
            platform=session.platform,
            account_id=session.account_id,
            external_user_id="user-delete",
            display_name="用户-delete",
            reason="测试独立策略保留",
            source=BlacklistSource.MANUAL,
            session_id=session.id,
        )
        attempt = _model_attempt(session_id=session.id, suffix="delete")
        await runtime.store.save_model_attempt(attempt)

        result = await runtime.chat.delete_session(session.id)

        assert result["blacklist_detached_count"] == 1
        assert result["orphan_identity_count"] == 1
        retained_entry = await runtime.blacklist.get(entry.id)
        assert retained_entry is not None
        assert retained_entry.session_id is None
        assert await runtime.store.list_model_attempts(
            session_id=session.id
        ) == []
        async with runtime.store.engine.connect() as connection:
            identity_count = (
                await connection.execute(
                    text(
                        "SELECT COUNT(*) FROM identities "
                        "WHERE external_user_id = :external_user_id"
                    ),
                    {"external_user_id": "user-delete"},
                )
            ).scalar_one()
        assert identity_count == 0

        # 已删除 Session 的迟到观测不得重新写回用户关联审计。
        await runtime.store.save_model_attempt(
            _model_attempt(session_id=session.id, suffix="late")
        )
        assert await runtime.store.list_model_attempts(
            session_id=session.id
        ) == []
    finally:
        await runtime.stop()


def test_delete_all_local_user_data_requires_exact_confirmation_and_keeps_operator_files(
    settings,
) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    secrets_marker = settings.storage.data_dir / "secrets" / "operator.keep"
    logs_marker = settings.storage.data_dir / "logs" / "operator.log"
    backup_marker = settings.project_root / "backups" / "operator.keep"
    for marker in (secrets_marker, logs_marker, backup_marker):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("operator-owned", encoding="utf-8")

    with TestClient(app) as client:
        session = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "private",
                "display_name": "全局删除测试",
                "external_chat_id": "delete-all",
                "participants": [
                    {
                        "external_user_id": "delete-all-user",
                        "display_name": "待删除用户",
                    }
                ],
            },
        ).json()
        client.post(
            "/api/blacklist",
            json={
                "platform": session["platform"],
                "account_id": session["account_id"],
                "external_user_id": "delete-all-user",
                "display_name": "待删除用户",
                "reason": "全局删除测试",
                "session_id": session["id"],
            },
        )
        upload = client.post(
            "/api/uploads",
            files={
                "file": (
                    "private.txt",
                    b"unsubmitted-private-upload",
                    "text/plain",
                )
            },
        )
        assert upload.status_code == 200
        assert any((settings.storage.data_dir / "uploads").iterdir())

        rejected = client.request(
            "DELETE",
            "/api/local-data",
            json={"confirmation": "删除全部数据"},
        )
        assert rejected.status_code == 400
        assert len(client.get("/api/sessions").json()) == 1

        deleted = client.request(
            "DELETE",
            "/api/local-data",
            json={"confirmation": "删除全部本地用户数据"},
        )
        assert deleted.status_code == 200
        assert deleted.json()["operation"] == "delete_all_local_user_data"
        assert client.get("/api/sessions").json() == []
        assert client.get("/api/blacklist").json() == []
        assert list((settings.storage.data_dir / "uploads").iterdir()) == []

    # 操作者配置、凭据目录、日志和独立备份不属于聊天用户数据。
    assert secrets_marker.read_text(encoding="utf-8") == "operator-owned"
    assert logs_marker.read_text(encoding="utf-8") == "operator-owned"
    assert backup_marker.read_text(encoding="utf-8") == "operator-owned"
