"""SQLite 权威状态的单一 façade、连接与 Unit of Work owner。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .repositories import (
    AssetRepositoryMixin,
    AuditRepositoryMixin,
    ChatRepositoryMixin,
    DataLifecycleRepositoryMixin,
    EngagementRepositoryMixin,
    FeedBlacklistRepositoryMixin,
    OutboundRepositoryMixin,
    ProactiveRepositoryMixin,
    ProfileMemoryRepositoryMixin,
    ScheduleRepositoryMixin,
    SocialLearningRepositoryMixin,
)
from .schema import Base, ProfileExtractionRunRow
from .unit_of_work import SQLiteUnitOfWork

_FTS_TABLES = {"message_search_fts", "memory_search_fts"}
_FTS_TRIGGERS = {
    "messages_search_ai",
    "messages_search_au",
    "messages_search_ad",
    "memory_search_ai",
    "memory_search_au",
    "memory_search_ad",
}

_FTS_REBUILD_STATEMENTS = (
    """
    CREATE VIRTUAL TABLE message_search_fts USING fts5(
        message_id UNINDEXED,
        session_id UNINDEXED,
        created_at UNINDEXED,
        search_text,
        tokenize='trigram'
    )
    """,
    """
    CREATE VIRTUAL TABLE memory_search_fts USING fts5(
        memory_id UNINDEXED,
        scope_key UNINDEXED,
        status UNINDEXED,
        search_text,
        tokenize='trigram'
    )
    """,
    """
    INSERT INTO message_search_fts(
        rowid, message_id, session_id, created_at, search_text
    )
    SELECT rowid, id, session_id, created_at, search_text
    FROM messages
    """,
    """
    INSERT INTO memory_search_fts(
        rowid, memory_id, scope_key, status, search_text
    )
    SELECT rowid, id, scope_key, status, content
    FROM memory_records
    """,
    """
    CREATE TRIGGER messages_search_ai
    AFTER INSERT ON messages
    BEGIN
        INSERT INTO message_search_fts(
            rowid, message_id, session_id, created_at, search_text
        )
        VALUES (
            new.rowid, new.id, new.session_id, new.created_at, new.search_text
        );
    END
    """,
    """
    CREATE TRIGGER messages_search_au
    AFTER UPDATE OF session_id, created_at, search_text ON messages
    BEGIN
        DELETE FROM message_search_fts WHERE rowid = old.rowid;
        INSERT INTO message_search_fts(
            rowid, message_id, session_id, created_at, search_text
        )
        VALUES (
            new.rowid, new.id, new.session_id, new.created_at, new.search_text
        );
    END
    """,
    """
    CREATE TRIGGER messages_search_ad
    AFTER DELETE ON messages
    BEGIN
        DELETE FROM message_search_fts WHERE rowid = old.rowid;
    END
    """,
    """
    CREATE TRIGGER memory_search_ai
    AFTER INSERT ON memory_records
    BEGIN
        INSERT INTO memory_search_fts(
            rowid, memory_id, scope_key, status, search_text
        )
        VALUES (
            new.rowid, new.id, new.scope_key, new.status, new.content
        );
    END
    """,
    """
    CREATE TRIGGER memory_search_au
    AFTER UPDATE OF scope_key, status, content ON memory_records
    BEGIN
        DELETE FROM memory_search_fts WHERE rowid = old.rowid;
        INSERT INTO memory_search_fts(
            rowid, memory_id, scope_key, status, search_text
        )
        VALUES (
            new.rowid, new.id, new.scope_key, new.status, new.content
        );
    END
    """,
    """
    CREATE TRIGGER memory_search_ad
    AFTER DELETE ON memory_records
    BEGIN
        DELETE FROM memory_search_fts WHERE rowid = old.rowid;
    END
    """,
)

_MESSAGE_FTS_DIFFERENCE_QUERY = """
SELECT 1
FROM (
    SELECT rowid, id, session_id, created_at, search_text
    FROM messages
    EXCEPT
    SELECT rowid, message_id, session_id, created_at, search_text
    FROM message_search_fts
)
UNION ALL
SELECT 1
FROM (
    SELECT rowid, message_id, session_id, created_at, search_text
    FROM message_search_fts
    EXCEPT
    SELECT rowid, id, session_id, created_at, search_text
    FROM messages
)
LIMIT 1
"""

_MEMORY_FTS_DIFFERENCE_QUERY = """
SELECT 1
FROM (
    SELECT rowid, id, scope_key, status, content
    FROM memory_records
    EXCEPT
    SELECT rowid, memory_id, scope_key, status, search_text
    FROM memory_search_fts
)
UNION ALL
SELECT 1
FROM (
    SELECT rowid, memory_id, scope_key, status, search_text
    FROM memory_search_fts
    EXCEPT
    SELECT rowid, id, scope_key, status, content
    FROM memory_records
)
LIMIT 1
"""


class DatabaseStore(
    AssetRepositoryMixin,
    AuditRepositoryMixin,
    ChatRepositoryMixin,
    DataLifecycleRepositoryMixin,
    EngagementRepositoryMixin,
    FeedBlacklistRepositoryMixin,
    OutboundRepositoryMixin,
    ProfileMemoryRepositoryMixin,
    ProactiveRepositoryMixin,
    ScheduleRepositoryMixin,
    SocialLearningRepositoryMixin,
):
    """组合领域仓储并集中拥有唯一 SQLite engine 与事务工厂。"""

    def __init__(self, database_path: Path) -> None:
        self._unit_of_work = SQLiteUnitOfWork(database_path)
        self.engine: AsyncEngine = self._unit_of_work.engine
        self.session_factory = self._unit_of_work.session_factory
        self._message_fts_available = False
        self._memory_fts_available = False

    async def initialize(self) -> None:
        """创建开发态 Schema，并校验或修复可选 FTS 派生索引。"""

        async with self.engine.begin() as connection:
            enabled = await connection.exec_driver_sql("PRAGMA foreign_keys")
            if enabled.scalar_one() != 1:
                raise RuntimeError("SQLite 外键约束未启用，拒绝启动")
            await connection.run_sync(Base.metadata.create_all)
            objects = {
                (row.type, row.name)
                for row in (
                    await connection.exec_driver_sql(
                        """
                        SELECT type, name
                        FROM sqlite_master
                        WHERE (type = 'table'
                               AND name IN (
                                   'message_search_fts',
                                   'memory_search_fts'
                               ))
                           OR (type = 'trigger'
                               AND name IN (
                                   'messages_search_ai',
                                   'messages_search_au',
                                   'messages_search_ad',
                                   'memory_search_ai',
                                   'memory_search_au',
                                   'memory_search_ad'
                               ))
                        """
                    )
                ).all()
            }
            fts_tables = {
                name for object_type, name in objects if object_type == "table"
            }
            fts_triggers = {
                name for object_type, name in objects if object_type == "trigger"
            }
            if not fts_tables and not fts_triggers:
                # 旧库未迁移或 SQLite 不支持 FTS5/trigram 时，查询层显式完整降级。
                self._message_fts_available = False
                self._memory_fts_available = False
                return
            if not await self._fts_objects_are_complete(
                connection,
                tables=fts_tables,
                triggers=fts_triggers,
            ):
                await self._rebuild_fts_objects(connection)
                objects = {
                    (row.type, row.name)
                    for row in (
                        await connection.exec_driver_sql(
                            """
                            SELECT type, name
                            FROM sqlite_master
                            WHERE (type = 'table'
                                   AND name IN (
                                       'message_search_fts',
                                       'memory_search_fts'
                                   ))
                               OR (type = 'trigger'
                                   AND name IN (
                                       'messages_search_ai',
                                       'messages_search_au',
                                       'messages_search_ad',
                                       'memory_search_ai',
                                       'memory_search_au',
                                       'memory_search_ad'
                                   ))
                            """
                        )
                    ).all()
                }
                fts_tables = {
                    name for object_type, name in objects if object_type == "table"
                }
                fts_triggers = {
                    name for object_type, name in objects if object_type == "trigger"
                }
                if not await self._fts_objects_are_complete(
                    connection,
                    tables=fts_tables,
                    triggers=fts_triggers,
                ):
                    raise RuntimeError("FTS 派生索引重建后仍不完整，拒绝启动")
            self._message_fts_available = True
            self._memory_fts_available = True

    @staticmethod
    async def _fts_objects_are_complete(
        connection: AsyncConnection,
        *,
        tables: set[str],
        triggers: set[str],
    ) -> bool:
        """确认 FTS DDL 完整，且派生索引与权威表逐行一致。"""

        if tables != _FTS_TABLES or triggers != _FTS_TRIGGERS:
            return False
        message_difference = await connection.exec_driver_sql(
            _MESSAGE_FTS_DIFFERENCE_QUERY
        )
        if message_difference.first() is not None:
            return False
        memory_difference = await connection.exec_driver_sql(
            _MEMORY_FTS_DIFFERENCE_QUERY
        )
        return memory_difference.first() is None

    @staticmethod
    async def _rebuild_fts_objects(connection: AsyncConnection) -> None:
        """在当前启动事务内从权威表完整重建 FTS 派生对象。"""

        for trigger_name in sorted(_FTS_TRIGGERS):
            await connection.exec_driver_sql(
                f"DROP TRIGGER IF EXISTS {trigger_name}"
            )
        for table_name in sorted(_FTS_TABLES):
            await connection.exec_driver_sql(
                f"DROP TABLE IF EXISTS {table_name}"
            )
        for statement in _FTS_REBUILD_STATEMENTS:
            await connection.exec_driver_sql(statement)

    @property
    def fts_search_enabled(self) -> bool:
        """仅在两类派生索引都存在时报告完整 FTS 能力。"""

        return self._message_fts_available and self._memory_fts_available

    async def readiness_check(self) -> bool:
        """验证连接、外键约束和迁移后的关键 Schema 仍可读取。"""

        async with self.engine.connect() as connection:
            probe = await connection.exec_driver_sql("SELECT 1")
            if probe.scalar_one() != 1:
                return False
            foreign_keys = await connection.exec_driver_sql(
                "PRAGMA foreign_keys"
            )
            if foreign_keys.scalar_one() != 1:
                return False
            required_tables = set(
                (
                    await connection.exec_driver_sql(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table'
                          AND name IN ('alembic_version', 'sessions')
                        """
                    )
                )
                .scalars()
                .all()
            )
            return required_tables == {"alembic_version", "sessions"}

    async def close(self) -> None:
        """释放唯一连接池。"""

        await self._unit_of_work.close()


__all__ = ["DatabaseStore", "ProfileExtractionRunRow"]
