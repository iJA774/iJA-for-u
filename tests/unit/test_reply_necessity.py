from datetime import UTC, datetime

from domain.models import ComponentType, MessageComponent, MessageRole, StoredMessage
from strategies.reply_necessity import score_group_reply


def message(
    text: str,
    *,
    message_id: str = "m1",
    role: MessageRole = MessageRole.USER,
    sender_id: str | None = None,
) -> StoredMessage:
    return StoredMessage(
        id=message_id,
        session_id="s1",
        role=role,
        sender_id=sender_id or ("u1" if role == MessageRole.USER else "agent"),
        sender_name="用户" if role == MessageRole.USER else "小佳",
        components=[MessageComponent.text_component(text)],
        created_at=datetime.now(UTC),
    )


def test_at_agent_forces_reply() -> None:
    item = message("看看这个")
    item.components.insert(
        0, MessageComponent(type=ComponentType.MENTION, target_id="agent", target_name="小佳")
    )
    result = score_group_reply(
        [item], [], agent_id="agent", trigger_count=100, frequency_factor=0.5
    )
    assert result.forced is True
    assert result.score == 100


def test_short_reaction_can_legitimately_stay_silent() -> None:
    result = score_group_reply(
        [message("哈哈")], [], agent_id="agent", trigger_count=3, frequency_factor=0.9
    )
    assert result.forced is False
    assert result.score < 80
    assert result.detail["content"] == -25


def test_recent_agent_presence_reduces_score() -> None:
    pending = [message("你觉得这个方案为什么会失败？能不能帮我看看", message_id=f"p{i}") for i in range(4)]
    quiet = score_group_reply(
        pending, [], agent_id="agent", trigger_count=1, frequency_factor=1
    )
    noisy_history = [message("在", message_id=f"a{i}", role=MessageRole.ASSISTANT) for i in range(8)]
    noisy = score_group_reply(
        pending, noisy_history, agent_id="agent", trigger_count=1, frequency_factor=1
    )
    assert noisy.score < quiet.score
    assert noisy.detail["presence_penalty"] == 25


def test_trigger_count_and_frequency_factor_control_independent_signals() -> None:
    """积压压力与基础评分缩放应可分别由独立配置控制。"""

    pending = [message("普通讨论", message_id=f"p{i}") for i in range(3)]
    quick_trigger = score_group_reply(
        pending, [], agent_id="agent", trigger_count=1, frequency_factor=0.5
    )
    slow_trigger = score_group_reply(
        pending, [], agent_id="agent", trigger_count=6, frequency_factor=0.5
    )
    amplified = score_group_reply(
        pending, [], agent_id="agent", trigger_count=6, frequency_factor=1
    )

    assert float(quick_trigger.detail["pressure"]) > float(
        slow_trigger.detail["pressure"]
    )
    assert amplified.detail["pressure"] == slow_trigger.detail["pressure"]
    assert amplified.score > slow_trigger.score


def test_reply_to_recent_agent_gets_continuity_signal() -> None:
    prior = [message("你更喜欢哪个？", message_id="a1", role=MessageRole.ASSISTANT)]
    result = score_group_reply(
        [message("第二个")],
        prior,
        agent_id="agent",
        trigger_count=1,
        frequency_factor=1,
    )
    assert result.detail["continuity_score"] == 20
    assert result.score > 0


def test_multi_member_thread_and_quote_reduce_barging() -> None:
    first = message("你怎么看？", message_id="m1", sender_id="u1")
    second = message("我觉得先等等", message_id="m2", sender_id="u2")
    second.components.insert(
        0,
        MessageComponent(
            type=ComponentType.QUOTE,
            message_id="m1",
            target_id="u1",
            target_name="甲",
        ),
    )
    result = score_group_reply(
        [first, second],
        [],
        agent_id="agent",
        trigger_count=1,
        frequency_factor=1,
    )
    assert result.detail["human_thread_penalty"] == 35
