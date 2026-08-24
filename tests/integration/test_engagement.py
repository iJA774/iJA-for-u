import asyncio
from datetime import timedelta

import pytest

from adapters.model import FakeModelProvider
from bootstrap import build_runtime
from domain.errors import InputValidationError
from domain.models import (
    CandidateSourceKind,
    ChatType,
    DeliveryReceipt,
    DeliveryStatus,
    DriftRun,
    DriftRunStatus,
    DriftStage,
    EngagementPolicy,
    FeedSource,
    InboundMessage,
    MemorySourceChain,
    MemoryStatus,
    MessageComponent,
    MessageOrigin,
    OutboundMessage,
    Participant,
    ProactiveCandidate,
    ProactiveCandidateStatus,
    ProactiveRunStatus,
    utc_now,
)
from ports import ModelRequest, ModelResult
from proactive.rss import FeedFetchResult, FeedItem


class FailFirstDriftActivityModel(FakeModelProvider):
    """首次 Drift 活动失败，用于验证选择 checkpoint 不会被重复执行。"""

    def __init__(self) -> None:
        super().__init__()
        self.select_calls = 0
        self.activity_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResult:
        system_text = "\n".join(
            message.content or ""
            for message in request.messages
            if message.role == "system"
        )
        if "# Drift 活动选择任务" in system_text:
            self.select_calls += 1
        if "# Drift 原子活动任务" in system_text:
            self.activity_calls += 1
            if self.activity_calls == 1:
                raise RuntimeError("模拟活动阶段瞬时失败")
        return await super().complete(request)


class BlockingDriftActivityModel(FakeModelProvider):
    """把 Drift 活动停在模型调用中，暴露快照复核竞态。"""

    def __init__(self) -> None:
        super().__init__()
        self.activity_entered = asyncio.Event()
        self.activity_release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResult:
        system_text = "\n".join(
            message.content or ""
            for message in request.messages
            if message.role == "system"
        )
        if "# Drift 原子活动任务" in system_text:
            self.activity_entered.set()
            await self.activity_release.wait()
        return await super().complete(request)


class FailingDriftSelectionModel(FakeModelProvider):
    """始终无法完成活动选择，用于验证选择阶段同样受续接上限约束。"""

    async def complete(self, request: ModelRequest) -> ModelResult:
        system_text = "\n".join(
            message.content or ""
            for message in request.messages
            if message.role == "system"
        )
        if "# Drift 活动选择任务" in system_text:
            raise RuntimeError("模拟选择阶段持续失败")
        return await super().complete(request)


class BlockingProactiveComposeModel(FakeModelProvider):
    """暂停主动文案生成，用于验证发送前会重新读取权威策略。"""

    def __init__(self) -> None:
        super().__init__()
        self.compose_entered = asyncio.Event()
        self.compose_release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResult:
        system_text = "\n".join(
            message.content or ""
            for message in request.messages
            if message.role == "system"
        )
        if "# 主动消息生成任务" in system_text:
            self.compose_entered.set()
            await self.compose_release.wait()
        return await super().complete(request)


class FailedChannel:
    """返回明确失败回执，用于验证候选重试预算。"""

    async def send(self, session, message):
        return DeliveryReceipt(
            outbound_id=message.id,
            status=DeliveryStatus.FAILED,
            error_code="simulated_failure",
            error_message="模拟投递失败",
        )


class DuplicateContentSource:
    """让两个 Feed 返回同一文章的不同跟踪链接。"""

    async def fetch(self, source: FeedSource) -> FeedFetchResult:
        marker = "one" if source.title == "源一" else "two"
        return FeedFetchResult(
            title=source.title,
            items=[
                FeedItem(
                    source_key=f"provider-{marker}",
                    title="同一篇文章",
                    summary="两个订阅源转载了同一内容。",
                    url=(
                        "https://EXAMPLE.com/article?"
                        f"utm_source={marker}&ref=homepage"
                    ),
                    published_at=None,
                )
            ],
            etag=None,
            last_modified=None,
        )

    async def close(self) -> None:
        return None


async def create_private(runtime, suffix: str = "engagement"):
    return await runtime.chat.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="主动私聊",
        external_chat_id=suffix,
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )


def proactive_candidate(session_id: str, suffix: str) -> ProactiveCandidate:
    """创建不会被后台调度器抢先消费、但可由 run-now 处理的候选。"""

    future = utc_now() + timedelta(hours=1)
    return ProactiveCandidate(
        session_id=session_id,
        source_kind=CandidateSourceKind.RSS,
        source_id=f"feed_{suffix}",
        source_key=f"entry-{suffix}",
        title=f"主动候选 {suffix}",
        summary="用于验证主动链路状态边界。",
        url=f"https://example.com/{suffix}",
        source_refs=[f"feed:feed_{suffix}:entry-{suffix}"],
        status=ProactiveCandidateStatus.DEFERRED,
        available_at=future,
        next_attempt_at=future,
        expires_at=utc_now() + timedelta(days=30),
    )


@pytest.mark.asyncio
async def test_private_default_policy_and_proactive_delivery(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await create_private(runtime)
        policy = await runtime.engagement.get_policy(session.id)
        assert policy.proactive_enabled is True
        assert policy.drift_enabled is True

        candidate = ProactiveCandidate(
            session_id=session.id,
            source_kind=CandidateSourceKind.RSS,
            source_id="feed_test",
            source_key="entry-1",
            title="一条值得关注的更新",
            summary="这是经过裁剪的摘要。",
            url="https://example.com/1",
            source_refs=["feed:feed_test:entry-1"],
            status=ProactiveCandidateStatus.DEFERRED,
            available_at=utc_now() + timedelta(minutes=30),
            next_attempt_at=utc_now() + timedelta(minutes=30),
            expires_at=utc_now() + timedelta(days=30),
        )
        await runtime.store.create_proactive_candidate(candidate)
        run = await runtime.engagement.run_proactive(session.id, force=True)

        assert run.status == ProactiveRunStatus.SENT
        saved = await runtime.store.get_proactive_candidate(candidate.id)
        assert saved is not None
        assert saved.status == ProactiveCandidateStatus.SENT
        messages = await runtime.store.list_messages(session.id)
        assert len(messages) == 1
        assert messages[0].origin == MessageOrigin.PROACTIVE
        assert candidate.id in messages[0].source_refs
        memories = await runtime.store.list_memories(
            session_id=session.id, statuses={MemoryStatus.ACTIVE}
        )
        assert any(
            item.source_chain == MemorySourceChain.PROACTIVE
            and candidate.id in item.source_refs
            for item in memories
        )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_proactive_egress_failure_blocks_original_message(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await create_private(runtime, "proactive-egress-failure")
        candidate = proactive_candidate(session.id, "egress-failure")
        await runtime.store.create_proactive_candidate(candidate)

        async def broken_filter(_):
            raise RuntimeError("模拟主动消息过滤器故障")

        runtime.engagement.set_egress_filter(broken_filter)
        run = await runtime.engagement.run_proactive(session.id, force=True)

        assert run.status == ProactiveRunStatus.FAILED
        assert run.error_message == "模拟主动消息过滤器故障"
        assert await runtime.store.list_messages(session.id) == []
        saved = await runtime.store.get_proactive_candidate(candidate.id)
        assert saved is not None
        assert saved.status == ProactiveCandidateStatus.DEFERRED
        assert saved.attempt_count == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_typed_sources_are_deduplicated_and_alerts_get_priority(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await create_private(runtime, "typed-sources")
        context_candidate, created = await runtime.engagement.submit_external_candidate(
            session.id,
            source_kind=CandidateSourceKind.CONTEXT,
            source_key="context-1",
            title="背景线索",
            summary="只用于补充判断。",
            url="",
            source_ref="context:calendar:1",
        )
        assert created is True
        duplicate, created = await runtime.engagement.submit_external_candidate(
            session.id,
            source_kind=CandidateSourceKind.CONTEXT,
            source_key="context-1",
            title="背景线索",
            summary="只用于补充判断。",
            url="",
            source_ref="context:calendar:1",
        )
        assert created is False
        assert duplicate.id == context_candidate.id
        alert, _ = await runtime.engagement.submit_external_candidate(
            session.id,
            source_kind=CandidateSourceKind.ALERT,
            source_key="alert-1",
            title="重要提醒",
            summary="需要优先判断。",
            url="",
            source_ref="alert:local:1",
        )
        eligible = await runtime.engagement._eligible_candidates(session.id, utc_now())
        assert eligible[0].id == alert.id
        assert {item.source_kind for item in eligible} == {
            CandidateSourceKind.ALERT,
            CandidateSourceKind.CONTEXT,
        }
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_presence_prefers_user_turn_then_alert_wake(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        engaged_session = await create_private(runtime, "presence-engaged")
        await runtime.store.append_inbound(
            InboundMessage(
                platform=engaged_session.platform,
                account_id=engaged_session.account_id,
                external_message_id="presence-user-1",
                external_chat_id=engaged_session.external_chat_id,
                sender_id="u1",
                sender_name="小明",
                chat_type=ChatType.PRIVATE,
                components=[MessageComponent.text_component("我正在输入需求")],
            )
        )
        engaged = await runtime.proactive_scheduler.presence(engaged_session.id)
        assert engaged.state == "engaged"
        assert engaged.energy == 0.05

        alert_session = await create_private(runtime, "presence-alert")
        await runtime.engagement.submit_external_candidate(
            alert_session.id,
            source_kind=CandidateSourceKind.ALERT,
            source_key="wake-alert",
            title="提醒",
            summary="优先检查",
            url="",
            source_ref="alert:test:wake",
        )
        alert = await runtime.proactive_scheduler.presence(alert_session.id)
        assert alert.state == "alert"
        assert alert.energy == 1
        assert alert.next_check_seconds == 10
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_same_content_across_feeds_is_deduplicated_with_provenance(
    settings,
) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await create_private(runtime, "cross-feed-dedupe")
        runtime.engagement.rss = DuplicateContentSource()
        future = utc_now() + timedelta(hours=1)
        first = await runtime.store.create_feed_source(
            FeedSource(
                session_id=session.id,
                url="https://example.com/feed-one.xml",
                title="源一",
                next_poll_at=future,
            )
        )
        second = await runtime.store.create_feed_source(
            FeedSource(
                session_id=session.id,
                url="https://example.com/feed-two.xml",
                title="源二",
                next_poll_at=future,
            )
        )

        assert await runtime.engagement.poll_feed(first) == 1
        assert await runtime.engagement.poll_feed(second) == 0

        candidates = await runtime.store.list_proactive_candidates(session.id)
        assert len(candidates) == 1
        assert candidates[0].source_refs == sorted(
            [
                f"feed:{first.id}:provider-one",
                f"feed:{second.id}:provider-two",
            ]
        )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_group_cannot_enable_background_chains(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.GROUP,
            display_name="群聊",
            external_chat_id="engagement-group",
            participants=[Participant(external_user_id="u1", display_name="小明")],
        )
        with pytest.raises(InputValidationError, match="群聊"):
            await runtime.engagement.update_policy(
                session.id,
                proactive_enabled=True,
                drift_enabled=False,
                timezone="Asia/Shanghai",
                quiet_start="22:00",
                quiet_end="08:00",
                minimum_interval_minutes=240,
            )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_proactive_rechecks_policy_after_model_generation(settings) -> None:
    model = BlockingProactiveComposeModel()
    runtime = build_runtime(settings, model_override=model)
    await runtime.start()
    try:
        session = await create_private(runtime, "proactive-policy-race")
        candidate = proactive_candidate(session.id, "policy-race")
        await runtime.store.create_proactive_candidate(candidate)

        task = asyncio.create_task(
            runtime.engagement.run_proactive(session.id, force=True)
        )
        await model.compose_entered.wait()
        policy = await runtime.engagement.get_policy(session.id)
        await runtime.engagement.update_policy(
            session.id,
            proactive_enabled=False,
            drift_enabled=policy.drift_enabled,
            timezone=policy.timezone,
            quiet_start=policy.quiet_start,
                quiet_end=policy.quiet_end,
                minimum_interval_minutes=policy.minimum_interval_minutes,
        )
        model.compose_release.set()
        run = await task

        assert run.status == ProactiveRunStatus.GATED
        assert run.gate_reason == "disabled"
        saved = await runtime.store.get_proactive_candidate(candidate.id)
        assert saved is not None
        assert saved.status == ProactiveCandidateStatus.DEFERRED
        assert await runtime.store.list_messages(session.id) == []
        assert run.outbound_id is not None
        receipt = await runtime.store.get_delivery(run.outbound_id)
        assert receipt is not None
        assert receipt.status == DeliveryStatus.DROPPED
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_prepared_proactive_recovers_and_delivers_once_after_restart(
    settings,
) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    session = await create_private(runtime, "proactive-prepared-recovery")
    candidate = proactive_candidate(session.id, "prepared-recovery")
    await runtime.store.create_proactive_candidate(candidate)

    async def crash_before_irreversible_send(*args, **kwargs):
        raise SystemExit("模拟 PREPARED 后进程退出")

    runtime.outbound.deliver = crash_before_irreversible_send  # type: ignore[method-assign]
    try:
        with pytest.raises(SystemExit, match="PREPARED"):
            await runtime.engagement.run_proactive(session.id, force=True)
        runs = await runtime.store.list_proactive_runs(session.id)
        prepared = runs[0]
        assert prepared.status == ProactiveRunStatus.PREPARED
        assert prepared.outbound_id is not None
        receipt = await runtime.store.get_delivery(prepared.outbound_id)
        assert receipt is not None
        assert receipt.status == DeliveryStatus.PREPARED
        reserved = await runtime.store.get_proactive_candidate(candidate.id)
        assert reserved is not None
        assert reserved.status == ProactiveCandidateStatus.PREPARED
    finally:
        await runtime.stop()

    recovered_runtime = build_runtime(
        settings, model_override=FakeModelProvider()
    )
    await recovered_runtime.start()
    try:
        recovered = (
            await recovered_runtime.store.list_proactive_runs(session.id)
        )[0]
        assert recovered.status == ProactiveRunStatus.SENT
        saved = await recovered_runtime.store.get_proactive_candidate(
            candidate.id
        )
        assert saved is not None
        assert saved.status == ProactiveCandidateStatus.SENT
        messages = await recovered_runtime.store.list_messages(session.id)
        assert len(
            [
                item
                for item in messages
                if item.origin_run_id == recovered.id
            ]
        ) == 1
    finally:
        await recovered_runtime.stop()


@pytest.mark.asyncio
async def test_sent_proactive_reconciles_memory_after_restart(settings) -> None:
    """发送权威提交后即使进程退出，重启也应幂等补齐派生记忆。"""

    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    session = await create_private(runtime, "proactive-memory-recovery")
    candidate = proactive_candidate(session.id, "memory-recovery")
    await runtime.store.create_proactive_candidate(candidate)

    async def crash_before_memory(*args, **kwargs):
        raise SystemExit("模拟发送提交后、记忆写入前进程退出")

    runtime.memory.try_record_chain_outcome = crash_before_memory  # type: ignore[method-assign]
    try:
        with pytest.raises(SystemExit, match="记忆写入前"):
            await runtime.engagement.run_proactive(session.id, force=True)
        sent = (await runtime.store.list_proactive_runs(session.id))[0]
        assert sent.status == ProactiveRunStatus.SENT
        assert await runtime.store.list_memories(session_id=session.id) == []
    finally:
        await runtime.stop()

    recovered_runtime = build_runtime(
        settings, model_override=FakeModelProvider()
    )
    await recovered_runtime.start()
    try:
        memories = await recovered_runtime.store.list_memories(
            session_id=session.id
        )
        assert len(memories) == 1
        assert memories[0].source_chain == MemorySourceChain.PROACTIVE
        assert memories[0].source_run_id == sent.id
        assert len(memories[0].source_message_ids) == 1
        reinforcement = memories[0].reinforcement
    finally:
        await recovered_runtime.stop()

    second_restart = build_runtime(
        settings, model_override=FakeModelProvider()
    )
    await second_restart.start()
    try:
        memories = await second_restart.store.list_memories(
            session_id=session.id
        )
        assert len(memories) == 1
        assert memories[0].reinforcement == reinforcement
    finally:
        await second_restart.stop()


@pytest.mark.asyncio
async def test_prepared_proactive_is_dropped_if_user_arrives_before_restart(
    settings,
) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    session = await create_private(runtime, "proactive-stale-recovery")
    candidate = proactive_candidate(session.id, "stale-recovery")
    await runtime.store.create_proactive_candidate(candidate)

    async def crash_before_irreversible_send(*args, **kwargs):
        raise SystemExit("模拟 PREPARED 后进程退出")

    runtime.outbound.deliver = crash_before_irreversible_send  # type: ignore[method-assign]
    try:
        with pytest.raises(SystemExit, match="PREPARED"):
            await runtime.engagement.run_proactive(session.id, force=True)
        prepared = (
            await runtime.store.list_proactive_runs(session.id)
        )[0]
        await runtime.chat.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="proactive-recovery-new-user",
                external_chat_id=session.external_chat_id,
                sender_id="u1",
                sender_name="小明",
                chat_type=ChatType.PRIVATE,
                components=[MessageComponent.text_component("先回复我的新消息")],
            ),
            schedule_turn=False,
        )
    finally:
        await runtime.stop()

    recovered_runtime = build_runtime(
        settings, model_override=FakeModelProvider()
    )
    await recovered_runtime.start()
    try:
        recovered = (
            await recovered_runtime.store.list_proactive_runs(session.id)
        )[0]
        assert recovered.id == prepared.id
        assert recovered.status == ProactiveRunStatus.GATED
        assert recovered.gate_reason == "new_user_message"
        saved = await recovered_runtime.store.get_proactive_candidate(
            candidate.id
        )
        assert saved is not None
        assert saved.status == ProactiveCandidateStatus.DEFERRED
        assert recovered.outbound_id is not None
        receipt = await recovered_runtime.store.get_delivery(
            recovered.outbound_id
        )
        assert receipt is not None
        assert receipt.status == DeliveryStatus.DROPPED
        messages = await recovered_runtime.store.list_messages(session.id)
        assert messages[0].plain_text == "先回复我的新消息"
        assert any(
            item.origin == MessageOrigin.REACTIVE for item in messages[1:]
        )
        assert all(item.origin != MessageOrigin.PROACTIVE for item in messages)
    finally:
        await recovered_runtime.stop()


@pytest.mark.asyncio
async def test_proactive_delivery_failures_exhaust_candidate_budget(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await create_private(runtime, "proactive-delivery-budget")
        runtime.outbound.channel = FailedChannel()
        candidate = proactive_candidate(session.id, "delivery-budget")
        await runtime.store.create_proactive_candidate(candidate)

        runs = [
            await runtime.engagement.run_proactive(session.id, force=True)
            for _ in range(3)
        ]

        assert all(run.status == ProactiveRunStatus.FAILED for run in runs)
        saved = await runtime.store.get_proactive_candidate(candidate.id)
        assert saved is not None
        assert saved.attempt_count == 3
        assert saved.status == ProactiveCandidateStatus.FAILED
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_drift_only_creates_candidate_and_never_sends(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        # 本用例验证显式 Drift 调用；后台调度器会与手动调用竞争同一候选池，
        # 慢速 runner 上可能先聚合父候选，使被测调用合法返回 idle。
        await runtime.drift_scheduler.stop()
        session = await create_private(runtime, "drift")
        await runtime.store.save_engagement_policy(
            EngagementPolicy(
                session_id=session.id,
                proactive_enabled=True,
                drift_enabled=True,
            )
        )
        for index in range(2):
            await runtime.store.create_proactive_candidate(
                ProactiveCandidate(
                    session_id=session.id,
                    source_kind=CandidateSourceKind.RSS,
                    source_id="feed_test",
                    source_key=f"skipped-{index}",
                    title=f"条目 {index}",
                    summary="相关内容",
                    source_refs=[f"feed:feed_test:{index}"],
                    status=ProactiveCandidateStatus.SKIPPED,
                    expires_at=utc_now() + timedelta(days=30),
                )
            )

        run = await runtime.engagement.run_drift(session.id, force=True)

        assert run.status.value == "completed"
        assert run.activity == "candidate_aggregation"
        assert run.produced_candidate_id
        produced = await runtime.store.get_proactive_candidate(
            run.produced_candidate_id
        )
        assert produced is not None
        assert produced.source_kind == CandidateSourceKind.DRIFT_AGGREGATION
        assert produced.status == ProactiveCandidateStatus.PENDING
        assert await runtime.store.list_messages(session.id) == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_dispatching_recovery_becomes_unknown_without_resend(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await create_private(runtime, "dispatching")
        outbound = OutboundMessage(
            session_id=session.id,
            components=[MessageComponent.text_component("不能重复发送")],
            origin=MessageOrigin.PROACTIVE,
            origin_run_id="proactive_run_test",
        )
        await runtime.store.save_delivery(
            outbound,
            DeliveryReceipt(
                outbound_id=outbound.id,
                status=DeliveryStatus.DISPATCHING,
            ),
        )

        receipt, committed = await runtime.outbound.deliver(session, outbound)

        assert receipt.status == DeliveryStatus.UNKNOWN
        assert committed is None
        assert await runtime.store.list_messages(session.id) == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_drift_stops_automatic_resume_after_bounded_failures(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await create_private(runtime, "drift-resume-limit")
        await runtime.store.save_engagement_policy(
            EngagementPolicy(
                session_id=session.id,
                proactive_enabled=True,
                drift_enabled=True,
            )
        )
        await runtime.store.save_drift_run(
            DriftRun(
                session_id=session.id,
                status=DriftRunStatus.PAUSED,
                activity="conversation_topic_preparation",
                resume_payload={
                    "activity": "conversation_topic_preparation",
                    "stage": "activity_execution",
                },
                auto_resume_count=settings.drift.max_auto_resumes,
            )
        )

        with pytest.raises(InputValidationError, match="自动续接上限"):
            await runtime.engagement.run_drift(session.id)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_drift_selection_failures_share_bounded_resume_budget(settings) -> None:
    runtime = build_runtime(
        settings, model_override=FailingDriftSelectionModel()
    )
    await runtime.start()
    try:
        session = await create_private(runtime, "drift-selection-limit")
        await runtime.store.save_engagement_policy(
            EngagementPolicy(
                session_id=session.id,
                proactive_enabled=True,
                drift_enabled=True,
            )
        )

        runs = [
            await runtime.engagement.run_drift(session.id, force=True)
            for _ in range(settings.drift.max_auto_resumes)
        ]

        assert [run.auto_resume_count for run in runs] == [1, 2, 3]
        assert all(run.stage == DriftStage.SELECTING for run in runs)
        assert runs[1].resumed_from_run_id == runs[0].id
        assert runs[2].resumed_from_run_id == runs[1].id
        with pytest.raises(InputValidationError, match="自动续接上限"):
            await runtime.engagement.run_drift(session.id)
        manual = await runtime.engagement.run_drift(session.id, force=True)
        assert manual.auto_resume_count == 1
        assert manual.resumed_from_run_id == runs[2].id
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_recover_drift_run_preserves_last_checkpoint_once(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await create_private(runtime, "drift-recover")
        running = DriftRun(
            session_id=session.id,
            status=DriftRunStatus.RUNNING,
            stage=DriftStage.EXECUTING,
            activity="conversation_topic_preparation",
        )
        await runtime.store.save_drift_run(running)

        await runtime.engagement.recover_drift_runs()
        await runtime.engagement.recover_drift_runs()

        recovered = await runtime.store.latest_drift_run(session.id)
        assert recovered is not None
        assert recovered.status == DriftRunStatus.PAUSED
        assert recovered.stage == DriftStage.EXECUTING
        assert recovered.auto_resume_count == 1
        assert recovered.error_code == "drift_interrupted"
        assert recovered.resume_payload == {
            "activity": "conversation_topic_preparation",
            "stage": "activity_execution",
            "next": "重新读取当前域证据，只执行已选原子活动并重新校验",
        }
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_manual_drift_never_bypasses_pending_user_message(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await create_private(runtime, "drift-pending")
        await runtime.store.save_engagement_policy(
            EngagementPolicy(
                session_id=session.id,
                proactive_enabled=True,
                drift_enabled=True,
            )
        )
        await runtime.chat.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="drift-pending-message",
                external_chat_id=session.external_chat_id,
                sender_id="u1",
                sender_name="小明",
                chat_type=ChatType.PRIVATE,
                components=[MessageComponent.text_component("先处理我这条消息")],
            ),
            schedule_turn=False,
        )

        with pytest.raises(InputValidationError, match="待处理用户消息"):
            await runtime.engagement.run_drift(session.id, force=True)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_drift_resumes_activity_checkpoint_without_reselecting(settings) -> None:
    model = FailFirstDriftActivityModel()
    runtime = build_runtime(settings, model_override=model)
    await runtime.start()
    try:
        session = await create_private(runtime, "drift-checkpoint")
        await runtime.store.save_engagement_policy(
            EngagementPolicy(
                session_id=session.id,
                proactive_enabled=True,
                drift_enabled=True,
            )
        )
        for index in range(2):
            await runtime.store.create_proactive_candidate(
                ProactiveCandidate(
                    session_id=session.id,
                    source_kind=CandidateSourceKind.RSS,
                    source_id="feed_checkpoint",
                    source_key=f"checkpoint-{index}",
                    title=f"断点条目 {index}",
                    summary="相关内容",
                    source_refs=[f"feed:feed_checkpoint:{index}"],
                    status=ProactiveCandidateStatus.SKIPPED,
                    expires_at=utc_now() + timedelta(days=30),
                )
            )

        first = await runtime.engagement.run_drift(session.id, force=True)
        second = await runtime.engagement.run_drift(session.id, force=True)

        assert first.status == DriftRunStatus.PAUSED
        assert first.resume_payload["stage"] == "activity_execution"
        assert second.status == DriftRunStatus.COMPLETED
        assert second.resumed_from_run_id == first.id
        assert model.select_calls == 1
        assert model.activity_calls == 2
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_drift_discards_stale_result_when_user_message_arrives(settings) -> None:
    model = BlockingDriftActivityModel()
    runtime = build_runtime(settings, model_override=model)
    await runtime.start()
    try:
        session = await create_private(runtime, "drift-snapshot")
        await runtime.store.save_engagement_policy(
            EngagementPolicy(
                session_id=session.id,
                proactive_enabled=True,
                drift_enabled=True,
            )
        )
        for index in range(2):
            await runtime.store.create_proactive_candidate(
                ProactiveCandidate(
                    session_id=session.id,
                    source_kind=CandidateSourceKind.RSS,
                    source_id="feed_snapshot",
                    source_key=f"snapshot-{index}",
                    title=f"快照条目 {index}",
                    summary="相关内容",
                    source_refs=[f"feed:feed_snapshot:{index}"],
                    status=ProactiveCandidateStatus.SKIPPED,
                    expires_at=utc_now() + timedelta(days=30),
                )
            )

        drift_task = asyncio.create_task(
            runtime.engagement.run_drift(session.id, force=True)
        )
        await model.activity_entered.wait()
        await runtime.chat.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="drift-snapshot-message",
                external_chat_id=session.external_chat_id,
                sender_id="u1",
                sender_name="小明",
                chat_type=ChatType.PRIVATE,
                components=[MessageComponent.text_component("这里有一条新消息")],
            ),
            schedule_turn=False,
        )
        model.activity_release.set()
        run = await drift_task

        assert run.status.value == "gated"
        assert run.error_code == "snapshot_stale"
        assert run.produced_candidate_id is None
        candidates = await runtime.store.list_proactive_candidates(session.id)
        assert all(
            item.source_kind == CandidateSourceKind.RSS for item in candidates
        )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_drift_does_not_reuse_aggregated_parent_candidates(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await create_private(runtime, "drift-idempotent")
        await runtime.store.save_engagement_policy(
            EngagementPolicy(
                session_id=session.id,
                proactive_enabled=True,
                drift_enabled=True,
            )
        )
        for index in range(2):
            await runtime.store.create_proactive_candidate(
                ProactiveCandidate(
                    session_id=session.id,
                    source_kind=CandidateSourceKind.RSS,
                    source_id="feed_idempotent",
                    source_key=f"idempotent-{index}",
                    title=f"幂等条目 {index}",
                    summary="相关内容",
                    source_refs=[f"feed:feed_idempotent:{index}"],
                    status=ProactiveCandidateStatus.SKIPPED,
                    expires_at=utc_now() + timedelta(days=30),
                )
            )

        first = await runtime.engagement.run_drift(session.id, force=True)
        second = await runtime.engagement.run_drift(session.id, force=True)

        assert first.status == DriftRunStatus.COMPLETED
        assert second.status == DriftRunStatus.COMPLETED
        assert second.activity == "idle"
        assert second.produced_candidate_id is None
        candidates = await runtime.store.list_proactive_candidates(session.id)
        assert len(
            [
                item
                for item in candidates
                if item.source_kind == CandidateSourceKind.DRIFT_AGGREGATION
            ]
        ) == 1
    finally:
        await runtime.stop()
