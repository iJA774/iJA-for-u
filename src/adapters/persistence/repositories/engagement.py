"""主动行为与群聊参与策略的 SQLite 仓储实现。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domain.errors import ConflictError, InputValidationError, NotFoundError
from domain.group_participation import (
    calculate_idle_backoff_seconds,
    project_external_interval,
)
from domain.models import (
    ChatType,
    EngagementPolicy,
    GroupParticipationMode,
    GroupParticipationPolicy,
    TurnDecision,
    utc_now,
)

from ..schema import EngagementPolicyRow, GroupParticipationPolicyRow, SessionRow
from ._base import RepositoryMixinSupport


class EngagementRepositoryMixin(RepositoryMixinSupport):
    """实现 Engagement 硬门控与群聊参与 CAS 状态。"""

    async def get_engagement_policy(
        self, session_id: str
    ) -> EngagementPolicy | None:
        """读取 Session 的主动行为硬门控。"""

        async with self.session_factory() as db:
            row = await db.get(EngagementPolicyRow, session_id)
            return self._policy_from_row(row) if row is not None else None

    async def list_engagement_policies(self) -> list[EngagementPolicy]:
        async with self.session_factory() as db:
            rows = (
                await db.execute(select(EngagementPolicyRow))
            ).scalars().all()
            return [self._policy_from_row(row) for row in rows]

    async def save_engagement_policy(
        self, policy: EngagementPolicy
    ) -> EngagementPolicy:
        """保存策略；群聊禁用约束由应用层在写入前校验。"""

        async with self.session_factory() as db:
            row = await db.get(EngagementPolicyRow, policy.session_id)
            if row is None:
                row = EngagementPolicyRow(session_id=policy.session_id)
                db.add(row)
            row.proactive_enabled = policy.proactive_enabled
            row.drift_enabled = policy.drift_enabled
            row.timezone = policy.timezone
            row.quiet_start = policy.quiet_start
            row.quiet_end = policy.quiet_end
            row.minimum_interval_minutes = policy.minimum_interval_minutes
            row.updated_at = policy.updated_at
            await db.commit()
            return self._policy_from_row(row)

    async def get_group_participation_policy(
        self, session_id: str
    ) -> GroupParticipationPolicy:
        """读取群聊参与配置和可恢复 idle 状态。"""

        async with self.session_factory() as db:
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise NotFoundError("会话不存在")
            if session.chat_type != ChatType.GROUP.value:
                raise InputValidationError("私聊会话没有群聊参与策略")
            row = await db.get(GroupParticipationPolicyRow, session_id)
            if row is None:
                raise RuntimeError("群聊参与策略缺失，数据库迁移或数据不完整")
            return self._group_participation_policy_from_row(row)

    async def update_group_participation_policy(
        self,
        session_id: str,
        *,
        mode: GroupParticipationMode,
        trigger_count: int,
        frequency_factor: float,
        cooldown_seconds: int,
        expected_revision: int,
    ) -> GroupParticipationPolicy:
        """用独立配置 revision 做 CAS，运行态推进不会制造 UI 冲突。"""

        now = utc_now()
        async with self.session_factory() as db:
            result = cast(
                CursorResult[Any],
                await db.execute(
                    update(GroupParticipationPolicyRow)
                    .where(
                        GroupParticipationPolicyRow.session_id == session_id,
                        GroupParticipationPolicyRow.revision
                        == expected_revision,
                    )
                    .values(
                        mode=mode.value,
                        trigger_count=trigger_count,
                        frequency_factor=frequency_factor,
                        cooldown_seconds=cooldown_seconds,
                        revision=expected_revision + 1,
                        updated_at=now,
                    )
                ),
            )
            if result.rowcount != 1:
                session = await db.get(SessionRow, session_id)
                if session is None:
                    raise NotFoundError("会话不存在")
                if session.chat_type != ChatType.GROUP.value:
                    raise InputValidationError("私聊会话没有群聊参与策略")
                raise ConflictError(
                    "群聊参与策略已被其他请求更新，请刷新后重试"
                )
            await db.commit()
        return await self.get_group_participation_policy(session_id)

    @staticmethod
    async def _advance_group_participation_in_transaction(
        db: AsyncSession,
        *,
        session_id: str,
        expected_state_version: int,
        decision: TurnDecision,
        external_at: datetime,
        observed_at: datetime,
    ) -> None:
        """在 Turn Unit of Work 内推进外部节奏和 idle，不预写发送结果。"""

        row = await db.get(GroupParticipationPolicyRow, session_id)
        if row is None:
            raise RuntimeError("群聊参与策略缺失，数据库迁移或数据不完整")
        projection = project_external_interval(
            previous_external_at=row.last_external_message_at,
            external_at=external_at,
            current_ewma_seconds=row.external_interval_ewma_seconds,
            current_sample_count=row.external_interval_sample_count,
        )
        effective_streak = row.idle_streak
        effective_backoff_until = row.idle_backoff_until
        if row.last_external_message_at is None or (
            projection.observed_seconds is not None
            and projection.observed_seconds >= 300
        ):
            effective_streak = 0
            effective_backoff_until = None
        if decision.score_detail.get("increment_idle_streak") is True:
            next_streak = effective_streak + 1
            next_backoff_until = observed_at + timedelta(
                seconds=calculate_idle_backoff_seconds(
                    row.cooldown_seconds,
                    next_streak,
                )
            )
        else:
            # REPLY 仅是决策，不代表已经真实送达；冷却、已有 idle 退避、
            # 静默模式和待发送回复都不应改变 streak 或重置退让截止时间。
            # 真实 SENT 由出站事务统一清零。
            next_streak = effective_streak
            next_backoff_until = effective_backoff_until
        result = cast(
            CursorResult[Any],
            await db.execute(
                update(GroupParticipationPolicyRow)
                .where(
                    GroupParticipationPolicyRow.session_id == session_id,
                    GroupParticipationPolicyRow.state_version
                    == expected_state_version,
                )
                .values(
                    idle_streak=next_streak,
                    idle_backoff_until=next_backoff_until,
                    last_external_message_at=projection.last_external_at,
                    external_interval_ewma_seconds=projection.ewma_seconds,
                    external_interval_sample_count=projection.sample_count,
                    state_version=expected_state_version + 1,
                    updated_at=observed_at,
                )
            ),
        )
        if result.rowcount != 1:
            raise ConflictError(
                "群聊参与运行态已推进，拒绝覆盖较新的 idle 状态"
            )
