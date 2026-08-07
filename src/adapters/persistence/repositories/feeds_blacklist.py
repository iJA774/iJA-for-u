"""订阅源与本地黑名单的 SQLite 仓储实现。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from domain.errors import ConflictError, NotFoundError
from domain.models import BlacklistEntry, FeedSource

from ..schema import BlacklistEntryRow, FeedSourceRow
from ._base import RepositoryMixinSupport


class FeedBlacklistRepositoryMixin(RepositoryMixinSupport):
    """实现相互独立的 Feed 与 Blacklist 叶子聚合。"""

    async def list_feed_sources(
        self, session_id: str | None = None
    ) -> list[FeedSource]:
        async with self.session_factory() as db:
            query = select(FeedSourceRow)
            if session_id is not None:
                query = query.where(FeedSourceRow.session_id == session_id)
            query = query.order_by(FeedSourceRow.created_at.desc())
            return [
                self._feed_from_row(row)
                for row in (await db.execute(query)).scalars().all()
            ]

    async def list_due_feed_sources(self, now: datetime) -> list[FeedSource]:
        async with self.session_factory() as db:
            query = (
                select(FeedSourceRow)
                .where(
                    FeedSourceRow.enabled.is_(True),
                    (
                        FeedSourceRow.next_poll_at.is_(None)
                        | (FeedSourceRow.next_poll_at <= now)
                    ),
                )
                .order_by(FeedSourceRow.next_poll_at.asc())
            )
            return [
                self._feed_from_row(row)
                for row in (await db.execute(query)).scalars().all()
            ]

    async def get_feed_source(self, feed_id: str) -> FeedSource | None:
        async with self.session_factory() as db:
            row = await db.get(FeedSourceRow, feed_id)
            return self._feed_from_row(row) if row is not None else None

    async def create_feed_source(self, source: FeedSource) -> FeedSource:
        async with self.session_factory() as db:
            db.add(self._feed_to_row(source))
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise ConflictError("当前私聊已订阅该 RSS/Atom 地址") from exc
            return source

    async def save_feed_source(self, source: FeedSource) -> FeedSource:
        async with self.session_factory() as db:
            row = await db.get(FeedSourceRow, source.id)
            if row is None:
                raise NotFoundError("订阅源不存在")
            for key, value in source.model_dump().items():
                setattr(row, key, value)
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise ConflictError("当前私聊已订阅该 RSS/Atom 地址") from exc
            return self._feed_from_row(row)

    async def delete_feed_source(self, feed_id: str) -> FeedSource:
        async with self.session_factory() as db:
            row = await db.get(FeedSourceRow, feed_id)
            if row is None:
                raise NotFoundError("订阅源不存在")
            source = self._feed_from_row(row)
            await db.delete(row)
            await db.commit()
            return source

    async def list_blacklist(self) -> list[BlacklistEntry]:
        """按创建时间倒序列出全部黑名单条目。"""

        async with self.session_factory() as db:
            query = select(BlacklistEntryRow).order_by(
                BlacklistEntryRow.created_at.desc()
            )
            return [
                self._blacklist_from_row(row)
                for row in (await db.execute(query)).scalars().all()
            ]

    async def get_blacklist(self, entry_id: str) -> BlacklistEntry | None:
        async with self.session_factory() as db:
            row = await db.get(BlacklistEntryRow, entry_id)
            return self._blacklist_from_row(row) if row is not None else None

    async def get_blacklist_by_route(
        self, platform: str, account_id: str, external_user_id: str
    ) -> BlacklistEntry | None:
        """按机器人账号+平台+外部用户读取黑名单条目。"""

        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(BlacklistEntryRow).where(
                        BlacklistEntryRow.platform == platform,
                        BlacklistEntryRow.account_id == account_id,
                        BlacklistEntryRow.external_user_id == external_user_id,
                    )
                )
            ).scalar_one_or_none()
            return self._blacklist_from_row(row) if row is not None else None

    async def is_blacklisted(
        self, platform: str, account_id: str, external_user_id: str
    ) -> bool:
        """判定某用户是否已被拉黑；拦截入站消息时使用。"""

        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(BlacklistEntryRow.id)
                    .where(
                        BlacklistEntryRow.platform == platform,
                        BlacklistEntryRow.account_id == account_id,
                        BlacklistEntryRow.external_user_id == external_user_id,
                    )
                    .limit(1)
                )
            ).first()
            return row is not None

    async def add_blacklist(self, entry: BlacklistEntry) -> BlacklistEntry:
        """幂等写入：同一用户已存在则更新原因、展示名与来源。"""

        async with self.session_factory() as db:
            existing = (
                await db.execute(
                    select(BlacklistEntryRow).where(
                        BlacklistEntryRow.platform == entry.platform,
                        BlacklistEntryRow.account_id == entry.account_id,
                        BlacklistEntryRow.external_user_id == entry.external_user_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(self._blacklist_to_row(entry))
            else:
                existing.display_name = entry.display_name
                existing.reason = entry.reason
                existing.source = entry.source.value
                existing.session_id = entry.session_id
            await db.commit()
        result = await self.get_blacklist_by_route(
            entry.platform,
            entry.account_id,
            entry.external_user_id,
        )
        if result is None:
            raise RuntimeError("黑名单写入后无法读取")
        return result

    async def remove_blacklist(self, entry_id: str) -> BlacklistEntry:
        async with self.session_factory() as db:
            row = await db.get(BlacklistEntryRow, entry_id)
            if row is None:
                raise NotFoundError("黑名单条目不存在")
            entry = self._blacklist_from_row(row)
            await db.delete(row)
            await db.commit()
            return entry
