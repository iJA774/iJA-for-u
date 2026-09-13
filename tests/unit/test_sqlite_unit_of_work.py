"""SQLiteUnitOfWork 的提交、回滚与连接边界。"""

import asyncio

import pytest
from sqlalchemy import text

from adapters.persistence.unit_of_work import SQLiteUnitOfWork


@pytest.mark.asyncio
async def test_sqlite_unit_of_work_commits_success_and_rolls_back_failure(
    tmp_path,
) -> None:
    """同一个 UoW 在异常时不泄漏半成品，成功时只提交一次。"""

    unit_of_work = SQLiteUnitOfWork(tmp_path / "unit-of-work.sqlite3")
    try:
        async with unit_of_work.engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )

        with pytest.raises(RuntimeError, match="模拟事务失败"):
            async with unit_of_work.transaction() as session:
                await session.execute(text("INSERT INTO probe (id, value) VALUES (1, 'rollback')"))
                raise RuntimeError("模拟事务失败")

        async with unit_of_work.session_factory() as session:
            assert (await session.execute(text("SELECT COUNT(*) FROM probe"))).scalar_one() == 0

        async with unit_of_work.transaction() as session:
            await session.execute(text("INSERT INTO probe (id, value) VALUES (2, 'commit')"))

        async with unit_of_work.session_factory() as session:
            assert (
                await session.execute(text("SELECT value FROM probe WHERE id = 2"))
            ).scalar_one() == "commit"
    finally:
        await unit_of_work.close()


@pytest.mark.asyncio
async def test_sqlite_unit_of_work_configures_async_concurrency_pragmas(tmp_path) -> None:
    """读事务存在时写提交仍应完成，避免后台任务与清理互相锁死。"""

    unit_of_work = SQLiteUnitOfWork(tmp_path / "concurrency.sqlite3")
    try:
        async with unit_of_work.engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1
            assert (
                await connection.exec_driver_sql("PRAGMA busy_timeout")
            ).scalar_one() >= 15_000
            assert (
                await connection.exec_driver_sql("PRAGMA journal_mode")
            ).scalar_one().lower() == "wal"

        async with unit_of_work.engine.connect() as reader:
            transaction = await reader.begin()
            await reader.exec_driver_sql("SELECT COUNT(*) FROM probe")

            async def write_while_reader_is_open() -> None:
                async with unit_of_work.transaction() as session:
                    await session.execute(
                        text("INSERT INTO probe (id, value) VALUES (1, 'concurrent')")
                    )

            await asyncio.wait_for(write_while_reader_is_open(), timeout=2)
            await transaction.rollback()

        async with unit_of_work.session_factory() as session:
            assert (
                await session.execute(text("SELECT value FROM probe WHERE id = 1"))
            ).scalar_one() == "concurrent"
    finally:
        await unit_of_work.close()
