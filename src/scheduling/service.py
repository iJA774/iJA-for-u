"""周期任务配置、权限与聊天控制工具的唯一应用服务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from application.events import EventHub
from config import AppSettings
from domain.errors import ConflictError, InputValidationError, NotFoundError
from domain.models import (
    ChatType,
    ParticipantRole,
    ScheduleStatus,
    ScheduleTask,
    utc_now,
)
from ports import OperationsRepository
from scheduling.recurrence import build_rule


class ScheduleService:
    """集中执行任务权限、规则校验和 optimistic revision。"""

    def __init__(
        self,
        settings: AppSettings,
        store: OperationsRepository,
        events: EventHub,
    ) -> None:
        self.settings = settings
        self.store = store
        self.events = events
        self._notify_changed: Callable[[], None] = lambda: None

    def set_change_notifier(self, notifier: Callable[[], None]) -> None:
        self._notify_changed = notifier

    async def require_manager(self, session_id: str, actor_id: str | None) -> None:
        if actor_id is None:
            raise InputValidationError("聊天任务操作缺少发送者身份")
        session = await self.store.get_session(session_id)
        if session is None:
            raise NotFoundError("会话不存在")
        member = next((item for item in session.participants if item.external_user_id == actor_id), None)
        if member is None:
            raise InputValidationError("发送者不是当前会话成员")
        if session.chat_type == ChatType.GROUP and member.role not in {
            ParticipantRole.OWNER,
            ParticipantRole.ADMIN,
        }:
            raise InputValidationError("只有群 owner/admin 可以管理周期任务")

    async def create(
        self,
        *,
        session_id: str,
        actor_id: str,
        title: str,
        instruction: str,
        source_text: str,
        timezone: str,
        dtstart: datetime,
        rrule: str,
    ) -> ScheduleTask:
        await self.require_manager(session_id, actor_id)
        active_count = await self.store.count_active_schedules(session_id)
        if active_count >= self.settings.schedule.max_active_per_session:
            raise ConflictError("当前会话活跃周期任务已达上限")
        start, normalized, rule = build_rule(
            dtstart=dtstart,
            timezone=timezone,
            rrule_text=rrule,
            minimum_interval_minutes=self.settings.schedule.minimum_interval_minutes,
        )
        now = utc_now()
        next_run = rule.after(now.astimezone(start.tzinfo), inc=True)
        if next_run is None:
            raise InputValidationError("周期规则没有未来执行时刻")
        schedule = ScheduleTask(
            session_id=session_id,
            created_by=actor_id,
            title=title,
            instruction=instruction,
            source_text=source_text,
            timezone=timezone,
            dtstart=start,
            rrule=normalized,
            next_run_at=next_run.astimezone(UTC),
        )
        await self.store.create_schedule(schedule)
        await self.events.publish("schedule.updated", schedule.model_dump(mode="json"))
        self._notify_changed()
        return schedule

    async def update(
        self,
        schedule_id: str,
        *,
        actor_id: str | None,
        expected_revision: int,
        title: str,
        instruction: str,
        source_text: str,
        timezone: str,
        dtstart: datetime,
        rrule: str,
    ) -> ScheduleTask:
        current = await self.store.get_schedule(schedule_id)
        if current is None or current.status == ScheduleStatus.DELETED:
            raise NotFoundError("周期任务不存在")
        if actor_id is not None:
            await self.require_manager(current.session_id, actor_id)
        start, normalized, rule = build_rule(
            dtstart=dtstart,
            timezone=timezone,
            rrule_text=rrule,
            minimum_interval_minutes=self.settings.schedule.minimum_interval_minutes,
        )
        next_run = rule.after(utc_now().astimezone(start.tzinfo), inc=True)
        if next_run is None:
            raise InputValidationError("周期规则没有未来执行时刻")
        current.title = title
        current.instruction = instruction
        current.source_text = source_text
        current.timezone = timezone
        current.dtstart = start
        current.rrule = normalized
        if current.status == ScheduleStatus.PAUSED:
            current.next_run_at = None
        else:
            current.status = ScheduleStatus.ACTIVE
            current.next_run_at = next_run.astimezone(UTC)
        updated = await self.store.update_schedule(current, expected_revision)
        await self.events.publish("schedule.updated", updated.model_dump(mode="json"))
        self._notify_changed()
        return updated

    async def set_status(
        self,
        schedule_id: str,
        status: ScheduleStatus,
        *,
        actor_id: str | None,
        expected_revision: int,
    ) -> ScheduleTask:
        current = await self.store.get_schedule(schedule_id)
        if current is None or current.status == ScheduleStatus.DELETED:
            raise NotFoundError("周期任务不存在")
        if actor_id is not None:
            await self.require_manager(current.session_id, actor_id)
        previous_status = current.status
        current.status = status
        if status in {ScheduleStatus.PAUSED, ScheduleStatus.DELETED}:
            current.next_run_at = None
        elif status == ScheduleStatus.ACTIVE:
            active_count = await self.store.count_active_schedules(current.session_id)
            if previous_status != ScheduleStatus.ACTIVE and (
                active_count >= self.settings.schedule.max_active_per_session
            ):
                raise ConflictError("当前会话活跃周期任务已达上限")
            _, _, rule = build_rule(
                dtstart=current.dtstart,
                timezone=current.timezone,
                rrule_text=current.rrule,
                minimum_interval_minutes=self.settings.schedule.minimum_interval_minutes,
            )
            next_run = rule.after(utc_now().astimezone(current.dtstart.tzinfo), inc=True)
            current.next_run_at = next_run.astimezone(UTC) if next_run else None
            current.consecutive_failures = 0
            if next_run is None:
                current.status = ScheduleStatus.COMPLETED
        updated = await self.store.update_schedule(current, expected_revision)
        await self.events.publish("schedule.updated", updated.model_dump(mode="json"))
        self._notify_changed()
        return updated

    async def list_for_tool(self, session_id: str, actor_id: str | None) -> list[ScheduleTask]:
        """在完成管理权限校验后列出当前会话的周期任务。"""

        await self.require_manager(session_id, actor_id)
        return await self.store.list_schedules(session_id)
