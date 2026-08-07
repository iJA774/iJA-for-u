"""Session、Message 与 Turn 的 SQLite 仓储实现。"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from domain.errors import ConflictError, InputValidationError, NotFoundError
from domain.models import (
    ChatType,
    DecisionAction,
    GroupParticipationMode,
    InboundMessage,
    MemoryConsolidationRun,
    MessageComponent,
    MessageOrigin,
    MessageRole,
    Participant,
    ParticipantRole,
    SessionResolver,
    SessionView,
    StoredMessage,
    TurnDecision,
    new_id,
    utc_now,
)

from ..schema import (
    EngagementPolicyRow,
    GroupParticipationPolicyRow,
    IdentityRow,
    MemoryConsolidationRunRow,
    MessageRow,
    SessionMemberRow,
    SessionRow,
    TurnDecisionRow,
)
from ..search import (
    fts_phrase as _fts_phrase,
)
from ..search import (
    message_search_text as _message_search_text,
)
from ..search import (
    normalize_search_text as _normalize_search_text,
)
from ..serialization import (
    components_json as _components_json,
)
from ..serialization import (
    parse_components as _parse_components,
)
from ._base import RepositoryMixinSupport


class ChatRepositoryMixin(RepositoryMixinSupport):
    """实现会话、历史、Turn 消费和记忆 outbox 原子提交。"""

    async def create_session(
        self,
        *,
        platform: str,
        account_id: str,
        external_chat_id: str,
        chat_type: ChatType,
        display_name: str,
        participants: list[Participant],
        group_trigger_count: int = 3,
        group_frequency_factor: float = 0.9,
    ) -> SessionView:
        session_id = SessionResolver.resolve(platform, account_id, external_chat_id, chat_type)
        now = utc_now()
        async with self.session_factory() as db:
            existing = await db.get(SessionRow, session_id)
            if existing is None:
                existing = SessionRow(
                    id=session_id,
                    platform=platform,
                    account_id=account_id,
                    external_chat_id=external_chat_id,
                    chat_type=chat_type.value,
                    display_name=display_name,
                    revision=1,
                    data_epoch=1,
                    memory_cleared_at=None,
                    created_at=now,
                    updated_at=now,
                )
                db.add(existing)
                # ORM 没有声明对象关系，不能依赖 Unit of Work 猜测插入顺序。
                # 先落父行，确保随后创建的默认策略和成员满足真实外键约束。
                await db.flush()
                db.add(
                    EngagementPolicyRow(
                        session_id=session_id,
                        proactive_enabled=chat_type == ChatType.PRIVATE,
                        drift_enabled=True,
                        timezone="Asia/Shanghai",
                        quiet_start="22:00",
                        quiet_end="08:00",
                        minimum_interval_minutes=240,
                        updated_at=now,
                    )
                )
                if chat_type == ChatType.GROUP:
                    db.add(
                        GroupParticipationPolicyRow(
                            session_id=session_id,
                            mode=GroupParticipationMode.NORMAL.value,
                            trigger_count=group_trigger_count,
                            frequency_factor=group_frequency_factor,
                            cooldown_seconds=60,
                            idle_streak=0,
                            idle_backoff_until=None,
                            last_ordinary_reply_at=None,
                            last_external_message_at=None,
                            external_interval_ewma_seconds=None,
                            external_interval_sample_count=0,
                            revision=1,
                            state_version=1,
                            updated_at=now,
                        )
                    )
            else:
                existing.display_name = display_name
                existing.revision += 1
                existing.updated_at = now
            for participant in participants:
                await self._upsert_member(db, existing, participant, now)
            await db.commit()
        result = await self.get_session(session_id)
        if result is None:
            raise RuntimeError("会话创建后无法读取")
        return result

    async def _upsert_member(
        self, db: AsyncSession, session: SessionRow, participant: Participant, now: datetime
    ) -> None:
        query = select(IdentityRow).where(
            IdentityRow.platform == session.platform,
            IdentityRow.account_id == session.account_id,
            IdentityRow.external_user_id == participant.external_user_id,
        )
        identity = (await db.execute(query)).scalar_one_or_none()
        if identity is None:
            identity = IdentityRow(
                id=new_id("identity"),
                platform=session.platform,
                account_id=session.account_id,
                external_user_id=participant.external_user_id,
                display_name=participant.display_name,
                created_at=now,
                updated_at=now,
            )
            db.add(identity)
            await db.flush()
        elif identity.display_name != participant.display_name:
            identity.display_name = participant.display_name
            identity.updated_at = now
        member_query = select(SessionMemberRow).where(
            SessionMemberRow.session_id == session.id, SessionMemberRow.identity_id == identity.id
        )
        member = (await db.execute(member_query)).scalar_one_or_none()
        if member is None:
            db.add(
                SessionMemberRow(
                    id=new_id("member"),
                    session_id=session.id,
                    identity_id=identity.id,
                    role=participant.role.value,
                    joined_at=now,
                )
            )
        else:
            member.role = participant.role.value

    async def list_sessions(self) -> list[SessionView]:
        async with self.session_factory() as db:
            result = await db.execute(select(SessionRow).order_by(SessionRow.updated_at.desc()))
            rows = result.scalars().all()
            return [await self._session_view(db, row) for row in rows]

    async def get_session(self, session_id: str) -> SessionView | None:
        async with self.session_factory() as db:
            row = await db.get(SessionRow, session_id)
            return await self._session_view(db, row) if row else None

    async def get_session_by_route(
        self, platform: str, account_id: str, external_chat_id: str, chat_type: ChatType
    ) -> SessionView | None:
        """通过唯一 SessionResolver 解析路由，禁止调用点重复拼接。"""

        session_id = SessionResolver.resolve(platform, account_id, external_chat_id, chat_type)
        return await self.get_session(session_id)

    async def advance_session_data_epochs(
        self,
        session_ids: list[str],
    ) -> dict[str, int]:
        """原子推进数据生命周期版本，使旧后台快照立即失去提交资格。"""

        unique_ids = sorted(set(session_ids))
        if not unique_ids:
            return {}
        async with self.session_factory() as db:
            rows = (await db.execute(select(SessionRow).where(SessionRow.id.in_(unique_ids)))).scalars().all()
            by_id = {row.id: row for row in rows}
            missing = [session_id for session_id in unique_ids if session_id not in by_id]
            if missing:
                raise NotFoundError(f"会话不存在，无法推进数据版本: {', '.join(missing)}")
            now = utc_now()
            for row in rows:
                row.data_epoch += 1
                row.updated_at = now
            await db.commit()
            return {row.id: row.data_epoch for row in rows}

    async def session_epoch_is_current(
        self,
        session_id: str,
        data_epoch: int,
    ) -> bool:
        """检查后台任务冻结的 epoch 是否仍可写入。"""

        async with self.session_factory() as db:
            row = await db.get(SessionRow, session_id)
            return row is not None and row.data_epoch == data_epoch

    async def _session_view(self, db: AsyncSession, row: SessionRow) -> SessionView:
        query = (
            select(IdentityRow, SessionMemberRow.role)
            .join(SessionMemberRow, SessionMemberRow.identity_id == IdentityRow.id)
            .where(SessionMemberRow.session_id == row.id)
            .order_by(IdentityRow.created_at)
        )
        identities = (await db.execute(query)).all()
        return SessionView(
            id=row.id,
            platform=row.platform,
            account_id=row.account_id,
            external_chat_id=row.external_chat_id,
            chat_type=ChatType(row.chat_type),
            display_name=row.display_name,
            participants=[
                Participant(
                    external_user_id=item.external_user_id,
                    display_name=item.display_name,
                    role=ParticipantRole(role),
                )
                for item, role in identities
            ],
            revision=row.revision,
            data_epoch=row.data_epoch,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def update_session_members(
        self, session_id: str, participants: list[Participant], expected_revision: int
    ) -> SessionView:
        """以 optimistic revision 原子替换模拟会话成员及角色。"""

        now = utc_now()
        async with self.session_factory() as db:
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise NotFoundError("会话不存在")
            if session.revision != expected_revision:
                raise ConflictError(f"会话已被更新，当前 revision={session.revision}")
            current = (
                (await db.execute(select(SessionMemberRow).where(SessionMemberRow.session_id == session_id)))
                .scalars()
                .all()
            )
            desired_ids: set[str] = set()
            for participant in participants:
                await self._upsert_member(db, session, participant, now)
                identity = (
                    await db.execute(
                        select(IdentityRow).where(
                            IdentityRow.platform == session.platform,
                            IdentityRow.account_id == session.account_id,
                            IdentityRow.external_user_id == participant.external_user_id,
                        )
                    )
                ).scalar_one()
                desired_ids.add(identity.id)
            for member in current:
                if member.identity_id not in desired_ids:
                    await db.delete(member)
            session.revision += 1
            session.updated_at = now
            await db.commit()
        result = await self.get_session(session_id)
        if result is None:
            raise RuntimeError("成员更新后无法读取会话")
        return result

    async def append_inbound(self, message: InboundMessage) -> tuple[StoredMessage, bool]:
        session_id = SessionResolver.resolve(
            message.platform, message.account_id, message.external_chat_id, message.chat_type
        )
        async with self.session_factory() as db:
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise NotFoundError("会话不存在，请先创建模拟会话")
            query = select(MessageRow).where(
                MessageRow.platform == message.platform,
                MessageRow.account_id == message.account_id,
                MessageRow.external_message_id == message.external_message_id,
            )
            duplicate = (await db.execute(query)).scalar_one_or_none()
            if duplicate is not None:
                return self._message_from_row(duplicate), False
            row = MessageRow(
                id=new_id("msg"),
                session_id=session_id,
                platform=message.platform,
                account_id=message.account_id,
                external_message_id=message.external_message_id,
                role=MessageRole.USER.value,
                sender_id=message.sender_id,
                sender_name=message.sender_name,
                components_json=_components_json(message.components),
                search_text=_message_search_text(message.components),
                processed_turn_id=None,
                origin=None,
                origin_run_id=None,
                source_refs_json=None,
                created_at=message.received_at,
            )
            session.updated_at = message.received_at
            db.add(row)
            await db.commit()
            return self._message_from_row(row), True

    async def get_message_by_external_id(
        self,
        *,
        platform: str,
        account_id: str,
        external_message_id: str,
    ) -> StoredMessage | None:
        """按平台原始标识解析消息引用，必须同时匹配机器人账号。"""

        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(MessageRow).where(
                        MessageRow.platform == platform,
                        MessageRow.account_id == account_id,
                        MessageRow.external_message_id == external_message_id,
                    )
                )
            ).scalar_one_or_none()
            return self._message_from_row(row) if row is not None else None

    async def list_messages(
        self, session_id: str, limit: int = 100, *, received_before: datetime | None = None
    ) -> list[StoredMessage]:
        async with self.session_factory() as db:
            query = select(MessageRow).where(MessageRow.session_id == session_id)
            if received_before is not None:
                query = query.where(MessageRow.created_at <= received_before)
            query = query.order_by(MessageRow.created_at.desc()).limit(limit)
            rows = list(reversed((await db.execute(query)).scalars().all()))
            return [self._message_from_row(row) for row in rows]

    async def search_session_messages(
        self,
        session_id: str,
        *,
        query_text: str,
        limit: int = 100,
    ) -> list[StoredMessage]:
        """按发送者名称或用户可见正文搜索当前会话保留的消息。"""

        normalized_query = _normalize_search_text(query_text)
        if not normalized_query:
            raise InputValidationError("搜索文本不能为空")
        if not 1 <= limit <= 500:
            raise InputValidationError("搜索结果数量必须在 1 到 500 之间")

        async with self.session_factory() as db:
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise NotFoundError("会话不存在")

            sender_condition = MessageRow.sender_name.icontains(
                normalized_query,
                autoescape=True,
            )
            parameters: dict[str, object] = {}
            fts_query = _fts_phrase(normalized_query)

            if self._message_fts_available and fts_query is not None:
                content_condition = text(
                    """
                    EXISTS (
                        SELECT 1
                        FROM message_search_fts
                        WHERE message_search_fts.message_id = messages.id
                          AND message_search_fts.session_id = :fts_session_id
                          AND message_search_fts MATCH :fts_query
                    )
                    """
                )
                parameters = {
                    "fts_session_id": session_id,
                    "fts_query": fts_query,
                }
            else:
                content_condition = MessageRow.search_text.contains(
                    normalized_query,
                    autoescape=True,
                )

            query = (
                select(MessageRow)
                .where(
                    MessageRow.session_id == session_id,
                    or_(sender_condition, content_condition),
                )
                .order_by(MessageRow.created_at.asc(), MessageRow.id.asc())
                .limit(limit)
            )
            rows = (await db.execute(query, parameters)).scalars().all()
            return [self._message_from_row(row) for row in rows]

    async def get_session_message_window(
        self,
        session_id: str,
        message_id: str,
        *,
        before: int = 50,
        after: int = 50,
    ) -> list[StoredMessage]:
        """按稳定时间顺序读取目标消息前后的有限上下文。"""

        if not 0 <= before <= 100 or not 0 <= after <= 100:
            raise InputValidationError("消息上下文数量必须在 0 到 100 之间")

        async with self.session_factory() as db:
            target = (
                await db.execute(
                    select(MessageRow).where(
                        MessageRow.id == message_id,
                        MessageRow.session_id == session_id,
                    )
                )
            ).scalar_one_or_none()
            if target is None:
                raise NotFoundError("消息不存在")

            earlier_rows = list(
                (
                    await db.execute(
                        select(MessageRow)
                        .where(
                            MessageRow.session_id == session_id,
                            or_(
                                MessageRow.created_at < target.created_at,
                                and_(
                                    MessageRow.created_at == target.created_at,
                                    MessageRow.id < target.id,
                                ),
                            ),
                        )
                        .order_by(
                            MessageRow.created_at.desc(),
                            MessageRow.id.desc(),
                        )
                        .limit(before)
                    )
                )
                .scalars()
                .all()
            )
            earlier_rows.reverse()

            later_rows = list(
                (
                    await db.execute(
                        select(MessageRow)
                        .where(
                            MessageRow.session_id == session_id,
                            or_(
                                MessageRow.created_at > target.created_at,
                                and_(
                                    MessageRow.created_at == target.created_at,
                                    MessageRow.id > target.id,
                                ),
                            ),
                        )
                        .order_by(
                            MessageRow.created_at.asc(),
                            MessageRow.id.asc(),
                        )
                        .limit(after)
                    )
                )
                .scalars()
                .all()
            )

            rows = [*earlier_rows, target, *later_rows]
            return [self._message_from_row(row) for row in rows]

    async def list_recallable_messages(
        self,
        session_id: str,
        limit: int = 100,
        *,
        received_before: datetime | None = None,
    ) -> list[StoredMessage]:
        """只返回最近一次记忆清空之后的消息，供所有 Agent 上下文使用。"""

        async with self.session_factory() as db:
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise NotFoundError("会话不存在")
            query = select(MessageRow).where(MessageRow.session_id == session_id)
            if session.memory_cleared_at is not None:
                query = query.where(
                    MessageRow.created_at > session.memory_cleared_at
                )
            if received_before is not None:
                query = query.where(MessageRow.created_at <= received_before)
            query = query.order_by(MessageRow.created_at.desc()).limit(limit)
            rows = list(reversed((await db.execute(query)).scalars().all()))
            return [self._message_from_row(row) for row in rows]

    async def page_recallable_messages(
        self,
        session_id: str,
        *,
        limit: int,
        cursor_created_at: datetime | None = None,
        cursor_message_id: str | None = None,
        query_text: str | None = None,
    ) -> list[StoredMessage]:
        """按稳定倒序游标分页读取当前可召回窗口，可选全文子串过滤。"""

        async with self.session_factory() as db:
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise NotFoundError("会话不存在")
            query = select(MessageRow).where(MessageRow.session_id == session_id)
            if session.memory_cleared_at is not None:
                query = query.where(MessageRow.created_at > session.memory_cleared_at)
            if cursor_created_at is not None:
                if not cursor_message_id:
                    raise InputValidationError("历史游标缺少 message_id")
                query = query.where(
                    or_(
                        MessageRow.created_at < cursor_created_at,
                        and_(
                            MessageRow.created_at == cursor_created_at,
                            MessageRow.id < cursor_message_id,
                        ),
                    )
                )
            normalized_query = (query_text or "").strip()
            parameters: dict[str, object] = {}
            if normalized_query:
                fts_query = _fts_phrase(normalized_query)
                if self._message_fts_available and fts_query is not None:
                    # FTS 表只保存派生候选；会话、清空边界与稳定游标仍由权威表判断。
                    query = query.where(
                        text(
                            """
                            EXISTS (
                                SELECT 1
                                FROM message_search_fts
                                WHERE message_search_fts.message_id = messages.id
                                  AND message_search_fts.session_id = :fts_session_id
                                  AND message_search_fts MATCH :fts_query
                            )
                            """
                        )
                    )
                    parameters = {
                        "fts_session_id": session_id,
                        "fts_query": fts_query,
                    }
                else:
                    query = query.where(
                        MessageRow.search_text.contains(
                            _normalize_search_text(normalized_query),
                            autoescape=True,
                        )
                    )
            rows = (
                await db.execute(
                    query.order_by(MessageRow.created_at.desc(), MessageRow.id.desc()).limit(limit),
                    parameters,
                )
            ).scalars()
            return [self._message_from_row(row) for row in rows]

    async def get_recallable_message(
        self, session_id: str, message_id: str
    ) -> StoredMessage | None:
        """精确读取当前会话可召回窗口中的一条消息。"""

        async with self.session_factory() as db:
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise NotFoundError("会话不存在")
            query = select(MessageRow).where(
                MessageRow.session_id == session_id,
                MessageRow.id == message_id,
            )
            if session.memory_cleared_at is not None:
                query = query.where(MessageRow.created_at > session.memory_cleared_at)
            row = (await db.execute(query)).scalar_one_or_none()
            return self._message_from_row(row) if row is not None else None

    async def messages_are_recallable(
        self, session_id: str, message_ids: list[str]
    ) -> bool:
        """检查一批来源消息是否仍位于当前会话的可召回时间窗内。"""

        if not message_ids:
            return True
        async with self.session_factory() as db:
            session = await db.get(SessionRow, session_id)
            if session is None:
                return False
            query = select(MessageRow).where(
                MessageRow.id.in_(message_ids),
                MessageRow.session_id == session_id,
            )
            if session.memory_cleared_at is not None:
                query = query.where(
                    MessageRow.created_at > session.memory_cleared_at
                )
            rows = (await db.execute(query)).scalars().all()
            return len({row.id for row in rows}) == len(set(message_ids))

    async def get_message(self, message_id: str) -> StoredMessage | None:
        """按内部消息 ID 读取单条权威消息，供 Channel 解析平台回复引用。"""

        async with self.session_factory() as db:
            row = await db.get(MessageRow, message_id)
            return self._message_from_row(row) if row is not None else None

    async def get_session_attachment(
        self, session_id: str, attachment_id: str
    ) -> MessageComponent | None:
        """按会话和附件 ID 读取最近一次权威引用，禁止跨会话取附件。"""

        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(MessageRow)
                    .where(
                        MessageRow.session_id == session_id,
                        MessageRow.components_json.contains(attachment_id),
                    )
                    .order_by(MessageRow.created_at.desc())
                )
            ).scalars()
            for row in rows:
                for component in _parse_components(row.components_json):
                    if component.attachment_id == attachment_id:
                        return component
        return None

    async def list_pending_messages(
        self, session_id: str, *, received_before: datetime | None = None
    ) -> list[StoredMessage]:
        async with self.session_factory() as db:
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise NotFoundError("会话不存在")
            query = (
                select(MessageRow)
                .where(
                    MessageRow.session_id == session_id,
                    MessageRow.role == MessageRole.USER.value,
                    MessageRow.processed_turn_id.is_(None),
                )
                .order_by(MessageRow.created_at)
            )
            if session.memory_cleared_at is not None:
                query = query.where(
                    MessageRow.created_at > session.memory_cleared_at
                )
            if received_before is not None:
                query = query.where(MessageRow.created_at <= received_before)
            return [self._message_from_row(row) for row in (await db.execute(query)).scalars().all()]

    async def save_decision(self, decision: TurnDecision, message_ids: list[str]) -> None:
        async with self.session_factory() as db:
            await self._save_decision_in_transaction(db, decision, message_ids)
            await db.commit()

    async def save_reactive_turn_with_memory_run(
        self,
        decision: TurnDecision,
        message_ids: list[str],
        run: MemoryConsolidationRun,
    ) -> tuple[MemoryConsolidationRun, bool]:
        """原子提交响应式 Turn 与其记忆归档 outbox。

        用户消息一旦被标记为已处理，就必须已有可恢复的归档 run。否则进程若在
        两次独立提交之间崩溃，消息既不会再次进入 Turn，也无法在启动时补归档。
        """

        if run.session_id != decision.session_id:
            raise InputValidationError("Turn 决策与记忆归档任务不属于同一会话")
        if set(run.source_message_ids) != set(message_ids):
            raise InputValidationError("记忆归档任务来源必须等于本次 Turn 消息")
        async with self._unit_of_work.transaction() as db:
            existing = (
                await db.execute(
                    select(MemoryConsolidationRunRow).where(
                        MemoryConsolidationRunRow.source_fingerprint == run.source_fingerprint
                    )
                )
            ).scalar_one_or_none()
            saved = self._memory_run_from_row(existing) if existing is not None else run
            await self._save_decision_in_transaction(db, decision, message_ids)
            if existing is None:
                db.add(self._memory_run_to_row(run))
            return saved, existing is None

    async def save_group_reactive_turn(
        self,
        decision: TurnDecision,
        message_ids: list[str],
        *,
        expected_state_version: int,
        external_at: datetime,
        observed_at: datetime,
        run: MemoryConsolidationRun | None,
    ) -> tuple[MemoryConsolidationRun | None, bool]:
        """原子提交群决策、消息消费、时间状态与可选记忆 outbox。"""

        if run is not None:
            if run.session_id != decision.session_id:
                raise InputValidationError("Turn 决策与记忆归档任务不属于同一会话")
            if set(run.source_message_ids) != set(message_ids):
                raise InputValidationError("记忆归档任务来源必须等于本次 Turn 消息")
        async with self._unit_of_work.transaction() as db:
            existing: MemoryConsolidationRunRow | None = None
            if run is not None:
                existing = (
                    await db.execute(
                        select(MemoryConsolidationRunRow).where(
                            MemoryConsolidationRunRow.source_fingerprint == run.source_fingerprint
                        )
                    )
                ).scalar_one_or_none()
            saved = self._memory_run_from_row(existing) if existing is not None else run
            await self._save_decision_in_transaction(db, decision, message_ids)
            await self._advance_group_participation_in_transaction(
                db,
                session_id=decision.session_id,
                expected_state_version=expected_state_version,
                decision=decision,
                external_at=external_at,
                observed_at=observed_at,
            )
            if run is not None and existing is None:
                db.add(self._memory_run_to_row(run))
            return saved, run is not None and existing is None

    @staticmethod
    async def _save_decision_in_transaction(
        db: AsyncSession,
        decision: TurnDecision,
        message_ids: list[str],
    ) -> None:
        """在调用方事务内保存决策并消费其精确消息快照。"""

        db.add(
            TurnDecisionRow(
                id=decision.id,
                session_id=decision.session_id,
                action=decision.action.value,
                strategy=decision.strategy,
                score=decision.score,
                threshold=decision.threshold,
                reason=decision.reason,
                score_detail_json=json.dumps(decision.score_detail, ensure_ascii=False),
                trigger_message_id=decision.trigger_message_id,
                group_participation_reply_recorded=False,
                created_at=decision.created_at,
            )
        )
        if not message_ids:
            return
        query = select(MessageRow).where(
            MessageRow.id.in_(message_ids),
            MessageRow.session_id == decision.session_id,
            MessageRow.role == MessageRole.USER.value,
        )
        rows = (await db.execute(query)).scalars().all()
        if {row.id for row in rows} != set(message_ids):
            raise InputValidationError("Turn 决策包含不存在或越域的用户消息")
        for row in rows:
            if row.processed_turn_id not in {None, decision.id}:
                raise ConflictError("Turn 消息已被其他决策消费")
            row.processed_turn_id = decision.id

    async def list_decisions(self, session_id: str) -> list[TurnDecision]:
        async with self.session_factory() as db:
            query = (
                select(TurnDecisionRow)
                .where(TurnDecisionRow.session_id == session_id)
                .order_by(TurnDecisionRow.created_at.desc())
            )
            return [
                TurnDecision(
                    id=row.id,
                    session_id=row.session_id,
                    action=DecisionAction(row.action),
                    strategy=row.strategy,
                    score=row.score,
                    threshold=row.threshold,
                    reason=row.reason,
                    score_detail=json.loads(row.score_detail_json),
                    trigger_message_id=row.trigger_message_id,
                    created_at=row.created_at,
                )
                for row in (await db.execute(query)).scalars().all()
            ]

    async def get_decision(self, decision_id: str) -> TurnDecision | None:
        """读取单个已提交 Turn，供人工重试复用原始回复边界。"""

        async with self.session_factory() as db:
            row = await db.get(TurnDecisionRow, decision_id)
            if row is None:
                return None
            return TurnDecision(
                id=row.id,
                session_id=row.session_id,
                action=DecisionAction(row.action),
                strategy=row.strategy,
                score=row.score,
                threshold=row.threshold,
                reason=row.reason,
                score_detail=json.loads(row.score_detail_json),
                trigger_message_id=row.trigger_message_id,
                created_at=row.created_at,
            )

    async def list_turn_source_messages(
        self, session_id: str, decision_id: str
    ) -> list[StoredMessage]:
        """读取一个 Turn 被消费的精确用户消息快照。"""

        async with self.session_factory() as db:
            query = (
                select(MessageRow)
                .where(
                    MessageRow.session_id == session_id,
                    MessageRow.role == MessageRole.USER.value,
                    MessageRow.processed_turn_id == decision_id,
                )
                .order_by(MessageRow.created_at)
            )
            return [
                self._message_from_row(row)
                for row in (await db.execute(query)).scalars().all()
            ]

    async def has_committed_reactive_reply(
        self, session_id: str, decision_id: str
    ) -> bool:
        """确认原 Turn 是否已有真实送达并提交的响应，防止人工重复发送。"""

        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(MessageRow.id).where(
                        MessageRow.session_id == session_id,
                        MessageRow.role == MessageRole.ASSISTANT.value,
                        MessageRow.origin == MessageOrigin.REACTIVE.value,
                        MessageRow.origin_run_id == decision_id,
                    ).limit(1)
                )
            ).first()
            return row is not None
