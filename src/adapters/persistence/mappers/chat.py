"""聊天、Turn 与出站 Row 的纯映射。"""

from __future__ import annotations

import json

from domain.models import MessageOrigin, MessageRole, OutboundMessage, StoredMessage

from ..schema import MessageRow, OutboundAttemptRow
from ..serialization import parse_components as _parse_components


def message_from_row(row: MessageRow) -> StoredMessage:
    return StoredMessage(
        id=row.id,
        session_id=row.session_id,
        role=MessageRole(row.role),
        sender_id=row.sender_id,
        sender_name=row.sender_name,
        external_message_id=row.external_message_id,
        components=_parse_components(row.components_json),
        created_at=row.created_at,
        processed_turn_id=row.processed_turn_id,
        origin=MessageOrigin(row.origin or MessageOrigin.UNKNOWN.value),
        origin_run_id=row.origin_run_id,
        source_refs=json.loads(row.source_refs_json or "[]"),
    )


def outbound_from_row(row: OutboundAttemptRow) -> OutboundMessage:
    """从出站尝试恢复不可变正文，不把回执状态混入消息协议。"""

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
