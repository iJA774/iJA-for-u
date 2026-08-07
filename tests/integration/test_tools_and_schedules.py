import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from bootstrap import build_runtime
from domain.errors import InputValidationError, ProviderError
from domain.models import (
    ChatType,
    InboundMessage,
    MemorySourceChain,
    MemoryStatus,
    MessageComponent,
    Participant,
    ParticipantRole,
    ScheduleRunStatus,
)
from ports import ModelRequest, ModelResult, ModelToolCall


class AlwaysFailModel:
    async def complete(self, request: ModelRequest) -> ModelResult:
        del request
        raise ProviderError("模拟模型故障")

    async def probe(self):
        return {"ok": False}

    async def close(self) -> None:
        return None


class MultiMessageModel:
    """聊天阶段用终态工具生成三个气泡，后台结构化任务返回空结果。"""

    async def complete(self, request: ModelRequest) -> ModelResult:
        system_text = "\n".join(
            item.content or ""
            for item in request.messages
            if item.role == "system"
        )
        if "# 画像事实提取任务" in system_text:
            return ModelResult(content='{"facts":[]}')
        if "# 长期记忆归档任务" in system_text:
            return ModelResult(
                content='{"memories":[],"retract_memory_ids":[]}'
            )
        return ModelResult(
            tool_calls=[
                ModelToolCall(
                    id="send-messages-call",
                    name="send_messages",
                    arguments=json.dumps(
                        {"messages": ["第一条", "第二条", "第三条"]},
                        ensure_ascii=False,
                    ),
                )
            ]
        )

    async def probe(self):
        return {"ok": True}

    async def close(self) -> None:
        return None


class FeedSubscribeModel:
    """从自然语言订阅意图调用聊天工具，并在工具成功后正常收尾。"""

    async def complete(self, request: ModelRequest) -> ModelResult:
        system_text = "\n".join(
            item.content or ""
            for item in request.messages
            if item.role == "system"
        )
        if "# 画像事实提取任务" in system_text:
            return ModelResult(content='{"facts":[]}')
        if "# 长期记忆归档任务" in system_text:
            return ModelResult(
                content='{"memories":[],"retract_memory_ids":[]}'
            )
        if request.messages[-1].role == "tool":
            return ModelResult(content="已订阅金融新闻。")
        return ModelResult(
            tool_calls=[
                ModelToolCall(
                    id="feed-subscribe-call",
                    name="feed_subscribe",
                    arguments=json.dumps(
                        {"topic": "金融新闻", "poll_interval_minutes": 30},
                        ensure_ascii=False,
                    ),
                )
            ]
        )

    async def probe(self):
        return {"ok": True}

    async def close(self) -> None:
        return None


async def cancel_debounce(runtime, session_id: str) -> None:
    task = runtime.chat._debounce_tasks[session_id]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reactive_time_tool_commits_only_final_message(settings) -> None:
    runtime = build_runtime(settings)
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="工具私聊",
            external_chat_id="tool-private",
            participants=[Participant(external_user_id="u1", display_name="小明")],
        )
        await runtime.chat.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="time-1",
                external_chat_id=session.external_chat_id,
                sender_id="u1",
                sender_name="小明",
                chat_type=ChatType.PRIVATE,
                components=[MessageComponent.text_component("上海现在几点？")],
            )
        )
        await cancel_debounce(runtime, session.id)
        await runtime.chat.process_session(session.id)
        messages = await runtime.store.list_messages(session.id)
        executions = await runtime.store.list_tool_executions(session_id=session.id)
        assert len(messages) == 2
        assert "工具结果" in messages[-1].plain_text
        assert [(item.tool_name, item.status) for item in executions] == [("get_current_time", "completed")]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_group_schedule_permissions_and_session_isolation(settings) -> None:
    runtime = build_runtime(settings)
    await runtime.start()
    try:
        group = await runtime.chat.create_session(
            chat_type=ChatType.GROUP,
            display_name="权限群",
            external_chat_id="schedule-group",
            participants=[
                Participant(
                    external_user_id="owner",
                    display_name="群主",
                    role=ParticipantRole.OWNER,
                ),
                Participant(external_user_id="member", display_name="成员"),
            ],
        )
        other = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="另一私聊",
            external_chat_id="schedule-other",
            participants=[Participant(external_user_id="u2", display_name="小红")],
        )
        with pytest.raises(InputValidationError, match="owner/admin"):
            await runtime.schedules.create(
                session_id=group.id,
                actor_id="member",
                title="越权任务",
                instruction="报时",
                source_text="每五分钟报时",
                timezone="UTC",
                dtstart=datetime.now(UTC),
                rrule="FREQ=MINUTELY;INTERVAL=5",
            )
        created = await runtime.schedules.create(
            session_id=group.id,
            actor_id="owner",
            title="群任务",
            instruction="报时",
            source_text="每五分钟报时",
            timezone="UTC",
            dtstart=datetime.now(UTC),
            rrule="FREQ=MINUTELY;INTERVAL=5",
        )
        assert [item.id for item in await runtime.store.list_schedules(group.id)] == [created.id]
        assert await runtime.store.list_schedules(other.id) == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_one_time_reminder_can_be_created_with_count_one(settings) -> None:
    runtime = build_runtime(settings)
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="单次提醒私聊",
            external_chat_id="one-time-reminder",
            participants=[
                Participant(external_user_id="u1", display_name="小明")
            ],
        )
        start = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=1)
        schedule = await runtime.schedules.create(
            session_id=session.id,
            actor_id="u1",
            title="15:30 提醒",
            instruction="提醒我打搅",
            source_text="15:30 提醒我打搅",
            timezone="Asia/Shanghai",
            dtstart=start,
            rrule="FREQ=DAILY;COUNT=1",
        )
        assert schedule.rrule == "FREQ=DAILY;COUNT=1"
        assert schedule.next_run_at == start
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_terminal_multi_message_tool_commits_separate_bubbles(
    settings,
) -> None:
    runtime = build_runtime(settings, model_override=MultiMessageModel())
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="多消息私聊",
            external_chat_id="multi-message",
            participants=[
                Participant(external_user_id="u1", display_name="小明")
            ],
        )
        await runtime.chat.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="multi-message-1",
                external_chat_id=session.external_chat_id,
                sender_id="u1",
                sender_name="小明",
                chat_type=ChatType.PRIVATE,
                components=[
                    MessageComponent.text_component("一次发三条消息")
                ],
            )
        )
        await cancel_debounce(runtime, session.id)
        await runtime.chat.process_session(session.id)
        messages = await runtime.store.list_messages(session.id)
        assert [item.plain_text for item in messages] == [
            "一次发三条消息",
            "第一条",
            "第二条",
            "第三条",
        ]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_chat_feed_tool_subscribes_natural_language_topic(
    settings, monkeypatch
) -> None:
    async def allow_test_destination(url: str) -> str:
        return url

    monkeypatch.setattr(
        "proactive.service.require_public_destination",
        allow_test_destination,
    )
    runtime = build_runtime(settings, model_override=FeedSubscribeModel())
    await runtime.start()
    try:
        # 此测试只验证聊天识别与订阅落库；停止轮询，避免读取真实网络。
        await runtime.proactive_scheduler.stop()
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="订阅私聊",
            external_chat_id="feed-topic",
            participants=[
                Participant(external_user_id="u1", display_name="小明")
            ],
        )
        await runtime.chat.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="feed-topic-1",
                external_chat_id=session.external_chat_id,
                sender_id="u1",
                sender_name="小明",
                chat_type=ChatType.PRIVATE,
                components=[
                    MessageComponent.text_component("帮我订阅金融新闻")
                ],
            )
        )
        await cancel_debounce(runtime, session.id)
        await runtime.chat.process_session(session.id)

        feeds = await runtime.store.list_feed_sources(session.id)
        assert len(feeds) == 1
        assert feeds[0].title == "金融新闻资讯"
        assert "news.google.com/rss/search" in feeds[0].url
        assert "%E9%87%91%E8%9E%8D%E6%96%B0%E9%97%BB" in feeds[0].url
        executions = await runtime.store.list_tool_executions(
            session_id=session.id
        )
        assert [(item.tool_name, item.status) for item in executions] == [
            ("feed_subscribe", "completed")
        ]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_run_now_uses_read_only_tools_and_returns_to_original_session(settings) -> None:
    runtime = build_runtime(settings)
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="主动私聊",
            external_chat_id="schedule-run",
            participants=[Participant(external_user_id="u1", display_name="小明")],
        )
        schedule = await runtime.schedules.create(
            session_id=session.id,
            actor_id="u1",
            title="报时",
            instruction="告诉我上海现在几点",
            source_text="每五分钟告诉我上海现在几点",
            timezone="Asia/Shanghai",
            dtstart=datetime.now(UTC),
            rrule="FREQ=MINUTELY;INTERVAL=5",
        )
        run = await runtime.scheduler.run_now(schedule.id)
        current = run
        for _ in range(100):
            current = (await runtime.store.list_schedule_runs(schedule.id))[0]
            if current.status in {ScheduleRunStatus.COMPLETED, ScheduleRunStatus.FAILED}:
                break
            await asyncio.sleep(0.02)
        assert current.status == ScheduleRunStatus.COMPLETED
        assert len(await runtime.store.list_messages(session.id)) == 1
        executions = await runtime.store.list_tool_executions(schedule_run_id=run.id)
        assert [item.tool_name for item in executions] == ["get_current_time"]
        memories = await runtime.store.list_memories(
            session_id=session.id, statuses={MemoryStatus.ACTIVE}
        )
        assert any(
            item.source_chain == MemorySourceChain.SCHEDULED
            and run.id in item.source_refs
            for item in memories
        )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_scheduled_egress_failure_blocks_original_message(settings) -> None:
    runtime = build_runtime(settings)
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="周期过滤失败私聊",
            external_chat_id="schedule-egress-failure",
            participants=[Participant(external_user_id="u1", display_name="小明")],
        )
        schedule = await runtime.schedules.create(
            session_id=session.id,
            actor_id="u1",
            title="过滤失败报时",
            instruction="告诉我上海现在几点",
            source_text="每五分钟告诉我上海现在几点",
            timezone="Asia/Shanghai",
            dtstart=datetime.now(UTC),
            rrule="FREQ=MINUTELY;INTERVAL=5",
        )

        async def broken_filter(_):
            raise RuntimeError("模拟周期任务过滤器故障")

        runtime.chat.set_egress_filter(broken_filter)
        run = await runtime.scheduler.run_now(schedule.id)
        current = run
        for _ in range(100):
            current = next(
                item
                for item in await runtime.store.list_schedule_runs(schedule.id)
                if item.id == run.id
            )
            if current.status == ScheduleRunStatus.FAILED:
                break
            await asyncio.sleep(0.02)

        assert current.status == ScheduleRunStatus.FAILED
        assert current.error_message == "模拟周期任务过滤器故障"
        assert await runtime.store.list_messages(session.id) == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_three_consecutive_failures_pause_and_alert(settings) -> None:
    runtime = build_runtime(settings, model_override=AlwaysFailModel())
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="失败私聊",
            external_chat_id="schedule-fail",
            participants=[Participant(external_user_id="u1", display_name="小明")],
        )
        schedule = await runtime.schedules.create(
            session_id=session.id,
            actor_id="u1",
            title="故障任务",
            instruction="告诉我现在几点",
            source_text="每五分钟告诉我现在几点",
            timezone="UTC",
            dtstart=datetime.now(UTC),
            rrule="FREQ=MINUTELY;INTERVAL=5",
        )
        for _ in range(3):
            run = await runtime.scheduler.run_now(schedule.id)
            for _ in range(100):
                current = next(
                    item for item in await runtime.store.list_schedule_runs(schedule.id) if item.id == run.id
                )
                if current.status == ScheduleRunStatus.FAILED:
                    break
                await asyncio.sleep(0.02)
        updated = await runtime.store.get_schedule(schedule.id)
        assert updated is not None
        assert updated.status == "paused"
        assert updated.consecutive_failures == 3
        messages = await runtime.store.list_messages(session.id)
        assert "连续失败 3 次" in messages[-1].plain_text
    finally:
        await runtime.stop()
