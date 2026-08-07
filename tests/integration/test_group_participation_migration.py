"""群聊参与策略迁移的真实旧 schema 回归。"""

from __future__ import annotations

import sqlite3

from alembic import command
from alembic.config import Config


def _alembic_config(settings) -> Config:
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(settings.project_root / "migrations"),
    )
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite:///{settings.storage.database_path.as_posix()}",
    )
    return config


def test_group_participation_migration_backfills_old_group_and_turn(settings) -> None:
    """0019 时代的群和 Turn 升级后应具备完整可恢复状态。"""

    database_path = settings.storage.database_path
    database_path.parent.mkdir(parents=True, exist_ok=True)
    config = _alembic_config(settings)
    command.upgrade(config, "0019_model_attempt_observability")
    with sqlite3.connect(database_path) as connection:
        # 0001 读取当前 metadata；显式移除 M-01 schema，模拟真实 0019 库。
        connection.execute("DROP TABLE group_participation_policies")
        connection.execute(
            "ALTER TABLE turn_decisions "
            "DROP COLUMN group_participation_reply_recorded"
        )
        connection.execute(
            """
            INSERT INTO sessions(
                id, platform, account_id, external_chat_id, chat_type,
                display_name, revision, data_epoch, memory_cleared_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-group",
                "migration-test",
                "ija-test",
                "old-group",
                "group",
                "旧群聊",
                1,
                1,
                None,
                "2026-07-01 00:00:00",
                "2026-07-01 00:00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO turn_decisions(
                id, session_id, action, strategy, score, threshold, reason,
                score_detail_json, trigger_message_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-turn",
                "old-group",
                "reply",
                "group_reply_necessity",
                90,
                80,
                "旧决策",
                '{"forced": false}',
                None,
                "2026-07-01 00:00:00",
            ),
        )
        connection.commit()

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        policy = connection.execute(
            """
            SELECT mode, trigger_count, frequency_factor, cooldown_seconds, idle_streak,
                   external_interval_ewma_seconds,
                   external_interval_sample_count, revision, state_version
            FROM group_participation_policies
            WHERE session_id = 'old-group'
            """
        ).fetchone()
        assert policy == ("normal", 3, 0.9, 60, 0, None, 0, 1, 1)
        assert connection.execute(
            """
            SELECT group_participation_reply_recorded
            FROM turn_decisions WHERE id = 'old-turn'
            """
        ).fetchone() == (0,)

    command.downgrade(config, "0019_model_attempt_observability")
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'group_participation_policies'
                """
            ).fetchone()
            is None
        )
        turn_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(turn_decisions)"
            ).fetchall()
        }
        assert "group_participation_reply_recorded" not in turn_columns
