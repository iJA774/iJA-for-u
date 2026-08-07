from datetime import UTC, datetime, timedelta

import pytest

from domain.errors import InputValidationError
from domain.models import ScheduleTask
from scheduling.recurrence import build_rule, coalesced_due


def test_rrule_rejects_seconds_and_too_short_interval() -> None:
    now = datetime.now(UTC)
    with pytest.raises(InputValidationError, match="秒级"):
        build_rule(
            dtstart=now,
            timezone="Asia/Shanghai",
            rrule_text="FREQ=SECONDLY;INTERVAL=30",
            minimum_interval_minutes=5,
        )
    with pytest.raises(InputValidationError, match="不能短于"):
        build_rule(
            dtstart=now,
            timezone="Asia/Shanghai",
            rrule_text="FREQ=MINUTELY;INTERVAL=4",
            minimum_interval_minutes=5,
        )


def test_overdue_occurrences_are_coalesced_once() -> None:
    now = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
    schedule = ScheduleTask(
        session_id="s1",
        created_by="u1",
        title="测试",
        instruction="报时",
        source_text="每五分钟报时",
        timezone="UTC",
        dtstart=now - timedelta(minutes=30),
        rrule="FREQ=MINUTELY;INTERVAL=5",
        next_run_at=now - timedelta(minutes=20),
    )
    due = coalesced_due(schedule, now)
    assert due is not None
    scheduled_for, next_future, missed = due
    assert scheduled_for == now
    assert next_future == now + timedelta(minutes=5)
    assert missed == 4


def test_one_time_rule_has_one_due_occurrence_and_then_completes() -> None:
    scheduled_for = datetime(2026, 7, 24, 7, 30, tzinfo=UTC)
    start, normalized, rule = build_rule(
        dtstart=scheduled_for,
        timezone="Asia/Shanghai",
        rrule_text="RRULE:FREQ=DAILY;COUNT=1",
        minimum_interval_minutes=5,
    )
    assert normalized == "FREQ=DAILY;COUNT=1"
    assert rule.after(start - timedelta(microseconds=1), inc=True) == start
    assert rule.after(start, inc=False) is None

    schedule = ScheduleTask(
        session_id="s1",
        created_by="u1",
        title="单次提醒",
        instruction="提醒我打搅",
        source_text="15:30 提醒我打搅",
        timezone="Asia/Shanghai",
        dtstart=start,
        rrule=normalized,
        next_run_at=scheduled_for,
    )
    due = coalesced_due(schedule, scheduled_for)
    assert due == (scheduled_for, None, 0)
