"""删除已由文件人格仓储取代的数据库单例人格表。"""

import sqlalchemy as sa
from alembic import op

revision = "0003_remove_legacy_persona"
down_revision = "0002_tools_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "agent_persona" in inspector.get_table_names():
        op.drop_table("agent_persona")


def downgrade() -> None:
    # 旧人格字段已被明确废弃，降级不能凭空重建或伪造权威人格数据。
    pass
