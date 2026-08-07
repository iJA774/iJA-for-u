"""SQLiteUnitOfWork 的提交、回滚与连接边界。"""

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
