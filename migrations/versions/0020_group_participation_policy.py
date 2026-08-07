"""增加每个群会话的参与配置和可恢复 idle 状态。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_group_participation_policy"
down_revision = "0019_model_attempt_observability"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    """创建新表，并为已有群会话补齐默认策略。"""

    tables = _tables()
    if "group_participation_policies" not in tables:
        op.create_table(
            "group_participation_policies",
            sa.Column("session_id", sa.String(length=80), nullable=False),
            sa.Column("mode", sa.String(length=20), nullable=False),
            sa.Column("talk_frequency", sa.Float(), nullable=False),
            sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
            sa.Column("idle_streak", sa.Integer(), nullable=False),
            sa.Column("last_ordinary_reply_at", sa.DateTime(), nullable=True),
            sa.Column("last_external_message_at", sa.DateTime(), nullable=True),
            sa.Column(
                "external_interval_ewma_seconds", sa.Float(), nullable=True
            ),
            sa.Column(
                "external_interval_sample_count", sa.Integer(), nullable=False
            ),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("state_version", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
            sa.PrimaryKeyConstraint("session_id"),
        )
        op.execute(
            sa.text(
                """
                INSERT INTO group_participation_policies (
                    session_id, mode, talk_frequency, cooldown_seconds,
                    idle_streak, external_interval_sample_count,
                    revision, state_version, updated_at
                )
                SELECT id, 'normal', 0.65, 60, 0, 0, 1, 1, updated_at
                FROM sessions
                WHERE chat_type = 'group'
                """
            )
        )
    else:
        policy_columns = _columns("group_participation_policies")
        if "external_interval_ewma_seconds" not in policy_columns:
            op.add_column(
                "group_participation_policies",
                sa.Column(
                    "external_interval_ewma_seconds", sa.Float(), nullable=True
                ),
            )
        if "external_interval_sample_count" not in policy_columns:
            op.add_column(
                "group_participation_policies",
                sa.Column(
                    "external_interval_sample_count",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
            )

    if (
        "turn_decisions" in tables
        and "group_participation_reply_recorded"
        not in _columns("turn_decisions")
    ):
        op.add_column(
            "turn_decisions",
            sa.Column(
                "group_participation_reply_recorded",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    if (
        "turn_decisions" in _tables()
        and "group_participation_reply_recorded"
        in _columns("turn_decisions")
    ):
        op.drop_column("turn_decisions", "group_participation_reply_recorded")
    if "group_participation_policies" in _tables():
        op.drop_table("group_participation_policies")
