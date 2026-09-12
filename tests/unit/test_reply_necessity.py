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


def test_question_is_target_when_another_member_adds_reaction() -> None:
    """插话不能接管前面问题触发的回复。"""
    question = message("你觉得这个方案为什么失败？能不能帮我看看", message_id="q")
    reaction = message("哈哈", message_id="r", sender_id="u2")
    result = score_group_reply(
        [question, reaction], [], agent_id="agent", trigger_count=1, frequency_factor=1
    )
    assert result.trigger_message_id == "q"
    assert result.detail["conversation_message_ids"] == ["q"]


def test_unrelated_message_does_not_inherit_agent_continuity() -> None:
    """Agent 刚说过话不代表新话题是在回应它。"""
    result = score_group_reply(
        [message("今晚足球比赛开始了")],
        [message("你更喜欢哪个？", message_id="a", role=MessageRole.ASSISTANT)],
        agent_id="agent", trigger_count=1, frequency_factor=1,
    )
    assert result.detail["continuity_score"] == 0


def test_explicit_reply_keeps_cross_member_context() -> None:
    """引用关系让不同成员的消息进入同一候选。"""
    first = message("这个方案有两个限制", message_id="a")
    second = message("你觉得怎么解决？", message_id="b", sender_id="u2")
    second.components.insert(0, MessageComponent(
        type=ComponentType.QUOTE, message_id="a", target_id="u1"
    ))
    result = score_group_reply(
        [first, second], [], agent_id="agent", trigger_count=1, frequency_factor=1,
    )
    assert result.trigger_message_id == "b"
    assert result.detail["conversation_message_ids"] == ["a", "b"]


def test_other_topics_do_not_accumulate_pressure_for_question() -> None:
    """其他成员无引用的发言不会把同一问题的必要性抬高。"""
    question = message("你觉得怎么解决这个问题？", message_id="q")
    alone = score_group_reply(
        [question], [], agent_id="agent", trigger_count=3, frequency_factor=1,
    )
    mixed = score_group_reply(
        [question, *[
            message("普通讨论", message_id=f"r{i}", sender_id=f"other{i}")
            for i in range(10)
        ]], [], agent_id="agent", trigger_count=3, frequency_factor=1,
    )
    assert mixed.trigger_message_id == "q"
    assert mixed.score == alone.score
    assert mixed.detail["pressure"] == alone.detail["pressure"]


def test_same_sender_reaction_does_not_replace_question() -> None:
    """问题后的同人短反应也不能成为目标。"""
    result = score_group_reply(
        [message("你觉得怎么解决？", message_id="q"), message("哈哈", message_id="r")],
        [], agent_id="agent", trigger_count=1, frequency_factor=1,
    )
    assert result.trigger_message_id == "q"
    assert result.detail["conversation_message_ids"] == ["q"]


def test_latest_direct_invitation_wins_across_candidates() -> None:
    """多次直接邀请保持最新邀请优先，不受候选长度影响。"""
    pending = [message("看看", message_id=f"m{i}", sender_id=f"u{i}") for i in range(2)]
    for item in pending:
        item.components.append(MessageComponent(type=ComponentType.MENTION, target_id="agent"))
    result = score_group_reply(
        pending, [], agent_id="agent", trigger_count=100, frequency_factor=0,
    )
    assert result.forced
    assert result.trigger_message_id == "m1"


def test_empty_batch_has_no_target() -> None:
    """空批次没有候选，也没有回复目标。"""
    result = score_group_reply([], [], agent_id="agent", trigger_count=1, frequency_factor=1)
    assert result.score == 0
    assert result.trigger_message_id is None
