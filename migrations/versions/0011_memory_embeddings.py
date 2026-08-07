"""增加长期记忆的派生 embedding 存储。"""

from alembic import op

from adapters.persistence.database import Base

revision = "0011_memory_embeddings"
down_revision = "0010_drift_default_on_drop_daily_limit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["memory_embeddings"].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["memory_embeddings"].drop(op.get_bind(), checkfirst=True)
