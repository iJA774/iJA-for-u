from __future__ import annotations

from datetime import timedelta

from adapters.model import FakeModelProvider
from bootstrap import build_runtime
from domain.models import (
    CandidateSourceKind,
    ChatType,
    DeliveryReceipt,
    DeliveryStatus,
    InboundMessage,
    MemoryKind,
    MemorySourceChain,
    MessageComponent,
    MessageOrigin,
    OutboundMessage,
    Participant,
    ProactiveCandidate,
    ProactiveCandidateStatus,
    ProactiveRun,
    ProactiveRunStatus,
    ProactiveStage,
    utc_now,
)


async def test_proactive_memory_reconciliation_pages_and_uses_exact_evidence(
    settings,
    monkeypatch,
) -> None:
    """对账应扫完所有 SENT run，并能越过最近 500 条消息精确找到证据。"""

    monkeypatch.setattr("proactive.service._RECONCILIATION_PAGE_SIZE", 2)
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="主动对账分页",
            external_chat_id="proactive-reconciliation-paged",
            participants=[
                Participant(external_user_id="user-reconcile", display_name="小明")
            ],
        )
        await runtime.engagement.get_policy(session.id)
        run_ids: list[str] = []
        for index in range(5):
            candidate = ProactiveCandidate(
                session_id=session.id,
                source_kind=CandidateSourceKind.RSS,
                source_id=f"feed-reconcile-{index}",
                source_key=f"entry-reconcile-{index}",
                title=f"分页主动候选 {index}",
                summary=f"用于验证第 {index} 条主动对账。",
                source_refs=[f"feed:reconcile:{index}"],
                status=ProactiveCandidateStatus.SENT,
                available_at=utc_now(),
                expires_at=utc_now() + timedelta(days=30),
            )
            await runtime.store.create_proactive_candidate(candidate)
            run = ProactiveRun(
                id=f"proactive-run-reconcile-{index:02d}",
                session_id=session.id,
                status=ProactiveRunStatus.SENT,
                stage=ProactiveStage.FINISHED,
                gate_reason="forced",
                decision_code="sent",
                decision_reason="基准对账",
                candidate_ids=[candidate.id],
                candidate_id=candidate.id,
                snapshot_at=utc_now(),
                manual_triggered=True,
            )
            outbound = OutboundMessage(
                session_id=session.id,
                components=[
                    MessageComponent.text_component(f"主动分享第 {index} 条内容")
                ],
                origin=MessageOrigin.PROACTIVE,
                origin_run_id=run.id,
                source_refs=[candidate.id],
            )
            run.outbound_id = outbound.id
            await runtime.store.save_proactive_run(run)
            await runtime.store.save_delivery(
                outbound,
                DeliveryReceipt(
                    outbound_id=outbound.id,
                    status=DeliveryStatus.SENT,
                    external_message_id=f"proactive-reconcile-{index}",
                    delivered_at=utc_now(),
                ),
            )
            await runtime.store.commit_outbound(outbound, "小佳")
            run_ids.append(run.id)

        # 旧实现只看最近 500 条，因此这些更早的主动证据会全部漏掉。
        for index in range(501):
            await runtime.store.append_inbound(
                InboundMessage(
                    platform=session.platform,
                    account_id=session.account_id,
                    external_message_id=f"reconciliation-filler-{index:03d}",
                    external_chat_id=session.external_chat_id,
                    sender_id="user-reconcile",
                    sender_name="小明",
                    chat_type=session.chat_type,
                    components=[MessageComponent.text_component(f"普通消息 {index}")],
                )
            )

        await runtime.memory.remember(
            session=session,
            content="这条等价内容由 reactive 链先合并，但已记录主动来源引用。",
            kind=MemoryKind.EVENT,
            source_chain=MemorySourceChain.REACTIVE,
            source_run_id="reactive-turn-before-reconciliation",
            source_refs=[f"proactive_run:{run_ids[0]}"],
        )
        await runtime.engagement._reconcile_sent_proactive_memories()
        memories = await runtime.store.list_memories(session_id=session.id)
        recorded_run_ids = {
            item.source_run_id
            for item in memories
            if item.source_chain == MemorySourceChain.PROACTIVE
            and item.source_run_id
        }
        recorded_run_ids.update(
            source_ref.removeprefix("proactive_run:")
            for item in memories
            for source_ref in item.source_refs
            if source_ref.startswith("proactive_run:")
        )
        assert recorded_run_ids == set(run_ids)
        reinforcement = {item.id: item.reinforcement for item in memories}

        await runtime.engagement._reconcile_sent_proactive_memories()
        repeated = await runtime.store.list_memories(session_id=session.id)
        assert {item.id: item.reinforcement for item in repeated} == reinforcement

        await runtime.memory.clear(session_id=session.id)
        assert (
            await runtime.store.page_sent_proactive_runs_for_memory_reconciliation(
                session.id,
                limit=2,
            )
            == []
        )
        await runtime.engagement._reconcile_sent_proactive_memories()
        assert await runtime.store.list_memories(session_id=session.id) == []
    finally:
        await runtime.stop()


async def test_proactive_memory_reconciliation_skips_group_policy(settings) -> None:
    """群聊可以持久化关闭状态的 engagement policy，对账时应直接跳过。"""

    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.GROUP,
            display_name="主动对账群聊",
            external_chat_id="proactive-reconciliation-group",
            participants=[
                Participant(external_user_id="group-user", display_name="小明")
            ],
        )
        policy = await runtime.engagement.get_policy(session.id)
        assert policy.proactive_enabled is False

        await runtime.engagement._reconcile_sent_proactive_memories()

        assert await runtime.store.list_memories(session_id=session.id) == []
    finally:
        await runtime.stop()
