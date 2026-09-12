"""对话理解的范围、语义结果和失败契约。"""

import json
from unittest.mock import AsyncMock

import pytest

from application.conversation import needs_conversation_understanding, understand_conversation
from application.planning import ReplyPlanner
from domain.errors import InvalidModelResponseError
from domain.models import (
    ChatType,
    ComponentType,
    DecisionAction,
    MessageComponent,
    MessageRole,
    SessionView,
    StoredMessage,
    TurnDecision,
    utc_now,
)
from ports import ModelResult
from prompting import PromptAssembler


def session(chat_type=ChatType.PRIVATE):
    """构造不依赖真实数据的会话。"""
    return SessionView(
        id="s",
        platform="web-simulator",
        account_id="local",
        external_chat_id="s",
        chat_type=chat_type,
        display_name="测试",
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def message(identifier, text):
    """构造带稳定 ID 的用户消息。"""
    return StoredMessage(
        id=identifier,
        session_id="s",
        role=MessageRole.USER,
        sender_id="u",
        sender_name="用户",
        components=[MessageComponent.text_component(text)],
        created_at=utc_now(),
    )


def payload():
    """构造一个已消解指代的分析结果。"""
    return {
        "target_message_id": "now",
        "relevant_message_ids": ["now"],
        "history_message_ids": ["before"],
        "retrieval_query": "离线记账工具的同步方案",
        "utility": 95,
        "needs_history": False,
        "needs_fresh_data": False,
        "missing_information": [],
        "ambiguous": False,
        "is_question": True,
    }


@pytest.mark.asyncio
async def test_anaphora_preserves_selected_history_and_independent_query(settings):
    current = message("now", "还是之前那个")
    current.components.append(MessageComponent(
        type=ComponentType.QUOTE, message_id="before", target_id="u",
    ))
    prior = message("before", "我们讨论离线记账工具的同步方案")
    decision = TurnDecision(
        session_id="s",
        action=DecisionAction.REPLY,
        strategy="private",
        score=100,
        threshold=1,
        reason="私聊",
        trigger_message_id="now",
    )
    model = AsyncMock()
    model.complete.return_value = ModelResult(content=json.dumps(payload()))
    result = await understand_conversation(
        settings=settings,
        model=model,
        prompting=PromptAssembler(settings.project_root / "prompts"),
        session=session(),
        pending=[current],
        history=[prior, current],
        decision=decision,
    )
    assert result.history_message_ids == ["before"]
    assert result.retrieval_query == "离线记账工具的同步方案"
    request = model.complete.call_args.args[0]
    assert "我们讨论离线记账工具" in request.messages[-1].content
    assert '"quoted_message_ids":["before"]' in request.messages[-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("history_message_ids", ["outside"]),
        ("relevant_message_ids", ["outside"]),
        ("target_message_id", None),
    ],
)
async def test_understanding_rejects_outside_or_missing_target(settings, field, value):
    result = payload()
    result[field] = value
    model = AsyncMock()
    model.complete.return_value = ModelResult(content=json.dumps(result))
    decision = TurnDecision(
        session_id="s",
        action=DecisionAction.REPLY,
        strategy="private",
        score=100,
        threshold=1,
        reason="私聊",
        trigger_message_id="now",
    )
    with pytest.raises(InvalidModelResponseError, match="快照"):
        await understand_conversation(
            settings=settings,
            model=model,
            prompting=PromptAssembler(settings.project_root / "prompts"),
            session=session(),
            pending=[message("now", "还是之前那个")],
            history=[message("before", "离线记账工具")],
            decision=decision,
        )


@pytest.mark.parametrize("text", ["今天心情很差", "之前那个方案继续做", "这家店还营业吗"])
def test_planner_leaves_information_gap_to_reply_when_no_analysis(text):
    decision = TurnDecision(
        session_id="s",
        action=DecisionAction.REPLY,
        strategy="private",
        score=100,
        threshold=1,
        reason="私聊",
        trigger_message_id="now",
    )
    plan = ReplyPlanner().plan(session=session(), decision=decision, pending=[message("now", text)])
    assert plan.evidence_mode.value == "adaptive"
    assert plan.preferred_tools == ()
    assert "外部当前状态" in plan.evidence_policy
    assert not plan.ask_follow_up


def test_simple_private_statement_does_not_add_analysis_call():
    assert not needs_conversation_understanding(
        session(),
        [message("now", "今天心情很差")],
        [],
    )
    assert needs_conversation_understanding(
        session(),
        [message("now", "后来怎么样了")],
        [message("before", "之前在讨论一个项目")],
    )
