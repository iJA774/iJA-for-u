from datetime import timedelta
from typing import cast

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.model import FakeModelProvider
from adapters.persistence import DatabaseStore
from application.events import EventHub
from application.social_learning import SocialLearningService
from config.settings import EmbeddingSettings
from domain.models import (
    BehaviorPattern,
    BehaviorScenarioProfile,
    BehaviorTagGroup,
    BehaviorTagKind,
    ChatType,
    DecisionAction,
    DeliveryReceipt,
    DeliveryStatus,
    GroupExpressionPattern,
    InboundMessage,
    JargonTerm,
    LearnedItemStatus,
    MessageComponent,
    OutboundMessage,
    Participant,
    SocialLearningRun,
    TurnDecision,
    utc_now,
)
from ports import ModelRequest, ModelResult
from prompting import PromptAssembler


class SocialSemanticEmbeddingProvider:
    """用稳定三维语义簇覆盖表达 profile、K-means 和 MMR。"""

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        vectors = []
        for text in texts:
            if any(
                marker in text
                for marker in ("技术", "报错", "配置", "接口", "排查")
            ):
                vectors.append([1.0, 0.05, 0.0])
            elif any(
                marker in text
                for marker in ("吐槽", "接梗", "玩笑", "离谱", "惊叹")
            ):
                vectors.append([0.05, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.05, 1.0])
        return vectors

    async def close(self) -> None:
        return None


class FailingSocialEmbeddingProvider:
    """模拟表达向量端点不可用。"""

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[list[float]]:
        del texts
        raise RuntimeError("embedding unavailable")

    async def close(self) -> None:
        return None


class InvalidJargonLearningModel(FakeModelProvider):
    """黑话任务返回坏 JSON，用于验证批次明确失败。"""

    async def complete(self, request: ModelRequest) -> ModelResult:
        system_text = "\n".join(
            item.content or ""
            for item in request.messages
            if item.role == "system"
        )
        if "# 黑话候选提取任务" in system_text:
            return ModelResult(content="{")
        return await super().complete(request)


async def _append_user(
    store: DatabaseStore,
    *,
    external_message_id: str,
    external_chat_id: str,
    text: str,
):
    message, created = await store.append_inbound(
        InboundMessage(
            platform="web-simulator",
            account_id="ija-local",
            external_message_id=external_message_id,
            external_chat_id=external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=ChatType.GROUP,
            components=[MessageComponent.text_component(text)],
        )
    )
    assert created
    return message


async def _append_private_user(
    store: DatabaseStore,
    *,
    external_message_id: str,
    external_chat_id: str,
    text: str,
):
    message, created = await store.append_inbound(
        InboundMessage(
            platform="web-simulator",
            account_id="ija-local",
            external_message_id=external_message_id,
            external_chat_id=external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=ChatType.PRIVATE,
            components=[MessageComponent.text_component(text)],
        )
    )
    assert created
    return message


async def _commit_assistant(
    store: DatabaseStore,
    *,
    session_id: str,
    external_message_id: str,
    text: str,
    turn_id: str,
):
    if await store.get_decision(turn_id) is None:
        await store.save_decision(
            TurnDecision(
                id=turn_id,
                session_id=session_id,
                action=DecisionAction.REPLY,
                strategy="social-learning-fixture",
                score=100,
                threshold=50,
                reason="测试夹具中的群响应式回复",
            ),
            [],
        )
    outbound = OutboundMessage(
        session_id=session_id,
        components=[MessageComponent.text_component(text)],
        origin_run_id=turn_id,
    )
    await store.save_delivery(
        outbound,
        DeliveryReceipt(
            outbound_id=outbound.id,
            status=DeliveryStatus.SENT,
            external_message_id=external_message_id,
            delivered_at=utc_now(),
        ),
    )
    return await store.commit_outbound(outbound, "小佳")


async def test_social_learning_builds_three_libraries_and_feedback_loop(settings) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="learning-group",
        chat_type=ChatType.GROUP,
        display_name="学习群",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    first = await _append_user(
        store,
        external_message_id="learn-u1",
        external_chat_id=session.external_chat_id,
        text="我们说 yyds 就是永远的神",
    )
    assistant = await _commit_assistant(
        store,
        session_id=session.id,
        external_message_id="learn-a1",
        text="你是说这个项目很厉害吗，还想确认哪个部分？",
        turn_id="turn-old",
    )
    await _append_user(
        store,
        external_message_id="learn-u2",
        external_chat_id=session.external_chat_id,
        text="对，这个项目就是 yyds",
    )
    await _append_user(
        store,
        external_message_id="learn-u3",
        external_chat_id=session.external_chat_id,
        text="我嘞个，这个配置也太离谱了",
    )
    await _append_user(
        store,
        external_message_id="learn-u4",
        external_chat_id=session.external_chat_id,
        text="我嘞个，接口又报错了，怎么排查？",
    )

    model = FakeModelProvider()
    service = SocialLearningService(
        settings=settings,
        store=store,
        model=model,
        prompting=PromptAssembler(settings.project_root / "prompts"),
        events=EventHub(),
    )
    run = await service.maybe_enqueue(session=session, source_run_id="turn-learn")
    assert run is not None
    await service.stop()

    jargons = await store.list_jargons(session.id)
    jargon = next(item for item in jargons if item.term.casefold() == "yyds")
    assert jargon.status == LearnedItemStatus.ACTIVE
    assert "永远的神" in jargon.meaning
    assert set(jargon.evidence_message_ids) == {first.id} | {
        item.id
        for item in await store.list_messages(session.id)
        if "yyds" in item.plain_text.casefold() and item.id != first.id
    }

    expressions = await store.list_group_expressions(session.id)
    assert len(expressions) == 1
    assert expressions[0].situation == "表示惊叹或意外"

    behaviors = await store.list_behavior_patterns(session.id)
    assert len(behaviors) == 1
    assert behaviors[0].action == "先确认当前信息边界，再追问一个关键点"

    history = await store.list_messages(session.id)
    reply_context = await service.prepare_reply_context(
        session=session,
        messages=history,
        turn_id="turn-select",
    )
    glossary = cast(
        list[dict[str, object]], reply_context["jargon_glossary"]
    )
    assert cast(str, glossary[0]["term"]).casefold() == "yyds"
    assert reply_context["group_expression_guidance"]
    assert reply_context["behavior_guidance"]

    selected_reply = await _commit_assistant(
        store,
        session_id=session.id,
        external_message_id="learn-a2",
        text="先确认一下：报错发生在哪个接口，能补充错误码吗？",
        turn_id="turn-select",
    )
    await service.record_reply_delivery(
        turn_id="turn-select", message_id=selected_reply.id
    )
    feedback = await _append_user(
        store,
        external_message_id="learn-u5",
        external_chat_id=session.external_chat_id,
        text="可以，我补充一下错误码，现在明白怎么排查了，谢谢",
    )
    service.schedule_feedback(session)
    await service.stop()

    updated = (await store.list_behavior_patterns(session.id))[0]
    assert updated.success_count == 1
    assert updated.score == 0.7
    selections = await store.list_pending_behavior_selections(session.id)
    assert selections == []
    assert feedback.id in {
        item.id
        for item in await store.list_messages(session.id)
        if item.role.value == "user"
    }
    assert assistant.id in behaviors[0].evidence_message_ids
    cleared = await store.clear_memory_state(session.id)
    assert cast(int, cleared["jargon_count"]) >= 1
    assert cleared["group_expression_count"] == 1
    assert cleared["behavior_pattern_count"] == 1
    assert cleared["behavior_selection_count"] == 1
    assert await store.list_jargons(session.id) == []
    assert await store.list_group_expressions(session.id) == []
    assert await store.list_behavior_patterns(session.id) == []
    cleared_indexes = await store.get_social_learning_index_data(session.id)
    assert cleared_indexes == {
        "expression_clusters": [],
        "behavior_tag_aliases": [],
        "behavior_scene_clusters": [],
    }
    await store.close()


async def test_social_learning_is_session_scoped_and_deleted_with_session(settings) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="scope-group",
        chat_type=ChatType.GROUP,
        display_name="隔离群",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    other = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="other-group",
        chat_type=ChatType.GROUP,
        display_name="另一个群",
        participants=[Participant(external_user_id="u2", display_name="小红")],
    )
    for index, text in enumerate(
        [
            "yyds 就是永远的神",
            "这个也是 yyds",
            "我嘞个太离谱",
            "我嘞个又来了",
        ]
    ):
        await _append_user(
            store,
            external_message_id=f"scope-{index}",
            external_chat_id=session.external_chat_id,
            text=text,
        )
    service = SocialLearningService(
        settings=settings,
        store=store,
        model=FakeModelProvider(),
        prompting=PromptAssembler(settings.project_root / "prompts"),
        events=EventHub(),
    )
    await service.maybe_enqueue(session=session, source_run_id="scope-turn")
    await service.stop()

    assert await store.list_jargons(session.id)
    assert await store.list_jargons(other.id) == []
    await store.delete_session(session.id)
    assert await store.list_jargons(session.id) == []
    assert await store.list_social_learning_runs(session.id) == []
    await store.close()


async def test_social_learning_invalid_model_output_fails_loudly(settings) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="invalid-learning",
        chat_type=ChatType.GROUP,
        display_name="失败群",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    for index in range(4):
        await _append_user(
            store,
            external_message_id=f"invalid-{index}",
            external_chat_id=session.external_chat_id,
            text=f"第 {index + 1} 条学习消息",
        )
    service = SocialLearningService(
        settings=settings,
        store=store,
        model=InvalidJargonLearningModel(),
        prompting=PromptAssembler(settings.project_root / "prompts"),
        events=EventHub(),
    )
    await service.maybe_enqueue(session=session, source_run_id="invalid-turn")
    await service.stop()

    runs = await store.list_social_learning_runs(session.id)
    assert len(runs) == 1
    assert runs[0].status.value == "failed"
    assert runs[0].error_code == "invalid_model_response"
    assert await store.list_jargons(session.id) == []

    service.set_model(FakeModelProvider())
    retried = await service.retry_run(runs[0].id)
    assert retried.status.value == "pending"
    await service.stop()
    completed = await store.get_social_learning_run(runs[0].id)
    assert completed is not None
    assert completed.status.value == "completed"
    assert completed.attempt_count == 2
    await store.close()


async def test_social_learning_late_lane_failure_leaves_no_partial_library(
    settings,
    monkeypatch,
) -> None:
    """事务内最后行为车道失败时，已经 flush 的前两条 lane 必须回滚。"""

    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="atomic-learning",
        chat_type=ChatType.GROUP,
        display_name="原子学习群",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    await _append_user(
        store,
        external_message_id="atomic-u1",
        external_chat_id=session.external_chat_id,
        text="我们说 yyds 就是永远的神",
    )
    await _commit_assistant(
        store,
        session_id=session.id,
        external_message_id="atomic-a1",
        text="你是说这个项目很厉害吗？",
        turn_id="atomic-old",
    )
    for index, text in enumerate(
        (
            "这个项目真是 yyds",
            "我嘞个，这个结果也太离谱了",
            "我嘞个，接口又报错了，怎么排查？",
        ),
        start=2,
    ):
        await _append_user(
            store,
            external_message_id=f"atomic-u{index}",
            external_chat_id=session.external_chat_id,
            text=text,
        )

    observed_in_transaction: dict[str, int] = {}

    async def fail_behavior_after_prior_lanes(
        self: DatabaseStore,
        db: AsyncSession,
        pattern: BehaviorPattern,
        *,
        scene_cluster_reuse_threshold: float,
    ) -> BehaviorPattern:
        del self, pattern, scene_cluster_reuse_threshold
        observed_in_transaction["jargons"] = (
            await db.execute(sql_text("SELECT COUNT(*) FROM jargon_terms"))
        ).scalar_one()
        observed_in_transaction["expressions"] = (
            await db.execute(
                sql_text("SELECT COUNT(*) FROM group_expression_patterns")
            )
        ).scalar_one()
        raise RuntimeError("注入行为车道事务内晚失败")

    monkeypatch.setattr(
        DatabaseStore,
        "_save_behavior_pattern_in_transaction",
        fail_behavior_after_prior_lanes,
    )
    service = SocialLearningService(
        settings=settings,
        store=store,
        model=FakeModelProvider(),
        prompting=PromptAssembler(settings.project_root / "prompts"),
        events=EventHub(),
    )
    run = await service.maybe_enqueue(
        session=session,
        source_run_id="atomic-turn",
    )
    assert run is not None
    await service.stop()

    persisted = await store.get_social_learning_run(run.id)
    assert persisted is not None
    assert persisted.status.value == "failed"
    assert persisted.error_code == "social_learning_failed"
    assert observed_in_transaction["jargons"] > 0
    assert observed_in_transaction["expressions"] > 0
    assert await store.list_jargons(session.id) == []
    assert await store.list_group_expressions(session.id) == []
    assert await store.list_behavior_patterns(session.id) == []
    await store.close()


async def test_private_session_learns_expression_and_reuses_stable_jargon(
    settings,
) -> None:
    """私聊也应学习表达方式，并只复用达到高证据门槛的黑话。"""

    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="private-style",
        chat_type=ChatType.PRIVATE,
        display_name="私聊表达学习",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    for index, text in enumerate(
        (
            "我嘞个，yyds 就是永远的神",
            "我嘞个，这次也必须说 yyds",
            "我嘞个，这个版本还是 yyds",
            "我嘞个，修好以后真是 yyds",
        ),
        start=1,
    ):
        await _append_private_user(
            store,
            external_message_id=f"private-style-{index}",
            external_chat_id=session.external_chat_id,
            text=text,
        )

    service = SocialLearningService(
        settings=settings,
        store=store,
        model=FakeModelProvider(),
        prompting=PromptAssembler(settings.project_root / "prompts"),
        events=EventHub(),
    )
    run = await service.maybe_enqueue(
        session=session,
        source_run_id="private-style-turn",
    )
    assert run is not None
    await service.stop()

    expressions = await store.list_group_expressions(session.id)
    assert len(expressions) == 1
    assert expressions[0].occurrence_count == 4
    jargons = await store.list_jargons(session.id, active_only=True)
    jargon = next(item for item in jargons if item.normalized_term == "yyds")
    assert jargon.occurrence_count == 4

    context = await service.prepare_reply_context(
        session=session,
        messages=await store.list_messages(session.id),
        turn_id="private-style-reply",
    )
    assert context["private_expression_guidance"]
    assert "group_expression_guidance" not in context
    reusable = cast(
        list[dict[str, object]], context["jargon_style_guidance"]
    )
    assert reusable[0]["term"] == "yyds"
    assert "有限复用" in cast(str, reusable[0]["usage_rule"])
    await store.close()


async def test_social_learning_recovers_persisted_pending_run(settings) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="recover-learning",
        chat_type=ChatType.GROUP,
        display_name="恢复群",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    source_ids = []
    for index, text in enumerate(
        [
            "yyds 就是永远的神",
            "这个真的 yyds",
            "我嘞个太离谱",
            "我嘞个又来了",
        ]
    ):
        message = await _append_user(
            store,
            external_message_id=f"recover-{index}",
            external_chat_id=session.external_chat_id,
            text=text,
        )
        source_ids.append(message.id)
    pending = SocialLearningRun(
        session_id=session.id,
        source_run_id="recover-turn",
        source_message_ids=source_ids,
        source_fingerprint="d" * 64,
    )
    _, created = await store.create_social_learning_run(pending)
    assert created

    service = SocialLearningService(
        settings=settings,
        store=store,
        model=FakeModelProvider(),
        prompting=PromptAssembler(settings.project_root / "prompts"),
        events=EventHub(),
    )
    await service.start()
    await service.stop()

    recovered = (await store.list_social_learning_runs(session.id))[0]
    assert recovered.status.value == "completed"
    assert any(
        item.term.casefold() == "yyds"
        for item in await store.list_jargons(session.id)
    )
    await store.close()


async def test_social_learning_maintenance_is_persistent_and_idempotent(
    settings,
) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="maintenance-learning",
        chat_type=ChatType.GROUP,
        display_name="维护群",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    now = utc_now()
    await store.save_jargon_candidate(
        JargonTerm(
            session_id=session.id,
            term="旧梗",
            normalized_term="旧梗",
            meaning="很久以前使用的群内说法",
            status=LearnedItemStatus.ACTIVE,
            confidence=0.9,
            last_seen_at=now - timedelta(days=400),
        )
    )
    await store.save_group_expression(
        GroupExpressionPattern(
            session_id=session.id,
            situation="群友提到很久以前的话题",
            style="使用已经过时的固定句式",
            pattern_hash="a" * 64,
            confidence=0.7,
            last_reinforced_at=now - timedelta(days=190),
        )
    )
    await store.save_behavior_pattern(
        BehaviorPattern(
            session_id=session.id,
            scene_summary="对方提到一个一次性的旧话题",
            action="使用旧的固定回应",
            expected_outcome="延续当时的话题",
            pattern_hash="b" * 64,
            confidence=0.8,
            last_reinforced_at=now - timedelta(days=140),
        )
    )
    service = SocialLearningService(
        settings=settings,
        store=store,
        model=FakeModelProvider(),
        prompting=PromptAssembler(settings.project_root / "prompts"),
        events=EventHub(),
    )

    first = await service.run_maintenance(now=now, force=True)
    second = await service.run_maintenance(now=now, force=True)

    assert first["disabled_count"] == 3
    assert first["decayed_count"] == 3
    assert second["decayed_count"] == 0
    assert (await store.list_jargons(session.id))[0].status == (
        LearnedItemStatus.DISABLED
    )
    assert (await store.list_group_expressions(session.id))[0].status == (
        LearnedItemStatus.DISABLED
    )
    behavior = (await store.list_behavior_patterns(session.id))[0]
    assert behavior.status == LearnedItemStatus.DISABLED
    assert behavior.score == -3.5
    await store.close()


async def test_social_learning_maintenance_pages_every_learning_lane(
    settings,
    monkeypatch,
) -> None:
    """维护必须扫到第二页以后，不能把面向 UI 的 top-K 当完整集合。"""

    monkeypatch.setattr("application.social_learning._MAINTENANCE_PAGE_SIZE", 2)
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="maintenance-paged",
        chat_type=ChatType.GROUP,
        display_name="分页维护群",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    now = utc_now()
    for index in range(5):
        await store.save_jargon_candidate(
            JargonTerm(
                session_id=session.id,
                term=f"旧梗{index}",
                normalized_term=f"旧梗{index}",
                meaning="很久以前使用的群内说法",
                status=LearnedItemStatus.ACTIVE,
                confidence=0.9,
                last_seen_at=now - timedelta(days=400),
            )
        )
        await store.save_group_expression(
            GroupExpressionPattern(
                session_id=session.id,
                situation=f"旧话题{index}",
                style="使用已经过时的固定句式",
                pattern_hash=f"{index + 10:064x}",
                confidence=0.7,
                last_reinforced_at=now - timedelta(days=190),
            )
        )
        await store.save_behavior_pattern(
            BehaviorPattern(
                session_id=session.id,
                scene_summary=f"一次性的旧话题{index}",
                action="使用旧的固定回应",
                expected_outcome="延续当时的话题",
                pattern_hash=f"{index + 20:064x}",
                confidence=0.8,
                last_reinforced_at=now - timedelta(days=140),
            )
        )
    service = SocialLearningService(
        settings=settings,
        store=store,
        model=FakeModelProvider(),
        prompting=PromptAssembler(settings.project_root / "prompts"),
        events=EventHub(),
    )

    result = await service.run_maintenance(now=now, force=True)

    assert result["jargon_count"] == 5
    assert result["expression_count"] == 5
    assert result["behavior_count"] == 5
    assert result["disabled_count"] == 15
    assert all(
        item.last_maintained_at == now
        for item in await store.list_jargons(session.id, limit=10)
    )
    assert all(
        item.last_maintained_at == now
        for item in await store.list_group_expressions(session.id, limit=10)
    )
    assert all(
        item.last_maintained_at == now
        for item in await store.list_behavior_patterns(session.id, limit=10)
    )
    await store.close()


async def test_expression_evidence_count_keeps_growing_after_history_cap(
    settings,
) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="expression-evidence-cap",
        chat_type=ChatType.GROUP,
        display_name="表达证据群",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    pattern = GroupExpressionPattern(
        session_id=session.id,
        situation="有人分享好消息",
        style="先表达惊喜，再追问细节",
        pattern_hash="c" * 64,
        confidence=0.8,
        occurrence_count=100,
        evidence_message_ids=[f"message-{index}" for index in range(100)],
    )
    await store.save_group_expression(pattern)

    await store.save_group_expression(
        pattern.model_copy(
            update={
                "evidence_message_ids": ["message-99", "message-100"],
                "last_reinforced_at": utc_now(),
            }
        )
    )

    updated = (await store.list_group_expressions(session.id))[0]
    assert updated.occurrence_count == 101
    assert len(updated.evidence_message_ids) == 100
    assert "message-0" not in updated.evidence_message_ids
    assert "message-100" in updated.evidence_message_ids
    await store.close()


async def test_expression_embedding_kmeans_index_selects_semantic_scene(
    settings,
) -> None:
    vector_settings = settings.model_copy(deep=True)
    vector_settings.embedding = EmbeddingSettings(
        enabled=True,
        base_url="https://embedding.example/v1",
        api_key="test-only-key",
        name="social-semantic-test",
    )
    store = DatabaseStore(vector_settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="vector-expression-group",
        chat_type=ChatType.GROUP,
        display_name="表达向量群",
        participants=[
            Participant(external_user_id="u1", display_name="小明")
        ],
    )
    patterns = [
        ("遇到接口报错时", "先简短确认，再追问关键配置"),
        ("群友分享离谱消息时", "用短促感叹接梗"),
        ("有人情绪低落时", "降低语速并先表达理解"),
        ("讨论部署失败时", "用排障口吻逐步确认环境"),
    ]
    for index, (situation, style) in enumerate(patterns):
        await store.save_group_expression(
            GroupExpressionPattern(
                id=f"expression-vector-{index}",
                session_id=session.id,
                situation=situation,
                style=style,
                pattern_hash=f"{index + 1:064x}",
                confidence=0.85,
                occurrence_count=3,
            )
        )
    query = await _append_user(
        store,
        external_message_id="vector-query",
        external_chat_id=session.external_chat_id,
        text="接口报错了，可能是哪项配置有问题？",
    )
    embedding = SocialSemanticEmbeddingProvider()
    service = SocialLearningService(
        settings=vector_settings,
        store=store,
        model=FakeModelProvider(),
        embedding=embedding,
        prompting=PromptAssembler(vector_settings.project_root / "prompts"),
        events=EventHub(),
    )

    context = await service.prepare_reply_context(
        session=session,
        messages=[query],
        turn_id="turn-vector-expression",
    )

    guidance = cast(
        list[dict[str, object]], context["group_expression_guidance"]
    )
    assert "接口" in cast(str, guidance[0]["situation"])
    index_data = await store.get_social_learning_index_data(session.id)
    clusters = cast(
        list[dict[str, object]], index_data["expression_clusters"]
    )
    assert len(clusters) == 2
    assert sum(cast(int, item["member_count"]) for item in clusters) == 4
    assert embedding.batch_sizes[0] == 3
    await store.clear_memory_state(session.id)
    cleared_indexes = await store.get_social_learning_index_data(session.id)
    assert cleared_indexes["expression_clusters"] == []
    await store.close()


async def test_behavior_alias_graph_spreads_to_related_scene(settings) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="behavior-graph-group",
        chat_type=ChatType.GROUP,
        display_name="行为图群",
        participants=[
            Participant(external_user_id="u1", display_name="小明")
        ],
    )
    direct = BehaviorPattern(
        id="behavior-direct",
        session_id=session.id,
        scene_summary="接口故障且信息不足",
        scene_tags=["技术排障", "信息不足"],
        tag_groups=[
            BehaviorTagGroup(
                kind=BehaviorTagKind.DOMAIN,
                tags=["技术排障", "定位报错", "接口故障"],
            ),
            BehaviorTagGroup(
                kind=BehaviorTagKind.DOMAIN,
                tags=["信息不足", "缺少上下文"],
            ),
        ],
        action="先确认错误位置",
        expected_outcome="缩小排查范围",
        pattern_hash="d" * 64,
        confidence=0.85,
        occurrence_count=4,
    )
    related = BehaviorPattern(
        id="behavior-related",
        session_id=session.id,
        scene_summary="信息不足且需要核对环境",
        scene_tags=["信息不足", "环境核对"],
        tag_groups=[
            BehaviorTagGroup(
                kind=BehaviorTagKind.DOMAIN,
                tags=["信息不足", "缺少上下文"],
            ),
            BehaviorTagGroup(
                kind=BehaviorTagKind.DOMAIN,
                tags=["环境核对", "检查配置环境"],
            ),
        ],
        action="请对方补充环境信息",
        expected_outcome="获得可复现条件",
        pattern_hash="e" * 64,
        confidence=0.8,
        occurrence_count=3,
    )
    await store.save_behavior_pattern(direct)
    await store.save_behavior_pattern(related)

    scores = await store.retrieve_behavior_graph_scores(
        session_id=session.id,
        profile=BehaviorScenarioProfile(
            summary="对方希望定位报错",
            tag_groups=[
                BehaviorTagGroup(
                    kind=BehaviorTagKind.DOMAIN, tags=["定位报错"]
                )
            ],
            confidence=0.8,
        ),
        max_depth=1,
        direct_lock_threshold=0.6,
    )

    assert scores["behavior-direct"] > scores["behavior-related"] > 0
    graph_data = await store.get_social_learning_index_data(session.id)
    aliases = cast(
        list[dict[str, object]], graph_data["behavior_tag_aliases"]
    )
    assert {
        cast(str, item["tag"]) for item in aliases
    } >= {"技术排障", "定位报错", "接口故障"}
    await store.close()


async def test_expression_embedding_failure_falls_back_to_local_selector(
    settings,
) -> None:
    vector_settings = settings.model_copy(deep=True)
    vector_settings.embedding = EmbeddingSettings(
        enabled=True,
        base_url="https://embedding.example/v1",
        api_key="test-only-key",
        name="failing-embedding",
    )
    store = DatabaseStore(vector_settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="vector-fallback-group",
        chat_type=ChatType.GROUP,
        display_name="表达降级群",
        participants=[
            Participant(external_user_id="u1", display_name="小明")
        ],
    )
    await store.save_group_expression(
        GroupExpressionPattern(
            session_id=session.id,
            situation="遇到离谱消息时",
            style="用短促感叹表达震惊",
            pattern_hash="9" * 64,
            confidence=0.85,
            occurrence_count=4,
        )
    )
    query = await _append_user(
        store,
        external_message_id="fallback-query",
        external_chat_id=session.external_chat_id,
        text="这个结果也太离谱了",
    )
    service = SocialLearningService(
        settings=vector_settings,
        store=store,
        model=FakeModelProvider(),
        embedding=FailingSocialEmbeddingProvider(),
        prompting=PromptAssembler(vector_settings.project_root / "prompts"),
        events=EventHub(),
    )

    context = await service.prepare_reply_context(
        session=session,
        messages=[query],
        turn_id="turn-vector-fallback",
    )

    assert context["group_expression_guidance"]
    await store.close()
