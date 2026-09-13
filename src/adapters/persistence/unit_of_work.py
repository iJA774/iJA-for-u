"""SQLite 连接、Session factory 与事务提交/回滚的唯一 owner。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_SQLITE_BUSY_TIMEOUT_MS = 15_000


class SQLiteUnitOfWork:
    """为同一个 DatabaseStore 提供共享 SQLite 事务边界。"""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path.as_posix()}",
            connect_args={"timeout": _SQLITE_BUSY_TIMEOUT_MS / 1000},
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def _configure_sqlite_connection(dbapi_connection, _) -> None:
            """为每条连接启用一致性与并发所需的 SQLite PRAGMA。"""

            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
                # 后台画像、记忆和清理会并发读写；WAL 允许读事务与写提交共存，
                # 避免数据生命周期清理在 Linux 调度下偶发 database is locked。
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """提交一个事务；任意取消或异常都先显式回滚再继续传播。"""

        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def close(self) -> None:
        """释放连接池；调用方不再直接管理 engine 生命周期。"""

        await self.engine.dispose()
