import asyncio
from types import SimpleNamespace

import pytest

from proactive.scheduler import DriftScheduler, ProactiveScheduler


class SchedulerStore:
    """提供固定策略列表的最小调度存储桩。"""

    def __init__(self, *, proactive: bool, drift: bool) -> None:
        self.policy = SimpleNamespace(
            session_id="session_scheduler",
            proactive_enabled=proactive,
            drift_enabled=drift,
        )

    async def list_engagement_policies(self):
        return [self.policy]


class BlockingProactiveService:
    """让第一次轮询可控暂停，以验证运行期间的 wake 不会丢失。"""

    def __init__(self) -> None:
        self.store = SchedulerStore(proactive=True, drift=False)
        self.poll_count = 0
        self.first_poll_entered = asyncio.Event()
        self.release_first_poll = asyncio.Event()
        self.second_poll_entered = asyncio.Event()

    async def recover_proactive_runs(self) -> None:
        return None

    async def poll_due_feeds(self) -> None:
        self.poll_count += 1
        if self.poll_count == 1:
            self.first_poll_entered.set()
            await self.release_first_poll.wait()
        elif self.poll_count == 2:
            self.second_poll_entered.set()

    async def has_eligible_candidates(self, session_id: str) -> bool:
        return False

    async def run_proactive(self, session_id: str) -> None:
        raise AssertionError("没有候选时不得运行主动链")


class CountingDriftService:
    """记录 Drift tick 数量，验证 wake 被消费后不会永久忙循环。"""

    def __init__(self) -> None:
        self.store = SchedulerStore(proactive=False, drift=True)
        self.run_count = 0
        self.first_run = asyncio.Event()
        self.second_run = asyncio.Event()

    async def recover_drift_runs(self) -> None:
        return None

    async def run_drift(self, session_id: str) -> None:
        self.run_count += 1
        if self.run_count == 1:
            self.first_run.set()
        elif self.run_count == 2:
            self.second_run.set()


@pytest.mark.asyncio
async def test_proactive_wake_during_work_triggers_immediate_next_tick(
    settings,
) -> None:
    service = BlockingProactiveService()
    scheduler = ProactiveScheduler(settings, service)  # type: ignore[arg-type]
    await scheduler.start()
    try:
        await asyncio.wait_for(service.first_poll_entered.wait(), timeout=1)
        scheduler.wake()
        service.release_first_poll.set()
        await asyncio.wait_for(service.second_poll_entered.wait(), timeout=1)
        assert service.poll_count == 2
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_drift_wake_is_consumed_without_busy_loop(settings) -> None:
    service = CountingDriftService()
    scheduler = DriftScheduler(settings, service)  # type: ignore[arg-type]
    await scheduler.start()
    try:
        await asyncio.wait_for(service.first_run.wait(), timeout=1)
        scheduler.wake()
        await asyncio.wait_for(service.second_run.wait(), timeout=1)
        await asyncio.sleep(0.1)
        assert service.run_count == 2
    finally:
        await scheduler.stop()
