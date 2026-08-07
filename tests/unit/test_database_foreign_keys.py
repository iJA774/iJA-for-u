"""SQLite 外键必须在真实运行连接上生效。"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from adapters.persistence.database import DatabaseStore, ProfileExtractionRunRow
from domain.models import ExtractionStatus, utc_now


async def test_sqlite_foreign_keys_are_enabled_for_every_store_connection(
    settings,
) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    try:
        async with store.engine.connect() as connection:
            result = await connection.exec_driver_sql("PRAGMA foreign_keys")
            assert result.scalar_one() == 1
    finally:
        await store.close()


async def test_sqlite_rejects_orphan_profile_extraction_run(settings) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    try:
        now = utc_now()
        async with store.session_factory() as db:
            db.add(
                ProfileExtractionRunRow(
                    id="extract_orphan",
                    session_id="session_missing",
                    subject_id="user_missing",
                    scope_key="private:session_missing",
                    source_message_ids_json="[]",
                    status=ExtractionStatus.PENDING.value,
                    error_code=None,
                    error_message=None,
                    attempt_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
            else:
                raise AssertionError("外键启用后不应允许画像任务引用不存在的 Session")
    finally:
        await store.close()
