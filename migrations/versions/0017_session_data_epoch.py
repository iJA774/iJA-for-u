"""为 Session 派生任务增加持久生命周期 epoch。"""

import sqlalchemy as sa
from alembic import op

revision = "0017_session_data_epoch"
down_revision = "0016_social_learning_vector_graph"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    """旧数据从 epoch=1 起步；清空或删除前由生命周期 owner 单调推进。"""

    for table_name in (
        "sessions",
        "profile_extraction_runs",
        "memory_consolidation_runs",
        "social_learning_runs",
    ):
        if "data_epoch" in _columns(table_name):
            continue
        with op.batch_alter_table(table_name) as batch:
            batch.add_column(
                sa.Column(
                    "data_epoch",
                    sa.Integer(),
                    nullable=False,
                    server_default="1",
                )
            )


def downgrade() -> None:
    for table_name in (
        "social_learning_runs",
        "memory_consolidation_runs",
        "profile_extraction_runs",
        "sessions",
    ):
        if "data_epoch" not in _columns(table_name):
            continue
        with op.batch_alter_table(table_name) as batch:
            batch.drop_column("data_epoch")
