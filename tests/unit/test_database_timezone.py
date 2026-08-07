from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import StatementError

from adapters.persistence.database import DatabaseStore
from domain.models import (
    ChatType,
    Participant,
    ScheduleStatus,
    ScheduleTask,
)


async def _make_session(store: DatabaseStore) -> str:
    session = await store.create_session(
        platform="internal",
        account_id="tz-probe",
        external_chat_id="tz-probe-private",
        chat_type=ChatType.PRIVATE,
        display_name="时区探针",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    return session.id


@pytest.mark.asyncio
async def test_database_round_trip_preserves_utc_timezone(settings) -> None:
    """SQLite 不保留时区；UTCDateTime 在读取边界补回 UTC tzinfo，避免前端当本地时间解析。

    修复前：读出 naive datetime，Pydantic 序列化不带时区后缀，前端 new Date() 误判
    本地时间，显示偏差等于本地时区偏移（北京时间差 8 小时）。
    修复后：读出 aware UTC datetime，序列化带 +00:00，前端正确转换。
    """
    store = DatabaseStore(settings.storage.data_dir / settings.storage.database_name)
    await store.initialize()
    try:
        session_id = await _make_session(store)
        # 04:30 UTC 对应北京 12:30；若前端误当本地时间会显示 04:30，偏差 8 小时。
        written_at = datetime(2026, 7, 27, 4, 30, tzinfo=UTC)
        schedule = ScheduleTask(
            id="schedule_tz_probe",
            session_id=session_id,
            created_by="u1",
            title="时区探针",
            instruction="校验读出时间仍为 UTC aware",
            source_text="每天 12:30",
            timezone="Asia/Shanghai",
            dtstart=written_at,
            rrule="FREQ=DAILY;BYHOUR=12;BYMINUTE=30",
            status=ScheduleStatus.ACTIVE,
            next_run_at=written_at,
            created_at=written_at,
            updated_at=written_at,
        )
        await store.create_schedule(schedule)

        read_back = await store.get_schedule(schedule.id)
        assert read_back is not None
        # 读出时间必须仍带 UTC tzinfo，否则前端 new Date() 会当作本地时间解析。
        assert read_back.next_run_at is not None
        assert read_back.next_run_at.tzinfo is not None, "读出时间丢失时区，前端会误判为本地时间"
        assert read_back.next_run_at == written_at
        # Pydantic 序列化必须带时区后缀（Z 或 +00:00），前端 new Date() 才能正确解析为 UTC。
        serialized = read_back.model_dump(mode="json")
        assert serialized["next_run_at"].endswith(("+00:00", "Z")), serialized["next_run_at"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_database_rejects_naive_datetime_write(settings) -> None:
    """UTCDateTime 写入边界拒绝 naive datetime，避免无时区时间悄悄入库后语义漂移。"""
    store = DatabaseStore(settings.storage.data_dir / settings.storage.database_name)
    await store.initialize()
    try:
        session_id = await _make_session(store)
        naive_schedule = ScheduleTask(
            id="schedule_tz_naive",
            session_id=session_id,
            created_by="u1",
            title="naive 探针",
            instruction="应被写入边界拒绝",
            source_text="每天 12:30",
            timezone="Asia/Shanghai",
            dtstart=datetime(2026, 7, 27, 4, 30),  # naive，无 tzinfo
            rrule="FREQ=DAILY;BYHOUR=12;BYMINUTE=30",
            status=ScheduleStatus.ACTIVE,
            next_run_at=datetime(2026, 7, 27, 4, 30),
            created_at=datetime(2026, 7, 27, 4, 30),
            updated_at=datetime(2026, 7, 27, 4, 30),
        )
        with pytest.raises(StatementError, match="naive datetime"):
            await store.create_schedule(naive_schedule)
    finally:
        await store.close()
