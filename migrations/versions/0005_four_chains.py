"""增加主动触达、Drift 与统一出站来源状态。"""

import sqlalchemy as sa
from alembic import op

from adapters.persistence.database import Base

revision = "0005_four_chains"
down_revision = "0004_expression_assets"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    message_columns = _columns("messages")
    with op.batch_alter_table("messages") as batch:
        if "origin" not in message_columns:
            batch.add_column(sa.Column("origin", sa.String(length=30), nullable=True))
            batch.create_index("ix_messages_origin", ["origin"])
        if "origin_run_id" not in message_columns:
            batch.add_column(
                sa.Column("origin_run_id", sa.String(length=80), nullable=True)
            )
            batch.create_index("ix_messages_origin_run_id", ["origin_run_id"])
        if "source_refs_json" not in message_columns:
            batch.add_column(sa.Column("source_refs_json", sa.Text(), nullable=True))

    outbound_columns = _columns("outbound_attempts")
    with op.batch_alter_table("outbound_attempts") as batch:
        if "origin" not in outbound_columns:
            batch.add_column(sa.Column("origin", sa.String(length=30), nullable=True))
            batch.create_index("ix_outbound_attempts_origin", ["origin"])
        if "origin_run_id" not in outbound_columns:
            batch.add_column(
                sa.Column("origin_run_id", sa.String(length=80), nullable=True)
            )
            batch.create_index(
                "ix_outbound_attempts_origin_run_id", ["origin_run_id"]
            )
        if "source_refs_json" not in outbound_columns:
            batch.add_column(sa.Column("source_refs_json", sa.Text(), nullable=True))

    bind = op.get_bind()
    for name in (
        "engagement_policies",
        "feed_sources",
        "proactive_candidates",
        "proactive_runs",
        "drift_runs",
    ):
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)

    now = bind.execute(sa.text("SELECT CURRENT_TIMESTAMP")).scalar_one()
    bind.execute(
        sa.text(
            """
            INSERT INTO engagement_policies(
                session_id, proactive_enabled, drift_enabled, timezone,
                quiet_start, quiet_end,
                minimum_interval_minutes, updated_at
            )
            SELECT id,
                   CASE WHEN chat_type = 'private' THEN 1 ELSE 0 END,
                   0, 'Asia/Shanghai', '22:00', '08:00', 240, :now
            FROM sessions
            WHERE id NOT IN (SELECT session_id FROM engagement_policies)
            """
        ),
        {"now": now},
    )


def downgrade() -> None:
    for name in (
        "drift_runs",
        "proactive_runs",
        "proactive_candidates",
        "feed_sources",
        "engagement_policies",
    ):
        op.drop_table(name)
    with op.batch_alter_table("outbound_attempts") as batch:
        batch.drop_index("ix_outbound_attempts_origin_run_id")
        batch.drop_index("ix_outbound_attempts_origin")
        batch.drop_column("source_refs_json")
        batch.drop_column("origin_run_id")
        batch.drop_column("origin")
    with op.batch_alter_table("messages") as batch:
        batch.drop_index("ix_messages_origin_run_id")
        batch.drop_index("ix_messages_origin")
        batch.drop_column("source_refs_json")
        batch.drop_column("origin_run_id")
        batch.drop_column("origin")
