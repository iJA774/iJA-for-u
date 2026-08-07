"""使用最早到期时间和唤醒事件驱动的可恢复调度器。"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from application.events import EventHub
from application.service import ChatService
from config import AppSettings
from domain.errors import NotFoundError
from domain.models import ScheduleRun, ScheduleStatus, ScheduleTask, utc_now
from ports import OperationsRepository
from scheduling.recurrence import coalesced_due

logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ScheduleScheduler:
    """进程内唯一调度 owner；SQLite 记录保证执行幂等和重启恢复。"""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: OperationsRepository,
        chat: ChatService,
        events: EventHub,
    ) -> None:
        self.settings = settings
        self.store = store
        self.chat = chat
        self.events = events
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._running: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(settings.schedule.max_concurrency)

    @property
    def running(self) -> bool:
        """返回主调度循环是否仍存活。"""

        return self._loop_task is not None and not self._loop_task.done()

    async def start(self) -> None:
        for run in await self.store.list_recoverable_schedule_runs():
            schedule = await self.store.get_schedule(run.schedule_id)
            if schedule is not None and schedule.status != ScheduleStatus.DELETED:
                self._spawn(schedule, run)
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)

    def wake(self) -> None:
        self._wake.set()

    async def run_now(self, schedule_id: str) -> ScheduleRun:
        schedule = await self.store.get_schedule(schedule_id)
        if schedule is None or schedule.status == ScheduleStatus.DELETED:
            raise NotFoundError("周期任务不存在")
        run = ScheduleRun(
            schedule_id=schedule.id,
            session_id=schedule.session_id,
            scheduled_for=utc_now(),
        )
        run, created = await self.store.create_schedule_run(run)
        if created:
            await self.events.publish("schedule.run.updated", run.model_dump(mode="json"))
            self._spawn(schedule, run)
        return run

    async def _loop(self) -> None:
        while True:
            try:
                await self._dispatch_due()
                delay = await self._next_delay()
                self._wake.clear()
                if delay is None:
                    await self._wake.wait()
                else:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=delay)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("周期调度循环失败", extra={"session_id": "-", "turn_id": "-"})
                await asyncio.sleep(1)

    async def _dispatch_due(self) -> None:
        now = utc_now()
        for schedule in await self.store.list_schedules():
            if schedule.status != ScheduleStatus.ACTIVE:
                continue
            due = coalesced_due(schedule, now)
            if due is None:
                continue
            scheduled_for, next_future, missed = due
            schedule.next_run_at = next_future
            if next_future is None:
                schedule.status = ScheduleStatus.COMPLETED
            schedule.updated_at = now
            await self.store.save_schedule_runtime(schedule)
            run = ScheduleRun(
                schedule_id=schedule.id,
                session_id=schedule.session_id,
                scheduled_for=scheduled_for,
                missed_occurrences=missed,
            )
            run, created = await self.store.create_schedule_run(run)
            if created:
                await self.events.publish("schedule.run.updated", run.model_dump(mode="json"))
                self._spawn(schedule, run)

    async def _next_delay(self) -> float | None:
        now = utc_now()
        candidates = [
            _as_utc(item.next_run_at)
            for item in await self.store.list_schedules()
            if item.status == ScheduleStatus.ACTIVE and item.next_run_at is not None
        ]
        if not candidates:
            return None
        return max(0.0, (min(candidates) - now).total_seconds())

    def _spawn(self, schedule: ScheduleTask, run: ScheduleRun) -> None:
        task = asyncio.create_task(self._execute(schedule, run))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _execute(self, schedule: ScheduleTask, run: ScheduleRun) -> None:
        async with self._semaphore:
            await self.chat.execute_scheduled(schedule, run)
