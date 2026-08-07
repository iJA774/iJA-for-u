import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from application.memory import memory_content_hash


def test_long_term_memory_migration_backfills_profile_facts(
    settings, tmp_path: Path
) -> None:
    """旧画像事实升级后进入统一记忆，且不伪造消息来源。"""

    database_path = tmp_path / "memory-backfill.sqlite3"
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(settings.project_root / "migrations")
    )
    config.set_main_option(
        "sqlalchemy.url", f"sqlite:///{database_path.as_posix()}"
    )
    command.upgrade(config, "0005_four_chains")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO profile_facts (
                id, subject_id, scope_key, category, content, confidence,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_legacy",
                "user-legacy",
                "private:session-legacy",
                "偏好",
                "用户喜欢手冲咖啡",
                0.9,
                "active",
                "2026-07-01 10:00:00",
                "2026-07-02 10:00:00",
            ),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT id, session_id, scope_key, subject_id, kind, content,
                   content_hash, status, source_chain,
                   source_message_ids_json, source_refs_json
            FROM memory_records
            WHERE id = ?
            """,
            ("memory_import_fact_legacy",),
        ).fetchone()
    assert row is not None
    assert row[:6] == (
        "memory_import_fact_legacy",
        "session-legacy",
        "private:session-legacy",
        "user-legacy",
        "profile",
        "用户喜欢手冲咖啡",
    )
    assert row[6] == memory_content_hash("用户喜欢手冲咖啡")
    assert row[7:9] == ("active", "imported")
    assert json.loads(row[9]) == []
    assert json.loads(row[10]) == ["fact_legacy"]


def test_drift_state_machine_migration_backfills_existing_runs(
    settings, tmp_path: Path
) -> None:
    """真实旧库缺少阶段列时，升级保留运行并补齐可读快照。"""

    database_path = tmp_path / "drift-state-machine.sqlite3"
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(settings.project_root / "migrations")
    )
    config.set_main_option(
        "sqlalchemy.url", f"sqlite:///{database_path.as_posix()}"
    )
    command.upgrade(config, "0007_memory_clear_boundary")

    # 0005 历史迁移使用运行时 metadata 建表；测试先还原真实 0007 旧表形状。
    with sqlite3.connect(database_path) as connection:
        for index_name in (
            "ix_drift_runs_stage",
            "ix_drift_runs_snapshot_message_id",
            "ix_drift_runs_resumed_from_run_id",
        ):
            connection.execute(f"DROP INDEX {index_name}")
        for column_name in (
            "stage",
            "snapshot_at",
            "snapshot_message_id",
            "resumed_from_run_id",
        ):
            connection.execute(
                f"ALTER TABLE drift_runs DROP COLUMN {column_name}"
            )
        connection.execute(
            """
            INSERT INTO drift_runs (
                id, session_id, status, activity, decision_reason,
                resume_payload_json, evidence_refs_json,
                produced_candidate_id, auto_resume_count,
                error_code, error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "drift_run_legacy",
                "session_legacy",
                "paused",
                "conversation_topic_preparation",
                "旧运行",
                '{"activity":"conversation_topic_preparation"}',
                "[]",
                None,
                1,
                "provider_error",
                "旧错误",
                "2026-07-01 10:00:00",
                "2026-07-01 10:01:00",
            ),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT status, stage, snapshot_at, snapshot_message_id,
                   resumed_from_run_id, auto_resume_count
            FROM drift_runs
            WHERE id = 'drift_run_legacy'
            """
        ).fetchone()
    assert row == (
        "paused",
        "executing",
        "2026-07-01 10:00:00",
        None,
        None,
        1,
    )


def test_proactive_state_machine_migration_backfills_existing_runs(
    settings, tmp_path: Path
) -> None:
    """旧主动运行升级后保留终态，并补齐阶段、候选快照和触发来源。"""

    database_path = tmp_path / "proactive-state-machine.sqlite3"
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(settings.project_root / "migrations")
    )
    config.set_main_option(
        "sqlalchemy.url", f"sqlite:///{database_path.as_posix()}"
    )
    command.upgrade(config, "0008_drift_state_machine")

    # 0005 会使用当前 metadata 建表，先恢复真实 0008 的旧表结构。
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX ix_proactive_runs_stage")
        for column_name in (
            "stage",
            "decision_code",
            "candidate_ids_json",
            "manual_triggered",
        ):
            connection.execute(
                f"ALTER TABLE proactive_runs DROP COLUMN {column_name}"
            )
        connection.execute(
            """
            INSERT INTO proactive_runs (
                id, session_id, status, gate_reason, decision_reason, score,
                candidate_id, snapshot_at, snapshot_message_id, outbound_id,
                error_code, error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "proactive_run_legacy",
                "session_legacy",
                "prepared",
                "",
                "旧主动运行",
                0.8,
                "candidate_legacy",
                "2026-07-01 10:00:00",
                "message_legacy",
                "outbound_legacy",
                None,
                None,
                "2026-07-01 10:00:00",
                "2026-07-01 10:01:00",
            ),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT status, stage, decision_code, candidate_ids_json,
                   manual_triggered
            FROM proactive_runs
            WHERE id = 'proactive_run_legacy'
            """
        ).fetchone()
    assert row == (
        "prepared",
        "prepared",
        "",
        '["candidate_legacy"]',
        0,
    )


def test_social_learning_decay_migration_backfills_existing_records(
    settings, tmp_path: Path
) -> None:
    """真实 0014 社交学习表升级后保留记录并恢复维护游标。"""

    database_path = tmp_path / "social-learning-decay.sqlite3"
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(settings.project_root / "migrations")
    )
    config.set_main_option(
        "sqlalchemy.url", f"sqlite:///{database_path.as_posix()}"
    )
    command.upgrade(config, "0014_vision_and_collected_expressions")

    with sqlite3.connect(database_path) as connection:
        for index_name in (
            "ix_jargon_terms_last_maintained_at",
            "ix_group_expression_patterns_last_reinforced_at",
            "ix_group_expression_patterns_last_maintained_at",
            "ix_behavior_patterns_last_reinforced_at",
            "ix_behavior_patterns_last_maintained_at",
        ):
            connection.execute(f"DROP INDEX {index_name}")
        connection.execute(
            "ALTER TABLE jargon_terms DROP COLUMN last_maintained_at"
        )
        connection.execute("ALTER TABLE jargon_terms DROP COLUMN decay_count")
        connection.execute(
            """
            ALTER TABLE jargon_terms
            DROP COLUMN last_inference_occurrence_count
            """
        )
        connection.execute(
            """
            ALTER TABLE group_expression_patterns
            DROP COLUMN last_maintained_at
            """
        )
        connection.execute(
            "ALTER TABLE group_expression_patterns DROP COLUMN decay_count"
        )
        connection.execute(
            """
            ALTER TABLE group_expression_patterns
            DROP COLUMN last_reinforced_at
            """
        )
        connection.execute(
            "ALTER TABLE behavior_patterns DROP COLUMN last_maintained_at"
        )
        connection.execute(
            "ALTER TABLE behavior_patterns DROP COLUMN decay_count"
        )
        connection.execute(
            "ALTER TABLE behavior_patterns DROP COLUMN last_reinforced_at"
        )
        connection.execute(
            """
            INSERT INTO jargon_terms (
                id, session_id, term, normalized_term, meaning, status,
                confidence, occurrence_count, inference_count,
                evidence_message_ids_json, last_seen_at, last_inferred_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "jargon_legacy",
                "session_legacy",
                "旧梗",
                "旧梗",
                "旧的群内表达",
                "active",
                0.8,
                8,
                2,
                "[]",
                "2026-05-01 10:00:00",
                "2026-05-01 10:05:00",
                "2026-04-01 10:00:00",
                "2026-05-01 10:05:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO group_expression_patterns (
                id, session_id, situation, style, pattern_hash, status,
                confidence, occurrence_count, selection_count,
                evidence_message_ids_json, last_selected_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "expression_legacy",
                "session_legacy",
                "旧情境",
                "旧表达",
                "a" * 64,
                "active",
                0.7,
                3,
                1,
                "[]",
                "2026-05-02 10:00:00",
                "2026-04-01 10:00:00",
                "2026-05-02 10:00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO behavior_patterns (
                id, session_id, scene_summary, scene_tags_json,
                need_tags_json, other_traits_json, action, expected_outcome,
                pattern_hash, actor_type, learning_type, status, confidence,
                occurrence_count, activation_count, success_count,
                failure_count, score, evidence_message_ids_json,
                last_selected_at, last_feedback_at, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
                "behavior_legacy",
                "session_legacy",
                "旧场景",
                "[]",
                "[]",
                "[]",
                "旧行为",
                "旧结果",
                "b" * 64,
                "other_user",
                "observed_behavior",
                "active",
                0.7,
                2,
                1,
                0,
                0,
                0.0,
                "[]",
                "2026-05-03 10:00:00",
                None,
                "2026-04-01 10:00:00",
                "2026-05-03 10:00:00",
            ),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        jargon = connection.execute(
            """
            SELECT last_inference_occurrence_count, decay_count
            FROM jargon_terms WHERE id = 'jargon_legacy'
            """
        ).fetchone()
        expression = connection.execute(
            """
            SELECT last_reinforced_at, decay_count
            FROM group_expression_patterns WHERE id = 'expression_legacy'
            """
        ).fetchone()
        behavior = connection.execute(
            """
            SELECT last_reinforced_at, decay_count
            FROM behavior_patterns WHERE id = 'behavior_legacy'
            """
        ).fetchone()

    assert jargon == (8, 0)
    assert expression == ("2026-05-02 10:00:00", 0)
    assert behavior == ("2026-05-03 10:00:00", 0)


def test_social_learning_vector_graph_migration_accepts_real_0015_shape(
    settings, tmp_path: Path
) -> None:
    """0015 旧库没有新表/列时也能升级，并保留原行为经验。"""

    database_path = tmp_path / "social-learning-vector-graph.sqlite3"
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(settings.project_root / "migrations")
    )
    config.set_main_option(
        "sqlalchemy.url", f"sqlite:///{database_path.as_posix()}"
    )
    command.upgrade(config, "0015_social_learning_decay")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DROP INDEX ix_behavior_patterns_scene_cluster_id"
        )
        connection.execute(
            "ALTER TABLE behavior_patterns DROP COLUMN scene_cluster_id"
        )
        connection.execute(
            "ALTER TABLE behavior_patterns DROP COLUMN tag_distribution_json"
        )
        connection.execute(
            "ALTER TABLE behavior_patterns DROP COLUMN tag_groups_json"
        )
        for table_name in (
            "behavior_scene_clusters",
            "behavior_scene_tag_aliases",
            "group_expression_cluster_centers",
            "group_expression_embeddings",
        ):
            connection.execute(f"DROP TABLE {table_name}")
        connection.execute(
            """
            INSERT INTO behavior_patterns (
                id, session_id, scene_summary, scene_tags_json,
                need_tags_json, other_traits_json, action, expected_outcome,
                pattern_hash, actor_type, learning_type, status, confidence,
                occurrence_count, activation_count, success_count,
                failure_count, score, evidence_message_ids_json,
                last_reinforced_at, last_selected_at, last_feedback_at,
                decay_count, last_maintained_at, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                "behavior-pre-vector",
                "session_legacy",
                "旧行为场景",
                '["技术排障"]',
                '["澄清信息"]',
                "[]",
                "追问一个关键配置",
                "获得可复现信息",
                "f" * 64,
                "agent_self",
                "self_reflection",
                "active",
                0.8,
                3,
                0,
                0,
                0,
                0.0,
                "[]",
                "2026-05-01 10:00:00",
                None,
                None,
                0,
                None,
                "2026-05-01 10:00:00",
                "2026-05-01 10:00:00",
            ),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "group_expression_embeddings",
            "group_expression_cluster_centers",
            "behavior_scene_tag_aliases",
            "behavior_scene_clusters",
        } <= tables
        row = connection.execute(
            """
            SELECT tag_groups_json, tag_distribution_json, scene_cluster_id
            FROM behavior_patterns
            WHERE id = 'behavior-pre-vector'
            """
        ).fetchone()
        assert row == ("[]", "{}", None)
