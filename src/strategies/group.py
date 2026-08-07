"""群聊必要性与每 Session 参与策略门控。"""

from datetime import datetime

from domain.group_participation import (
    calculate_idle_backoff_seconds,
    project_external_interval,
)
from domain.models import (
    DecisionAction,
    GroupParticipationMode,
    GroupParticipationPolicy,
    Participant,
    StoredMessage,
    TurnDecision,
    utc_now,
)
from strategies.reply_necessity import score_group_reply


class GroupChatStrategy:
    """先用代码门控决定是否值得调用模型。"""

    def __init__(
        self,
        *,
        threshold: int,
        trigger_count: int,
        frequency_factor: float,
        agent_id: str = "agent",
    ) -> None:
        self.threshold = threshold
        self.trigger_count = trigger_count
        self.frequency_factor = frequency_factor
        self.agent_id = agent_id

    def decide(
        self,
        session_id: str,
        pending: list[StoredMessage],
        recent_history: list[StoredMessage],
        *,
        policy: GroupParticipationPolicy | None = None,
        participants: list[Participant] | None = None,
        now: datetime | None = None,
    ) -> TurnDecision:
        """硬门优先；普通发言再应用冻结的 Session 策略与时间信号。"""

        snapshot_time = now or utc_now()
        effective_policy = policy or GroupParticipationPolicy(
            session_id=session_id,
            trigger_count=self.trigger_count,
            frequency_factor=self.frequency_factor,
        )
        threshold = self.threshold
        trigger_count = effective_policy.trigger_count
        frequency_factor = effective_policy.frequency_factor
        if effective_policy.mode == GroupParticipationMode.FOCUSED:
            threshold = max(0, threshold - 50)
            # 专注模式沿用原先“至少按 0.85 频率参与”的效果，但分别作用于
            # 两个独立参数：至多两条语义消息触发一次压力，评分倍率至少 0.925。
            trigger_count = min(2, trigger_count)
            frequency_factor = max(0.925, frequency_factor)
        score = score_group_reply(
            pending,
            recent_history,
            agent_id=self.agent_id,
            trigger_count=trigger_count,
            frequency_factor=frequency_factor,
            participants=participants,
            now=snapshot_time,
        )
        external_at = max((item.created_at for item in pending), default=snapshot_time)
        interval_projection = project_external_interval(
            previous_external_at=effective_policy.last_external_message_at,
            external_at=external_at,
            current_ewma_seconds=effective_policy.external_interval_ewma_seconds,
            current_sample_count=effective_policy.external_interval_sample_count,
        )
        external_interval_seconds = interval_projection.observed_seconds
        idle_streak = effective_policy.idle_streak
        idle_backoff_until = effective_policy.idle_backoff_until
        if external_interval_seconds is None or external_interval_seconds >= 300:
            idle_streak = 0
            idle_backoff_until = None
        backoff_seconds = calculate_idle_backoff_seconds(
            effective_policy.cooldown_seconds,
            idle_streak,
        )
        idle_backoff_multiplier = (
            backoff_seconds // effective_policy.cooldown_seconds
            if effective_policy.cooldown_seconds > 0
            else 0
        )
        idle_compensation_seconds = 0
        if (
            external_interval_seconds is not None
            and effective_policy.external_interval_ewma_seconds is not None
        ):
            # 当前间隔明显慢于该群自身节奏时，额外释放一部分退避；
            # 这只使用 Turn 开始时冻结的历史 EWMA，不依赖全局猜测。
            idle_compensation_seconds = min(
                backoff_seconds,
                max(
                    0,
                    int(
                        external_interval_seconds
                        - effective_policy.external_interval_ewma_seconds
                    ),
                ),
            )
        reply_cooldown_remaining = 0
        if effective_policy.last_ordinary_reply_at is not None:
            reply_cooldown_remaining = max(
                0,
                effective_policy.cooldown_seconds
                - int(
                    (
                        snapshot_time - effective_policy.last_ordinary_reply_at
                    ).total_seconds()
                ),
            )
        backoff_remaining = (
            0
            if idle_backoff_until is None
            else max(
                0,
                int((idle_backoff_until - snapshot_time).total_seconds())
                - idle_compensation_seconds,
            )
        )

        if score.forced:
            action = DecisionAction.REPLY
            reason = "直接 @ 或引用 Agent，强制回复"
        elif effective_policy.mode == GroupParticipationMode.SILENT:
            action = DecisionAction.SILENCE
            reason = "群聊参与模式为静默，普通发言合法沉默"
        elif reply_cooldown_remaining > 0:
            action = DecisionAction.SILENCE
            reason = f"普通回复冷却中，剩余 {reply_cooldown_remaining} 秒"
        elif backoff_remaining > 0:
            action = DecisionAction.SILENCE
            reason = f"连续 idle 退避中，剩余 {backoff_remaining} 秒"
        else:
            action = (
                DecisionAction.REPLY
                if score.score >= threshold
                else DecisionAction.SILENCE
            )
            reason = (
                f"必要性评分 {score.score} 达到阈值 {threshold}"
                if action == DecisionAction.REPLY
                else f"必要性评分 {score.score} 低于阈值 {threshold}，合法沉默"
            )
        increment_idle_streak = (
            action == DecisionAction.SILENCE
            and effective_policy.mode != GroupParticipationMode.SILENT
            and reply_cooldown_remaining == 0
            and backoff_remaining == 0
            and score.score < threshold
        )
        detail: dict[str, object] = dict(score.detail)
        detail.update(
            {
                "participation_mode": effective_policy.mode.value,
                "policy_revision": effective_policy.revision,
                "policy_state_version": effective_policy.state_version,
                "snapshot_time": snapshot_time.isoformat(),
                "external_message_at": external_at.isoformat(),
                "external_interval_seconds": external_interval_seconds,
                "external_interval_ewma_seconds": (
                    effective_policy.external_interval_ewma_seconds
                ),
                "external_interval_sample_count": (
                    effective_policy.external_interval_sample_count
                ),
                "projected_external_interval_ewma_seconds": (
                    interval_projection.ewma_seconds
                ),
                "projected_external_interval_sample_count": (
                    interval_projection.sample_count
                ),
                "idle_streak": effective_policy.idle_streak,
                "effective_idle_streak": idle_streak,
                "idle_backoff_until": (
                    idle_backoff_until.isoformat()
                    if idle_backoff_until is not None
                    else None
                ),
                "cooldown_seconds": effective_policy.cooldown_seconds,
                "reply_cooldown_remaining_seconds": reply_cooldown_remaining,
                "idle_backoff_multiplier": idle_backoff_multiplier,
                "idle_backoff_seconds": backoff_seconds,
                "idle_compensation_seconds": idle_compensation_seconds,
                "idle_backoff_remaining_seconds": backoff_remaining,
                "increment_idle_streak": increment_idle_streak,
                "effective_trigger_count": trigger_count,
                "effective_frequency_factor": frequency_factor,
                "effective_threshold": threshold,
            }
        )
        return TurnDecision(
            session_id=session_id,
            action=action,
            strategy="group_reply_necessity",
            score=score.score,
            threshold=threshold,
            reason=reason,
            score_detail=detail,
            trigger_message_id=score.trigger_message_id,
        )
