"""增加不含正文的模型调用 attempt 观测。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_model_attempt_observability"
down_revision = "0018_fts_search_indexes"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _indexes() -> set[str]:
    if "model_attempts" not in _tables():
        return set()
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("model_attempts")
        if index["name"]
    }


def upgrade() -> None:
    """只保存安全标识、用量、耗时和结果，不保存 Prompt 或模型输出。"""

    if "model_attempts" not in _tables():
        op.create_table(
            "model_attempts",
            sa.Column("id", sa.String(length=80), nullable=False),
            sa.Column("invocation_id", sa.String(length=80), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("task", sa.String(length=100), nullable=False),
            sa.Column("provider", sa.String(length=100), nullable=False),
            sa.Column("profile", sa.String(length=100), nullable=False),
            sa.Column("model", sa.String(length=300), nullable=False),
            sa.Column("session_id", sa.String(length=80), nullable=True),
            sa.Column("turn_id", sa.String(length=100), nullable=True),
            sa.Column("run_id", sa.String(length=100), nullable=True),
            sa.Column("streamed", sa.Boolean(), nullable=False),
            sa.Column("tool_call_count", sa.Integer(), nullable=False),
            sa.Column("input_tokens", sa.Integer(), nullable=True),
            sa.Column("output_tokens", sa.Integer(), nullable=True),
            sa.Column("total_tokens", sa.Integer(), nullable=True),
            sa.Column("usage_source", sa.String(length=20), nullable=False),
            sa.Column("latency_ms", sa.Integer(), nullable=False),
            sa.Column("success", sa.Boolean(), nullable=False),
            sa.Column("error_type", sa.String(length=100), nullable=True),
            sa.Column("error_code", sa.String(length=100), nullable=True),
            sa.Column("cost_microusd", sa.Integer(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["session_id"],
                ["sessions.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    existing = _indexes()
    for name, columns in (
        ("ix_model_attempts_invocation_id", ["invocation_id"]),
        ("ix_model_attempts_task", ["task"]),
        ("ix_model_attempts_provider", ["provider"]),
        ("ix_model_attempts_profile", ["profile"]),
        ("ix_model_attempts_session_id", ["session_id"]),
        ("ix_model_attempts_turn_id", ["turn_id"]),
        ("ix_model_attempts_run_id", ["run_id"]),
        ("ix_model_attempts_success", ["success"]),
        ("ix_model_attempts_started_at", ["started_at"]),
        ("ix_model_attempts_session_started", ["session_id", "started_at"]),
        ("ix_model_attempts_task_started", ["task", "started_at"]),
        (
            "ix_model_attempts_invocation_attempt",
            ["invocation_id", "attempt_number"],
        ),
    ):
        if name not in existing:
            op.create_index(name, "model_attempts", columns)


def downgrade() -> None:
    if "model_attempts" in _tables():
        op.drop_table("model_attempts")
