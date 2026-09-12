import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from adapters.model import FakeModelProvider
from adapters.persistence import DatabaseStore, PersonaStore
from adapters.web_simulator import AttachmentStore, WebSimulatorChannel
from application.events import EventHub
from application.expressions import ExpressionService
from application.service import ChatService
from domain.errors import ConflictError, InputValidationError
from domain.models import (
    ChatType,
    ComponentType,
    DecisionAction,
    DeliveryReceipt,
    DeliveryStatus,
    GroupParticipationMode,
    InboundMessage,
    MessageComponent,
    MessageRole,
    OutboundMessage,
    Participant,
    SessionView,
    TurnDecision,
    new_id,
    utc_now,
)
from plugins._host import PluginContributionCatalog
from ports import ModelRequest, ModelResult
from prompting import PromptAssembler
from scheduling import ScheduleService
from skill_runtime import SkillCatalog
from tools import ToolRegistry, build_messaging_tools, build_schedule_tools


class PausedModelProvider:
    """把首个聊天生成暂停，用于验证 Turn 快照与排队语义。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.chat_requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResult:
        if request.json_mode:
            return await FakeModelProvider().complete(request)
        self.chat_requests.append(request)
        if len(self.chat_requests) == 1:
            self.started.set()
            await self.release.wait()
        return ModelResult(content="收到")

    async def probe(self):
        return {"ok": True}

    async def close(self) -> None:
        return None


class CancellationResistantProfileModel:
    """模拟忽略一次取消、仍试图提交旧画像结果的远端 Provider。"""

    def __init__(self, source_message_id: str) -> None:
        self.source_message_id = source_message_id
        self.started = asyncio.Event()
        self.cancellation_observed = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResult:
        del request
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # 真实 HTTP 客户端可能在取消竞争中仍返回已收完的响应；应用层 epoch
            # 必须独立阻止这个旧结果写回。
            self.cancellation_observed.set()
        return ModelResult(
            content=(
                '{"facts":[{"category":"偏好","content":"喜欢旧结果",'
                f'"confidence":0.95,"source_message_ids":["{self.source_message_id}"]}}]'
                "}"
            )
        )

    async def probe(self):
        return {"ok": True}

    async def close(self) -> None:
        return None


class FailedChannel:
    async def send(self, session, message):
        return DeliveryReceipt(
            outbound_id=message.id,
            status=DeliveryStatus.FAILED,
            error_code="simulated_failure",
            error_message="模拟投递失败",
        )


class CapturingChannel:
    """记录应用层最终交给 Channel 的回复目标。"""

    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    async def send(
        self,
        session: SessionView,
        message: OutboundMessage,
    ) -> DeliveryReceipt:
        del session
        self.messages.append(message.model_copy(deep=True))
        return DeliveryReceipt(
            outbound_id=message.id,
            status=DeliveryStatus.SENT,
            external_message_id=new_id("captured"),
            delivered_at=utc_now(),
        )


class PartialFailureChannel:
    """首条发送成功、第二条明确失败，用于验证批次恢复。"""

    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    async def send(
        self,
        session: SessionView,
        message: OutboundMessage,
    ) -> DeliveryReceipt:
        del session
        self.messages.append(message.model_copy(deep=True))
        if len(self.messages) == 1:
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.SENT,
                external_message_id=new_id("partial"),
                delivered_at=utc_now(),
            )
        return DeliveryReceipt(
            outbound_id=message.id,
            status=DeliveryStatus.FAILED,
            error_code="second_bubble_failed",
            error_message="第二气泡模拟失败",
        )


class NoExternalMessageIdChannel:
    """模拟 OneBot 成功但不返回平台 message_id。"""

    async def send(
        self,
        session: SessionView,
        message: OutboundMessage,
    ) -> DeliveryReceipt:
        del session
        return DeliveryReceipt(
            outbound_id=message.id,
            status=DeliveryStatus.SENT,
            delivered_at=utc_now(),
        )


async def build_service(settings, response: str = "好的，我知道了。"):
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    model = FakeModelProvider(response)
    events = EventHub()
    schedules = ScheduleService(settings, store, events)
    personas = PersonaStore(
        project_root=settings.project_root,
        data_root=settings.storage.data_dir,
        active_character_id=settings.persona.active_character_id,
    )
    attachments = AttachmentStore(
        uploads_root=settings.storage.data_dir / "uploads",
        personas_root=settings.storage.data_dir / "personas",
        max_bytes=settings.chat.max_attachment_bytes,
    )
    service = ChatService(
        settings=settings,
        store=store,
        model=model,
        channel=WebSimulatorChannel(),
        attachment_validator=attachments,
        events=events,
        tool_registry=ToolRegistry(
            [*build_schedule_tools(schedules), *build_messaging_tools()]
        ),
        schedule_service=schedules,
        personas=personas,
        expressions=ExpressionService(
            store=store,
            personas=personas,
            attachments=attachments,
            image_provider=None,
        ),
        skills=SkillCatalog(settings.project_root / "skills"),
        prompting=PromptAssembler(settings.project_root / "prompts"),
        channel_capabilities=PluginContributionCatalog(
            settings.project_root, include_bundled=True
        ).channel_capabilities,
    )
    return service, store, model


async def cancel_debounce(service: ChatService, session_id: str) -> None:
    task = service._debounce_tasks[session_id]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def delivery_statuses(
    store: DatabaseStore,
    messages: list[OutboundMessage],
) -> list[DeliveryStatus]:
    """读取批次状态并在测试边界断言正文与回执一一对应。"""

    statuses: list[DeliveryStatus] = []
    for message in messages:
        receipt = await store.get_delivery(message.id)
        assert receipt is not None
        statuses.append(receipt.status)
    return statuses


@pytest.mark.asyncio
async def test_private_messages_are_saved_separately_and_replied_once(settings) -> None:
    service, store, model = await build_service(settings)
    session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="私聊",
        external_chat_id="u1",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    for index, text in enumerate(["第一条", "第二条"]):
        await service.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id=f"e{index}",
                external_chat_id=session.external_chat_id,
                sender_id="u1",
                sender_name="小明",
                chat_type=ChatType.PRIVATE,
                components=[MessageComponent.text_component(text)],
            )
        )
    await cancel_debounce(service, session.id)
    await service.process_session(session.id)
    messages = await store.list_messages(session.id)
    assert [item.role for item in messages] == [MessageRole.USER, MessageRole.USER, MessageRole.ASSISTANT]
    chat_requests = [request for request in model.requests if not request.json_mode]
    assert len(chat_requests) == 1
    decision = (await store.list_decisions(session.id))[0]
    assert decision.score_detail["reply_plan"]["response_mode"] == "acknowledge_and_continue"
    system_prompt = chat_requests[0].messages[0].content or ""
    assert "# 本轮回复计划（应用层约束）" in system_prompt
    assert '"target_message_id"' in system_prompt
    assert system_prompt.index("# 本轮回复计划（应用层约束）") < system_prompt.index(
        "# 本轮真实能力"
    )
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_model_short_lines_are_committed_as_separate_bubbles(settings) -> None:
    service, store, _ = await build_service(
        settings,
        response="哈喽～\n这么晚还没休息吗？\n今天过得怎么样呀？",
    )
    session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="多气泡私聊",
        external_chat_id="short-lines",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="short-lines-1",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=ChatType.PRIVATE,
            components=[MessageComponent.text_component("哈喽呀")],
        )
    )
    await cancel_debounce(service, session.id)
    await service.process_session(session.id)

    messages = await store.list_messages(session.id)
    assert [message.plain_text for message in messages] == [
        "哈喽呀",
        "哈喽～",
        "这么晚还没休息吗？",
        "今天过得怎么样呀？",
    ]
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_predebounced_private_batch_triggers_immediately_and_replies_once(settings) -> None:
    service, store, model = await build_service(settings)
    session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="延迟插件私聊",
        external_chat_id="delayed-u1",
        participants=[Participant(external_user_id="delayed-u1", display_name="小明")],
    )
    first = InboundMessage(
        platform=session.platform,
        account_id=session.account_id,
        external_message_id="delayed-e1",
        external_chat_id=session.external_chat_id,
        sender_id="delayed-u1",
        sender_name="小明",
        chat_type=ChatType.PRIVATE,
        components=[MessageComponent.text_component("第一句")],
    )
    second = first.model_copy(
        update={
            "external_message_id": "delayed-e2",
            "components": [MessageComponent.text_component("第二句")],
        }
    )

    await service.ingest(first, schedule_turn=False)
    assert session.id not in service._debounce_tasks
    await service.ingest(second, debounce_seconds=0)
    task = service._debounce_tasks[session.id]
    await task

    messages = await store.list_messages(session.id)
    assert [item.role for item in messages] == [
        MessageRole.USER,
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert len([request for request in model.requests if not request.json_mode]) == 1
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_group_short_reaction_is_silenced_without_model_call(settings) -> None:
    service, store, model = await build_service(settings)
    session = await service.create_session(
        chat_type=ChatType.GROUP,
        display_name="测试群",
        external_chat_id="g1",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="g-short",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=ChatType.GROUP,
            components=[MessageComponent.text_component("哈哈")],
        )
    )
    await cancel_debounce(service, session.id)
    await service.process_session(session.id)
    assert not [request for request in model.requests if not request.json_mode]
    assert (await store.list_decisions(session.id))[0].action == "silence"
    assert len(await store.list_messages(session.id)) == 1
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_group_mention_forces_reply(settings) -> None:
    service, store, _ = await build_service(settings)
    session = await service.create_session(
        chat_type=ChatType.GROUP,
        display_name="测试群",
        external_chat_id="g2",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="g-at",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=ChatType.GROUP,
            components=[
                MessageComponent(type=ComponentType.MENTION, target_id="agent", target_name="小佳"),
                MessageComponent.text_component("你怎么看？"),
            ],
        )
    )
    await cancel_debounce(service, session.id)
    await service.process_session(session.id)
    decisions = await store.list_decisions(session.id)
    assert decisions[0].score == 100
    assert len(await store.list_messages(session.id)) == 2
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_private_and_group_turns_use_their_assigned_personas(settings) -> None:
    service, store, model = await build_service(settings)
    service.personas.assign(private="uzi", group="default")
    private_session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="人格私聊",
        external_chat_id="persona-private",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    group_session = await service.create_session(
        chat_type=ChatType.GROUP,
        display_name="人格群聊",
        external_chat_id="persona-group",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )

    await service.ingest(
        InboundMessage(
            platform=private_session.platform,
            account_id=private_session.account_id,
            external_message_id="persona-private-message",
            external_chat_id=private_session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=ChatType.PRIVATE,
            components=[MessageComponent.text_component("在吗？")],
        )
    )
    await cancel_debounce(service, private_session.id)
    await service.process_session(private_session.id)

    await service.ingest(
        InboundMessage(
            platform=group_session.platform,
            account_id=group_session.account_id,
            external_message_id="persona-group-message",
            external_chat_id=group_session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=ChatType.GROUP,
            components=[
                MessageComponent(
                    type=ComponentType.MENTION,
                    target_id="agent",
                    target_name="佳佳子",
                ),
                MessageComponent.text_component("你怎么看？"),
            ],
        )
    )
    await cancel_debounce(service, group_session.id)
    await service.process_session(group_session.id)

    chat_requests = [request for request in model.requests if not request.json_mode]
    assert len(chat_requests) == 2
    private_prompt = chat_requests[0].messages[0].content or ""
    group_prompt = chat_requests[1].messages[0].content or ""
    assert "名叫“苏柚”" in private_prompt
    assert "名叫“尚好佳”" not in private_prompt
    assert "名叫“尚好佳”" in group_prompt
    assert "名叫“苏柚”" not in group_prompt
    assert (await store.list_messages(private_session.id))[-1].sender_name == "苏柚"
    assert (await store.list_messages(group_session.id))[-1].sender_name == "佳佳子"
    await service.stop()
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("forced", [False, True])
async def test_group_planner_keeps_earlier_target_after_other_member_message(
    settings, monkeypatch, forced,
) -> None:
    service, store, model = await build_service(settings)
    recall = AsyncMock(return_value=[])
    monkeypatch.setattr(service.memory, "retrieve", recall)
    channel = CapturingChannel()
    service.channel = channel
    session = await service.create_session(
        chat_type=ChatType.GROUP,
        display_name="多人目标测试群",
        external_chat_id="group-target",
        participants=[
            Participant(external_user_id="u1", display_name="小明"),
            Participant(external_user_id="u2", display_name="小红"),
        ],
    )
    await service.update_group_participation_policy(
        session.id, mode=GroupParticipationMode.NORMAL, trigger_count=1,
        frequency_factor=1, cooldown_seconds=60, expected_revision=1,
    )
    direct_result = await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="group-target-direct",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=ChatType.GROUP,
            components=[
                *([
                    MessageComponent(
                        type=ComponentType.MENTION,
                        target_id="agent",
                        target_name="小佳",
                    ),
                ] if forced else []),
                MessageComponent.text_component("你觉得这个方案为什么失败？能不能帮我看看"),
            ],
        ),
        schedule_turn=False,
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="group-target-later",
            external_chat_id=session.external_chat_id,
            sender_id="u2",
            sender_name="小红",
            chat_type=ChatType.GROUP,
            components=[MessageComponent.text_component("我先去吃饭")],
        ),
        schedule_turn=False,
    )
    assert direct_result.message is not None

    await service.process_session(session.id)

    decision = (await store.list_decisions(session.id))[0]
    plan = decision.score_detail["reply_plan"]
    assert plan["target_message_id"] == direct_result.message.id
    assert plan["address_sender_id"] == "u1"
    assert plan["relevant_message_ids"] == [direct_result.message.id]
    assert decision.score_detail["conversation_message_ids"] == [direct_result.message.id]
    query = recall.call_args.kwargs["query"]
    assert "方案为什么失败" in query
    assert "我先去吃饭" not in query
    assert channel.messages[0].reply_to_message_id == direct_result.message.id
    chat_request = next(request for request in model.requests if not request.json_mode)
    system_prompt = chat_request.messages[0].content or ""
    assert f'"target_message_id":"{direct_result.message.id}"' in system_prompt
    assert '"address_sender_id":"u1"' in system_prompt
    target_prompt_message = next(
        message
        for message in chat_request.messages[1:]
        if direct_result.message.id in (message.content or "")
    )
    assert (
        f'"message_id":"{direct_result.message.id}"'
        in (target_prompt_message.content or "")
    )
    assert '"sender_id":"u1"' in (target_prompt_message.content or "")
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_semantic_conversation_drives_target_memory_and_learning(settings, monkeypatch) -> None:
    """无引用的跨成员话题使用同一语义结果驱动目标、召回和学习。"""
    service, store, model = await build_service(settings)
    recall = AsyncMock(return_value=[])
    learning = AsyncMock(return_value={})
    monkeypatch.setattr(service.memory, "retrieve", recall)
    monkeypatch.setattr(service.social_learning, "prepare_reply_context", learning)
    original_complete = model.complete

    async def semantic_complete(request):
        if "# 本轮对话理解任务" in (request.messages[0].content or ""):
            data = model._last_untrusted_payload(request)
            first, question, _noise = data["pending"]
            return ModelResult(content=json.dumps({
                "target_message_id": question["message_id"],
                "relevant_message_ids": [first["message_id"], question["message_id"]],
                "history_message_ids": [], "retrieval_query": "离线记账工具的延迟同步方案",
                "utility": 100, "needs_history": False, "needs_fresh_data": False,
                "missing_information": [], "ambiguous": False, "is_question": True,
            }))
        return await original_complete(request)

    monkeypatch.setattr(model, "complete", semantic_complete)
    channel = CapturingChannel()
    service.channel = channel
    session = await service.create_session(
        chat_type=ChatType.GROUP, display_name="语义话题", external_chat_id="semantic",
        participants=[Participant(external_user_id=f"u{i}", display_name=f"成员{i}") for i in range(3)],
    )
    try:
        ids = []
        for index, text in enumerate([
            "我在做一个离线记账工具", "没有网络能不能先记，连上后再同步？", "我去打球了",
        ]):
            result = await service.ingest(InboundMessage(
                platform=session.platform, account_id=session.account_id,
                external_message_id=f"semantic-{index}", external_chat_id=session.external_chat_id,
                sender_id=f"u{index}", sender_name=f"成员{index}", chat_type=ChatType.GROUP,
                components=[MessageComponent.text_component(text)],
            ), schedule_turn=False)
            assert result.message is not None
            ids.append(result.message.id)
        await service.process_session(session.id)
        decision = (await store.list_decisions(session.id))[0]
        assert decision.trigger_message_id == ids[1]
        assert channel.messages[0].reply_to_message_id == ids[1]
        assert channel.messages[0].source_refs == ids[:2]
        assert recall.call_args.kwargs["query"] == "离线记账工具的延迟同步方案"
        assert [item.id for item in learning.call_args.kwargs["messages"]] == ids[:2]
        assert decision.score_detail["conversation_understanding"]["relevant_message_ids"] == ids[:2]
    finally:
        await service.stop()
        await store.close()


@pytest.mark.asyncio
async def test_selected_conversation_survives_recent_history_window(settings, monkeypatch) -> None:
    """引用链即使被后来的闲聊挤出最近窗口，也完整进入生成和召回。"""
    settings.chat.recent_context_messages = 5
    service, store, model = await build_service(settings)
    recall = AsyncMock(return_value=[])
    monkeypatch.setattr(service.memory, "retrieve", recall)
    session = await service.create_session(
        chat_type=ChatType.GROUP, display_name="引用上下文", external_chat_id="quote-context",
        participants=[Participant(external_user_id=f"u{i}", display_name=f"成员{i}") for i in range(3)],
    )
    try:
        source_id = ""
        for index in range(8):
            components = [MessageComponent.text_component("普通闲聊")]
            if index == 0:
                components = [MessageComponent.text_component("方案必须满足离线运行这个限制")]
            elif index == 1:
                components = [
                    MessageComponent(type=ComponentType.QUOTE, message_id=source_id, target_id="u0"),
                    MessageComponent(type=ComponentType.MENTION, target_id="agent"),
                    MessageComponent.text_component("你觉得怎么解决？"),
                ]
            result = await service.ingest(InboundMessage(
                platform=session.platform, account_id=session.account_id,
                external_message_id=f"quote-{index}", external_chat_id=session.external_chat_id,
                sender_id=f"u{min(index, 2)}", sender_name=f"成员{min(index, 2)}",
                chat_type=ChatType.GROUP, components=components,
            ), schedule_turn=False)
            assert result.message is not None
            if index == 0:
                source_id = result.message.id
        await service.process_session(session.id)
        request = next(item for item in model.requests if not item.json_mode)
        history_text = "\n".join(item.content or "" for item in request.messages[1:])
        assert "离线运行这个限制" in history_text
        assert "你觉得怎么解决" in history_text
        assert "离线运行这个限制" in recall.call_args.kwargs["query"]
        assert "普通闲聊" not in recall.call_args.kwargs["query"]
    finally:
        await service.stop()
        await store.close()


@pytest.mark.asyncio
async def test_message_arriving_during_generation_stays_for_next_turn(settings) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    model = PausedModelProvider()
    events = EventHub()
    schedules = ScheduleService(settings, store, events)
    personas = PersonaStore(
        project_root=settings.project_root,
        data_root=settings.storage.data_dir,
        active_character_id=settings.persona.active_character_id,
    )
    attachments = AttachmentStore(
        uploads_root=settings.storage.data_dir / "uploads",
        personas_root=settings.storage.data_dir / "personas",
        max_bytes=settings.chat.max_attachment_bytes,
    )
    service = ChatService(
        settings=settings,
        store=store,
        model=model,
        channel=WebSimulatorChannel(),
        attachment_validator=attachments,
        events=events,
        tool_registry=ToolRegistry(build_schedule_tools(schedules)),
        schedule_service=schedules,
        personas=personas,
        expressions=ExpressionService(
            store=store,
            personas=personas,
            attachments=attachments,
            image_provider=None,
        ),
        skills=SkillCatalog(settings.project_root / "skills"),
        prompting=PromptAssembler(settings.project_root / "prompts"),
        channel_capabilities=PluginContributionCatalog(
            settings.project_root, include_bundled=True
        ).channel_capabilities,
    )
    session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="并发私聊",
        external_chat_id="fifo-user",
        participants=[Participant(external_user_id="fifo-user", display_name="小明")],
    )

    async def ingest(external_id: str, text: str) -> None:
        await service.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id=external_id,
                external_chat_id=session.external_chat_id,
                sender_id="fifo-user",
                sender_name="小明",
                chat_type=ChatType.PRIVATE,
                components=[MessageComponent.text_component(text)],
            )
        )

    await ingest("fifo-1", "第一条")
    await cancel_debounce(service, session.id)
    first_turn = asyncio.create_task(service.process_session(session.id))
    await model.started.wait()
    await ingest("fifo-2", "生成期间的新消息")
    model.release.set()
    await first_turn

    first_prompt = "\n".join(item.content or "" for item in model.chat_requests[0].messages)
    assert "生成期间的新消息" not in first_prompt
    await cancel_debounce(service, session.id)
    await service.process_session(session.id)
    second_prompt = "\n".join(item.content or "" for item in model.chat_requests[1].messages)
    assert "生成期间的新消息" in second_prompt

    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_stop_control_cancels_active_private_turn(settings) -> None:
    service, store, _ = await build_service(settings)
    model = PausedModelProvider()
    service.model = model
    service.profile_model = model
    session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="中断私聊",
        external_chat_id="interrupt-user",
        participants=[
            Participant(external_user_id="interrupt-user", display_name="小明")
        ],
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="interrupt-message",
            external_chat_id=session.external_chat_id,
            sender_id="interrupt-user",
            sender_name="小明",
            chat_type=ChatType.PRIVATE,
            components=[MessageComponent.text_component("请执行一个较长任务")],
        ),
        schedule_turn=False,
    )
    turn = asyncio.create_task(service.process_session(session.id))
    await model.started.wait()

    result = await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="interrupt-stop",
            external_chat_id=session.external_chat_id,
            sender_id="interrupt-user",
            sender_name="小明",
            chat_type=ChatType.PRIVATE,
            components=[MessageComponent.text_component("/stop")],
        )
    )
    outcome = await asyncio.gather(turn, return_exceptions=True)

    assert result.control is True
    assert isinstance(outcome[0], asyncio.CancelledError)
    assert session.id not in service._active_turn_tasks
    assert [message.role for message in await store.list_messages(session.id)] == [
        MessageRole.USER
    ]
    await service.stop()
    await store.close()


@pytest.mark.parametrize("operation", ["clear_memory", "clear_chat", "delete"])
@pytest.mark.asyncio
async def test_session_data_epoch_rejects_late_profile_result(
    settings,
    operation: str,
) -> None:
    """三种数据删除与画像请求竞态时，旧 epoch 不得复活事实或记忆。"""

    service, store, _ = await build_service(settings)
    session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="画像竞态私聊",
        external_chat_id=f"profile-race-{operation}",
        participants=[
            Participant(
                external_user_id=f"profile-race-{operation}",
                display_name="小明",
            )
        ],
    )
    ingress = await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id=f"profile-race-message-{operation}",
            external_chat_id=session.external_chat_id,
            sender_id=f"profile-race-{operation}",
            sender_name="小明",
            chat_type=ChatType.PRIVATE,
            components=[MessageComponent.text_component("我喜欢旧结果")],
        ),
        schedule_turn=False,
    )
    assert ingress.message is not None
    model = CancellationResistantProfileModel(ingress.message.id)
    service.profile_model = model

    await service.process_session(session.id)
    await model.started.wait()
    if operation == "clear_memory":
        await service.clear_memory(session.id)
    elif operation == "clear_chat":
        await service.clear_chat(session.id)
    else:
        await service.delete_session(session.id)

    assert model.cancellation_observed.is_set()
    assert await store.list_facts() == []
    assert await store.list_memories(session_id=session.id) == []
    assert [
        run
        for run in await store.list_extraction_runs()
        if run.session_id == session.id
    ] == []
    if operation in {"clear_memory", "clear_chat"}:
        cleared_session = await store.get_session(session.id)
        assert cleared_session is not None
        assert cleared_session.data_epoch == session.data_epoch + 1
        messages = await store.list_messages(session.id)
        if operation == "clear_memory":
            assert messages
            assert await store.list_recallable_messages(session.id) == []
        else:
            assert messages == []
    else:
        assert await store.get_session(session.id) is None

    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_failed_delivery_never_becomes_visible_agent_history(settings) -> None:
    service, store, _ = await build_service(settings)
    service.channel = FailedChannel()
    session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="投递失败私聊",
        external_chat_id="failed-delivery",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="failed-delivery-message",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[MessageComponent.text_component("你好")],
        )
    )
    await cancel_debounce(service, session.id)
    await service.process_session(session.id)
    messages = await store.list_messages(session.id)
    assert [message.role for message in messages] == [MessageRole.USER]
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_egress_failure_blocks_original_private_reply(settings) -> None:
    service, store, _ = await build_service(settings, "绝不能绕过过滤发送")
    session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="过滤失败私聊",
        external_chat_id="egress-failure",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )

    async def broken_filter(_):
        raise RuntimeError("模拟过滤器故障")

    service.set_egress_filter(broken_filter)
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="egress-failure-message",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[MessageComponent.text_component("你好")],
        ),
        schedule_turn=False,
    )

    with pytest.raises(RuntimeError, match="过滤器故障"):
        await service.process_session(session.id)
    assert [message.role for message in await store.list_messages(session.id)] == [
        MessageRole.USER
    ]
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_manual_retry_replays_original_turn_without_reprocessing_memory(settings) -> None:
    service, store, _ = await build_service(settings, "这次送达了")
    service.channel = FailedChannel()
    session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="重试私聊",
        external_chat_id="retry-delivery",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="retry-delivery-message",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[MessageComponent.text_component("请回复我")],
        )
    )
    await cancel_debounce(service, session.id)
    await service.process_session(session.id)
    decision = (await store.list_decisions(session.id))[0]
    memory_runs_before = await store.list_memory_consolidation_runs(session.id)

    service.channel = WebSimulatorChannel()
    result = await service.retry_reactive_reply(session.id, decision.id)

    assert result["status"] == "retried"
    assert [message.plain_text for message in await store.list_messages(session.id)] == [
        "请回复我",
        "这次送达了",
    ]
    assert len(await store.list_decisions(session.id)) == 1
    assert len(await store.list_memory_consolidation_runs(session.id)) == len(memory_runs_before)
    with pytest.raises(InputValidationError, match="已有真实送达"):
        await service.retry_reactive_reply(session.id, decision.id)
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_group_retry_reuses_frozen_participation_decision(settings) -> None:
    service, store, _ = await build_service(settings, "这次送达了")
    service.channel = FailedChannel()
    session = await service.create_session(
        chat_type=ChatType.GROUP,
        display_name="重试群聊",
        external_chat_id="group-retry-participation",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="group-retry-message",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[
                MessageComponent(
                    type=ComponentType.MENTION,
                    target_id="agent",
                    target_name="小佳",
                ),
                MessageComponent.text_component("请回复我"),
            ],
        ),
        schedule_turn=False,
    )
    await service.process_session(session.id)
    decision = (await store.list_decisions(session.id))[0]
    state_after_decision = await store.get_group_participation_policy(session.id)

    service.channel = WebSimulatorChannel()
    await service.retry_reactive_reply(session.id, decision.id)
    state_after_retry = await store.get_group_participation_policy(session.id)

    assert decision.score_detail["policy_state_version"] == 1
    assert state_after_decision.state_version == 2
    assert state_after_retry.state_version == state_after_decision.state_version
    assert state_after_retry.idle_streak == state_after_decision.idle_streak
    await service.stop()
    await store.close()
    reopened = DatabaseStore(settings.storage.database_path)
    await reopened.initialize()
    restored = await reopened.get_group_participation_policy(session.id)
    assert restored.state_version == state_after_retry.state_version
    assert restored.idle_streak == state_after_retry.idle_streak
    await reopened.close()


@pytest.mark.asyncio
async def test_group_turn_unit_of_work_rolls_back_decision_and_message_on_stale_state(
    settings,
) -> None:
    service, store, _ = await build_service(settings)
    session = await service.create_session(
        chat_type=ChatType.GROUP,
        display_name="群决策事务",
        external_chat_id="group-turn-uow",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    ingress = await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="group-turn-uow-message",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[MessageComponent.text_component("普通讨论")],
        ),
        schedule_turn=False,
    )
    assert ingress.message is not None
    decision = TurnDecision(
        session_id=session.id,
        action=DecisionAction.SILENCE,
        strategy="group_reply_necessity",
        score=0,
        threshold=80,
        reason="测试合法沉默",
        trigger_message_id=ingress.message.id,
    )

    with pytest.raises(ConflictError, match="运行态已推进"):
        await store.save_group_reactive_turn(
            decision,
            [ingress.message.id],
            expected_state_version=0,
            external_at=ingress.message.created_at,
            observed_at=utc_now(),
            run=None,
        )

    assert await store.list_decisions(session.id) == []
    pending = await store.list_pending_messages(session.id)
    assert [item.id for item in pending] == [ingress.message.id]
    assert pending[0].processed_turn_id is None
    policy = await store.get_group_participation_policy(session.id)
    assert policy.state_version == 1

    await store.save_group_reactive_turn(
        decision,
        [ingress.message.id],
        expected_state_version=policy.state_version,
        external_at=ingress.message.created_at,
        observed_at=utc_now(),
        run=None,
    )
    assert len(await store.list_decisions(session.id)) == 1
    assert await store.list_pending_messages(session.id) == []
    assert (
        await store.get_group_participation_policy(session.id)
    ).state_version == 2
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_group_idle_backoff_silence_does_not_reinforce_streak(settings) -> None:
    service, store, _ = await build_service(settings)
    session = await service.create_session(
        chat_type=ChatType.GROUP,
        display_name="idle 退避群聊",
        external_chat_id="group-idle-backoff-no-reinforcement",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )

    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="group-idle-backoff-first",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[MessageComponent.text_component("嗯")],
        ),
        schedule_turn=False,
    )
    await service.process_session(session.id)
    first_decision = (await store.list_decisions(session.id))[0]
    after_first = await store.get_group_participation_policy(session.id)

    assert first_decision.action == DecisionAction.SILENCE
    assert first_decision.score_detail["increment_idle_streak"] is True
    assert after_first.idle_streak == 1
    assert after_first.idle_backoff_until is not None

    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="group-idle-backoff-second",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[MessageComponent.text_component("哦")],
        ),
        schedule_turn=False,
    )
    await service.process_session(session.id)
    decisions = await store.list_decisions(session.id)
    second_decision = next(item for item in decisions if item.id != first_decision.id)
    after_second = await store.get_group_participation_policy(session.id)

    assert second_decision.action == DecisionAction.SILENCE
    assert second_decision.score_detail["idle_backoff_remaining_seconds"] > 0
    assert second_decision.score_detail["increment_idle_streak"] is False
    assert after_second.idle_streak == 1
    assert after_second.idle_backoff_until == after_first.idle_backoff_until

    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_group_ordinary_cooldown_starts_only_after_real_sent_and_retry(
    settings,
) -> None:
    service, store, _ = await build_service(settings, "这次真实送达")
    service.channel = FailedChannel()
    session = await service.create_session(
        chat_type=ChatType.GROUP,
        display_name="普通回复冷却",
        external_chat_id="group-ordinary-cooldown",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    await service.update_group_participation_policy(
        session.id,
        mode=GroupParticipationMode.FOCUSED,
        trigger_count=1,
        frequency_factor=1,
        cooldown_seconds=60,
        expected_revision=1,
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="group-ordinary-cooldown-message",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[
                MessageComponent.text_component(
                    "你觉得这个方案为什么会失败？能不能帮忙看看，有什么建议？"
                )
            ],
        ),
        schedule_turn=False,
    )
    await service.process_session(session.id)
    decision = (await store.list_decisions(session.id))[0]
    after_failure = await store.get_group_participation_policy(session.id)

    assert decision.action == DecisionAction.REPLY
    assert decision.score_detail["forced"] is False
    assert after_failure.last_ordinary_reply_at is None

    service.channel = WebSimulatorChannel()
    await service.retry_reactive_reply(session.id, decision.id)
    after_retry = await store.get_group_participation_policy(session.id)

    assert after_retry.last_ordinary_reply_at is not None
    assert after_retry.idle_streak == 0
    assert after_retry.idle_backoff_until is None
    assert after_retry.last_external_message_at == after_failure.last_external_message_at
    assert (
        after_retry.external_interval_sample_count
        == after_failure.external_interval_sample_count
    )
    assert (
        after_retry.external_interval_ewma_seconds
        == after_failure.external_interval_ewma_seconds
    )
    assert after_retry.state_version == after_failure.state_version + 1
    with pytest.raises(InputValidationError, match="已有真实送达"):
        await service.retry_reactive_reply(session.id, decision.id)
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_group_external_interval_ewma_survives_restart(settings) -> None:
    service, store, _ = await build_service(settings)
    session = await service.create_session(
        chat_type=ChatType.GROUP,
        display_name="群消息节奏",
        external_chat_id="group-interval-ewma",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    first_at = utc_now()
    observed_times = [
        first_at,
        first_at + timedelta(seconds=30),
        first_at + timedelta(seconds=90),
    ]
    for index, received_at in enumerate(observed_times):
        ingress = await service.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id=f"group-interval-{index}",
                external_chat_id=session.external_chat_id,
                sender_id="u1",
                sender_name="小明",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component("嗯")],
                received_at=received_at,
            ),
            schedule_turn=False,
        )
        assert ingress.message is not None
        current = await store.get_group_participation_policy(session.id)
        decision = TurnDecision(
            session_id=session.id,
            action=DecisionAction.SILENCE,
            strategy="group_reply_necessity",
            score=0,
            threshold=80,
            reason="测试节奏采样",
            trigger_message_id=ingress.message.id,
        )
        await store.save_group_reactive_turn(
            decision,
            [ingress.message.id],
            expected_state_version=current.state_version,
            external_at=ingress.message.created_at,
            observed_at=received_at,
            run=None,
        )

    before_restart = await store.get_group_participation_policy(session.id)
    assert before_restart.external_interval_sample_count == 2
    assert before_restart.external_interval_ewma_seconds == pytest.approx(37.5)
    await service.stop()
    await store.close()

    reopened = DatabaseStore(settings.storage.database_path)
    await reopened.initialize()
    restored = await reopened.get_group_participation_policy(session.id)
    assert restored.external_interval_sample_count == 2
    assert restored.external_interval_ewma_seconds == pytest.approx(37.5)
    await reopened.close()


@pytest.mark.asyncio
async def test_group_policy_concurrent_updates_allow_only_one_revision(settings) -> None:
    service, store, _ = await build_service(settings)
    session = await service.create_session(
        chat_type=ChatType.GROUP,
        display_name="并发策略群",
        external_chat_id="group-policy-concurrent",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )

    results = await asyncio.gather(
        service.update_group_participation_policy(
            session.id,
            mode=GroupParticipationMode.SILENT,
            trigger_count=25,
            frequency_factor=0.9,
            cooldown_seconds=30,
            expected_revision=1,
        ),
        service.update_group_participation_policy(
            session.id,
            mode=GroupParticipationMode.FOCUSED,
            trigger_count=2,
            frequency_factor=0.9,
            cooldown_seconds=120,
            expected_revision=1,
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, ConflictError) for item in results) == 1
    current = await store.get_group_participation_policy(session.id)
    assert current.revision == 2
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_partial_multibubble_retry_sends_only_frozen_missing_bubble(
    settings,
) -> None:
    service, store, model = await build_service(
        settings,
        "第一气泡\n第二气泡",
    )
    channel = PartialFailureChannel()
    service.channel = channel
    session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="多气泡恢复私聊",
        external_chat_id="partial-retry",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="partial-retry-message",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[MessageComponent.text_component("分两条回复")],
        ),
        schedule_turn=False,
    )
    await service.process_session(session.id)
    decision = (await store.list_decisions(session.id))[0]
    batch = await store.list_reactive_outbound_batch(session.id, decision.id)

    assert len(batch) == 2
    assert await delivery_statuses(store, batch) == [
        DeliveryStatus.SENT,
        DeliveryStatus.FAILED,
    ]
    assert [
        message.plain_text for message in await store.list_messages(session.id)
    ] == ["分两条回复", "第一气泡"]
    assert await service.reactive_reply_retryable(session.id, decision) is True

    recovery_channel = CapturingChannel()
    service.channel = recovery_channel
    requests_before = len(
        [request for request in model.requests if not request.json_mode]
    )
    await service.retry_reactive_reply(session.id, decision.id)

    assert len(recovery_channel.messages) == 1
    assert recovery_channel.messages[0].components[0].text == "第二气泡"
    assert len([request for request in model.requests if not request.json_mode]) == requests_before
    assert [
        message.plain_text for message in await store.list_messages(session.id)
    ] == ["分两条回复", "第一气泡", "第二气泡"]
    assert await service.reactive_reply_retryable(session.id, decision) is False
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_group_multibubble_without_external_ids_commits_each_bubble_once(
    settings,
) -> None:
    service, store, _ = await build_service(settings, "第一气泡\n第二气泡")
    service.channel = NoExternalMessageIdChannel()
    session = await service.create_session(
        chat_type=ChatType.GROUP,
        display_name="无平台消息 ID 群聊",
        external_chat_id="group-no-external-message-id",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    await service.update_group_participation_policy(
        session.id,
        mode=GroupParticipationMode.FOCUSED,
        trigger_count=1,
        frequency_factor=1,
        cooldown_seconds=60,
        expected_revision=1,
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="group-no-external-id-message",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[
                MessageComponent.text_component(
                    "你觉得这个方案为什么会失败？能不能帮忙看看，有什么建议？"
                )
            ],
        ),
        schedule_turn=False,
    )

    await service.process_session(session.id)

    messages = await store.list_messages(session.id)
    assert [message.plain_text for message in messages] == [
        "你觉得这个方案为什么会失败？能不能帮忙看看，有什么建议？",
        "第一气泡",
        "第二气泡",
    ]
    assistant_external_ids = [
        message.external_message_id
        for message in messages
        if message.role == MessageRole.ASSISTANT
    ]
    assert len(set(assistant_external_ids)) == 2
    assert all(
        external_id and external_id.startswith("ija-outbound:")
        for external_id in assistant_external_ids
    )
    assert await store.list_recoverable_reactive_outbound_batches() == []
    policy = await store.get_group_participation_policy(session.id)
    assert policy.last_ordinary_reply_at is not None
    assert policy.state_version == 3
    await service.stop()
    await store.close()


@pytest.mark.asyncio
async def test_restart_resumes_blocked_multibubble_after_first_commit(
    settings,
    monkeypatch,
) -> None:
    service, store, _ = await build_service(settings, "第一气泡\n第二气泡")
    session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="崩溃恢复私聊",
        external_chat_id="partial-restart",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )

    async def interrupt_after_first_commit(**_):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        service.social_learning,
        "record_reply_delivery",
        interrupt_after_first_commit,
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="partial-restart-message",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[MessageComponent.text_component("分两条回复")],
        ),
        schedule_turn=False,
    )
    with pytest.raises(asyncio.CancelledError):
        await service.process_session(session.id)
    decision = (await store.list_decisions(session.id))[0]
    batch = await store.list_reactive_outbound_batch(session.id, decision.id)
    assert await delivery_statuses(store, batch) == [
        DeliveryStatus.SENT,
        DeliveryStatus.BLOCKED,
    ]
    await service.stop()
    await store.close()

    recovered_service, recovered_store, _ = await build_service(
        settings, "不应重新生成"
    )
    recovered_channel = CapturingChannel()
    recovered_service.channel = recovered_channel
    await recovered_service.start()

    assert len(recovered_channel.messages) == 1
    assert recovered_channel.messages[0].components[0].text == "第二气泡"
    assert [
        message.plain_text
        for message in await recovered_store.list_messages(session.id)
    ] == ["分两条回复", "第一气泡", "第二气泡"]
    await recovered_service.stop()
    await recovered_store.close()


@pytest.mark.asyncio
async def test_startup_processes_committed_pending_private_message(settings) -> None:
    service, store, _ = await build_service(settings, "旧进程不会回复")
    session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="pending 恢复私聊",
        external_chat_id="pending-restart",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="pending-restart-message",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[MessageComponent.text_component("重启前已入库")],
        ),
        schedule_turn=False,
    )
    await service.stop()
    await store.close()

    recovered_service, recovered_store, _ = await build_service(
        settings, "重启后自动回复"
    )
    await recovered_service.start()
    for _ in range(100):
        messages = await recovered_store.list_messages(session.id)
        if any(message.role == MessageRole.ASSISTANT for message in messages):
            break
        await asyncio.sleep(0.01)

    assert await recovered_store.list_pending_messages(session.id) == []
    assert [
        message.plain_text
        for message in await recovered_store.list_messages(session.id)
    ] == ["重启前已入库", "重启后自动回复"]
    await recovered_service.stop()
    await recovered_store.close()


@pytest.mark.asyncio
async def test_turn_and_memory_outbox_roll_back_together_on_commit_failure(settings, monkeypatch) -> None:
    service, store, _ = await build_service(settings)
    session = await service.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="原子 Turn",
        external_chat_id="atomic-turn",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    await service.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="atomic-turn-message",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[MessageComponent.text_component("我喜欢原子操作")],
        )
    )
    await cancel_debounce(service, session.id)
    original = DatabaseStore._save_decision_in_transaction

    async def fail_after_staging(db, decision, message_ids) -> None:
        await original(db, decision, message_ids)
        raise RuntimeError("模拟事务提交前崩溃")

    monkeypatch.setattr(
        DatabaseStore,
        "_save_decision_in_transaction",
        staticmethod(fail_after_staging),
    )
    with pytest.raises(RuntimeError, match="提交前崩溃"):
        await service.process_session(session.id)

    assert len(await store.list_pending_messages(session.id)) == 1
    assert await store.list_decisions(session.id) == []
    assert await store.list_memory_consolidation_runs(session.id) == []
    await service.stop()
    await store.close()
