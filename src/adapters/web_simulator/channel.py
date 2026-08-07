"""把本地控制台视为真实 Channel，提供明确投递回执。"""

from domain.models import DeliveryReceipt, DeliveryStatus, OutboundMessage, SessionView, new_id, utc_now


class WebSimulatorChannel:
    """第一阶段的确定性本地出站通道。"""

    async def send(self, session: SessionView, message: OutboundMessage) -> DeliveryReceipt:
        return DeliveryReceipt(
            outbound_id=message.id,
            status=DeliveryStatus.SENT,
            external_message_id=new_id("webmsg"),
            delivered_at=utc_now(),
        )
