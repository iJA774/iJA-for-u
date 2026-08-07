"""Channel 插件入站消息的会话创建、成员同步与核心投递。"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from adapters.persistence import DatabaseStore
from application.service import ChatService, IngressResult
from domain.errors import ConflictError, InputValidationError
from domain.models import (
    ChatType,
    Participant,
    ParticipantRole,
    SessionResolver,
    SessionView,
)

from .capabilities import ChannelCapabilityRegistry
from .contracts import InboundEnvelope


class PlatformIngress:
    """统一拥有插件入站到核心会话的规范化边界。"""

    def __init__(
        self,
        store: DatabaseStore,
        chat: ChatService,
        capabilities: ChannelCapabilityRegistry,
    ) -> None:
        self.store = store
        self.chat = chat
        self.capabilities = capabilities
        self._route_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def accept(self, envelope: InboundEnvelope) -> IngressResult:
        """幂等创建会话、同步当前发送者后交给聊天核心。"""

        message = envelope.message
        rejected = [
            component.type.value
            for component in message.components
            if not self.capabilities.supports_ingress(
                message.platform,
                component.type,
            )
        ]
        if rejected:
            raise InputValidationError(
                "平台 capability 不允许 canonical 入站组件: "
                + ", ".join(sorted(set(rejected)))
            )
        # 黑名单拦截：在创建会话或同步成员前直接拒绝，避免为被拉黑用户写入副作用。
        if await self.store.is_blacklisted(
            message.platform, message.account_id, message.sender_id
        ):
            return IngressResult(accepted=False, control=True)
        session_id = SessionResolver.resolve(
            message.platform,
            message.account_id,
            message.external_chat_id,
            message.chat_type,
        )
        async with self._route_locks[session_id]:
            session = await self.store.get_session(session_id)
            participant = Participant(
                external_user_id=message.sender_id,
                display_name=message.sender_name,
                role=envelope.participant_role,
            )
            if session is None:
                if message.chat_type == ChatType.PRIVATE:
                    participant.role = ParticipantRole.OWNER
                elif participant.role != ParticipantRole.OWNER:
                    # 核心要求群聊始终有一个权限 owner。后续观察到平台群主时会迁移 owner。
                    participant.role = ParticipantRole.OWNER
                session = await self.chat.create_session(
                    chat_type=message.chat_type,
                    display_name=envelope.session_display_name,
                    external_chat_id=message.external_chat_id,
                    participants=[participant],
                    platform=message.platform,
                    account_id=message.account_id,
                )
            else:
                session = await self._sync_participant(session, participant)
        return await self.chat.ingest(
            message,
            schedule_turn=envelope.schedule_turn,
            schedule_duplicate=envelope.schedule_duplicate,
            debounce_seconds=envelope.debounce_seconds,
        )

    async def _sync_participant(self, session: SessionView, participant: Participant) -> SessionView:
        existing = next(
            (item for item in session.participants if item.external_user_id == participant.external_user_id),
            None,
        )
        if session.chat_type == ChatType.PRIVATE:
            participant.role = ParticipantRole.OWNER
        elif participant.role == ParticipantRole.OWNER:
            for item in session.participants:
                if item.role == ParticipantRole.OWNER:
                    item.role = ParticipantRole.MEMBER
        elif existing is not None and existing.role == ParticipantRole.OWNER:
            # 平台普通消息可能仍把发送者标成 member，不能因此撤销核心 owner。
            participant.role = ParticipantRole.OWNER
        elif not any(item.role == ParticipantRole.OWNER for item in session.participants):
            participant.role = ParticipantRole.OWNER

        if (
            existing is not None
            and existing.display_name == participant.display_name
            and existing.role == participant.role
        ):
            return session

        participants = [
            item for item in session.participants if item.external_user_id != participant.external_user_id
        ]
        participants.append(participant)
        try:
            return await self.chat.update_session_members(session.id, participants, session.revision)
        except ConflictError:
            # 同一路由进程内已串行；一次冲突只可能来自控制台并发修改。
            latest = await self.store.get_session(session.id)
            if latest is None:
                raise
            refreshed = [
                item for item in latest.participants if item.external_user_id != participant.external_user_id
            ]
            if latest.chat_type == ChatType.GROUP and participant.role == ParticipantRole.OWNER:
                for item in refreshed:
                    if item.role == ParticipantRole.OWNER:
                        item.role = ParticipantRole.MEMBER
            refreshed.append(participant)
            return await self.chat.update_session_members(latest.id, refreshed, latest.revision)
