"""补全 Drift 阶段、快照与续接关系。"""

import sqlalchemy as sa
from alembic import op

revision = "0008_drift_state_machine"
down_revision = "0007_memory_clear_boundary"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("drift_runs")
    }


def _indexes() -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("drift_runs")
        if index["name"]
    }


def upgrade() -> None:
    columns = _columns()
    added_stage = "stage" not in columns
    added_snapshot = "snapshot_at" not in columns
    with op.batch_alter_table("drift_runs") as batch:
        if added_stage:
            batch.add_column(
                sa.Column(
                    "stage",
                    sa.String(length=30),
                    nullable=False,
                    server_default="finished",
                )
            )
        if added_snapshot:
            batch.add_column(
                sa.Column(
                    "snapshot_at",
                    sa.DateTime(timezone=True),
                    nullable=True,
                )
            )
        if "snapshot_message_id" not in columns:
            batch.add_column(
                sa.Column("snapshot_message_id", sa.String(length=80), nullable=True)
            )
        if "resumed_from_run_id" not in columns:
            batch.add_column(
                sa.Column("resumed_from_run_id", sa.String(length=80), nullable=True)
            )

    if added_stage:
        op.execute(
            """
            UPDATE drift_runs
            SET stage = CASE
                WHEN status IN ('running', 'paused') AND activity <> ''
                    THEN 'executing'
                WHEN status IN ('running', 'paused')
                    THEN 'selecting'
                ELSE 'finished'
            END
            """
        )
    if added_snapshot:
        op.execute(
            """
            UPDATE drift_runs
            SET snapshot_at = created_at
            WHERE snapshot_at IS NULL
            """
        )
        with op.batch_alter_table("drift_runs") as batch:
            batch.alter_column("snapshot_at", nullable=False)

    indexes = _indexes()
    with op.batch_alter_table("drift_runs") as batch:
        if "ix_drift_runs_stage" not in indexes:
            batch.create_index("ix_drift_runs_stage", ["stage"])
        if "ix_drift_runs_snapshot_message_id" not in indexes:
            batch.create_index(
                "ix_drift_runs_snapshot_message_id", ["snapshot_message_id"]
            )
        if "ix_drift_runs_resumed_from_run_id" not in indexes:
            batch.create_index(
                "ix_drift_runs_resumed_from_run_id", ["resumed_from_run_id"]
            )


def downgrade() -> None:
    with op.batch_alter_table("drift_runs") as batch:
        batch.drop_index("ix_drift_runs_resumed_from_run_id")
        batch.drop_index("ix_drift_runs_snapshot_message_id")
        batch.drop_index("ix_drift_runs_stage")
        batch.drop_column("resumed_from_run_id")
        batch.drop_column("snapshot_message_id")
        batch.drop_column("snapshot_at")
        batch.drop_column("stage")
