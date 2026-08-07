"""周期任务及运行记录的 SQLite 仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from domain.errors import ConflictError, NotFoundError
from domain.models import (
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleStatus,
    ScheduleTask,
    utc_now,
)

from ..schema import ScheduleRow, ScheduleRunRow
from ._base import RepositoryMixinSupport


class ScheduleRepositoryMixin(RepositoryMixinSupport):
    """实现 Schedule 聚合的查询与命令，不单独拥有连接或事务工厂。"""

    async def create_schedule(self, schedule: ScheduleTask) -> ScheduleTask:
        async with self.session_factory() as db:
            db.add(self._schedule_to_row(schedule))
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise ConflictError("周期任务 ID 已存在") from exc
        return schedule

    async def get_schedule(self, schedule_id: str) -> ScheduleTask | None:
        async with self.session_factory() as db:
            row = await db.get(ScheduleRow, schedule_id)
            return self._schedule_from_row(row) if row else None

    async def list_schedules(
        self, session_id: str | None = None, *, include_deleted: bool = False
    ) -> list[ScheduleTask]:
        async with self.session_factory() as db:
            query = select(ScheduleRow)
            if session_id is not None:
                query = query.where(ScheduleRow.session_id == session_id)
            if not include_deleted:
                query = query.where(ScheduleRow.status != ScheduleStatus.DELETED.value)
            query = query.order_by(ScheduleRow.updated_at.desc())
            return [
                self._schedule_from_row(row)
                for row in (await db.execute(query)).scalars().all()
            ]

    async def count_active_schedules(self, session_id: str) -> int:
        schedules = await self.list_schedules(session_id)
        return sum(item.status == ScheduleStatus.ACTIVE for item in schedules)

    async def update_schedule(
        self, schedule: ScheduleTask, expected_revision: int
    ) -> ScheduleTask:
        """更新用户可编辑字段并递增 revision。"""

        async with self.session_factory() as db:
            row = await db.get(ScheduleRow, schedule.id)
            if row is None or row.status == ScheduleStatus.DELETED.value:
                raise NotFoundError("周期任务不存在")
            if row.revision != expected_revision:
                raise ConflictError(f"周期任务已被更新，当前 revision={row.revision}")
            row.title = schedule.title
            row.instruction = schedule.instruction
            row.source_text = schedule.source_text
            row.timezone = schedule.timezone
            row.dtstart = schedule.dtstart
            row.rrule = schedule.rrule
            row.status = schedule.status.value
            row.next_run_at = schedule.next_run_at
            row.revision += 1
            row.updated_at = utc_now()
            await db.commit()
            return self._schedule_from_row(row)

    async def save_schedule_runtime(self, schedule: ScheduleTask) -> None:
        """调度器更新运行字段，不制造用户配置 revision 冲突。"""

        async with self.session_factory() as db:
            row = await db.get(ScheduleRow, schedule.id)
            if row is None:
                raise NotFoundError("周期任务不存在")
            row.status = schedule.status.value
            row.next_run_at = schedule.next_run_at
            row.last_run_at = schedule.last_run_at
            row.consecutive_failures = schedule.consecutive_failures
            row.updated_at = schedule.updated_at
            await db.commit()

    async def create_schedule_run(
        self, run: ScheduleRun
    ) -> tuple[ScheduleRun, bool]:
        async with self.session_factory() as db:
            row = self._schedule_run_to_row(run)
            db.add(row)
            try:
                await db.commit()
                return run, True
            except IntegrityError:
                await db.rollback()
                existing = (
                    await db.execute(
                        select(ScheduleRunRow).where(
                            ScheduleRunRow.schedule_id == run.schedule_id,
                            ScheduleRunRow.scheduled_for == run.scheduled_for,
                        )
                    )
                ).scalar_one()
                return self._schedule_run_from_row(existing), False

    async def save_schedule_run(self, run: ScheduleRun) -> None:
        async with self.session_factory() as db:
            row = await db.get(ScheduleRunRow, run.id)
            if row is None:
                db.add(self._schedule_run_to_row(run))
            else:
                row.status = run.status.value
                row.missed_occurrences = run.missed_occurrences
                row.outbound_id = run.outbound_id
                row.error_code = run.error_code
                row.error_message = run.error_message
                row.started_at = run.started_at
                row.completed_at = run.completed_at
                row.updated_at = run.updated_at
            await db.commit()

    async def list_schedule_runs(self, schedule_id: str) -> list[ScheduleRun]:
        async with self.session_factory() as db:
            query = (
                select(ScheduleRunRow)
                .where(ScheduleRunRow.schedule_id == schedule_id)
                .order_by(ScheduleRunRow.scheduled_for.desc())
            )
            return [
                self._schedule_run_from_row(row)
                for row in (await db.execute(query)).scalars().all()
            ]

    async def list_recoverable_schedule_runs(self) -> list[ScheduleRun]:
        async with self.session_factory() as db:
            query = select(ScheduleRunRow).where(
                ScheduleRunRow.status.in_(
                    [
                        ScheduleRunStatus.PENDING.value,
                        ScheduleRunStatus.RUNNING.value,
                    ]
                )
            )
            return [
                self._schedule_run_from_row(row)
                for row in (await db.execute(query)).scalars().all()
            ]
