from datetime import UTC, datetime, timedelta

from domain.models import (
    ComponentType,
    DecisionAction,
    GroupParticipationMode,
    GroupParticipationPolicy,
    MessageComponent,
    MessageRole,
    Participant,
    StoredMessage,
)
from strategies import GroupChatStrategy

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def message(
    text: str,
    *,
    message_id: str = "m1",
    created_at: datetime = NOW,
    role: MessageRole = MessageRole.USER,
    sender_id: str | None = None,
    components: list[MessageComponent] | None = None,
) -> StoredMessage:
    return StoredMessage(
        id=message_id,
        session_id="s1",
        role=role,
        sender_id=sender_id or (
            "agent" if role == MessageRole.ASSISTANT else "u1"
        ),
        sender_name="小佳" if role == MessageRole.ASSISTANT else "用户",
        components=components or [MessageComponent.text_component(text)],
        created_at=created_at,
    )


def strategy() -> GroupChatStrategy:
    return GroupChatStrategy(threshold=80, trigger_count=3, frequency_factor=0.9)


def policy(**changes: object) -> GroupParticipationPolicy:
    return GroupParticipationPolicy(session_id="s1").model_copy(update=changes)


def test_silent_mode_keeps_ordinary_message_silent_but_not_forced_mention() -> None:
    ordinary = strategy().decide(
        "s1",
        [message("你觉得这个方案怎么样？能不能帮我看看")],
        [],
        policy=policy(mode=GroupParticipationMode.SILENT),
        now=NOW,
    )
    mentioned = message("帮我看看", message_id="m2")
    mentioned.components.insert(
        0,
        MessageComponent(
            type=ComponentType.MENTION,
            target_id="agent",
            target_name="小佳",
        ),
    )
    forced = strategy().decide(
        "s1",
        [mentioned],
        [],
        policy=policy(mode=GroupParticipationMode.SILENT),
        now=NOW,
    )

    assert ordinary.action == DecisionAction.SILENCE
    assert ordinary.score_detail["participation_mode"] == "silent"
    assert forced.action == DecisionAction.REPLY
    assert forced.score_detail["forced"] is True


def test_focused_mode_lowers_threshold_and_freezes_effective_values() -> None:
    pending = [
        message("你觉得这个方案怎么样？", message_id=f"m{index}")
        for index in range(2)
    ]
    normal = strategy().decide(
        "s1", pending, [], policy=policy(), now=NOW
    )
    focused = strategy().decide(
        "s1",
        pending,
        [],
        policy=policy(
            mode=GroupParticipationMode.FOCUSED,
            trigger_count=3,
            frequency_factor=0.9,
            revision=4,
        ),
        now=NOW,
    )

    assert focused.threshold == 30
    assert focused.score >= normal.score
    assert focused.score_detail["effective_trigger_count"] == 2
    assert focused.score_detail["effective_frequency_factor"] == 0.925
    assert focused.score_detail["policy_revision"] == 4
    assert focused.score_detail["snapshot_time"] == NOW.isoformat()


def test_cooldown_and_idle_backoff_gate_only_ordinary_messages() -> None:
    pending = [
        message("你觉得这个方案为什么会失败？能不能帮我看看", message_id=f"m{i}")
        for i in range(4)
    ]
    cooling = strategy().decide(
        "s1",
        pending,
        [],
        policy=policy(
            trigger_count=1,
            frequency_factor=1,
            cooldown_seconds=60,
            last_ordinary_reply_at=NOW - timedelta(seconds=20),
        ),
        now=NOW,
    )
    backing_off = strategy().decide(
        "s1",
        pending,
        [],
        policy=policy(
            trigger_count=1,
            frequency_factor=1,
            cooldown_seconds=30,
            idle_streak=3,
            idle_backoff_until=NOW + timedelta(seconds=80),
            last_external_message_at=NOW - timedelta(seconds=10),
        ),
        now=NOW,
    )
    reset_after_quiet = strategy().decide(
        "s1",
        pending,
        [],
        policy=policy(
            trigger_count=1,
            frequency_factor=1,
            cooldown_seconds=30,
            idle_streak=3,
            idle_backoff_until=NOW + timedelta(seconds=80),
            last_external_message_at=NOW - timedelta(minutes=6),
        ),
        now=NOW,
    )

    assert cooling.action == DecisionAction.SILENCE
    assert cooling.score_detail["reply_cooldown_remaining_seconds"] == 40
    assert cooling.score_detail["increment_idle_streak"] is False
    assert backing_off.action == DecisionAction.SILENCE
    assert backing_off.score_detail["idle_backoff_multiplier"] == 3
    assert backing_off.score_detail["idle_backoff_seconds"] == 90
    assert backing_off.score_detail["increment_idle_streak"] is False
    assert reset_after_quiet.score_detail["effective_idle_streak"] == 0
    assert reset_after_quiet.score_detail["idle_backoff_until"] is None
    assert reset_after_quiet.score_detail["idle_backoff_remaining_seconds"] == 0


def test_idle_backoff_uses_bounded_gentle_ladder() -> None:
    pending = [message("普通讨论")]

    expected_multipliers = (1, 1, 2, 3, 5, 8, 8, 8)
    for idle_streak, expected_multiplier in enumerate(expected_multipliers):
        decision = strategy().decide(
            "s1",
            pending,
            [],
            policy=policy(
                cooldown_seconds=30,
                idle_streak=idle_streak,
                last_external_message_at=NOW - timedelta(seconds=1),
            ),
            now=NOW,
        )

        assert decision.score_detail["idle_backoff_multiplier"] == expected_multiplier
        assert decision.score_detail["idle_backoff_seconds"] == (
            30 * expected_multiplier
        )


def test_idle_backoff_deadline_is_not_reset_by_each_external_message() -> None:
    backoff_until = NOW + timedelta(seconds=60)
    first = strategy().decide(
        "s1",
        [message("普通讨论", created_at=NOW + timedelta(seconds=10))],
        [],
        policy=policy(
            cooldown_seconds=60,
            idle_streak=1,
            idle_backoff_until=backoff_until,
            last_external_message_at=NOW,
        ),
        now=NOW + timedelta(seconds=10),
    )
    second = strategy().decide(
        "s1",
        [message("继续讨论", created_at=NOW + timedelta(seconds=20))],
        [],
        policy=policy(
            cooldown_seconds=60,
            idle_streak=1,
            idle_backoff_until=backoff_until,
            last_external_message_at=NOW + timedelta(seconds=10),
        ),
        now=NOW + timedelta(seconds=20),
    )

    assert first.score_detail["idle_backoff_remaining_seconds"] == 50
    assert second.score_detail["idle_backoff_remaining_seconds"] == 40
    assert second.score_detail["increment_idle_streak"] is False


def test_only_low_score_silence_requests_idle_streak_increment() -> None:
    low_score = strategy().decide(
        "s1",
        [message("嗯")],
        [],
        policy=policy(cooldown_seconds=0),
        now=NOW,
    )
    silent_mode = strategy().decide(
        "s1",
        [message("嗯")],
        [],
        policy=policy(
            mode=GroupParticipationMode.SILENT,
            cooldown_seconds=0,
        ),
        now=NOW,
    )

    assert low_score.action == DecisionAction.SILENCE
    assert low_score.score < low_score.threshold
    assert low_score.score_detail["increment_idle_streak"] is True
    assert silent_mode.action == DecisionAction.SILENCE
    assert silent_mode.score_detail["increment_idle_streak"] is False


def test_presence_penalty_only_counts_last_five_minutes() -> None:
    pending = [
        message("你觉得这个方案为什么会失败？能不能帮我看看", message_id=f"p{i}")
        for i in range(4)
    ]
    old_agent_messages = [
        message(
            "旧回复",
            message_id=f"a{i}",
            role=MessageRole.ASSISTANT,
            created_at=NOW - timedelta(minutes=6),
        )
        for i in range(8)
    ]
    decision = strategy().decide(
        "s1",
        pending,
        old_agent_messages,
        policy=policy(trigger_count=1, frequency_factor=1),
        now=NOW,
    )

    assert decision.score_detail["presence_message_count"] == 0
    assert decision.score_detail["assistant_ratio"] == 0


def test_media_placeholders_and_invisible_noise_do_not_create_reply_pressure() -> None:
    decision = strategy().decide(
        "s1",
        [
            message(
                (
                    "[图片:cat.png] [图片读取失败] [语音消息] "
                    "[forward消息] [QQ image/png：cat.png] "
                    "\u200b [CQ:image,file=abc] <sticker>"
                ),
                message_id=f"noise-{index}",
            )
            for index in range(8)
        ],
        [],
        policy=policy(trigger_count=1, frequency_factor=1),
        now=NOW,
    )

    assert decision.action == DecisionAction.SILENCE
    assert decision.score == 0
    assert decision.score_detail["semantic_message_count"] == 0
    assert decision.score_detail["removed_noise_message_count"] == 8


def test_questions_targeting_other_members_are_penalized() -> None:
    participants = [
        Participant(external_user_id="u1", display_name="小明"),
        Participant(external_user_id="u2", display_name="小红"),
    ]
    ordinary = strategy().decide(
        "s1",
        [message("你觉得这个方案为什么会失败？能不能帮忙看看")],
        [],
        policy=policy(trigger_count=1, frequency_factor=1),
        participants=participants,
        now=NOW,
    )
    mentioned = message("你觉得这个方案为什么会失败？能不能帮忙看看")
    mentioned.components.insert(
        0,
        MessageComponent(
            type=ComponentType.MENTION,
            target_id="u2",
            target_name="小红",
        ),
    )
    targeted = strategy().decide(
        "s1",
        [mentioned],
        [],
        policy=policy(trigger_count=1, frequency_factor=1),
        participants=participants,
        now=NOW,
    )
    named = strategy().decide(
        "s1",
        [message("小红你觉得这个方案为什么会失败？")],
        [],
        policy=policy(trigger_count=1, frequency_factor=1),
        participants=participants,
        now=NOW,
    )

    assert targeted.score < ordinary.score
    assert targeted.score_detail["other_target_penalty"] == 35
    assert named.score_detail["other_target_penalty"] == 35


def test_external_interval_ewma_projection_and_idle_compensation_are_frozen() -> None:
    decision = strategy().decide(
        "s1",
        [message("普通讨论", created_at=NOW)],
        [],
        policy=policy(
            cooldown_seconds=60,
            idle_streak=2,
            last_external_message_at=NOW - timedelta(seconds=30),
            external_interval_ewma_seconds=20,
            external_interval_sample_count=4,
        ),
        now=NOW,
    )

    assert decision.score_detail["external_interval_ewma_seconds"] == 20
    assert decision.score_detail["external_interval_sample_count"] == 4
    assert decision.score_detail[
        "projected_external_interval_ewma_seconds"
    ] == 22.5
    assert decision.score_detail[
        "projected_external_interval_sample_count"
    ] == 5
    assert decision.score_detail["idle_compensation_seconds"] == 10
