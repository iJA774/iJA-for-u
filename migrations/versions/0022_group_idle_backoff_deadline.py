"""为群聊连续 idle 退让增加稳定截止时间。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_group_idle_backoff_deadline"
down_revision = "0021_split_group_participation_frequency"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(
            "group_participation_policies"
        )
    }


def upgrade() -> None:
    """增加可恢复的退让截止时间；旧运行态在升级后从下一次 idle 重新计时。"""

    if "idle_backoff_until" not in _columns():
        with op.batch_alter_table("group_participation_policies") as batch:
            batch.add_column(
                sa.Column("idle_backoff_until", sa.DateTime(), nullable=True)
            )


def downgrade() -> None:
    """移除退让截止时间字段。"""

    if "idle_backoff_until" in _columns():
        with op.batch_alter_table("group_participation_policies") as batch:
            batch.drop_column("idle_backoff_until")
