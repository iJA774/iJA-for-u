"""为社交学习增加分阶段推断和时间衰减游标。"""

import sqlalchemy as sa
from alembic import op

revision = "0015_social_learning_decay"
down_revision = "0014_vision_and_collected_expressions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加独立强化时间，避免选择行为伪装成新聊天证据。"""

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    jargon_columns = {
        column["name"] for column in inspector.get_columns("jargon_terms")
    }
    added_jargon_inference_cursor = (
        "last_inference_occurrence_count" not in jargon_columns
    )
    with op.batch_alter_table("jargon_terms") as batch:
        if "last_inference_occurrence_count" not in jargon_columns:
            batch.add_column(
                sa.Column(
                    "last_inference_occurrence_count",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )
        if "decay_count" not in jargon_columns:
            batch.add_column(
                sa.Column(
                    "decay_count",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )
        if "last_maintained_at" not in jargon_columns:
            batch.add_column(
                sa.Column(
                    "last_maintained_at",
                    sa.DateTime(timezone=True),
                    nullable=True,
                )
            )

    expression_columns = {
        column["name"]
        for column in inspector.get_columns("group_expression_patterns")
    }
    added_expression_reinforced_at = (
        "last_reinforced_at" not in expression_columns
    )
    with op.batch_alter_table("group_expression_patterns") as batch:
        if "last_reinforced_at" not in expression_columns:
            batch.add_column(
                sa.Column(
                    "last_reinforced_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.text("CURRENT_TIMESTAMP"),
                )
            )
        if "decay_count" not in expression_columns:
            batch.add_column(
                sa.Column(
                    "decay_count",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )
        if "last_maintained_at" not in expression_columns:
            batch.add_column(
                sa.Column(
                    "last_maintained_at",
                    sa.DateTime(timezone=True),
                    nullable=True,
                )
            )

    behavior_columns = {
        column["name"] for column in inspector.get_columns("behavior_patterns")
    }
    added_behavior_reinforced_at = "last_reinforced_at" not in behavior_columns
    with op.batch_alter_table("behavior_patterns") as batch:
        if "last_reinforced_at" not in behavior_columns:
            batch.add_column(
                sa.Column(
                    "last_reinforced_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=sa.text("CURRENT_TIMESTAMP"),
                )
            )
        if "decay_count" not in behavior_columns:
            batch.add_column(
                sa.Column(
                    "decay_count",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )
        if "last_maintained_at" not in behavior_columns:
            batch.add_column(
                sa.Column(
                    "last_maintained_at",
                    sa.DateTime(timezone=True),
                    nullable=True,
                )
            )

    if added_jargon_inference_cursor:
        op.execute(
            """
            UPDATE jargon_terms
            SET last_inference_occurrence_count =
                CASE WHEN inference_count > 0 THEN occurrence_count ELSE 0 END
            """
        )
    if added_expression_reinforced_at:
        op.execute(
            """
            UPDATE group_expression_patterns
            SET last_reinforced_at = updated_at
            """
        )
    if added_behavior_reinforced_at:
        op.execute(
            """
            UPDATE behavior_patterns
            SET last_reinforced_at = updated_at
            """
        )

    index_specs = (
        (
            "jargon_terms",
            "ix_jargon_terms_last_maintained_at",
            ["last_maintained_at"],
        ),
        (
            "group_expression_patterns",
            "ix_group_expression_patterns_last_reinforced_at",
            ["last_reinforced_at"],
        ),
        (
            "group_expression_patterns",
            "ix_group_expression_patterns_last_maintained_at",
            ["last_maintained_at"],
        ),
        (
            "behavior_patterns",
            "ix_behavior_patterns_last_reinforced_at",
            ["last_reinforced_at"],
        ),
        (
            "behavior_patterns",
            "ix_behavior_patterns_last_maintained_at",
            ["last_maintained_at"],
        ),
    )
    for table_name, index_name, columns in index_specs:
        existing_indexes = {
            item["name"] for item in sa.inspect(bind).get_indexes(table_name)
        }
        if index_name not in existing_indexes:
            op.create_index(index_name, table_name, columns)


def downgrade() -> None:
    """移除社交学习维护字段。"""

    op.drop_index(
        "ix_behavior_patterns_last_maintained_at",
        table_name="behavior_patterns",
    )
    op.drop_index(
        "ix_behavior_patterns_last_reinforced_at",
        table_name="behavior_patterns",
    )
    op.drop_index(
        "ix_group_expression_patterns_last_maintained_at",
        table_name="group_expression_patterns",
    )
    op.drop_index(
        "ix_group_expression_patterns_last_reinforced_at",
        table_name="group_expression_patterns",
    )
    op.drop_index(
        "ix_jargon_terms_last_maintained_at",
        table_name="jargon_terms",
    )

    with op.batch_alter_table("behavior_patterns") as batch:
        batch.drop_column("last_maintained_at")
        batch.drop_column("decay_count")
        batch.drop_column("last_reinforced_at")
    with op.batch_alter_table("group_expression_patterns") as batch:
        batch.drop_column("last_maintained_at")
        batch.drop_column("decay_count")
        batch.drop_column("last_reinforced_at")
    with op.batch_alter_table("jargon_terms") as batch:
        batch.drop_column("last_maintained_at")
        batch.drop_column("decay_count")
        batch.drop_column("last_inference_occurrence_count")
