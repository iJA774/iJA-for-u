"""统一拥有出站准备、不可逆发送和可见历史提交顺序。"""

from __future__ import annotations

import logging

from adapters.persistence import PersonaStore
from application.events import EventHub
from domain.models import (
    DeliveryReceipt,
    DeliveryStatus,
    MessageOrigin,
    OutboundMessage,
    SessionView,
    StoredMessage,
)
from ports import ChannelAdapter, OperationsRepository

logger = logging.getLogger(__name__)


class OutboundCoordinator:
    """四条链路共享的唯一出站副作用 owner。"""

    def __init__(
        self,
        *,
        store: OperationsRepository,
        channel: ChannelAdapter,
        personas: PersonaStore,
        events: EventHub,
    ) -> None:
        self.store = store
        self.channel = channel
        self.personas = personas
        self.events = events

    async def deliver(
        self, session: SessionView, message: OutboundMessage
    ) -> tuple[DeliveryReceipt, StoredMessage | None]:
        """以稳定 outbound ID 投递；不确定发送绝不自动重试。"""

        existing = await self.store.get_delivery(message.id)
        if existing is not None:
            if existing.status == DeliveryStatus.SENT:
                return existing, await self._commit(session, message)
            if existing.status == DeliveryStatus.DISPATCHING:
                unknown = DeliveryReceipt(
                    outbound_id=message.id,
                    status=DeliveryStatus.UNKNOWN,
                    error_code="delivery_outcome_unknown",
                    error_message="进程在发送开始后未能确认投递结果，已停止自动重试",
                )
                await self.store.save_delivery(message, unknown)
                await self.events.publish(
                    "delivery.updated", unknown.model_dump(mode="json")
                )
                return unknown, None
            if existing.status in {
                DeliveryStatus.BLOCKED,
                DeliveryStatus.UNKNOWN,
                DeliveryStatus.FAILED,
                DeliveryStatus.DROPPED,
            }:
                return existing, None
        else:
            prepared = DeliveryReceipt(
                outbound_id=message.id, status=DeliveryStatus.PREPARED
            )
            await self.store.save_delivery(message, prepared)
            await self.events.publish(
                "delivery.updated", prepared.model_dump(mode="json")
            )

        dispatching = DeliveryReceipt(
            outbound_id=message.id, status=DeliveryStatus.DISPATCHING
        )
        await self.store.save_delivery(message, dispatching)
        await self.events.publish(
            "delivery.updated", dispatching.model_dump(mode="json")
        )
        try:
            receipt = await self.channel.send(session, message)
        except Exception as exc:
            logger.exception(
                "Channel 发送结果不确定",
                extra={
                    "session_id": session.id,
                    "outbound_id": message.id,
                    "origin": message.origin.value,
                },
            )
            receipt = DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.UNKNOWN,
                error_code=getattr(exc, "code", "delivery_outcome_unknown"),
                error_message=str(exc)[:500],
            )
        await self.store.save_delivery(message, receipt)
        await self.events.publish(
            "delivery.updated", receipt.model_dump(mode="json")
        )
        if receipt.status != DeliveryStatus.SENT:
            return receipt, None
        return receipt, await self._commit(session, message)

    async def prepare_batch(
        self, messages: list[OutboundMessage]
    ) -> list[DeliveryReceipt]:
        """原子准备有序出站批次，并公开每条消息的初始状态。"""

        receipts = await self.store.prepare_outbound_batch(messages)
        for receipt in receipts:
            await self.events.publish(
                "delivery.updated", receipt.model_dump(mode="json")
            )
        return receipts

    async def promote_blocked(
        self, message: OutboundMessage
    ) -> DeliveryReceipt:
        """当前序消息送达后，显式开放下一条批次消息。"""

        receipt = await self.store.transition_delivery_to_prepared(
            message.id,
            allowed_statuses={DeliveryStatus.BLOCKED},
        )
        await self.events.publish(
            "delivery.updated", receipt.model_dump(mode="json")
        )
        return receipt

    async def retry_failed(
        self, message: OutboundMessage
    ) -> DeliveryReceipt:
        """只在人工重试路径把明确失败恢复为 PREPARED。"""

        receipt = await self.store.transition_delivery_to_prepared(
            message.id,
            allowed_statuses={DeliveryStatus.FAILED},
        )
        await self.events.publish(
            "delivery.updated", receipt.model_dump(mode="json")
        )
        return receipt

    async def recover(self) -> None:
        """启动时恢复非响应式链路；响应式批次由 ChatService 串行恢复。"""

        for message in await self.store.list_recoverable_outbound_messages():
            receipt = await self.store.get_delivery(message.id)
            if (
                message.origin == MessageOrigin.PROACTIVE
                and receipt is not None
                and receipt.status == DeliveryStatus.PREPARED
            ):
                # 主动链需要在恢复发送前重新检查策略、用户抢占和快照。
                # EngagementService 会在同一 Session 锁内完成该门控。
                continue
            session = await self.store.get_session(message.session_id)
            if session is None:
                logger.error(
                    "在途出站对应会话不存在",
                    extra={
                        "session_id": message.session_id,
                        "outbound_id": message.id,
                    },
                )
                continue
            await self.deliver(session, message)

    async def _commit(
        self,
        session: SessionView,
        message: OutboundMessage,
    ) -> StoredMessage:
        persona = self.personas.get_for_chat_type(session.chat_type)
        committed = await self.store.commit_outbound(message, persona.name)
        await self._mark_expression_used(message)
        await self.events.publish(
            "message.committed", committed.model_dump(mode="json")
        )
        return committed

    async def _mark_expression_used(self, message: OutboundMessage) -> None:
        expression_ids = {
            component.attachment_id
            for component in message.components
            if (component.attachment_id or "").startswith("expression_")
        }
        await self.store.record_expression_usage(
            message.id,
            {
                expression_id
                for expression_id in expression_ids
                if expression_id is not None
            },
        )
