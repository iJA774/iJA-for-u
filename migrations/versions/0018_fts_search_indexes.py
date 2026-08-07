"""为消息与长期记忆增加可重建的 trigram FTS 候选索引。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import OperationalError

revision = "0018_fts_search_indexes"
down_revision = "0017_session_data_epoch"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _indexes(table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index["name"]
    }


def _create_index_if_missing(
    name: str,
    table_name: str,
    columns: list[str],
) -> None:
    if name not in _indexes(table_name):
        op.create_index(name, table_name, columns)


def _drop_index_if_present(name: str, table_name: str) -> None:
    if name in _indexes(table_name):
        op.drop_index(name, table_name=table_name)


def _drop_fts_objects() -> None:
    for trigger_name in (
        "messages_search_ai",
        "messages_search_au",
        "messages_search_ad",
        "memory_search_ai",
        "memory_search_au",
        "memory_search_ad",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
    op.execute(sa.text("DROP TABLE IF EXISTS message_search_fts"))
    op.execute(sa.text("DROP TABLE IF EXISTS memory_search_fts"))


def _fts_is_unsupported(exc: OperationalError) -> bool:
    message = str(exc.orig).casefold()
    return (
        "no such module: fts5" in message
        or "no such tokenizer" in message
        or "error in tokenizer constructor" in message
    )


def _create_fts_objects() -> None:
    """创建派生索引；SQLite 未编译 FTS5/trigram 时保留纯列降级。"""

    try:
        op.execute(
            sa.text(
                """
                CREATE VIRTUAL TABLE message_search_fts USING fts5(
                    message_id UNINDEXED,
                    session_id UNINDEXED,
                    created_at UNINDEXED,
                    search_text,
                    tokenize='trigram'
                )
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE VIRTUAL TABLE memory_search_fts USING fts5(
                    memory_id UNINDEXED,
                    scope_key UNINDEXED,
                    status UNINDEXED,
                    search_text,
                    tokenize='trigram'
                )
                """
            )
        )
    except OperationalError as exc:
        _drop_fts_objects()
        if _fts_is_unsupported(exc):
            return
        raise

    op.execute(
        sa.text(
            """
            INSERT INTO message_search_fts(
                rowid, message_id, session_id, created_at, search_text
            )
            SELECT rowid, id, session_id, created_at, search_text
            FROM messages
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO memory_search_fts(
                rowid, memory_id, scope_key, status, search_text
            )
            SELECT rowid, id, scope_key, status, content
            FROM memory_records
            """
        )
    )
    op.execute(
        sa.text(
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
            """
        )
    )
    op.execute(
        sa.text(
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
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER messages_search_ad
            AFTER DELETE ON messages
            BEGIN
                DELETE FROM message_search_fts WHERE rowid = old.rowid;
            END
            """
        )
    )
    op.execute(
        sa.text(
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
            """
        )
    )
    op.execute(
        sa.text(
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
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER memory_search_ad
            AFTER DELETE ON memory_records
            BEGIN
                DELETE FROM memory_search_fts WHERE rowid = old.rowid;
            END
            """
        )
    )


def upgrade() -> None:
    """回填规范化可见文本，并在平台支持时启用 trigram FTS5。"""

    _create_index_if_missing(
        "ix_messages_session_origin_run",
        "messages",
        ["session_id", "origin", "origin_run_id"],
    )
    _create_index_if_missing(
        "ix_jargon_terms_session_id_id",
        "jargon_terms",
        ["session_id", "id"],
    )
    _create_index_if_missing(
        "ix_group_expression_patterns_session_id_id",
        "group_expression_patterns",
        ["session_id", "id"],
    )
    _create_index_if_missing(
        "ix_behavior_patterns_session_id_id",
        "behavior_patterns",
        ["session_id", "id"],
    )
    _create_index_if_missing(
        "ix_proactive_runs_session_status_id",
        "proactive_runs",
        ["session_id", "status", "id"],
    )
    if "search_text" not in _columns("messages"):
        op.add_column(
            "messages",
            sa.Column(
                "search_text",
                sa.Text(),
                nullable=False,
                server_default="",
            ),
        )
    op.execute(
        sa.text(
            """
            UPDATE messages
            SET search_text = lower(trim(COALESCE((
                SELECT group_concat(
                    trim(CASE json_extract(component.value, '$.type')
                        WHEN 'text' THEN
                            COALESCE(json_extract(component.value, '$.text'), '')
                        WHEN 'mention' THEN
                            '@' || COALESCE(
                                json_extract(component.value, '$.target_name'),
                                json_extract(component.value, '$.target_id'),
                                ''
                            )
                        WHEN 'image_ref' THEN
                            COALESCE(
                                json_extract(component.value, '$.description'),
                                '[图片:' || COALESCE(
                                    json_extract(component.value, '$.filename'), ''
                                ) || ']'
                            )
                        WHEN 'audio_ref' THEN
                            COALESCE(
                                json_extract(component.value, '$.description'),
                                '[语音:' || COALESCE(
                                    json_extract(component.value, '$.filename'), ''
                                ) || ']'
                            )
                        WHEN 'file_ref' THEN
                            COALESCE(
                                json_extract(component.value, '$.description'),
                                '[文件:' || COALESCE(
                                    json_extract(component.value, '$.filename'), ''
                                ) || ']'
                            )
                        ELSE ''
                    END),
                    ' '
                )
                FROM json_each(messages.components_json) AS component
            ), '')))
            """
        )
    )
    _drop_fts_objects()
    _create_fts_objects()


def downgrade() -> None:
    _drop_fts_objects()
    _drop_index_if_present(
        "ix_proactive_runs_session_status_id",
        "proactive_runs",
    )
    _drop_index_if_present(
        "ix_behavior_patterns_session_id_id",
        "behavior_patterns",
    )
    _drop_index_if_present(
        "ix_group_expression_patterns_session_id_id",
        "group_expression_patterns",
    )
    _drop_index_if_present(
        "ix_jargon_terms_session_id_id",
        "jargon_terms",
    )
    _drop_index_if_present(
        "ix_messages_session_origin_run",
        "messages",
    )
    if "search_text" in _columns("messages"):
        with op.batch_alter_table("messages") as batch:
            batch.drop_column("search_text")
