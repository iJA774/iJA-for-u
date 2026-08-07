"""Proactive 与 Drift 各自独立的后台调度 owner。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from config import AppSettings
from domain.errors import IJAError
from domain.models import (
    CandidateSourceKind,
    EngagementPolicy,
    MessageRole,
    ProactiveCandidateStatus,
)
from proactive.service import EngagementService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PresenceSnapshot:
    """Wake 调度使用的瞬时存在状态，不是用户事实。"""

    session_id: str
    state: str
    energy: float
    next_check_seconds: int
    reason: str
    observed_at: str


class ProactiveScheduler:
    """轮询真实来源，并只为存在可用候选的私聊运行判断链。"""

    def __init__(
        self, settings: AppSettings, service: EngagementService
    ) -> None:
        self.settings = settings
        self.service = service
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._presence: dict[str, PresenceSnapshot] = {}
        self._last_wake_reason = "startup"

    @property
    def running(self) -> bool:
        """返回 Proactive 主循环是否仍存活。"""

        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self.service.recover_proactive_runs()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    def wake(self, reason: str = "external_change") -> None:
        """来源、策略或外部事件变化时立即打断 Presence 等待。"""

        self._last_wake_reason = reason
        self._wake.set()

    async def presence(self, session_id: str) -> PresenceSnapshot:
        """读取并刷新单个会话的派生 Presence 状态。"""

        policy = await self.service.get_policy(session_id)
        snapshot = await self._build_presence(policy)
        self._presence[session_id] = snapshot
        return snapshot

    async def refresh_presence(self) -> list[PresenceSnapshot]:
        """为所有开启主动触达的私聊重算 Wake 检查间隔。"""

        policies = [
            policy
            for policy in await self.service.store.list_engagement_policies()
            if policy.proactive_enabled
        ]
        snapshots = await asyncio.gather(
            *(self._build_presence(policy) for policy in policies)
        )
        current = {item.session_id: item for item in snapshots}
        for session_id, snapshot in current.items():
            previous = self._presence.get(session_id)
            if previous is None or (
                previous.state,
                previous.energy,
                previous.next_check_seconds,
                previous.reason,
            ) != (
                snapshot.state,
                snapshot.energy,
                snapshot.next_check_seconds,
                snapshot.reason,
            ):
                await self.service.events.publish(
                    "proactive.presence.updated", asdict(snapshot)
                )
        self._presence = current
        return snapshots

    async def _build_presence(self, policy: EngagementPolicy) -> PresenceSnapshot:
        """依据最近互动和候选紧急度计算下一次检查，不改变主动终态。"""

        base = self.settings.proactive.scheduler_interval_seconds
        now = datetime.now(UTC)
        pending = await self.service.store.list_pending_messages(policy.session_id)
        history = await self.service.store.list_recallable_messages(
            policy.session_id, limit=20
        )
        candidates = await self.service.store.list_proactive_candidates(
            policy.session_id,
            statuses={
                ProactiveCandidateStatus.PENDING,
                ProactiveCandidateStatus.DEFERRED,
            },
            limit=20,
        )
        has_alert = any(
            item.source_kind == CandidateSourceKind.ALERT for item in candidates
        )
        if pending:
            state, energy, delay, reason = (
                "engaged",
                0.05,
                min(3600, base * 4),
                "存在待处理用户消息，响应式链优先",
            )
        elif has_alert:
            state, energy, delay, reason = (
                "alert",
                1.0,
                10,
                "存在 alert 候选，尽快重新检查",
            )
        elif history:
            last = history[-1]
            created_at = (
                last.created_at.replace(tzinfo=UTC)
                if last.created_at.tzinfo is None
                else last.created_at.astimezone(UTC)
            )
            idle_seconds = max(0.0, (now - created_at).total_seconds())
            if idle_seconds < 30 * 60:
                state = (
                    "engaged"
                    if last.role == MessageRole.USER
                    else "cooling_down"
                )
                energy, delay = 0.15, min(3600, base * 3)
                reason = "刚发生会话互动，降低主动打扰频率"
            elif idle_seconds >= 12 * 60 * 60:
                state, energy, delay = "available", 0.85, max(10, base // 2)
                reason = "长时间无互动，提高候选检查频率"
            else:
                state, energy, delay = "balanced", 0.5, base
                reason = "会话处于普通空闲状态"
        else:
            state, energy, delay = "available", 0.8, max(10, base // 2)
            reason = "尚无互动历史，保持适度检查"
        return PresenceSnapshot(
            session_id=policy.session_id,
            state=state,
            energy=energy,
            next_check_seconds=delay,
            reason=reason,
            observed_at=now.isoformat(),
        )

    async def _loop(self) -> None:
        while True:
            # 先消费上一轮信号；本轮执行期间到达的新信号会让后续等待立即返回。
            self._wake.clear()
            try:
                await self.service.poll_due_feeds()
                policies = [
                    policy
                    for policy in (
                        await self.service.store.list_engagement_policies()
                    )
                    if policy.proactive_enabled
                ]
                semaphore = asyncio.Semaphore(
                    self.settings.proactive.max_concurrency
                )
                await asyncio.gather(
                    *(
                        self._run_policy(policy.session_id, semaphore)
                        for policy in policies
                    )
                )
                presence = await self.refresh_presence()
                wait_seconds = min(
                    (item.next_check_seconds for item in presence),
                    default=self.settings.proactive.scheduler_interval_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Proactive 调度循环失败",
                    extra={"session_id": "-", "turn_id": "-"},
                )
                wait_seconds = self.settings.proactive.scheduler_interval_seconds
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=wait_seconds,
                )
            except TimeoutError:
                pass

    async def _run_policy(
        self,
        session_id: str,
        semaphore: asyncio.Semaphore,
    ) -> None:
        """隔离单个会话故障，并限制模型与出站并发。"""

        async with semaphore:
            try:
                if await self.service.has_eligible_candidates(session_id):
                    await self.service.run_proactive(session_id)
            except Exception:
                logger.exception(
                    "Proactive 会话调度失败",
                    extra={"session_id": session_id},
                )


class DriftScheduler:
    """仅调度显式开启的私聊 Drift，不拥有任何出站能力。"""

    def __init__(
        self, settings: AppSettings, service: EngagementService
    ) -> None:
        self.settings = settings
        self.service = service
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    @property
    def running(self) -> bool:
        """返回 Drift 主循环是否仍存活。"""

        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self.service.recover_drift_runs()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    def wake(self) -> None:
        """策略变更或人工动作后提前重新检查。"""

        self._wake.set()

    async def _loop(self) -> None:
        while True:
            # 与 Proactive 相同：只消费上一轮信号，保留运行期间的新唤醒。
            self._wake.clear()
            try:
                for policy in await self.service.store.list_engagement_policies():
                    if not policy.drift_enabled:
                        continue
                    try:
                        await self.service.run_drift(policy.session_id)
                    except IJAError:
                        # 未达到空闲/间隔等合法门控不是调度故障。
                        continue
                    except Exception:
                        logger.exception(
                            "Drift 调度运行失败",
                            extra={"session_id": policy.session_id},
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Drift 调度循环失败",
                    extra={"session_id": "-", "turn_id": "-"},
                )
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self.settings.drift.scheduler_interval_seconds,
                )
            except TimeoutError:
                pass
