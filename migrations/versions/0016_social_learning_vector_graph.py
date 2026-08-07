"""增加表达向量聚类和行为场景标签图派生索引。"""

import sqlalchemy as sa
from alembic import op

revision = "0016_social_learning_vector_graph"
down_revision = "0015_social_learning_decay"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table_name: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _create_index_if_missing(
    name: str, table_name: str, columns: list[str]
) -> None:
    if name not in _index_names(table_name):
        op.create_index(name, table_name, columns)


def upgrade() -> None:
    """创建可重建向量/簇索引，并把行为路径接到场景簇。"""

    tables = _table_names()
    if "group_expression_embeddings" not in tables:
        op.create_table(
            "group_expression_embeddings",
            sa.Column("expression_id", sa.String(80), primary_key=True),
            sa.Column("profile_marker", sa.String(64), nullable=False),
            sa.Column("model_name", sa.String(300), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("dimension", sa.Integer(), nullable=False),
            sa.Column("vector_json", sa.Text(), nullable=False),
            sa.Column("cluster_id", sa.Integer(), nullable=True),
            sa.Column(
                "cluster_fingerprint",
                sa.String(64),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.ForeignKeyConstraint(
                ["expression_id"],
                ["group_expression_patterns.id"],
                ondelete="CASCADE",
            ),
        )
    if "group_expression_cluster_centers" not in tables:
        op.create_table(
            "group_expression_cluster_centers",
            sa.Column("id", sa.String(80), primary_key=True),
            sa.Column("session_id", sa.String(80), nullable=False),
            sa.Column("profile_marker", sa.String(64), nullable=False),
            sa.Column("model_name", sa.String(300), nullable=False),
            sa.Column("index_fingerprint", sa.String(64), nullable=False),
            sa.Column("cluster_id", sa.Integer(), nullable=False),
            sa.Column("dimension", sa.Integer(), nullable=False),
            sa.Column("centroid_json", sa.Text(), nullable=False),
            sa.Column("member_count", sa.Integer(), nullable=False),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
            sa.UniqueConstraint(
                "session_id",
                "profile_marker",
                "index_fingerprint",
                "cluster_id",
            ),
        )
    if "behavior_scene_tag_aliases" not in tables:
        op.create_table(
            "behavior_scene_tag_aliases",
            sa.Column("id", sa.String(80), primary_key=True),
            sa.Column("session_id", sa.String(80), nullable=False),
            sa.Column("tag_kind", sa.String(30), nullable=False),
            sa.Column("normalized_tag", sa.String(80), nullable=False),
            sa.Column("display_tag", sa.String(80), nullable=False),
            sa.Column("cluster_key", sa.String(80), nullable=False),
            sa.Column("source_count", sa.Integer(), nullable=False),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
            sa.UniqueConstraint(
                "session_id", "tag_kind", "normalized_tag"
            ),
        )
    if "behavior_scene_clusters" not in tables:
        op.create_table(
            "behavior_scene_clusters",
            sa.Column("id", sa.String(80), primary_key=True),
            sa.Column("session_id", sa.String(80), nullable=False),
            sa.Column(
                "tag_distribution_json", sa.Text(), nullable=False
            ),
            sa.Column("source_count", sa.Integer(), nullable=False),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        )

    behavior_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(
            "behavior_patterns"
        )
    }
    with op.batch_alter_table("behavior_patterns") as batch:
        if "tag_groups_json" not in behavior_columns:
            batch.add_column(
                sa.Column(
                    "tag_groups_json",
                    sa.Text(),
                    nullable=False,
                    server_default="[]",
                )
            )
        if "tag_distribution_json" not in behavior_columns:
            batch.add_column(
                sa.Column(
                    "tag_distribution_json",
                    sa.Text(),
                    nullable=False,
                    server_default="{}",
                )
            )
        if "scene_cluster_id" not in behavior_columns:
            batch.add_column(
                sa.Column("scene_cluster_id", sa.String(80), nullable=True)
            )

    indexes = (
        (
            "group_expression_embeddings",
            "ix_group_expression_embeddings_profile_marker",
            ["profile_marker"],
        ),
        (
            "group_expression_embeddings",
            "ix_group_expression_embeddings_model_name",
            ["model_name"],
        ),
        (
            "group_expression_embeddings",
            "ix_group_expression_embeddings_content_hash",
            ["content_hash"],
        ),
        (
            "group_expression_embeddings",
            "ix_group_expression_embeddings_cluster_fingerprint",
            ["cluster_fingerprint"],
        ),
        (
            "group_expression_embeddings",
            "ix_group_expression_embeddings_updated_at",
            ["updated_at"],
        ),
        (
            "group_expression_cluster_centers",
            "ix_group_expression_cluster_centers_session_id",
            ["session_id"],
        ),
        (
            "group_expression_cluster_centers",
            "ix_group_expression_cluster_centers_profile_marker",
            ["profile_marker"],
        ),
        (
            "group_expression_cluster_centers",
            "ix_group_expression_cluster_centers_model_name",
            ["model_name"],
        ),
        (
            "group_expression_cluster_centers",
            "ix_group_expression_cluster_centers_index_fingerprint",
            ["index_fingerprint"],
        ),
        (
            "group_expression_cluster_centers",
            "ix_group_expression_cluster_centers_updated_at",
            ["updated_at"],
        ),
        (
            "behavior_scene_tag_aliases",
            "ix_behavior_scene_tag_aliases_session_id",
            ["session_id"],
        ),
        (
            "behavior_scene_tag_aliases",
            "ix_behavior_scene_tag_aliases_tag_kind",
            ["tag_kind"],
        ),
        (
            "behavior_scene_tag_aliases",
            "ix_behavior_scene_tag_aliases_normalized_tag",
            ["normalized_tag"],
        ),
        (
            "behavior_scene_tag_aliases",
            "ix_behavior_scene_tag_aliases_cluster_key",
            ["cluster_key"],
        ),
        (
            "behavior_scene_tag_aliases",
            "ix_behavior_scene_tag_aliases_updated_at",
            ["updated_at"],
        ),
        (
            "behavior_scene_clusters",
            "ix_behavior_scene_clusters_session_id",
            ["session_id"],
        ),
        (
            "behavior_scene_clusters",
            "ix_behavior_scene_clusters_updated_at",
            ["updated_at"],
        ),
        (
            "behavior_patterns",
            "ix_behavior_patterns_scene_cluster_id",
            ["scene_cluster_id"],
        ),
    )
    for table_name, index_name, columns in indexes:
        _create_index_if_missing(index_name, table_name, columns)


def downgrade() -> None:
    """移除全部派生索引和行为路径图引用。"""

    behavior_indexes = _index_names("behavior_patterns")
    if "ix_behavior_patterns_scene_cluster_id" in behavior_indexes:
        op.drop_index(
            "ix_behavior_patterns_scene_cluster_id",
            table_name="behavior_patterns",
        )
    behavior_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(
            "behavior_patterns"
        )
    }
    with op.batch_alter_table("behavior_patterns") as batch:
        for column in (
            "scene_cluster_id",
            "tag_distribution_json",
            "tag_groups_json",
        ):
            if column in behavior_columns:
                batch.drop_column(column)
    for table_name in (
        "behavior_scene_clusters",
        "behavior_scene_tag_aliases",
        "group_expression_cluster_centers",
        "group_expression_embeddings",
    ):
        if table_name in _table_names():
            op.drop_table(table_name)
