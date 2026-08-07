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


class SQLiteUnitOfWork:
    """为同一个 DatabaseStore 提供共享 SQLite 事务边界。"""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")

        @event.listens_for(self.engine.sync_engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _) -> None:
            """SQLite 每条连接都必须显式启用外键，不能依赖 ORM 声明。"""

            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
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
