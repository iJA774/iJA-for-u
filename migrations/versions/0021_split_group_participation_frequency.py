"""拆分群聊积压触发条数与评分倍率。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_split_group_participation_frequency"
down_revision = "0020_group_participation_policy"
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
    """将旧频率拆成独立参数，并提高新评分倍率默认值。"""

    columns = _columns()
    with op.batch_alter_table("group_participation_policies") as batch:
        if "trigger_count" not in columns:
            batch.add_column(
                sa.Column(
                    "trigger_count",
                    sa.Integer(),
                    nullable=False,
                    server_default="3",
                )
            )
        if "frequency_factor" not in columns:
            batch.add_column(
                sa.Column(
                    "frequency_factor",
                    sa.Float(),
                    nullable=False,
                    server_default="0.9",
                )
            )

    # 沿用旧滑杆对积压压力的映射，避免已有自定义策略突然变得更积极；
    # 评分倍率则统一使用提高后的默认值 0.9。
    if "talk_frequency" in columns:
        op.execute(
            sa.text(
                """
                UPDATE group_participation_policies
                SET trigger_count = CASE
                    WHEN talk_frequency <= 0 THEN 999999
                    ELSE CAST(1.0 / (talk_frequency * talk_frequency) AS INTEGER)
                        + CASE
                            WHEN 1.0 / (talk_frequency * talk_frequency)
                                 > CAST(1.0 / (talk_frequency * talk_frequency) AS INTEGER)
                            THEN 1 ELSE 0
                          END
                END,
                    frequency_factor = 0.9
                """
            )
        )
        with op.batch_alter_table("group_participation_policies") as batch:
            batch.drop_column("talk_frequency")


def downgrade() -> None:
    """恢复旧列，供回退至 0020 的数据库结构使用。"""

    columns = _columns()
    with op.batch_alter_table("group_participation_policies") as batch:
        if "talk_frequency" not in columns:
            batch.add_column(
                sa.Column(
                    "talk_frequency",
                    sa.Float(),
                    nullable=False,
                    server_default="0.65",
                )
            )

    with op.batch_alter_table("group_participation_policies") as batch:
        batch.drop_column("frequency_factor")
        batch.drop_column("trigger_count")
