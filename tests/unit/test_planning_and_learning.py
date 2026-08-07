from datetime import UTC, datetime

import pytest

from application.planning import ReplyPlanner
from application.replies import validate_reply_draft
from domain.errors import InputValidationError, InvalidModelResponseError
from domain.models import (
    ChatType,
    ComponentType,
    DecisionAction,
    MessageComponent,
    MessageRole,
    Participant,
    ReplyDraft,
    ReplyTargetReason,
    SessionView,
    StoredMessage,
    TurnDecision,
)


def _message(message_id: str, text: str) -> StoredMessage:
    return StoredMessage(
        id=message_id,
        session_id="session-plan",
        role=MessageRole.USER,
        sender_id="u1",
        sender_name="小明",
        components=[MessageComponent.text_component(text)],
        created_at=datetime.now(UTC),
    )


def test_reply_planner_routes_history_evidence_before_reply() -> None:
    now = datetime.now(UTC)
    session = SessionView(
        id="session-plan",
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="plan",
        chat_type=ChatType.PRIVATE,
        display_name="规划测试",
        participants=[Participant(external_user_id="u1", display_name="小明")],
        created_at=now,
        updated_at=now,
    )
    decision = TurnDecision(
        session_id=session.id,
        action=DecisionAction.REPLY,
        strategy="private",
        score=100,
        threshold=0,
        reason="私聊",
    )
    plan = ReplyPlanner().plan(
        session=session,
        decision=decision,
        pending=[_message("m1", "你还记得我上次的原话吗？")],
    )
    assert plan.response_mode == "direct_answer"
    assert plan.preferred_tools == ("search_messages", "fetch_source")
    assert "精确回源" in plan.evidence_policy
    assert plan.target_message_id == "m1"
    assert plan.relevant_message_ids == ("m1",)
    assert plan.schema_version == 2


def test_group_planner_keeps_earlier_direct_mention_as_target() -> None:
    now = datetime.now(UTC)
    session = SessionView(
        id="session-group-plan",
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="group-plan",
        chat_type=ChatType.GROUP,
        display_name="群聊规划测试",
        participants=[
            Participant(external_user_id="u1", display_name="小明"),
            Participant(external_user_id="u2", display_name="小红"),
        ],
        created_at=now,
        updated_at=now,
    )
    direct = _message("m1", "你怎么看？").model_copy(
        update={
            "session_id": session.id,
            "components": [
                MessageComponent(
                    type=ComponentType.MENTION,
                    target_id="agent",
                    target_name="小佳",
                ),
                MessageComponent.text_component("你怎么看？"),
            ],
        }
    )
    later = _message("m2", "我先去吃饭").model_copy(
        update={
            "session_id": session.id,
            "sender_id": "u2",
            "sender_name": "小红",
        }
    )
    decision = TurnDecision(
        session_id=session.id,
        action=DecisionAction.REPLY,
        strategy="group_reply_necessity",
        score=100,
        threshold=80,
        reason="直接 @ Agent，强制回复",
        score_detail={"forced": True},
        trigger_message_id=direct.id,
    )

    plan = ReplyPlanner().plan(
        session=session,
        decision=decision,
        pending=[direct, later],
    )

    assert plan.target_message_id == direct.id
    assert plan.address_sender_id == "u1"
    assert plan.relevant_message_ids == (direct.id,)
    assert plan.target_reason == ReplyTargetReason.DIRECT_MENTION
    assert plan.response_mode == "group_forced_reply"
    assert plan.max_visible_messages == 2


def test_planner_rejects_silence_and_damaged_frozen_plan() -> None:
    now = datetime.now(UTC)
    session = SessionView(
        id="session-plan-validation",
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="plan-validation",
        chat_type=ChatType.PRIVATE,
        display_name="规划校验",
        participants=[Participant(external_user_id="u1", display_name="小明")],
        created_at=now,
        updated_at=now,
    )
    pending = [_message("m1", "你好").model_copy(update={"session_id": session.id})]
    silence = TurnDecision(
        session_id=session.id,
        action=DecisionAction.SILENCE,
        strategy="test",
        score=0,
        threshold=80,
        reason="沉默",
        trigger_message_id="m1",
    )
    with pytest.raises(InputValidationError, match="只有回复决策"):
        ReplyPlanner().plan(session=session, decision=silence, pending=pending)

    reply = silence.model_copy(update={"action": DecisionAction.REPLY})
    plan = ReplyPlanner().plan(session=session, decision=reply, pending=pending)
    damaged = plan.model_dump(mode="json")
    damaged["target_message_id"] = "not-in-turn"
    with pytest.raises(InputValidationError, match="不符合 v2 契约|目标消息"):
        ReplyPlanner().restore(
            payload=damaged,
            session=session,
            decision=reply,
            pending=pending,
        )
    with pytest.raises(InputValidationError, match="版本未知或内容损坏"):
        ReplyPlanner().restore(
            payload={},
            session=session,
            decision=reply,
            pending=pending,
        )

    legacy = {
        "objective": "旧版目标",
        "response_mode": "acknowledge_and_continue",
        "address_sender_id": "u1",
        "evidence_policy": "旧版证据规则",
    }
    restored = ReplyPlanner().restore(
        payload=legacy,
        session=session,
        decision=reply,
        pending=pending,
    )
    assert restored.schema_version == 2
    assert restored.target_message_id == "m1"


def test_replyer_draft_must_obey_frozen_output_budget() -> None:
    now = datetime.now(UTC)
    session = SessionView(
        id="session-draft-budget",
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="draft-budget",
        chat_type=ChatType.GROUP,
        display_name="草稿预算",
        participants=[Participant(external_user_id="u1", display_name="小明")],
        created_at=now,
        updated_at=now,
    )
    pending = [_message("m1", "你怎么看？").model_copy(update={"session_id": session.id})]
    decision = TurnDecision(
        session_id=session.id,
        action=DecisionAction.REPLY,
        strategy="test",
        score=100,
        threshold=80,
        reason="回复",
        trigger_message_id="m1",
    )
    plan = ReplyPlanner().plan(session=session, decision=decision, pending=pending)
    valid = ReplyDraft(components=[MessageComponent.text_component("可以。")])

    assert validate_reply_draft(valid, plan) is valid
    too_many = ReplyDraft(
        components=[MessageComponent.text_component("第一条")],
        follow_up_components=[
            [MessageComponent.text_component("第二条")],
            [MessageComponent.text_component("第三条")],
        ],
    )
    with pytest.raises(InvalidModelResponseError, match="超过计划上限"):
        validate_reply_draft(too_many, plan)
