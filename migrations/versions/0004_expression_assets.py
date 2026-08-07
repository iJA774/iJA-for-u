"""增加按角色共享的可复用表情图库。"""

import sqlalchemy as sa
from alembic import op

revision = "0004_expression_assets"
down_revision = "0003_remove_legacy_persona"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 的历史实现会用“当前”ORM 元数据创建新库；新安装因此可能已经有本表。
    # 已有 0001-0003 数据库不会命中此分支，仍由本迁移正常建表。
    inspector = sa.inspect(op.get_bind())
    if "expression_assets" not in inspector.get_table_names():
        op.create_table(
            "expression_assets",
            sa.Column("id", sa.String(length=80), primary_key=True),
            sa.Column("character_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=40), nullable=False),
            sa.Column("normalized_name", sa.String(length=80), nullable=False),
            sa.Column("emotion", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("generation_key", sa.String(length=100), nullable=True),
            sa.Column("source_portrait_sha256", sa.String(length=64), nullable=False),
            sa.Column("storage_path", sa.Text(), nullable=False),
            sa.Column("mime_type", sa.String(length=40), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("width", sa.Integer(), nullable=False),
            sa.Column("height", sa.Integer(), nullable=False),
            sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("character_id", "normalized_name"),
        )
        op.create_index(
            "ix_expression_assets_character_id", "expression_assets", ["character_id"]
        )
        op.create_index(
            "ix_expression_assets_source_portrait_sha256",
            "expression_assets",
            ["source_portrait_sha256"],
        )
        op.create_index(
            "ix_expression_assets_generation_key",
            "expression_assets",
            ["generation_key"],
            unique=True,
        )
        op.create_index("ix_expression_assets_sha256", "expression_assets", ["sha256"])
    else:
        expression_columns = {
            column["name"]
            for column in sa.inspect(op.get_bind()).get_columns("expression_assets")
        }
        if "generation_key" not in expression_columns:
            with op.batch_alter_table("expression_assets") as batch:
                batch.add_column(
                    sa.Column("generation_key", sa.String(length=100), nullable=True)
                )
        expression_indexes = {
            index["name"]
            for index in sa.inspect(op.get_bind()).get_indexes("expression_assets")
        }
        if "ix_expression_assets_generation_key" not in expression_indexes:
            op.create_index(
                "ix_expression_assets_generation_key",
                "expression_assets",
                ["generation_key"],
                unique=True,
            )
    outbound_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("outbound_attempts")
    }
    if "expression_usage_recorded" not in outbound_columns:
        with op.batch_alter_table("outbound_attempts") as batch:
            batch.add_column(
                sa.Column(
                    "expression_usage_recorded",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    outbound_columns = {
        column["name"] for column in inspector.get_columns("outbound_attempts")
    }
    if "expression_usage_recorded" in outbound_columns:
        with op.batch_alter_table("outbound_attempts") as batch:
            batch.drop_column("expression_usage_recorded")
    if "expression_assets" in sa.inspect(op.get_bind()).get_table_names():
        indexes = {
            index["name"]
            for index in sa.inspect(op.get_bind()).get_indexes("expression_assets")
        }
        if "ix_expression_assets_generation_key" in indexes:
            op.drop_index(
                "ix_expression_assets_generation_key", table_name="expression_assets"
            )
        op.drop_index("ix_expression_assets_sha256", table_name="expression_assets")
        op.drop_index(
            "ix_expression_assets_source_portrait_sha256", table_name="expression_assets"
        )
        op.drop_index("ix_expression_assets_character_id", table_name="expression_assets")
        op.drop_table("expression_assets")
