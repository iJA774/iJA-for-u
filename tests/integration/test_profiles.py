import pytest

from adapters.persistence import DatabaseStore
from domain.models import (
    ChatType,
    FactStatus,
    InboundMessage,
    MessageComponent,
    Participant,
    ProfileExtractionRun,
    ProfileFact,
    scope_key_for,
)


@pytest.mark.asyncio
async def test_profile_sources_merge_and_conflicts_remain_explicit(settings) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="profile-private",
        chat_type=ChatType.PRIVATE,
        display_name="画像测试",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )

    source_ids: list[str] = []
    for index, text in enumerate(("我喜欢爵士乐", "我还是喜欢爵士乐", "我更喜欢摇滚乐")):
        message, _ = await store.append_inbound(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id=f"profile-{index}",
                external_chat_id=session.external_chat_id,
                sender_id="u1",
                sender_name="小明",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component(text)],
            )
        )
        source_ids.append(message.id)

    scope = scope_key_for(session)
    first = await store.save_fact(
        ProfileFact(
            subject_id="u1",
            scope_key=scope,
            category="偏好",
            content="喜欢爵士乐",
            confidence=0.8,
            source_message_ids=[source_ids[0]],
        )
    )
    merged = await store.save_fact(
        ProfileFact(
            subject_id="u1",
            scope_key=scope,
            category="偏好",
            content="喜欢爵士乐",
            confidence=0.9,
            source_message_ids=[source_ids[1]],
        )
    )
    assert merged.id == first.id
    assert set(merged.source_message_ids) == set(source_ids[:2])

    await store.save_fact(
        ProfileFact(
            subject_id="u1",
            scope_key=scope,
            category="偏好",
            content="喜欢摇滚乐",
            confidence=0.85,
            source_message_ids=[source_ids[2]],
        )
    )
    facts = await store.list_facts(scope)
    assert len(facts) == 2
    assert {fact.status for fact in facts} == {FactStatus.CONFLICTED}
    await store.close()


@pytest.mark.asyncio
async def test_profile_fact_memory_and_run_completion_are_atomic(
    settings,
    monkeypatch,
) -> None:
    """画像投影记忆失败时，事实与 Run 完成态必须一起回滚。"""

    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="profile-atomic",
        chat_type=ChatType.PRIVATE,
        display_name="画像原子提交",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    message, _ = await store.append_inbound(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="profile-atomic-source",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[MessageComponent.text_component("我喜欢爵士乐")],
        )
    )
    run = ProfileExtractionRun(
        session_id=session.id,
        subject_id="u1",
        scope_key=scope_key_for(session),
        source_message_ids=[message.id],
        data_epoch=session.data_epoch,
    )
    assert await store.save_extraction_run(run)

    def fail_memory_projection(memory):
        del memory
        raise RuntimeError("模拟画像记忆投影失败")

    monkeypatch.setattr(
        DatabaseStore,
        "_memory_to_row",
        staticmethod(fail_memory_projection),
    )
    with pytest.raises(RuntimeError, match="模拟画像记忆投影失败"):
        await store.commit_profile_extraction(
            run=run,
            facts=[
                ProfileFact(
                    subject_id="u1",
                    scope_key=scope_key_for(session),
                    category="偏好",
                    content="喜欢爵士乐",
                    confidence=0.9,
                    source_message_ids=[message.id],
                )
            ],
        )

    assert await store.list_facts(scope_key_for(session)) == []
    assert await store.list_memories(session_id=session.id) == []
    persisted = await store.get_extraction_run(run.id)
    assert persisted is not None
    assert persisted.status.value == "pending"
    await store.close()
