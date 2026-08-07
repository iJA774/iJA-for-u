"""群聊参与策略使用的纯时间状态计算。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

EXTERNAL_INTERVAL_EWMA_ALPHA = 0.25
IDLE_BACKOFF_MULTIPLIERS = (1, 1, 2, 3, 5, 8)


def calculate_idle_backoff_seconds(cooldown_seconds: int, idle_streak: int) -> int:
    """按有界温和阶梯计算一次 idle 退让持续时间。"""

    if cooldown_seconds < 0:
        raise ValueError("基础冷却时间不能为负数")
    if idle_streak < 0:
        raise ValueError("连续 idle 次数不能为负数")
    multiplier = IDLE_BACKOFF_MULTIPLIERS[
        min(idle_streak, len(IDLE_BACKOFF_MULTIPLIERS) - 1)
    ]
    return cooldown_seconds * multiplier


@dataclass(frozen=True, slots=True)
class ExternalIntervalProjection:
    """一次外部消息观测投影出的可恢复间隔状态。"""

    observed_seconds: float | None
    ewma_seconds: float | None
    sample_count: int
    last_external_at: datetime


def project_external_interval(
    *,
    previous_external_at: datetime | None,
    external_at: datetime,
    current_ewma_seconds: float | None,
    current_sample_count: int,
) -> ExternalIntervalProjection:
    """按固定权重投影 EWMA；乱序消息不会倒退时间或污染样本。"""

    if current_sample_count < 0:
        raise ValueError("外部消息间隔样本数不能为负数")
    if (current_sample_count == 0) != (current_ewma_seconds is None):
        raise ValueError("外部消息间隔 EWMA 与样本数状态不一致")
    if current_ewma_seconds is not None and current_ewma_seconds < 0:
        raise ValueError("外部消息间隔 EWMA 不能为负数")
    if previous_external_at is None:
        return ExternalIntervalProjection(
            observed_seconds=None,
            ewma_seconds=current_ewma_seconds,
            sample_count=current_sample_count,
            last_external_at=external_at,
        )
    if external_at <= previous_external_at:
        return ExternalIntervalProjection(
            observed_seconds=None,
            ewma_seconds=current_ewma_seconds,
            sample_count=current_sample_count,
            last_external_at=previous_external_at,
        )

    observed_seconds = (external_at - previous_external_at).total_seconds()
    next_ewma = (
        observed_seconds
        if current_ewma_seconds is None
        else (
            EXTERNAL_INTERVAL_EWMA_ALPHA * observed_seconds
            + (1 - EXTERNAL_INTERVAL_EWMA_ALPHA) * current_ewma_seconds
        )
    )
    return ExternalIntervalProjection(
        observed_seconds=observed_seconds,
        ewma_seconds=next_ewma,
        sample_count=current_sample_count + 1,
        last_external_at=external_at,
    )
