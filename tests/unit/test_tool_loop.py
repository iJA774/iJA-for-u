import json
import logging
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from adapters.persistence import DatabaseStore
from adapters.web_simulator import AttachmentStore
from application.events import EventHub
from application.replies import draft_from_model_text
from application.tool_loop import ToolLoop
from domain.errors import InvalidModelResponseError, ToolLimitError
from domain.models import (
    ChatType,
    ComponentType,
    InboundMessage,
    Participant,
    ToolExecutionStatus,
)
from observability import LogHub, LogHubHandler
from ports import ModelMessage, ModelRequest, ModelResult, ModelToolCall
from tools import RegisteredTool, ToolContext, ToolRegistry
from tools.messaging import build_messaging_tools


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def succeed(_: BaseModel, __: ToolContext) -> dict[str, Any]:
    return {"summary": "成功"}


class LoopingModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResult:
        del request
        self.calls += 1
        return ModelResult(
            tool_calls=[
                ModelToolCall(id=f"call_{self.calls}", name="repeat", arguments="{}")
            ]
        )

    async def probe(self):
        return {"ok": True}

    async def close(self) -> None:
        return None


class HiddenToolModel:
    async def complete(self, request: ModelRequest) -> ModelResult:
        del request
        return ModelResult(
            tool_calls=[
                ModelToolCall(
                    id="hidden-call",
                    name="hidden_action",
                    arguments="{}",
                )
            ]
        )

    async def probe(self):
        return {"ok": True}

    async def close(self) -> None:
        return None


class LargeResultModel:
    def __init__(self) -> None:
        self.calls = 0
        self.tool_result: dict[str, Any] | None = None

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.calls += 1
        if self.calls == 1:
            return ModelResult(
                tool_calls=[
                    ModelToolCall(
                        id="large-call",
                        name="large_result",
                        arguments="{}",
                    )
                ]
            )
        tool_message = next(
            item for item in reversed(request.messages) if item.role == "tool"
        )
        self.tool_result = json.loads(tool_message.content or "{}")
        return ModelResult(content="已确认工具执行成功")

    async def probe(self):
        return {"ok": True}

    async def close(self) -> None:
        return None


class LoadThenUseSkillModel:
    def __init__(self) -> None:
        self.calls = 0
        self.exposed_tools: list[set[str]] = []

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.calls += 1
        self.exposed_tools.append({tool.name for tool in request.tools or []})
        if self.calls == 1:
            return ModelResult(
                tool_calls=[
                    ModelToolCall(id="load", name="load_skill", arguments="{}")
                ]
            )
        if self.calls == 2:
            return ModelResult(
                tool_calls=[
                    ModelToolCall(id="use", name="skill_action", arguments="{}")
                ]
            )
        return ModelResult(content="Skill 工具已执行")

    async def probe(self):
        return {"ok": True}

    async def close(self) -> None:
        return None


class SensitiveToolModel:
    """执行一次带敏感参数的工具，再消费结果完成回复。"""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.calls += 1
        if self.calls == 1:
            return ModelResult(
                tool_calls=[
                    ModelToolCall(
                        id="sensitive-call",
                        name="remember_memory",
                        arguments=json.dumps(
                            {
                                "content": "绝密正文",
                                "url": "https://example.invalid/private?token=secret",
                                "filename": "隐私文件.txt",
                                "confirmation_phrase": "确认删除全部",
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]
            )
        assert request.messages[-1].role == "tool"
        return ModelResult(content="已经记住")

    async def probe(self):
        return {"ok": True}

    async def close(self) -> None:
        return None


class SensitiveArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    url: str
    filename: str
    confirmation_phrase: str


@pytest.mark.asyncio
async def test_tool_loop_stops_at_configured_round_limit(settings) -> None:
    settings.tools.max_rounds = 2
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="tool-loop",
        chat_type=ChatType.PRIVATE,
        display_name="工具循环",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    registry = ToolRegistry(
        [RegisteredTool("repeat", "重复测试工具", EmptyArguments, succeed)]
    )
    loop = ToolLoop(settings=settings, store=store, events=EventHub(), registry=registry)
    model = LoopingModel()
    with pytest.raises(ToolLimitError, match="最大轮次"):
        await loop.run(
            model=model,
            messages=[ModelMessage(role="user", content="循环")],
            context=ToolContext(session_id=session.id, actor_id="u1", turn_id="turn-test"),
            allowed_tools={"repeat"},
        )
    assert model.calls == 2
    assert len(await store.list_tool_executions(turn_id="turn-test")) == 2
    await store.close()


@pytest.mark.asyncio
async def test_tool_runtime_events_and_logs_never_expose_arguments(
    settings,
    monkeypatch,
) -> None:
    """完整审计保留参数，但 LogHub 与 WebSocket 共用事件只暴露安全元数据。"""

    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="sensitive-tool",
        chat_type=ChatType.PRIVATE,
        display_name="隐私工具测试",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    events = EventHub()
    queue = events.subscribe()
    log_hub = LogHub(events)
    isolated_logger = logging.getLogger("test.tool_loop.privacy")
    isolated_logger.handlers = [LogHubHandler(log_hub)]
    isolated_logger.setLevel(logging.INFO)
    isolated_logger.propagate = False
    monkeypatch.setattr("application.tool_loop.logger", isolated_logger)

    async def remember(arguments: BaseModel, _: ToolContext) -> dict[str, Any]:
        validated = SensitiveArguments.model_validate(arguments)
        try:
            raise ValueError(
                    f"处理失败: {validated.content} {validated.url} "
                    f"{validated.filename} {validated.confirmation_phrase}"
            )
        except ValueError:
            isolated_logger.exception(
                "工具内部异常，输入=%s",
                validated.content,
            )
        return {"summary": "已保存", "echo": validated.content}

    loop = ToolLoop(
        settings=settings,
        store=store,
        events=events,
        registry=ToolRegistry(
            [
                RegisteredTool(
                    "remember_memory",
                    "保存敏感记忆",
                    SensitiveArguments,
                    remember,
                )
            ]
        ),
    )
    await loop.run(
        model=SensitiveToolModel(),
        messages=[ModelMessage(role="user", content="请记住")],
        context=ToolContext(
            session_id=session.id,
            actor_id="u1",
            turn_id="sensitive-turn",
        ),
        allowed_tools={"remember_memory"},
    )

    event_payloads: list[dict[str, Any]] = []
    while not queue.empty():
        event_payloads.append(queue.get_nowait())
    public_projection = json.dumps(
        {"events": event_payloads, "logs": log_hub.snapshot()},
        ensure_ascii=False,
    )
    for secret in (
        "绝密正文",
        "token=secret",
        "隐私文件.txt",
        "确认删除全部",
    ):
        assert secret not in public_projection
    tool_events = [
        item for item in event_payloads if item["type"].startswith("tool.")
    ]
    assert [item["type"] for item in tool_events] == [
        "tool.started",
        "tool.completed",
    ]
    assert all("arguments" not in item["payload"] for item in tool_events)
    assert all("result" not in item["payload"] for item in tool_events)

    [audit] = await store.list_tool_executions(turn_id="sensitive-turn")
    assert audit.arguments["content"] == "绝密正文"
    assert audit.result == {"summary": "已保存", "echo": "绝密正文"}
    await store.close()


@pytest.mark.asyncio
async def test_tool_loop_exposes_skill_tools_only_after_load(settings) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="dynamic-skill-tools",
        chat_type=ChatType.PRIVATE,
        display_name="动态 Skill 工具",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )

    async def load_skill(_: BaseModel, context: ToolContext) -> dict[str, Any]:
        context.loaded_skills.add("demo-skill")
        return {"skill": "demo-skill", "instructions": "先加载再执行。"}

    registry = ToolRegistry(
        [
            RegisteredTool(
                "load_skill",
                "加载 Skill",
                EmptyArguments,
                load_skill,
            ),
            RegisteredTool(
                "skill_action",
                "Skill 原子动作",
                EmptyArguments,
                succeed,
                required_skill="demo-skill",
            ),
        ]
    )
    loop = ToolLoop(
        settings=settings,
        store=store,
        events=EventHub(),
        registry=registry,
    )
    model = LoadThenUseSkillModel()

    draft = await loop.run(
        model=model,
        messages=[ModelMessage(role="user", content="使用 Skill")],
        context=ToolContext(
            session_id=session.id,
            actor_id="u1",
            turn_id="dynamic-skill-turn",
        ),
        allowed_tools={"load_skill", "skill_action"},
    )

    assert draft == draft_from_model_text(
        "Skill 工具已执行",
        split_short_lines=True,
    )
    assert model.exposed_tools == [
        {"load_skill"},
        {"load_skill", "skill_action"},
        {"load_skill", "skill_action"},
    ]
    await store.close()


@pytest.mark.asyncio
async def test_tool_loop_rejects_model_call_to_tool_not_exposed_this_turn(
    settings,
) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="hidden-tool",
        chat_type=ChatType.PRIVATE,
        display_name="隐藏工具测试",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )

    async def must_not_run(_: BaseModel, __: ToolContext) -> dict[str, Any]:
        raise AssertionError("未下发工具不应执行")

    registry = ToolRegistry(
        [
            RegisteredTool(
                "hidden_action",
                "不应暴露的工具",
                EmptyArguments,
                must_not_run,
            )
        ]
    )
    loop = ToolLoop(
        settings=settings,
        store=store,
        events=EventHub(),
        registry=registry,
    )
    with pytest.raises(
        InvalidModelResponseError,
        match="未授权或未下发的工具: hidden_action",
    ):
        await loop.run(
            model=HiddenToolModel(),
            messages=[ModelMessage(role="user", content="尝试隐藏工具")],
            context=ToolContext(
                session_id=session.id,
                actor_id="u1",
                turn_id="hidden-turn",
            ),
            allowed_tools=set(),
        )
    assert await store.list_tool_executions(turn_id="hidden-turn") == []
    await store.close()


@pytest.mark.asyncio
async def test_large_success_result_stays_successful_in_model_transcript_and_audit(
    settings,
) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="large-tool-result",
        chat_type=ChatType.PRIVATE,
        display_name="大结果测试",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )

    async def return_large(_: BaseModel, __: ToolContext) -> dict[str, Any]:
        return {"sensitive_detail": "x" * 40_000}

    registry = ToolRegistry(
        [
            RegisteredTool(
                "large_result",
                "返回大结果",
                EmptyArguments,
                return_large,
            )
        ]
    )
    loop = ToolLoop(
        settings=settings,
        store=store,
        events=EventHub(),
        registry=registry,
    )
    model = LargeResultModel()
    draft = await loop.run(
        model=model,
        messages=[ModelMessage(role="user", content="执行大结果工具")],
        context=ToolContext(
            session_id=session.id,
            actor_id="u1",
            turn_id="large-result-turn",
        ),
        allowed_tools={"large_result"},
    )
    assert draft.components[0].text == "已确认工具执行成功"
    assert model.tool_result == {
        "ok": True,
        "result_truncated": True,
        "tool_call_id": "large-call",
        "message": "工具已成功执行，但详细结果过大，请使用对应查询工具按需获取。",
    }
    executions = await store.list_tool_executions(
        turn_id="large-result-turn"
    )
    assert len(executions) == 1
    assert executions[0].status == ToolExecutionStatus.COMPLETED
    assert executions[0].result is not None
    assert len(str(executions[0].result["sensitive_detail"])) == 40_000
    await store.close()


@pytest.mark.asyncio
async def test_send_messages_prepares_separate_visible_messages() -> None:
    registry = ToolRegistry(build_messaging_tools())
    outcome = await registry.execute(
        "send_messages",
        {"messages": ["第一条", "第二条", "第三条"]},
        ToolContext(session_id="session-test", actor_id="u1"),
    )
    assert outcome.reply_draft is not None
    assert outcome.reply_draft.components[0].text == "第一条"
    assert [
        components[0].text
        for components in outcome.reply_draft.follow_up_components
    ] == ["第二条", "第三条"]


@pytest.mark.asyncio
async def test_send_attachment_prepares_current_onebot_audio(settings) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="onebot",
        account_id="bot-1",
        external_chat_id="10001",
        chat_type=ChatType.PRIVATE,
        display_name="OneBot 私聊",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    attachments = AttachmentStore(
        uploads_root=settings.storage.data_dir / "uploads",
        personas_root=settings.storage.data_dir / "personas",
        max_bytes=settings.chat.max_attachment_bytes,
    )
    component = attachments.save_attachment(
        "voice.amr", "audio/amr", b"#!AMR\n" + b"\x00" * 10
    )
    await store.append_inbound(
        InboundMessage(
            platform="onebot",
            account_id="bot-1",
            external_message_id="m1",
            external_chat_id="10001",
            sender_id="u1",
            sender_name="小明",
            chat_type=ChatType.PRIVATE,
            components=[component],
        )
    )
    registry = ToolRegistry(build_messaging_tools(store, attachments))
    outcome = await registry.execute(
        "send_attachment",
        {"attachment_id": component.attachment_id, "caption": "听一下"},
        ToolContext(
            session_id=session.id,
            actor_id="u1",
            supported_egress_components=frozenset(
                {ComponentType.AUDIO_REF.value}
            ),
        ),
    )
    assert outcome.reply_draft is not None
    assert outcome.reply_draft.components[0].text == "听一下"
    assert outcome.reply_draft.components[1].type == ComponentType.AUDIO_REF
    await store.close()


def test_short_plain_lines_are_recovered_as_separate_messages() -> None:
    draft = draft_from_model_text(
        "哈喽～\n这么晚还没休息吗？\n今天过得怎么样呀？",
        split_short_lines=True,
    )

    assert draft.components[0].text == "哈喽～"
    assert [
        components[0].text for components in draft.follow_up_components
    ] == ["这么晚还没休息吗？", "今天过得怎么样呀？"]


@pytest.mark.parametrize(
    "content",
    [
        "说明：\n\n这是同一条消息的第二段。",
        "- 第一项\n- 第二项\n- 第三项",
        "```text\n第一行\n第二行\n```",
    ],
)
def test_structured_multiline_text_stays_in_one_message(content: str) -> None:
    draft = draft_from_model_text(content, split_short_lines=True)

    assert draft.components[0].text == content
    assert draft.follow_up_components == []
