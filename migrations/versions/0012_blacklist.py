"""增加本地黑名单表，记录不再受理请求的用户。"""

from alembic import op

from adapters.persistence.database import Base

revision = "0012_blacklist"
down_revision = "0011_memory_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["blacklist_entries"].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["blacklist_entries"].drop(op.get_bind(), checkfirst=True)
