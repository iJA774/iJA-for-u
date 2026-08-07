"""Proactive 候选、运行及 Drift 审计的 SQLite 仓储实现。"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from domain.errors import ConflictError, InputValidationError, NotFoundError
from domain.models import (
    DeliveryReceipt,
    DeliveryStatus,
    DriftRun,
    DriftRunStatus,
    DriftStage,
    MessageOrigin,
    MessageRole,
    OutboundMessage,
    ProactiveCandidate,
    ProactiveCandidateStatus,
    ProactiveRun,
    ProactiveRunStatus,
    ProactiveStage,
    StoredMessage,
    new_id,
    utc_now,
)

from ..schema import (
    DriftRunRow,
    MessageRow,
    OutboundAttemptRow,
    ProactiveCandidateRow,
    ProactiveRunRow,
    SessionRow,
)
from ..serialization import components_json as _components_json
from ._base import RepositoryMixinSupport


class ProactiveRepositoryMixin(RepositoryMixinSupport):
    """实现主动链候选、可恢复投递和 Drift 到候选的原子提交。"""

    async def create_proactive_candidate(
        self, candidate: ProactiveCandidate
    ) -> tuple[ProactiveCandidate, bool]:
        """按 Session、来源类型和稳定来源键幂等写入候选。"""

        async with self.session_factory() as db:
            db.add(self._candidate_to_row(candidate))
            try:
                await db.commit()
                return candidate, True
            except IntegrityError:
                await db.rollback()
                existing = (
                    await db.execute(
                        select(ProactiveCandidateRow).where(
                            ProactiveCandidateRow.session_id == candidate.session_id,
                            ProactiveCandidateRow.source_kind
                            == candidate.source_kind.value,
                            ProactiveCandidateRow.source_key == candidate.source_key,
                        )
                    )
                ).scalar_one()
                merged_refs = sorted(
                    {
                        *json.loads(existing.source_refs_json),
                        *candidate.source_refs,
                    }
                )
                if merged_refs != json.loads(existing.source_refs_json):
                    existing.source_refs_json = json.dumps(
                        merged_refs, ensure_ascii=False
                    )
                    existing.updated_at = utc_now()
                    await db.commit()
                return self._candidate_from_row(existing), False

    async def get_proactive_candidate(
        self, candidate_id: str
    ) -> ProactiveCandidate | None:
        async with self.session_factory() as db:
            row = await db.get(ProactiveCandidateRow, candidate_id)
            return self._candidate_from_row(row) if row is not None else None

    async def list_proactive_candidates(
        self,
        session_id: str,
        *,
        statuses: set[ProactiveCandidateStatus] | None = None,
        limit: int = 200,
    ) -> list[ProactiveCandidate]:
        async with self.session_factory() as db:
            query = select(ProactiveCandidateRow).where(
                ProactiveCandidateRow.session_id == session_id
            )
            if statuses:
                query = query.where(
                    ProactiveCandidateRow.status.in_(
                        [status.value for status in statuses]
                    )
                )
            query = query.order_by(
                ProactiveCandidateRow.published_at.desc(),
                ProactiveCandidateRow.created_at.desc(),
            ).limit(limit)
            return [
                self._candidate_from_row(row)
                for row in (await db.execute(query)).scalars().all()
            ]

    async def save_proactive_candidate(
        self, candidate: ProactiveCandidate
    ) -> ProactiveCandidate:
        async with self.session_factory() as db:
            row = await db.get(ProactiveCandidateRow, candidate.id)
            if row is None:
                raise NotFoundError("主动候选不存在")
            values = self._candidate_to_row(candidate)
            for column in (
                "status",
                "decision_reason",
                "attempt_count",
                "available_at",
                "next_attempt_at",
                "expires_at",
                "updated_at",
                "source_refs_json",
                "parent_candidate_ids_json",
                "aggregation_key",
            ):
                setattr(row, column, getattr(values, column))
            await db.commit()
            return self._candidate_from_row(row)

    async def save_proactive_run(self, run: ProactiveRun) -> ProactiveRun:
        async with self.session_factory() as db:
            row = await db.get(ProactiveRunRow, run.id)
            values = self._proactive_run_to_row(run)
            if row is None:
                db.add(values)
            else:
                for column in (
                    "status",
                    "stage",
                    "gate_reason",
                    "decision_code",
                    "decision_reason",
                    "score",
                    "candidate_ids_json",
                    "candidate_id",
                    "snapshot_at",
                    "snapshot_message_id",
                    "manual_triggered",
                    "outbound_id",
                    "error_code",
                    "error_message",
                    "updated_at",
                ):
                    setattr(row, column, getattr(values, column))
            await db.commit()
            return run

    async def save_proactive_run_with_candidates(
        self,
        run: ProactiveRun,
        candidates: list[ProactiveCandidate],
    ) -> ProactiveRun:
        """在同一事务中提交主动运行阶段和候选状态，避免半完成批次。"""

        async with self.session_factory() as db:
            run_row = await db.get(ProactiveRunRow, run.id)
            run_values = self._proactive_run_to_row(run)
            if run_row is None:
                db.add(run_values)
            else:
                self._update_proactive_run_row(run_row, run_values)
            for candidate in candidates:
                candidate_row = await db.get(
                    ProactiveCandidateRow, candidate.id
                )
                if candidate_row is None:
                    raise NotFoundError("主动候选不存在")
                self._update_proactive_candidate_row(
                    candidate_row, self._candidate_to_row(candidate)
                )
            await db.commit()
            return run

    async def prepare_proactive_delivery(
        self,
        run: ProactiveRun,
        candidate: ProactiveCandidate,
        message: OutboundMessage,
    ) -> None:
        """原子保留候选并写入可恢复的 PREPARED 出站尝试。"""

        if (
            run.status != ProactiveRunStatus.PREPARED
            or run.stage != ProactiveStage.PREPARED
            or candidate.status != ProactiveCandidateStatus.PREPARED
            or run.candidate_id != candidate.id
            or run.outbound_id != message.id
        ):
            raise InputValidationError("主动 PREPARED 状态不完整")
        if (
            run.session_id != candidate.session_id
            or run.session_id != message.session_id
            or message.origin != MessageOrigin.PROACTIVE
            or message.origin_run_id != run.id
        ):
            raise InputValidationError("主动运行、候选与出站消息不属于同一执行")

        async with self.session_factory() as db:
            run_row = await db.get(ProactiveRunRow, run.id)
            if run_row is None:
                raise NotFoundError("主动运行不存在")
            if run_row.status != ProactiveRunStatus.RUNNING.value:
                raise ConflictError("主动运行已经离开可准备状态")
            candidate_row = await db.get(
                ProactiveCandidateRow, candidate.id
            )
            if candidate_row is None:
                raise NotFoundError("主动候选不存在")
            if candidate_row.status not in {
                ProactiveCandidateStatus.PENDING.value,
                ProactiveCandidateStatus.DEFERRED.value,
            }:
                raise ConflictError("主动候选已经被其他运行消费")
            existing = (
                await db.execute(
                    select(OutboundAttemptRow).where(
                        OutboundAttemptRow.outbound_id == message.id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ConflictError("主动出站 ID 已存在")

            self._update_proactive_run_row(
                run_row, self._proactive_run_to_row(run)
            )
            self._update_proactive_candidate_row(
                candidate_row, self._candidate_to_row(candidate)
            )
            db.add(
                OutboundAttemptRow(
                    id=new_id("delivery"),
                    outbound_id=message.id,
                    session_id=message.session_id,
                    status=DeliveryStatus.PREPARED.value,
                    components_json=_components_json(message.components),
                    reply_to_message_id=message.reply_to_message_id,
                    origin=message.origin.value,
                    origin_run_id=message.origin_run_id,
                    source_refs_json=json.dumps(
                        message.source_refs, ensure_ascii=False
                    ),
                    external_message_id=None,
                    error_code=None,
                    error_message=None,
                    created_at=message.created_at,
                    delivered_at=None,
                    expression_usage_recorded=False,
                )
            )
            await db.commit()

    async def drop_prepared_proactive_delivery(
        self,
        run: ProactiveRun,
        candidate: ProactiveCandidate,
        message: OutboundMessage,
        receipt: DeliveryReceipt,
    ) -> None:
        """原子撤销尚未发送的主动准备态，并重新开放候选。"""

        if receipt.status != DeliveryStatus.DROPPED:
            raise InputValidationError("撤销主动准备态必须使用 dropped 回执")
        async with self.session_factory() as db:
            run_row = await db.get(ProactiveRunRow, run.id)
            candidate_row = await db.get(
                ProactiveCandidateRow, candidate.id
            )
            attempt_row = (
                await db.execute(
                    select(OutboundAttemptRow).where(
                        OutboundAttemptRow.outbound_id == message.id
                    )
                )
            ).scalar_one_or_none()
            if run_row is None or candidate_row is None or attempt_row is None:
                raise NotFoundError("主动准备态记录不完整")
            if attempt_row.status != DeliveryStatus.PREPARED.value:
                raise ConflictError("只有尚未发送的主动准备态可以撤销")

            self._update_proactive_run_row(
                run_row, self._proactive_run_to_row(run)
            )
            self._update_proactive_candidate_row(
                candidate_row, self._candidate_to_row(candidate)
            )
            attempt_row.status = receipt.status.value
            attempt_row.error_code = receipt.error_code
            attempt_row.error_message = receipt.error_message
            attempt_row.delivered_at = receipt.delivered_at
            await db.commit()

    async def get_proactive_run(self, run_id: str) -> ProactiveRun | None:
        async with self.session_factory() as db:
            row = await db.get(ProactiveRunRow, run_id)
            return self._proactive_run_from_row(row) if row is not None else None

    async def list_proactive_runs(
        self, session_id: str, limit: int = 200
    ) -> list[ProactiveRun]:
        async with self.session_factory() as db:
            query = (
                select(ProactiveRunRow)
                .where(ProactiveRunRow.session_id == session_id)
                .order_by(ProactiveRunRow.created_at.desc())
                .limit(limit)
            )
            return [
                self._proactive_run_from_row(row)
                for row in (await db.execute(query)).scalars().all()
            ]

    async def page_sent_proactive_runs_for_memory_reconciliation(
        self,
        session_id: str,
        *,
        after_id: str | None = None,
        limit: int = 200,
    ) -> list[ProactiveRun]:
        """分页读取清空边界后的已发送运行，供派生记忆完整对账。"""

        if limit < 1 or limit > 1000:
            raise InputValidationError("主动运行对账分页条数必须在 1 到 1000 之间")
        async with self.session_factory() as db:
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise NotFoundError("会话不存在")
            query = select(ProactiveRunRow).where(
                ProactiveRunRow.session_id == session_id,
                ProactiveRunRow.status == ProactiveRunStatus.SENT.value,
                ProactiveRunRow.candidate_id.is_not(None),
            )
            if session.memory_cleared_at is not None:
                query = query.where(ProactiveRunRow.updated_at > session.memory_cleared_at)
            if after_id is not None:
                query = query.where(ProactiveRunRow.id > after_id)
            rows = (await db.execute(query.order_by(ProactiveRunRow.id.asc()).limit(limit))).scalars()
            return [self._proactive_run_from_row(row) for row in rows]

    async def get_recallable_proactive_message_by_run(
        self,
        session_id: str,
        run_id: str,
    ) -> StoredMessage | None:
        """精确读取清空边界后的主动助手消息，不依赖最近消息固定窗口。"""

        async with self.session_factory() as db:
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise NotFoundError("会话不存在")
            query = select(MessageRow).where(
                MessageRow.session_id == session_id,
                MessageRow.role == MessageRole.ASSISTANT.value,
                MessageRow.origin == MessageOrigin.PROACTIVE.value,
                MessageRow.origin_run_id == run_id,
            )
            if session.memory_cleared_at is not None:
                query = query.where(MessageRow.created_at > session.memory_cleared_at)
            row = (
                await db.execute(query.order_by(MessageRow.created_at.desc()).limit(1))
            ).scalar_one_or_none()
            return self._message_from_row(row) if row is not None else None

    async def list_recoverable_proactive_runs(self) -> list[ProactiveRun]:
        async with self.session_factory() as db:
            query = select(ProactiveRunRow).where(
                ProactiveRunRow.status.in_(
                    [
                        ProactiveRunStatus.RUNNING.value,
                        ProactiveRunStatus.PREPARED.value,
                    ]
                )
            ).order_by(
                ProactiveRunRow.created_at
            )
            return [
                self._proactive_run_from_row(row)
                for row in (await db.execute(query)).scalars().all()
            ]

    async def last_sent_proactive_run(
        self, session_id: str
    ) -> ProactiveRun | None:
        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(ProactiveRunRow)
                    .where(
                        ProactiveRunRow.session_id == session_id,
                        ProactiveRunRow.status == ProactiveRunStatus.SENT.value,
                    )
                    .order_by(ProactiveRunRow.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return self._proactive_run_from_row(row) if row is not None else None

    async def save_drift_run(self, run: DriftRun) -> DriftRun:
        async with self.session_factory() as db:
            row = await db.get(DriftRunRow, run.id)
            values = self._drift_run_to_row(run)
            if row is None:
                db.add(values)
            else:
                for column in (
                    "status",
                    "stage",
                    "activity",
                    "decision_reason",
                    "resume_payload_json",
                    "evidence_refs_json",
                    "produced_candidate_id",
                    "snapshot_at",
                    "snapshot_message_id",
                    "resumed_from_run_id",
                    "auto_resume_count",
                    "error_code",
                    "error_message",
                    "updated_at",
                ):
                    setattr(row, column, getattr(values, column))
            await db.commit()
            return run

    async def complete_drift_run_with_candidate(
        self,
        run: DriftRun,
        candidate: ProactiveCandidate,
    ) -> tuple[DriftRun, ProactiveCandidate, bool]:
        """在同一事务中幂等写入候选并提交 Drift 终态。"""

        if (
            run.status != DriftRunStatus.COMPLETED
            or run.stage != DriftStage.FINISHED
        ):
            raise InputValidationError("Drift 候选只能与 completed 终态一起提交")
        if candidate.session_id != run.session_id:
            raise InputValidationError("Drift 候选与运行不属于同一 Session")
        async with self.session_factory() as db:
            row = await db.get(DriftRunRow, run.id)
            if row is None:
                raise NotFoundError("Drift 运行不存在")
            if row.status != DriftRunStatus.RUNNING.value:
                raise ConflictError("Drift 运行已经收敛，不能重复提交终态")
            existing_row = (
                await db.execute(
                    select(ProactiveCandidateRow).where(
                        ProactiveCandidateRow.session_id == candidate.session_id,
                        ProactiveCandidateRow.source_kind
                        == candidate.source_kind.value,
                        ProactiveCandidateRow.source_key == candidate.source_key,
                    )
                )
            ).scalar_one_or_none()
            created = existing_row is None
            if existing_row is None:
                existing_row = self._candidate_to_row(candidate)
                db.add(existing_row)

            run.produced_candidate_id = existing_row.id
            values = self._drift_run_to_row(run)
            for column in (
                "status",
                "stage",
                "activity",
                "decision_reason",
                "resume_payload_json",
                "evidence_refs_json",
                "produced_candidate_id",
                "snapshot_at",
                "snapshot_message_id",
                "resumed_from_run_id",
                "auto_resume_count",
                "error_code",
                "error_message",
                "updated_at",
            ):
                setattr(row, column, getattr(values, column))
            await db.commit()
            return run, self._candidate_from_row(existing_row), created

    async def list_drift_runs(
        self, session_id: str, limit: int = 200
    ) -> list[DriftRun]:
        async with self.session_factory() as db:
            query = (
                select(DriftRunRow)
                .where(DriftRunRow.session_id == session_id)
                .order_by(DriftRunRow.created_at.desc())
                .limit(limit)
            )
            return [
                self._drift_run_from_row(row)
                for row in (await db.execute(query)).scalars().all()
            ]

    async def latest_drift_run(self, session_id: str) -> DriftRun | None:
        runs = await self.list_drift_runs(session_id, limit=1)
        return runs[0] if runs else None

    async def list_recoverable_drift_runs(self) -> list[DriftRun]:
        """读取重启前没有持久化终态的 Drift 运行。"""

        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(DriftRunRow)
                    .where(DriftRunRow.status == DriftRunStatus.RUNNING.value)
                    .order_by(DriftRunRow.created_at)
                )
            ).scalars().all()
            return [self._drift_run_from_row(row) for row in rows]
