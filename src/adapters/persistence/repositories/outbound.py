"""响应式及后台链路共享的 Outbound SQLite 仓储实现。"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.errors import ConflictError, InputValidationError, NotFoundError
from domain.models import (
    ChatType,
    DecisionAction,
    DeliveryReceipt,
    DeliveryStatus,
    MessageOrigin,
    MessageRole,
    OutboundMessage,
    StoredMessage,
    new_id,
    utc_now,
)

from ..schema import (
    GroupParticipationPolicyRow,
    MessageRow,
    OutboundAttemptRow,
    SessionRow,
    TurnDecisionRow,
)
from ..search import message_search_text as _message_search_text
from ..serialization import (
    components_json as _components_json,
)
from ..serialization import (
    parse_components as _parse_components,
)
from ._base import RepositoryMixinSupport


def _committed_external_message_id(
    outbound_id: str,
    external_message_id: str | None,
) -> str:
    """为不返回消息 ID 的 Channel 生成稳定、逐出站唯一的历史键。"""

    return external_message_id or f"ija-outbound:{outbound_id}"


class OutboundRepositoryMixin(RepositoryMixinSupport):
    """实现出站准备、回执状态与真实 SENT 可见历史提交。"""

    async def save_delivery(self, message: OutboundMessage, receipt: DeliveryReceipt) -> None:
        async with self.session_factory() as db:
            existing = (
                await db.execute(
                    select(OutboundAttemptRow).where(OutboundAttemptRow.outbound_id == message.id)
                )
            ).scalar_one_or_none()
            if existing is None:
                existing = OutboundAttemptRow(
                    id=new_id("delivery"),
                    outbound_id=message.id,
                    session_id=message.session_id,
                    status=receipt.status.value,
                    components_json=_components_json(message.components),
                    reply_to_message_id=message.reply_to_message_id,
                    origin=message.origin.value,
                    origin_run_id=message.origin_run_id,
                    source_refs_json=json.dumps(message.source_refs, ensure_ascii=False),
                    external_message_id=receipt.external_message_id,
                    error_code=receipt.error_code,
                    error_message=receipt.error_message,
                    created_at=message.created_at,
                    delivered_at=receipt.delivered_at,
                    expression_usage_recorded=False,
                )
                db.add(existing)
            else:
                existing.status = receipt.status.value
                existing.origin = message.origin.value
                existing.origin_run_id = message.origin_run_id
                existing.source_refs_json = json.dumps(message.source_refs, ensure_ascii=False)
                existing.external_message_id = receipt.external_message_id
                existing.error_code = receipt.error_code
                existing.error_message = receipt.error_message
                existing.delivered_at = receipt.delivered_at
            await db.commit()

    async def prepare_outbound_batch(
        self, messages: list[OutboundMessage]
    ) -> list[DeliveryReceipt]:
        """原子持久化有序出站批次；只有首条立即具备发送资格。"""

        if not messages:
            raise InputValidationError("出站批次不能为空")
        identity = {
            (message.session_id, message.origin, message.origin_run_id)
            for message in messages
        }
        if len(identity) != 1:
            raise InputValidationError("同一出站批次必须属于同一 Session 和运行")
        if len({message.id for message in messages}) != len(messages):
            raise InputValidationError("出站批次不能包含重复 ID")

        receipts: list[DeliveryReceipt] = []
        async with self.session_factory() as db:
            for index, message in enumerate(messages):
                existing = (
                    await db.execute(
                        select(OutboundAttemptRow).where(
                            OutboundAttemptRow.outbound_id == message.id
                        )
                    )
                ).scalar_one_or_none()
                intended_status = (
                    DeliveryStatus.PREPARED
                    if index == 0
                    else DeliveryStatus.BLOCKED
                )
                if existing is None:
                    existing = OutboundAttemptRow(
                        id=new_id("delivery"),
                        outbound_id=message.id,
                        session_id=message.session_id,
                        status=intended_status.value,
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
                    db.add(existing)
                elif (
                    existing.session_id != message.session_id
                    or existing.components_json != _components_json(message.components)
                    or existing.reply_to_message_id != message.reply_to_message_id
                    or existing.origin != message.origin.value
                    or existing.origin_run_id != message.origin_run_id
                    or existing.source_refs_json
                    != json.dumps(message.source_refs, ensure_ascii=False)
                ):
                    raise ConflictError("稳定出站 ID 已绑定不同批次正文")
                receipts.append(
                    DeliveryReceipt(
                        outbound_id=existing.outbound_id,
                        status=DeliveryStatus(existing.status),
                        external_message_id=existing.external_message_id,
                        error_code=existing.error_code,
                        error_message=existing.error_message,
                        delivered_at=existing.delivered_at,
                    )
                )
            await db.commit()
        return receipts

    async def transition_delivery_to_prepared(
        self,
        outbound_id: str,
        *,
        allowed_statuses: set[DeliveryStatus],
    ) -> DeliveryReceipt:
        """按显式前态把已持久化正文推进回可发送的 PREPARED。"""

        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(OutboundAttemptRow).where(
                        OutboundAttemptRow.outbound_id == outbound_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("待推进的出站消息不存在")
            current = DeliveryStatus(row.status)
            if current not in allowed_statuses:
                raise ConflictError(
                    f"出站消息当前状态 {current.value} 不允许推进为 prepared"
                )
            row.status = DeliveryStatus.PREPARED.value
            row.external_message_id = None
            row.error_code = None
            row.error_message = None
            row.delivered_at = None
            await db.commit()
            return DeliveryReceipt(
                outbound_id=row.outbound_id,
                status=DeliveryStatus.PREPARED,
            )

    async def commit_outbound(self, message: OutboundMessage, sender_name: str) -> StoredMessage:
        """提交 SENT 正文；群普通回复冷却与首条可见历史同事务生效。"""

        async with self.session_factory() as db:
            attempt = (
                await db.execute(
                    select(OutboundAttemptRow).where(OutboundAttemptRow.outbound_id == message.id)
                )
            ).scalar_one_or_none()
            if attempt is None or attempt.status != DeliveryStatus.SENT.value:
                raise ConflictError("只有收到 sent 回执的出站消息才能提交到可见历史")
            session = await db.get(SessionRow, message.session_id)
            if session is None:
                raise NotFoundError("出站消息对应会话不存在")
            committed_external_id = _committed_external_message_id(
                message.id,
                attempt.external_message_id,
            )
            await self._record_group_reply_sent_in_transaction(
                db,
                session=session,
                message=message,
                sent_at=attempt.delivered_at or utc_now(),
            )
            existing = (
                await db.execute(
                    select(MessageRow).where(
                        MessageRow.platform == session.platform,
                        MessageRow.account_id == session.account_id,
                        MessageRow.external_message_id == committed_external_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                await db.commit()
                return self._message_from_row(existing)
            row = MessageRow(
                id=new_id("msg"),
                session_id=message.session_id,
                platform=session.platform,
                account_id=session.account_id,
                external_message_id=committed_external_id,
                role=MessageRole.ASSISTANT.value,
                sender_id="agent",
                sender_name=sender_name,
                components_json=_components_json(message.components),
                search_text=_message_search_text(message.components),
                processed_turn_id=None,
                origin=message.origin.value,
                origin_run_id=message.origin_run_id,
                source_refs_json=json.dumps(message.source_refs, ensure_ascii=False),
                created_at=attempt.delivered_at or utc_now(),
            )
            db.add(row)
            if session:
                session.updated_at = row.created_at
            await db.commit()
            return self._message_from_row(row)

    @staticmethod
    async def _record_group_reply_sent_in_transaction(
        db: AsyncSession,
        *,
        session: SessionRow,
        message: OutboundMessage,
        sent_at: datetime,
    ) -> None:
        """仅在真实 SENT 后按 Turn 幂等提交群普通回复冷却。"""

        if session.chat_type != ChatType.GROUP.value or message.origin != MessageOrigin.REACTIVE:
            return
        if message.origin_run_id is None:
            raise RuntimeError("群响应式出站缺少对应 Turn ID")
        decision = await db.get(TurnDecisionRow, message.origin_run_id)
        if decision is None or decision.session_id != session.id:
            raise RuntimeError("群响应式出站缺少同会话的 Turn 决策")
        if decision.action != DecisionAction.REPLY.value:
            raise RuntimeError("合法沉默的群 Turn 不允许提交响应式出站")
        if decision.group_participation_reply_recorded:
            return

        score_detail = json.loads(decision.score_detail_json)
        if not isinstance(score_detail, dict):
            raise RuntimeError("群 Turn 的评分明细损坏")
        if score_detail.get("forced") is not True:
            policy = await db.get(GroupParticipationPolicyRow, session.id)
            if policy is None:
                raise RuntimeError("群聊参与策略缺失，数据库迁移或数据不完整")
            policy.idle_streak = 0
            policy.idle_backoff_until = None
            policy.last_ordinary_reply_at = (
                sent_at
                if policy.last_ordinary_reply_at is None
                else max(policy.last_ordinary_reply_at, sent_at)
            )
            policy.state_version += 1
            policy.updated_at = max(policy.updated_at, sent_at)
        decision.group_participation_reply_recorded = True

    async def get_delivery(self, outbound_id: str) -> DeliveryReceipt | None:
        """按稳定出站 ID 查询最近回执，用于崩溃恢复时避免重复发送。"""

        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(OutboundAttemptRow).where(OutboundAttemptRow.outbound_id == outbound_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return DeliveryReceipt(
                outbound_id=row.outbound_id,
                status=DeliveryStatus(row.status),
                external_message_id=row.external_message_id,
                error_code=row.error_code,
                error_message=row.error_message,
                delivered_at=row.delivered_at,
            )

    async def get_outbound_message(self, outbound_id: str) -> OutboundMessage | None:
        """恢复已准备或已发送的出站正文。"""

        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(OutboundAttemptRow).where(OutboundAttemptRow.outbound_id == outbound_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return OutboundMessage(
                id=row.outbound_id,
                session_id=row.session_id,
                components=_parse_components(row.components_json),
                reply_to_message_id=row.reply_to_message_id,
                origin=MessageOrigin(row.origin or MessageOrigin.UNKNOWN.value),
                origin_run_id=row.origin_run_id,
                source_refs=json.loads(row.source_refs_json or "[]"),
                created_at=row.created_at,
            )

    async def list_reactive_outbound_batch(
        self, session_id: str, turn_id: str
    ) -> list[OutboundMessage]:
        """按稳定批次顺序读取一次响应式 Turn 的全部出站正文。"""

        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(OutboundAttemptRow)
                    .where(
                        OutboundAttemptRow.session_id == session_id,
                        OutboundAttemptRow.origin == MessageOrigin.REACTIVE.value,
                        OutboundAttemptRow.origin_run_id == turn_id,
                    )
                    .order_by(
                        OutboundAttemptRow.created_at.asc(),
                        OutboundAttemptRow.outbound_id.asc(),
                    )
                )
            ).scalars().all()
            return [self._outbound_from_row(row) for row in rows]

    async def list_recoverable_reactive_outbound_batches(
        self,
    ) -> list[list[OutboundMessage]]:
        """列出需要补提交、继续批次或收敛未知状态的响应式出站。"""

        async with self.session_factory() as db:
            keys = {
                (row.session_id, row.origin_run_id)
                for row in (
                    await db.execute(
                        select(OutboundAttemptRow).where(
                            OutboundAttemptRow.origin
                            == MessageOrigin.REACTIVE.value,
                            OutboundAttemptRow.status.in_(
                                [
                                    DeliveryStatus.BLOCKED.value,
                                    DeliveryStatus.PREPARED.value,
                                    DeliveryStatus.DISPATCHING.value,
                                ]
                            ),
                        )
                    )
                )
                .scalars()
                .all()
                if row.origin_run_id is not None
            }
            sent_rows = (
                await db.execute(
                    select(OutboundAttemptRow).where(
                        OutboundAttemptRow.origin == MessageOrigin.REACTIVE.value,
                        OutboundAttemptRow.status == DeliveryStatus.SENT.value,
                    )
                )
            ).scalars().all()
            for row in sent_rows:
                if row.origin_run_id is None:
                    continue
                committed_external_id = _committed_external_message_id(
                    row.outbound_id,
                    row.external_message_id,
                )
                committed = (
                    await db.execute(
                        select(MessageRow.id).where(
                            MessageRow.session_id == row.session_id,
                            MessageRow.role == MessageRole.ASSISTANT.value,
                            MessageRow.external_message_id
                            == committed_external_id,
                        )
                    )
                ).scalar_one_or_none()
                if committed is None:
                    keys.add((row.session_id, row.origin_run_id))

            batches: list[list[OutboundMessage]] = []
            for session_id, turn_id in sorted(keys):
                rows = (
                    await db.execute(
                        select(OutboundAttemptRow)
                        .where(
                            OutboundAttemptRow.session_id == session_id,
                            OutboundAttemptRow.origin
                            == MessageOrigin.REACTIVE.value,
                            OutboundAttemptRow.origin_run_id == turn_id,
                        )
                        .order_by(
                            OutboundAttemptRow.created_at.asc(),
                            OutboundAttemptRow.outbound_id.asc(),
                        )
                    )
                ).scalars().all()
                batches.append([self._outbound_from_row(row) for row in rows])
            return batches

    async def list_recoverable_outbound_messages(self) -> list[OutboundMessage]:
        """列出需要补发送、补提交或标记未知的在途出站。"""

        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(OutboundAttemptRow).where(
                        or_(
                            OutboundAttemptRow.origin.is_(None),
                            OutboundAttemptRow.origin
                            != MessageOrigin.REACTIVE.value,
                        ),
                        OutboundAttemptRow.status.in_(
                            [
                                DeliveryStatus.PREPARED.value,
                                DeliveryStatus.DISPATCHING.value,
                                DeliveryStatus.SENT.value,
                            ]
                        )
                    ).order_by(
                        OutboundAttemptRow.created_at.asc(),
                        OutboundAttemptRow.outbound_id.asc(),
                    )
                )
            ).scalars().all()
            return [
                OutboundMessage(
                    id=row.outbound_id,
                    session_id=row.session_id,
                    components=_parse_components(row.components_json),
                    reply_to_message_id=row.reply_to_message_id,
                    origin=MessageOrigin(
                        row.origin or MessageOrigin.UNKNOWN.value
                    ),
                    origin_run_id=row.origin_run_id,
                    source_refs=json.loads(row.source_refs_json or "[]"),
                    created_at=row.created_at,
                )
                for row in rows
            ]
