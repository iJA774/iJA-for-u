"""反馈只能归给对应回复，不借用其他轮次的用户反应。"""

import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from application.events import EventHub
from application.social_learning import SocialLearningService
from domain.errors import InvalidModelResponseError
from domain.models import (
    BehaviorPattern,
    BehaviorSelection,
    ChatType,
    ComponentType,
    MessageComponent,
    MessageRole,
    SessionView,
    StoredMessage,
    utc_now,
)
from ports import ModelResult
from prompting import PromptAssembler


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_quote", [False, True])
async def test_feedback_does_not_cross_reply_windows(settings, foreign_quote):
    now = utc_now()
    session = SessionView(
        id="s",
        platform="web-simulator",
        account_id="local",
        external_chat_id="s",
        chat_type=ChatType.GROUP,
        display_name="群",
        created_at=now,
        updated_at=now,
    )
    history = [
        StoredMessage(
            id=identifier,
            session_id="s",
            role=role,
            sender_id="agent" if role == MessageRole.ASSISTANT else "u",
            sender_name="成员",
            components=[MessageComponent.text_component(text)],
            created_at=now + timedelta(seconds=index),
        )
        for index, (identifier, role, text) in enumerate(
            [
                ("a1", MessageRole.ASSISTANT, "能补充错误码吗？"),
                ("u1", MessageRole.USER, "我看看"),
                ("a2", MessageRole.ASSISTANT, "那场比赛很精彩"),
                ("u2", MessageRole.USER, "确实，谢谢"),
            ]
        )
    ]
    selection = BehaviorSelection(
        id="s1",
        session_id="s",
        turn_id="t1",
        behavior_id="b",
        scene_summary="排查报错",
        assistant_message_ids=["a1"],
    )
    if foreign_quote:
        history[1].components.append(
            MessageComponent(
                type=ComponentType.QUOTE,
                message_id="other-member-message",
                target_id="other",
            )
        )
    store = AsyncMock()
    store.list_pending_behavior_selections.return_value = [selection]
    store.list_recallable_messages.return_value = history
    store.get_behavior_pattern.return_value = BehaviorPattern(
        id="b",
        session_id="s",
        scene_summary="排查",
        action="追问",
        expected_outcome="补充信息",
        pattern_hash="a" * 64,
    )
    model = AsyncMock()
    model.complete.return_value = ModelResult(
        content=json.dumps(
            {
                "feedback": [
                    {
                        "selection_id": "s1",
                        "response_to_message_id": "a1",
                        "attribution": "behavior",
                        "signal": "direct",
                        "adopted": True,
                        "status": "success",
                        "score_delta": 0.8,
                        "outcome": "得到积极回应",
                        "reason": "借用了下一话题的反馈",
                        "source_message_ids": ["a1", "u2"],
                    }
                ]
            }
        )
    )
    service = SocialLearningService(
        settings=settings,
        store=store,
        model=model,
        prompting=PromptAssembler(settings.project_root / "prompts"),
        events=EventHub(),
    )
    if foreign_quote:
        await service._evaluate_pending_feedback(session)
        model.complete.assert_not_awaited()
    else:
        with pytest.raises(InvalidModelResponseError, match="同一反馈窗口"):
            await service._evaluate_pending_feedback(session)
    store.save_behavior_feedback.assert_not_awaited()
