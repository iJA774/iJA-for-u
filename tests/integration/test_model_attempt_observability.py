"""模型 attempt 的迁移、并发隐私边界与 SQL 聚合契约。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from adapters.model import FakeModelProvider
from adapters.persistence import DatabaseStore
from adapters.persistence.migration import upgrade_database
from api.app import create_app
from domain.errors import InputValidationError
from domain.models import (
    ChatType,
    ModelAttempt,
    Participant,
    utc_now,
)


def _attempt(
    *,
    suffix: str,
    session_id: str | None,
    known_usage: bool,
    success: bool = True,
) -> ModelAttempt:
    now = utc_now()
    return ModelAttempt(
        invocation_id=f"invocation-{suffix}",
        attempt_number=1,
        task="chat.reply",
        provider="test-protocol",
        profile="chat",
        model="test-model",
        session_id=session_id,
        turn_id=f"turn-{suffix}" if session_id is not None else None,
        input_tokens=10 if known_usage else None,
        output_tokens=5 if known_usage else None,
        total_tokens=15 if known_usage else None,
        usage_source="provider" if known_usage else "unknown",
        latency_ms=10,
        success=success,
        error_type=None if success else "ProviderError",
        error_code=None if success else "provider_error",
        cost_microusd=30 if known_usage else None,
        started_at=now,
        completed_at=now,
    )


async def _store(settings) -> DatabaseStore:
    upgrade_database(
        settings.project_root,
        settings.storage.database_path,
    )
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    return store


async def _session(store: DatabaseStore, suffix: str):
    return await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id=f"attempt-{suffix}",
        chat_type=ChatType.PRIVATE,
        display_name=f"attempt-{suffix}",
        participants=[
            Participant(
                external_user_id=f"user-{suffix}",
                display_name=f"用户-{suffix}",
            )
        ],
    )


@pytest.mark.asyncio
async def test_model_attempt_fk_and_summary_use_database_aggregation(
    settings,
) -> None:
    store = await _store(settings)
    try:
        session = await _session(store, "summary")
        await store.save_model_attempt(
            _attempt(
                suffix="known",
                session_id=session.id,
                known_usage=True,
            )
        )
        await store.save_model_attempt(
            _attempt(
                suffix="unknown",
                session_id=session.id,
                known_usage=False,
                success=False,
            )
        )

        async with store.engine.connect() as connection:
            foreign_keys = (
                await connection.execute(
                    text("PRAGMA foreign_key_list(model_attempts)")
                )
            ).mappings().all()
        assert any(
            row["table"] == "sessions"
            and row["from"] == "session_id"
            and row["to"] == "id"
            and row["on_delete"].upper() == "CASCADE"
            for row in foreign_keys
        )

        statements: list[str] = []

        def capture_statement(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(statement)

        event.listen(
            store.engine.sync_engine,
            "before_cursor_execute",
            capture_statement,
        )
        try:
            summary = await store.summarize_model_attempts(
                session_id=session.id
            )
        finally:
            event.remove(
                store.engine.sync_engine,
                "before_cursor_execute",
                capture_statement,
            )

        assert summary == {
            "attempt_count": 2,
            "success_count": 1,
            "error_count": 1,
            "usage_unknown_count": 1,
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "average_latency_ms": 10.0,
            "known_cost_microusd": 30,
            "cost_unknown_count": 1,
        }
        model_queries = [
            statement.casefold()
            for statement in statements
            if "model_attempts" in statement.casefold()
        ]
        assert len(model_queries) == 1
        assert all(
            aggregate in model_queries[0]
            for aggregate in ("count(", "sum(", "avg(")
        )
        assert "order by" not in model_queries[0]

        with pytest.raises(InputValidationError, match="小写任务标识"):
            await store.summarize_model_attempts(task="INVALID/TASK")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_late_attempt_cannot_survive_concurrent_session_delete(
    settings,
) -> None:
    store = await _store(settings)
    try:
        for index in range(6):
            session = await _session(store, f"race-{index}")
            attempt = _attempt(
                suffix=f"race-{index}",
                session_id=session.id,
                known_usage=False,
            )
            await asyncio.gather(
                store.save_model_attempt(attempt),
                store.delete_session(session.id),
            )
            assert (
                await store.list_model_attempts(session_id=session.id)
            ) == []

            # 删除边界之后才完成的 Provider 结果同样只能插入 0 行。
            await store.save_model_attempt(
                attempt.model_copy(
                    update={
                        "id": f"late-{index}",
                        "invocation_id": f"late-invocation-{index}",
                    }
                )
            )
            assert (
                await store.list_model_attempts(session_id=session.id)
            ) == []

        global_attempt = _attempt(
            suffix="global",
            session_id=None,
            known_usage=False,
        )
        await store.save_model_attempt(global_attempt)
        assert [item.id for item in await store.list_model_attempts()] == [
            global_attempt.id
        ]
    finally:
        await store.close()


def test_model_attempt_api_exposes_only_safe_operational_fields(
    settings,
) -> None:
    app = create_app(
        settings=settings,
        model_override=FakeModelProvider(),
    )
    with TestClient(app) as client:
        assert client.post("/api/model/probe").status_code == 200
        response = client.get(
            "/api/model-attempts",
            params={"task": "model.probe", "limit": 10},
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        assert set(items[0]) == {
            "id",
            "invocation_id",
            "attempt_number",
            "task",
            "provider",
            "profile",
            "model",
            "session_id",
            "turn_id",
            "run_id",
            "streamed",
            "tool_call_count",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "usage_source",
            "latency_ms",
            "success",
            "error_type",
            "error_code",
            "cost_microusd",
            "started_at",
            "completed_at",
        }
        serialized = response.text.casefold()
        for forbidden in (
            "prompt",
            "messages",
            "response_body",
            "base_url",
            "api_key",
            "authorization",
        ):
            assert forbidden not in serialized

        summary = client.get(
            "/api/model-attempts/summary",
            params={"task": "model.probe"},
        )
        assert summary.status_code == 200
        assert summary.json()["attempt_count"] == 1
        assert summary.json()["usage_unknown_count"] == 1
        assert summary.json()["cost_unknown_count"] == 1

        invalid = client.get(
            "/api/model-attempts/summary",
            params={"task": "INVALID/TASK"},
        )
        assert invalid.status_code == 400
