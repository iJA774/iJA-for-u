"""增加黑话、群体表达与行为反馈学习库。"""

from alembic import op

from adapters.persistence.database import Base

revision = "0013_social_learning"
down_revision = "0012_blacklist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建三类学习权威表和可恢复运行记录。"""

    bind = op.get_bind()
    for name in (
        "jargon_terms",
        "group_expression_patterns",
        "behavior_patterns",
        "behavior_selections",
        "social_learning_runs",
    ):
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    """按外键依赖逆序移除学习表。"""

    for name in (
        "social_learning_runs",
        "behavior_selections",
        "behavior_patterns",
        "group_expression_patterns",
        "jargon_terms",
    ):
        op.drop_table(name)
