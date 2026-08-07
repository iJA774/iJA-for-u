"""增加视觉理解缓存和聊天表情包来源状态。"""

import sqlalchemy as sa
from alembic import op

from adapters.persistence.database import Base

revision = "0014_vision_and_collected_expressions"
down_revision = "0013_social_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """升级表情素材来源字段并创建图片理解派生缓存。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    expression_columns = {
        column["name"] for column in inspector.get_columns("expression_assets")
    }
    with op.batch_alter_table("expression_assets") as batch:
        if "source_kind" not in expression_columns:
            batch.add_column(
                sa.Column(
                    "source_kind",
                    sa.String(length=30),
                    nullable=False,
                    server_default="generated",
                )
            )
        source_portrait = next(
            (
                column
                for column in inspector.get_columns("expression_assets")
                if column["name"] == "source_portrait_sha256"
            ),
            None,
        )
        if source_portrait is not None and not source_portrait["nullable"]:
            batch.alter_column(
                "source_portrait_sha256",
                existing_type=sa.String(length=64),
                nullable=True,
            )
    # 旧版本没有来源字段，但手动上传素材有稳定的描述前缀。先恢复其真实来源，
    # 避免后续替换基础形象时把用户上传的独立表情当成生成素材删除。
    op.execute(
        sa.text(
            "UPDATE expression_assets "
            "SET source_kind = 'uploaded' "
            "WHERE description LIKE '手动上传：%'"
        )
    )
    inspector = sa.inspect(bind)
    expression_indexes = {
        index["name"] for index in inspector.get_indexes("expression_assets")
    }
    if "ix_expression_assets_source_kind" not in expression_indexes:
        op.create_index(
            "ix_expression_assets_source_kind",
            "expression_assets",
            ["source_kind"],
        )
    Base.metadata.tables["image_analyses"].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    """移除视觉缓存并恢复旧表情 schema。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "image_analyses" in inspector.get_table_names():
        op.drop_table("image_analyses")
    expression_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("expression_assets")
    }
    if "ix_expression_assets_source_kind" in expression_indexes:
        op.drop_index(
            "ix_expression_assets_source_kind",
            table_name="expression_assets",
        )
    expression_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("expression_assets")
    }
    if "source_portrait_sha256" in expression_columns:
        # 旧 schema 无法表示与基础形象解绑的上传/收集素材。
        op.execute(
            sa.text(
                "DELETE FROM expression_assets "
                "WHERE source_portrait_sha256 IS NULL"
            )
        )
    with op.batch_alter_table("expression_assets") as batch:
        if "source_kind" in expression_columns:
            batch.drop_column("source_kind")
        batch.alter_column(
            "source_portrait_sha256",
            existing_type=sa.String(length=64),
            nullable=False,
        )
