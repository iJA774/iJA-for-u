"""增加会话级记忆清空召回边界。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_memory_clear_boundary"
down_revision = "0006_long_term_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("sessions")}
    if "memory_cleared_at" not in columns:
        with op.batch_alter_table("sessions") as batch:
            batch.add_column(
                sa.Column(
                    "memory_cleared_at",
                    sa.DateTime(timezone=True),
                    nullable=True,
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("sessions")}
    if "memory_cleared_at" in columns:
        with op.batch_alter_table("sessions") as batch:
            batch.drop_column("memory_cleared_at")
