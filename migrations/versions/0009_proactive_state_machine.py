"""补全 Proactive 阶段、候选快照与人工触发审计。"""

import sqlalchemy as sa
from alembic import op

revision = "0009_proactive_state_machine"
down_revision = "0008_drift_state_machine"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("proactive_runs")
    }


def _indexes() -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("proactive_runs")
        if index["name"]
    }


def upgrade() -> None:
    columns = _columns()
    added_stage = "stage" not in columns
    added_candidates = "candidate_ids_json" not in columns
    with op.batch_alter_table("proactive_runs") as batch:
        if added_stage:
            batch.add_column(
                sa.Column(
                    "stage",
                    sa.String(length=30),
                    nullable=False,
                    server_default="finished",
                )
            )
        if "decision_code" not in columns:
            batch.add_column(
                sa.Column(
                    "decision_code",
                    sa.String(length=100),
                    nullable=False,
                    server_default="",
                )
            )
        if added_candidates:
            batch.add_column(
                sa.Column(
                    "candidate_ids_json",
                    sa.Text(),
                    nullable=False,
                    server_default="[]",
                )
            )
        if "manual_triggered" not in columns:
            batch.add_column(
                sa.Column(
                    "manual_triggered",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )

    if added_stage:
        op.execute(
            """
            UPDATE proactive_runs
            SET stage = CASE
                WHEN status = 'prepared' THEN 'prepared'
                WHEN status = 'running' THEN 'judging'
                ELSE 'finished'
            END
            """
        )
    if added_candidates:
        op.execute(
            """
            UPDATE proactive_runs
            SET candidate_ids_json = CASE
                WHEN candidate_id IS NULL THEN '[]'
                ELSE '["' || candidate_id || '"]'
            END
            """
        )

    indexes = _indexes()
    if "ix_proactive_runs_stage" not in indexes:
        with op.batch_alter_table("proactive_runs") as batch:
            batch.create_index("ix_proactive_runs_stage", ["stage"])


def downgrade() -> None:
    indexes = _indexes()
    with op.batch_alter_table("proactive_runs") as batch:
        if "ix_proactive_runs_stage" in indexes:
            batch.drop_index("ix_proactive_runs_stage")
        batch.drop_column("manual_triggered")
        batch.drop_column("candidate_ids_json")
        batch.drop_column("decision_code")
        batch.drop_column("stage")
