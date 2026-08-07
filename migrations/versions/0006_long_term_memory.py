"""增加统一长期记忆、归档任务与画像事实回填。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from adapters.persistence.database import Base

revision = "0006_long_term_memory"
down_revision = "0005_four_chains"
branch_labels = None
depends_on = None


def _content_hash(content: str) -> str:
    normalized = " ".join(content.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    """SQLite 旧列返回 naive 时间时，将其按历史 UTC 约定补齐。"""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def upgrade() -> None:
    bind = op.get_bind()
    for name in ("memory_records", "memory_consolidation_runs"):
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)

    facts_query = sa.text(
        """
        SELECT id, subject_id, scope_key, category, content, confidence,
               status, created_at, updated_at
        FROM profile_facts
        """
    ).columns(
        created_at=sa.DateTime(timezone=True),
        updated_at=sa.DateTime(timezone=True),
    )
    # text() 默认不知道结果列类型；显式声明时间列，兼容 SQLite 返回的字符串。
    facts = bind.execute(facts_query).mappings()
    for fact in facts:
        scope_key = str(fact["scope_key"])
        _, separator, session_id = scope_key.partition(":")
        if not separator or not session_id:
            continue
        sources = [
            str(row[0])
            for row in bind.execute(
                sa.text(
                    "SELECT message_id FROM profile_fact_sources "
                    "WHERE fact_id = :fact_id"
                ),
                {"fact_id": fact["id"]},
            ).all()
        ]
        status = (
            "retracted"
            if str(fact["status"]) == "retracted"
            else "active"
        )
        bind.execute(
            Base.metadata.tables["memory_records"].insert().values(
                id=f"memory_import_{fact['id']}",
                session_id=session_id,
                scope_key=scope_key,
                subject_id=fact["subject_id"],
                kind="profile",
                content=fact["content"],
                content_hash=_content_hash(str(fact["content"])),
                confidence=fact["confidence"],
                importance=0.7,
                status=status,
                source_chain="imported",
                source_run_id=None,
                source_message_ids_json=json.dumps(sources, ensure_ascii=False),
                source_refs_json=json.dumps([fact["id"]], ensure_ascii=False),
                supersedes_id=None,
                happened_at=_as_utc(fact["created_at"]),
                reinforcement=1,
                recall_count=0,
                last_recalled_at=None,
                created_at=_as_utc(fact["created_at"]),
                updated_at=_as_utc(fact["updated_at"]),
            )
        )


def downgrade() -> None:
    op.drop_table("memory_consolidation_runs")
    op.drop_table("memory_records")
