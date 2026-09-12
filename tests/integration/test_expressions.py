import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from adapters.model import FakeImageModelProvider, FakeModelProvider
from bootstrap import build_runtime
from domain.errors import ConflictError, NotFoundError
from domain.models import (
    ChatType,
    ComponentType,
    DeliveryReceipt,
    DeliveryStatus,
    InboundMessage,
    MessageComponent,
    OutboundMessage,
    Participant,
    ScheduleRun,
    ScheduleRunStatus,
    ToolExecution,
    ToolExecutionStatus,
)
from ports import ModelRequest, ModelResult, ModelToolCall


class ExpressionCallingModel:
    """始终先加载 Skill，再根据完整名称清单生成或复用。"""

    def __init__(self) -> None:
        self.loaded_runtimes: list[dict] = []

    async def complete(self, request: ModelRequest) -> ModelResult:
        if "# 本轮对话理解任务" in (request.messages[0].content or ""):
            return await FakeModelProvider().complete(request)
        if request.json_mode:
            return ModelResult(content='{"facts": []}')
        last = request.messages[-1]
        if last.role == "tool":
            payload = json.loads(last.content or "{}")
            runtime = payload["runtime"]
            self.loaded_runtimes.append(runtime)
            names = runtime["expression_names"]
            action = "reuse" if names else "generate"
            arguments = {
                "action": action,
                "name": names[0] if names else "开心挥手",
                "emotion": "开心",
                "caption": "一起开心！" if names else None,
            }
            if action == "generate":
                arguments["image_prompt"] = "眼睛弯弯地笑着挥手"
            return ModelResult(
                tool_calls=[
                    ModelToolCall(
                        id=f"send-{len(self.loaded_runtimes)}",
                        name="send_expression",
                        arguments=json.dumps(arguments, ensure_ascii=False),
                    )
                ]
            )
        available = {tool.name for tool in request.tools or []}
        # Skill runtime 工具只会在 load_skill 成功后动态暴露。
        if "load_skill" not in available:
            return ModelResult(content="本轮表情 Skill 不可用。")
        return ModelResult(
            tool_calls=[
                ModelToolCall(
                    id=f"load-{len(self.loaded_runtimes) + 1}",
                    name="load_skill",
                    arguments='{"skill":"send-expression"}',
                )
            ]
        )

    async def probe(self):
        return {"ok": True}

    async def close(self) -> None:
        return None


class NeverCalledModel:
    """验证 PREPARED 恢复路径不会再次进入模型。"""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResult:
        del request
        self.calls += 1
        raise AssertionError("PREPARED 恢复不应再次调用模型")

    async def probe(self):
        return {"ok": True}

    async def close(self) -> None:
        return None


def portrait_bytes(color: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (576, 1024), color).save(output, format="PNG")
    return output.getvalue()


async def cancel_debounce(runtime, session_id: str) -> None:
    task = runtime.chat._debounce_tasks[session_id]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def send_and_process(
    runtime,
    *,
    session,
    external_id: str,
    sender_id: str,
    sender_name: str,
    components: list[MessageComponent],
) -> None:
    await runtime.chat.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id=external_id,
            external_chat_id=session.external_chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_type=session.chat_type,
            components=components,
        )
    )
    await cancel_debounce(runtime, session.id)
    await runtime.chat.process_session(session.id)


@pytest.mark.asyncio
async def test_generate_once_then_reuse_in_private_group_and_schedule(settings) -> None:
    chat_model = ExpressionCallingModel()
    image_model = FakeImageModelProvider()
    runtime = build_runtime(
        settings,
        model_override=chat_model,
        image_model_override=image_model,
    )
    await runtime.start()
    try:
        persona = runtime.personas.get_active()
        portrait = runtime.attachments.save_persona_portrait(
            persona.character_id,
            "image/png",
            portrait_bytes("#496b9d"),
        )
        runtime.personas.update_portrait(portrait, expected_revision=persona.revision)

        private = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="表情私聊",
            external_chat_id="expression-private",
            participants=[Participant(external_user_id="u1", display_name="小明")],
        )
        await send_and_process(
            runtime,
            session=private,
            external_id="expression-1",
            sender_id="u1",
            sender_name="小明",
            components=[MessageComponent.text_component("我拿到录取通知啦！")],
        )
        first_messages = await runtime.store.list_messages(private.id)
        assert [item.type for item in first_messages[-1].components] == [
            ComponentType.IMAGE_REF
        ]
        assert first_messages[-1].components[-1].is_expression is True
        assert image_model.calls == 1

        await send_and_process(
            runtime,
            session=private,
            external_id="expression-2",
            sender_id="u1",
            sender_name="小明",
            components=[MessageComponent.text_component("再替我开心一下")],
        )
        second_messages = await runtime.store.list_messages(private.id)
        assert [item.type for item in second_messages[-1].components] == [
            ComponentType.TEXT,
            ComponentType.IMAGE_REF,
        ]
        assert second_messages[-1].components[-1].is_expression is True
        assert image_model.calls == 1
        assets = await runtime.expressions.list_current()
        assert [(item.name, item.use_count) for item in assets] == [("开心挥手", 2)]
        assert chat_model.loaded_runtimes[1]["expression_names"] == ["开心挥手"]
        runtime.expressions.set_image_provider(None)
        reuse_only = await runtime.expressions.availability()
        assert reuse_only["can_generate"] is False
        assert "send-expression" in await runtime.chat._available_skills()
        settings.model.supports_tools = False
        assert await runtime.chat._available_skills() == set()
        settings.model.supports_tools = True

        group = await runtime.chat.create_session(
            chat_type=ChatType.GROUP,
            display_name="表情群聊",
            external_chat_id="expression-group",
            participants=[Participant(external_user_id="u1", display_name="小明")],
        )
        await send_and_process(
            runtime,
            session=group,
            external_id="expression-group-1",
            sender_id="u1",
            sender_name="小明",
            components=[
                MessageComponent(type=ComponentType.MENTION, target_id="agent", target_name="小佳"),
                MessageComponent.text_component("一起庆祝吧"),
            ],
        )
        assert (await runtime.store.list_messages(group.id))[-1].components[-1].type == (
            ComponentType.IMAGE_REF
        )
        assert image_model.calls == 1

        schedule = await runtime.schedules.create(
            session_id=private.id,
            actor_id="u1",
            title="开心提醒",
            instruction="用开心表情提醒我休息",
            source_text="每隔五分钟用开心表情提醒我休息",
            timezone="UTC",
            dtstart=datetime.now(UTC),
            rrule="FREQ=MINUTELY;INTERVAL=5",
        )
        run = await runtime.scheduler.run_now(schedule.id)
        current = run
        for _ in range(100):
            current = next(
                item
                for item in await runtime.store.list_schedule_runs(schedule.id)
                if item.id == run.id
            )
            if current.status in {ScheduleRunStatus.COMPLETED, ScheduleRunStatus.FAILED}:
                break
            await asyncio.sleep(0.02)
        assert current.status == ScheduleRunStatus.COMPLETED
        assert image_model.calls == 1

        historical = second_messages[-1].components[-1]
        source_path = assets[0].storage_path
        active = runtime.personas.get_active()
        replacement = runtime.attachments.save_persona_portrait(
            persona.character_id,
            "image/png",
            portrait_bytes("#9d6449"),
        )
        runtime.personas.update_portrait(
            replacement,
            expected_revision=active.revision,
            current_snapshot=active,
        )
        await runtime.expressions.clear_character(persona.character_id)
        assert await runtime.expressions.list_current() == []
        assert not await asyncio.to_thread(Path(source_path).exists)
        assert runtime.attachments.validate_history_image(historical).is_file()
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_prepared_schedule_recovery_reuses_generated_history_copy(settings) -> None:
    chat_model = NeverCalledModel()
    image_model = FakeImageModelProvider()
    runtime = build_runtime(
        settings,
        model_override=chat_model,
        image_model_override=image_model,
    )
    await runtime.start()
    try:
        persona = runtime.personas.get_active()
        portrait = runtime.attachments.save_persona_portrait(
            persona.character_id,
            "image/png",
            portrait_bytes("#5c7e55"),
        )
        runtime.personas.update_portrait(portrait, expected_revision=persona.revision)
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="恢复私聊",
            external_chat_id="prepared-expression",
            participants=[Participant(external_user_id="u1", display_name="小明")],
        )
        schedule = await runtime.schedules.create(
            session_id=session.id,
            actor_id="u1",
            title="明日提醒",
            instruction="用表情提醒我",
            source_text="明天用表情提醒我",
            timezone="UTC",
            dtstart=datetime.now(UTC) + timedelta(days=1),
            rrule="FREQ=DAILY",
        )
        run = ScheduleRun(
            schedule_id=schedule.id,
            session_id=session.id,
            scheduled_for=datetime.now(UTC),
        )
        outbound_id = "out_schedule_" + hashlib.sha256(run.id.encode()).hexdigest()[:32]
        draft = await runtime.expressions.prepare_reply(
            action="generate",
            name="认真提醒",
            normalized_name="认真提醒",
            filename="认真提醒.png",
            emotion="认真又温柔",
            image_prompt="微笑着举起提醒手势",
            model_prompt="保持角色身份，生成认真又温柔的 9:16 提醒表情。",
            caption=None,
        )
        prepared = OutboundMessage(
            id=outbound_id,
            session_id=session.id,
            components=draft.components,
        )
        await runtime.store.save_delivery(
            prepared,
            DeliveryReceipt(outbound_id=outbound_id, status=DeliveryStatus.PREPARED),
        )

        await runtime.chat.execute_scheduled(schedule, run)

        assert run.status == ScheduleRunStatus.COMPLETED
        assert image_model.calls == 1
        assert chat_model.calls == 0
        assets = await runtime.expressions.list_current()
        assert [(item.name, item.use_count) for item in assets] == [("认真提醒", 1)]
        assert (await runtime.store.list_messages(session.id))[0].components[0].type == (
            ComponentType.IMAGE_REF
        )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_schedule_recovers_asset_created_before_prepared_without_regeneration(
    settings,
) -> None:
    chat_model = NeverCalledModel()
    image_model = FakeImageModelProvider()
    runtime = build_runtime(
        settings,
        model_override=chat_model,
        image_model_override=image_model,
    )
    await runtime.start()
    try:
        persona = runtime.personas.get_active()
        portrait = runtime.attachments.save_persona_portrait(
            persona.character_id,
            "image/png",
            portrait_bytes("#635b8d"),
        )
        runtime.personas.update_portrait(portrait, expected_revision=persona.revision)
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="生成崩溃恢复",
            external_chat_id="recover-generated-expression",
            participants=[Participant(external_user_id="u1", display_name="小明")],
        )
        schedule = await runtime.schedules.create(
            session_id=session.id,
            actor_id="u1",
            title="恢复生成",
            instruction="用开心表情提醒我",
            source_text="明天用开心表情提醒我",
            timezone="UTC",
            dtstart=datetime.now(UTC) + timedelta(days=1),
            rrule="FREQ=DAILY",
        )
        run = ScheduleRun(
            schedule_id=schedule.id,
            session_id=session.id,
            scheduled_for=datetime.now(UTC),
        )
        runtime_state = await runtime.expressions.availability()
        await runtime.store.save_tool_execution(
            ToolExecution(
                session_id=session.id,
                schedule_run_id=run.id,
                tool_call_id="load-before-crash",
                tool_name="load_skill",
                arguments={"skill": "send-expression"},
                result={
                    "skill": "send-expression",
                    "instructions": "test",
                    "runtime": runtime_state,
                },
                status=ToolExecutionStatus.COMPLETED,
            )
        )
        await runtime.store.save_tool_execution(
            ToolExecution(
                session_id=session.id,
                schedule_run_id=run.id,
                tool_call_id="generate-before-crash",
                tool_name="send_expression",
                arguments={
                    "action": "generate",
                    "name": "开心挥手",
                    "emotion": "开心",
                    "image_prompt": "笑着挥手",
                    "caption": "记得休息一下。",
                },
            )
        )
        await runtime.expressions.prepare_reply(
            action="generate",
            name="开心挥手",
            normalized_name="开心挥手",
            filename="开心挥手.png",
            emotion="开心",
            image_prompt="笑着挥手",
            model_prompt="保持角色身份，生成开心挥手的 9:16 表情。",
            caption="记得休息一下。",
            generation_key=run.id,
        )
        assert await runtime.store.get_delivery(
            "out_schedule_" + hashlib.sha256(run.id.encode()).hexdigest()[:32]
        ) is None

        await runtime.chat.execute_scheduled(schedule, run)

        assert run.status == ScheduleRunStatus.COMPLETED
        assert image_model.calls == 1
        assert chat_model.calls == 0
        assets = await runtime.expressions.list_current()
        assert [(item.name, item.use_count) for item in assets] == [("开心挥手", 1)]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_schedule_does_not_retry_uncertain_paid_generation(settings) -> None:
    chat_model = NeverCalledModel()
    image_model = FakeImageModelProvider()
    runtime = build_runtime(
        settings,
        model_override=chat_model,
        image_model_override=image_model,
    )
    await runtime.start()
    try:
        persona = runtime.personas.get_active()
        portrait = runtime.attachments.save_persona_portrait(
            persona.character_id,
            "image/png",
            portrait_bytes("#8d5b70"),
        )
        runtime.personas.update_portrait(portrait, expected_revision=persona.revision)
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="不确定生成恢复",
            external_chat_id="uncertain-generated-expression",
            participants=[Participant(external_user_id="u1", display_name="小明")],
        )
        schedule = await runtime.schedules.create(
            session_id=session.id,
            actor_id="u1",
            title="不重试生成",
            instruction="用开心表情提醒我",
            source_text="明天用开心表情提醒我",
            timezone="UTC",
            dtstart=datetime.now(UTC) + timedelta(days=1),
            rrule="FREQ=DAILY",
        )
        run = ScheduleRun(
            schedule_id=schedule.id,
            session_id=session.id,
            scheduled_for=datetime.now(UTC),
        )
        await runtime.store.save_tool_execution(
            ToolExecution(
                session_id=session.id,
                schedule_run_id=run.id,
                tool_call_id="uncertain-generate",
                tool_name="send_expression",
                arguments={
                    "action": "generate",
                    "name": "开心挥手",
                    "emotion": "开心",
                    "image_prompt": "笑着挥手",
                },
            )
        )

        await runtime.chat.execute_scheduled(schedule, run)

        assert run.status == ScheduleRunStatus.FAILED
        assert "不自动重试图片模型" in (run.error_message or "")
        assert image_model.calls == 0
        assert chat_model.calls == 0
    finally:
        await runtime.stop()


def _prepare_portrait(runtime) -> None:
    """为当前角色保存 9:16 base_image 并更新人格引用。"""

    persona = runtime.personas.get_active()
    portrait = runtime.attachments.save_persona_portrait(
        persona.character_id, "image/png", portrait_bytes("#496b9d")
    )
    runtime.personas.update_portrait(portrait, expected_revision=persona.revision)


@pytest.mark.asyncio
async def test_upload_expression_creates_reusable_asset(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        _prepare_portrait(runtime)
        asset = await runtime.expressions.upload_expression(
            content=portrait_bytes("#a1b2c3"),
            mime_type="image/png",
            name="微笑",
            emotion="开心",
        )
        assert asset.name == "微笑"
        assert asset.emotion == "开心"
        assert asset.width * 16 == asset.height * 9
        # 手动上传后即使未启用图片模型，Skill 也可用（可复用路径）
        assert "send-expression" in await runtime.chat._available_skills()
        assets = await runtime.expressions.list_current()
        assert [item.name for item in assets] == ["微笑"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_upload_expression_without_portrait_accepts_arbitrary_ratio(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        output = BytesIO()
        Image.new("RGB", (160, 100), "#a1b2c3").save(output, format="PNG")
        asset = await runtime.expressions.upload_expression(
            content=output.getvalue(),
            mime_type="image/png",
            name="微笑",
            emotion="开心",
        )
        assert (asset.width, asset.height) == (160, 100)
        assert asset.source_portrait_sha256 is None
        assert asset.source_kind.value == "uploaded"
        assert "send-expression" in await runtime.chat._available_skills()
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_upload_expression_rejects_duplicate_name(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        _prepare_portrait(runtime)
        await runtime.expressions.upload_expression(
            content=portrait_bytes("#aaaaaa"),
            mime_type="image/png",
            name="微笑",
            emotion="开心",
        )
        with pytest.raises(ConflictError, match="已存在"):
            await runtime.expressions.upload_expression(
                content=portrait_bytes("#bbbbbb"),
                mime_type="image/png",
                name="微笑",
                emotion="另一种",
            )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_rename_expression_updates_name_and_file(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        _prepare_portrait(runtime)
        asset = await runtime.expressions.upload_expression(
            content=portrait_bytes("#cccccc"),
            mime_type="image/png",
            name="微笑",
            emotion="开心",
        )
        old_path = asset.storage_path
        renamed = await runtime.expressions.rename_expression(asset.id, "大笑")
        assert renamed.name == "大笑"
        assert renamed.normalized_name == "大笑"
        assert renamed.storage_path != old_path
        assert not await asyncio.to_thread(Path(old_path).exists)
        assert await asyncio.to_thread(Path(renamed.storage_path).exists)
        assets = await runtime.expressions.list_current()
        assert [item.name for item in assets] == ["大笑"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_rename_expression_rejects_duplicate(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        _prepare_portrait(runtime)
        await runtime.expressions.upload_expression(
            content=portrait_bytes("#dddddd"),
            mime_type="image/png",
            name="微笑",
            emotion="开心",
        )
        second = await runtime.expressions.upload_expression(
            content=portrait_bytes("#eeeeee"),
            mime_type="image/png",
            name="大笑",
            emotion="愉快",
        )
        with pytest.raises(ConflictError, match="已存在"):
            await runtime.expressions.rename_expression(second.id, "微笑")
        assets = await runtime.expressions.list_current()
        assert {item.name for item in assets} == {"微笑", "大笑"}
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_rename_expression_missing(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        _prepare_portrait(runtime)
        with pytest.raises(NotFoundError, match="表情不存在"):
            await runtime.expressions.rename_expression("expression_nope", "无所谓")
    finally:
        await runtime.stop()
