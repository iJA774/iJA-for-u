"""Drift 默认开启并移除 Proactive 每日上限。"""

import sqlalchemy as sa
from alembic import op

revision = "0010_drift_default_on_drop_daily_limit"
down_revision = "0009_proactive_state_machine"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("engagement_policies")
    }


def upgrade() -> None:
    # 已存在的私聊 Session 强制开启 Drift，与新默认值保持一致。
    op.execute(
        """
        UPDATE engagement_policies
        SET drift_enabled = 1
        WHERE session_id IN (
            SELECT id FROM sessions WHERE chat_type = 'private'
        )
        """
    )
    if "daily_limit" in _columns():
        with op.batch_alter_table("engagement_policies") as batch:
            batch.drop_column("daily_limit")


def downgrade() -> None:
    if "daily_limit" not in _columns():
        with op.batch_alter_table("engagement_policies") as batch:
            batch.add_column(
                sa.Column(
                    "daily_limit",
                    sa.Integer(),
                    nullable=False,
                    server_default="3",
                )
            )
    # 回退 Drift 默认开启：无法区分用户显式开关，统一回到默认关闭语义。
    op.execute(
        """
        UPDATE engagement_policies
        SET drift_enabled = 0
        WHERE session_id IN (
            SELECT id FROM sessions WHERE chat_type = 'private'
        )
        """
    )
