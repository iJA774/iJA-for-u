"""DatabaseStore 公共仓储行为的 characterization tests。"""

import pytest

from adapters.persistence import DatabaseStore
from domain.errors import InputValidationError
from domain.models import (
    ChatType,
    DecisionAction,
    InboundMessage,
    MemoryConsolidationRun,
    MemorySourceChain,
    MessageComponent,
    Participant,
    TurnDecision,
    scope_key_for,
)
from ports import (
    ApplicationRepository,
    ChatRepository,
    OperationsRepository,
    ProfileMemoryRepository,
    SocialLearningRepository,
)


async def _create_private(
    store: DatabaseStore,
    *,
    suffix: str,
):
    """创建一个彼此隔离的私聊 Session。"""

    return await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id=f"repository-contract-{suffix}",
        chat_type=ChatType.PRIVATE,
        display_name=f"仓储契约-{suffix}",
        participants=[
            Participant(
                external_user_id=f"user-{suffix}",
                display_name=f"用户-{suffix}",
            )
        ],
    )


def _inbound(session, *, suffix: str, text: str) -> InboundMessage:
    return InboundMessage(
        platform=session.platform,
        account_id=session.account_id,
        external_message_id=f"repository-message-{suffix}",
        external_chat_id=session.external_chat_id,
        sender_id=f"user-{suffix}",
        sender_name=f"用户-{suffix}",
        chat_type=session.chat_type,
        components=[MessageComponent.text_component(text)],
    )


def _decision(session_id: str, message_id: str, *, suffix: str) -> TurnDecision:
    return TurnDecision(
        id=f"turn_repository_{suffix}",
        session_id=session_id,
        action=DecisionAction.SILENCE,
        strategy="repository-characterization",
        score=0,
        threshold=50,
        reason="锁定重构前公共事务行为",
        trigger_message_id=message_id,
    )


def _memory_run(
    session,
    message_id: str,
    *,
    suffix: str,
) -> MemoryConsolidationRun:
    return MemoryConsolidationRun(
        id=f"memory_run_repository_{suffix}",
        session_id=session.id,
        scope_key=scope_key_for(session),
        source_chain=MemorySourceChain.REACTIVE,
        source_run_id=f"turn_repository_{suffix}",
        source_message_ids=[message_id],
        source_fingerprint=(suffix[0] if suffix else "a") * 64,
        data_epoch=session.data_epoch,
    )


@pytest.mark.asyncio
async def test_public_chat_repository_round_trip_is_idempotent_across_reopen(
    settings,
) -> None:
    """入站幂等、组件序列化和 Turn/outbox 原子提交在重启后保持一致。"""

    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    try:
        session = await _create_private(store, suffix="roundtrip")
        inbound = _inbound(
            session,
            suffix="roundtrip",
            text="保留  空格、换行\n和 Unicode 表情：🥳",
        )
        saved, created = await store.append_inbound(inbound)
        duplicate, duplicate_created = await store.append_inbound(inbound)

        assert created is True
        assert duplicate_created is False
        assert duplicate == saved
        assert saved.components == inbound.components

        decision = _decision(session.id, saved.id, suffix="roundtrip")
        run = _memory_run(session, saved.id, suffix="a-roundtrip")
        persisted_run, run_created = await store.save_reactive_turn_with_memory_run(
            decision,
            [saved.id],
            run,
        )
        assert run_created is True
        assert persisted_run == run
    finally:
        await store.close()

    reopened = DatabaseStore(settings.storage.database_path)
    await reopened.initialize()
    try:
        messages = await reopened.list_messages(session.id)
        assert len(messages) == 1
        assert messages[0].components == inbound.components
        assert messages[0].processed_turn_id == decision.id
        assert await reopened.list_pending_messages(session.id) == []
        assert await reopened.get_decision(decision.id) == decision
        assert await reopened.get_memory_consolidation_run(run.id) == run
    finally:
        await reopened.close()


def test_database_store_is_the_single_repository_protocol_implementation(
    settings,
) -> None:
    """四个 bounded context 端口仍由同一个 DatabaseStore 组合实现。"""

    store = DatabaseStore(settings.storage.database_path)
    assert isinstance(store, ChatRepository)
    assert isinstance(store, ProfileMemoryRepository)
    assert isinstance(store, SocialLearningRepository)
    assert isinstance(store, OperationsRepository)
    assert isinstance(store, ApplicationRepository)


@pytest.mark.asyncio
async def test_public_turn_transaction_rolls_back_on_cross_session_message(
    settings,
) -> None:
    """跨 Session 消息校验失败时，决策、处理标记和 outbox 必须全部回滚。"""

    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    try:
        first = await _create_private(store, suffix="owner")
        second = await _create_private(store, suffix="intruder")
        message, _ = await store.append_inbound(_inbound(first, suffix="owner", text="只能属于第一个会话"))
        decision = _decision(second.id, message.id, suffix="rollback")
        run = _memory_run(second, message.id, suffix="b-rollback")

        with pytest.raises(
            InputValidationError,
            match="不存在或越域",
        ):
            await store.save_reactive_turn_with_memory_run(
                decision,
                [message.id],
                run,
            )

        assert await store.get_decision(decision.id) is None
        assert await store.get_memory_consolidation_run(run.id) is None
        pending = await store.list_pending_messages(first.id)
        assert [item.id for item in pending] == [message.id]
        assert pending[0].processed_turn_id is None
    finally:
        await store.close()
