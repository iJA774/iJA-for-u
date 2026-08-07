"""周期规则的集中校验与下次执行时间计算。"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulebase, rrulestr

from domain.errors import InputValidationError
from domain.models import ScheduleTask


def normalize_rrule(raw: str) -> str:
    """只接受一条 RFC 5545 RRULE，拒绝 RDATE/EXDATE 和多行注入。"""

    value = raw.strip().upper()
    if value.startswith("RRULE:"):
        value = value[6:]
    if not value or "\n" in value or "\r" in value or not re.fullmatch(r"[A-Z0-9=;,+-]+", value):
        raise InputValidationError("rrule 必须是一条合法的 RFC 5545 规则")
    if "FREQ=SECONDLY" in value:
        raise InputValidationError("不支持秒级周期任务")
    if "FREQ=" not in value:
        raise InputValidationError("rrule 缺少 FREQ")
    return value


def aware_in_timezone(value: datetime, timezone: str) -> datetime:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise InputValidationError(f"未知 IANA 时区: {timezone}") from exc
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def build_rule(
    *, dtstart: datetime, timezone: str, rrule_text: str, minimum_interval_minutes: int
) -> tuple[datetime, str, rrulebase]:
    """校验一次性或周期规则；周期任务的相邻执行不得短于安全下限。"""

    normalized = normalize_rrule(rrule_text)
    start = aware_in_timezone(dtstart, timezone)
    try:
        rule = rrulestr(f"RRULE:{normalized}", dtstart=start)
        first = rule.after(start - timedelta(microseconds=1), inc=True)
        second = rule.after(first, inc=False) if first is not None else None
    except (ValueError, TypeError) as exc:
        raise InputValidationError(f"rrule 无法解析: {exc}") from exc
    if first is None:
        raise InputValidationError("调度规则没有执行时刻")
    if second is not None and second - first < timedelta(minutes=minimum_interval_minutes):
        raise InputValidationError(f"周期不能短于 {minimum_interval_minutes} 分钟")
    return start, normalized, rule


def next_occurrence(schedule: ScheduleTask, after: datetime, *, inclusive: bool = False) -> datetime | None:
    _, _, rule = build_rule(
        dtstart=schedule.dtstart,
        timezone=schedule.timezone,
        rrule_text=schedule.rrule,
        minimum_interval_minutes=1,
    )
    local_after = aware_in_timezone(after, schedule.timezone)
    result = rule.after(local_after, inc=inclusive)
    return result.astimezone(UTC) if result is not None else None


def coalesced_due(schedule: ScheduleTask, now: datetime) -> tuple[datetime, datetime | None, int] | None:
    """把所有逾期时刻合并为最近一次，并给出下个未来时刻。"""

    if schedule.next_run_at is None:
        return None
    next_run = schedule.next_run_at
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=UTC)
    now_utc = now.astimezone(UTC)
    if next_run > now_utc:
        return None
    _, _, rule = build_rule(
        dtstart=schedule.dtstart,
        timezone=schedule.timezone,
        rrule_text=schedule.rrule,
        minimum_interval_minutes=1,
    )
    local_now = aware_in_timezone(now_utc, schedule.timezone)
    latest = rule.before(local_now, inc=True)
    if latest is None:
        return None
    next_future = rule.after(local_now, inc=False)
    occurrences = rule.between(aware_in_timezone(next_run, schedule.timezone), latest, inc=True)
    missed = max(0, min(10_000, len(occurrences) - 1))
    return (
        latest.astimezone(UTC),
        next_future.astimezone(UTC) if next_future is not None else None,
        missed,
    )
