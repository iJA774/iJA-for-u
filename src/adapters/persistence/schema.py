"""SQLite ORM schema；只声明表结构和 UTC 时间边界。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from domain.models import BlacklistSource, ExpressionSourceKind, ParticipantRole


class UTCDateTime(TypeDecorator):
    """SQLite 不保留时区；在读写边界统一归一化为 aware UTC datetime。

    SQLite 的 ``DateTime(timezone=True)`` 实际是空操作：写入 aware datetime 后读出会
    丢失 tzinfo，导致 Pydantic 序列化不带时区后缀，前端 ``new Date()`` 把 UTC 数值误判
    为本地时间，显示偏差等于本地时区偏移。这里在读取边界把 SQLite 丢掉的 tzinfo 补回
    UTC；写入边界拒绝 naive datetime，强制调用方使用 utc_now() 或带时区时间。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        # 写入边界：拒绝 naive datetime，避免无时区时间悄悄入库。
        if value is None:
            return value
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None:
            raise ValueError("UTCDateTime 拒绝写入 naive datetime；请使用 utc_now() 或带时区时间")
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        # 读取边界：SQLite 丢掉 tzinfo，统一补回 UTC，确保序列化带时区后缀。
        if value is None:
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class IdentityRow(Base):
    __tablename__ = "identities"
    __table_args__ = (UniqueConstraint("platform", "account_id", "external_user_id"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    platform: Mapped[str] = mapped_column(String(50), index=True)
    account_id: Mapped[str] = mapped_column(String(200), index=True)
    external_user_id: Mapped[str] = mapped_column(String(200), index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    platform: Mapped[str] = mapped_column(String(50), index=True)
    account_id: Mapped[str] = mapped_column(String(200), index=True)
    external_chat_id: Mapped[str] = mapped_column(String(300), index=True)
    chat_type: Mapped[str] = mapped_column(String(20))
    display_name: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    data_epoch: Mapped[int] = mapped_column(Integer, default=1)
    memory_cleared_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class SessionMemberRow(Base):
    __tablename__ = "session_members"
    __table_args__ = (UniqueConstraint("session_id", "identity_id"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), index=True)
    role: Mapped[str] = mapped_column(String(20), default=ParticipantRole.MEMBER.value)
    joined_at: Mapped[datetime] = mapped_column(UTCDateTime())


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("platform", "account_id", "external_message_id"),
        Index(
            "ix_messages_session_origin_run",
            "session_id",
            "origin",
            "origin_run_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    platform: Mapped[str] = mapped_column(String(50))
    account_id: Mapped[str] = mapped_column(String(200))
    external_message_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    role: Mapped[str] = mapped_column(String(20))
    sender_id: Mapped[str] = mapped_column(String(200))
    sender_name: Mapped[str] = mapped_column(String(100))
    components_json: Mapped[str] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    processed_turn_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    origin: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    origin_run_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    source_refs_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class TurnDecisionRow(Base):
    __tablename__ = "turn_decisions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    action: Mapped[str] = mapped_column(String(20))
    strategy: Mapped[str] = mapped_column(String(80))
    score: Mapped[int] = mapped_column(Integer)
    threshold: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    score_detail_json: Mapped[str] = mapped_column(Text)
    trigger_message_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    group_participation_reply_recorded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class OutboundAttemptRow(Base):
    __tablename__ = "outbound_attempts"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    outbound_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    components_json: Mapped[str] = mapped_column(Text)
    reply_to_message_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    origin: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    origin_run_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    source_refs_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_message_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    expression_usage_recorded: Mapped[bool] = mapped_column(Boolean, default=False)


class ProfileFactRow(Base):
    __tablename__ = "profile_facts"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(200), index=True)
    scope_key: Mapped[str] = mapped_column(String(160), index=True)
    category: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ProfileFactSourceRow(Base):
    __tablename__ = "profile_fact_sources"
    __table_args__ = (UniqueConstraint("fact_id", "message_id"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    fact_id: Mapped[str] = mapped_column(ForeignKey("profile_facts.id"), index=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), index=True)


class ProfileExtractionRunRow(Base):
    __tablename__ = "profile_extraction_runs"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    subject_id: Mapped[str] = mapped_column(String(200))
    scope_key: Mapped[str] = mapped_column(String(160), index=True)
    source_message_ids_json: Mapped[str] = mapped_column(Text)
    data_epoch: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), index=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class MemoryRecordRow(Base):
    __tablename__ = "memory_records"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    scope_key: Mapped[str] = mapped_column(String(160), index=True)
    subject_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    importance: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), index=True)
    source_chain: Mapped[str] = mapped_column(String(30), index=True)
    source_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    source_message_ids_json: Mapped[str] = mapped_column(Text)
    source_refs_json: Mapped[str] = mapped_column(Text)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("memory_records.id"), nullable=True, index=True
    )
    happened_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    reinforcement: Mapped[int] = mapped_column(Integer, default=1)
    recall_count: Mapped[int] = mapped_column(Integer, default=0)
    last_recalled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class MemoryEmbeddingRow(Base):
    """记忆正文的派生向量；不是权威事实，可按模型或正文变更重建。"""

    __tablename__ = "memory_embeddings"

    memory_id: Mapped[str] = mapped_column(
        ForeignKey("memory_records.id", ondelete="CASCADE"), primary_key=True
    )
    model_name: Mapped[str] = mapped_column(String(300), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    dimension: Mapped[int] = mapped_column(Integer)
    vector_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class MemoryConsolidationRunRow(Base):
    __tablename__ = "memory_consolidation_runs"
    __table_args__ = (UniqueConstraint("source_fingerprint"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    scope_key: Mapped[str] = mapped_column(String(160), index=True)
    source_chain: Mapped[str] = mapped_column(String(30), index=True)
    source_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    source_message_ids_json: Mapped[str] = mapped_column(Text)
    source_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    data_epoch: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), index=True)
    produced_memory_ids_json: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class JargonTermRow(Base):
    """黑话候选与已确认释义的权威记录。"""

    __tablename__ = "jargon_terms"
    __table_args__ = (
        UniqueConstraint("session_id", "normalized_term"),
        Index("ix_jargon_terms_session_id_id", "session_id", "id"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    term: Mapped[str] = mapped_column(String(64))
    normalized_term: Mapped[str] = mapped_column(String(128), index=True)
    meaning: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    occurrence_count: Mapped[int] = mapped_column(Integer)
    inference_count: Mapped[int] = mapped_column(Integer)
    last_inference_occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    evidence_message_ids_json: Mapped[str] = mapped_column(Text)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    last_inferred_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    decay_count: Mapped[int] = mapped_column(Integer, default=0)
    last_maintained_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class GroupExpressionPatternRow(Base):
    """群聊“情境→表达方式”库。"""

    __tablename__ = "group_expression_patterns"
    __table_args__ = (
        UniqueConstraint("session_id", "pattern_hash"),
        Index(
            "ix_group_expression_patterns_session_id_id",
            "session_id",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    situation: Mapped[str] = mapped_column(String(200))
    style: Mapped[str] = mapped_column(String(200))
    pattern_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    occurrence_count: Mapped[int] = mapped_column(Integer)
    selection_count: Mapped[int] = mapped_column(Integer)
    evidence_message_ids_json: Mapped[str] = mapped_column(Text)
    last_reinforced_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    last_selected_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    decay_count: Mapped[int] = mapped_column(Integer, default=0)
    last_maintained_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class GroupExpressionEmbeddingRow(Base):
    """表达情境与风格的派生向量；内容或 profile 变化后可重建。"""

    __tablename__ = "group_expression_embeddings"

    expression_id: Mapped[str] = mapped_column(
        ForeignKey("group_expression_patterns.id", ondelete="CASCADE"),
        primary_key=True,
    )
    profile_marker: Mapped[str] = mapped_column(String(64), index=True)
    model_name: Mapped[str] = mapped_column(String(300), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    dimension: Mapped[int] = mapped_column(Integer)
    vector_json: Mapped[str] = mapped_column(Text)
    cluster_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cluster_fingerprint: Mapped[str] = mapped_column(String(64), default="", index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class GroupExpressionClusterCenterRow(Base):
    """Session 内某个表达索引快照的 K-means 中心。"""

    __tablename__ = "group_expression_cluster_centers"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "profile_marker",
            "index_fingerprint",
            "cluster_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    profile_marker: Mapped[str] = mapped_column(String(64), index=True)
    model_name: Mapped[str] = mapped_column(String(300), index=True)
    index_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    cluster_id: Mapped[int] = mapped_column(Integer)
    dimension: Mapped[int] = mapped_column(Integer)
    centroid_json: Mapped[str] = mapped_column(Text)
    member_count: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class BehaviorSceneTagAliasRow(Base):
    """Session 内行为标签同义簇成员索引。"""

    __tablename__ = "behavior_scene_tag_aliases"
    __table_args__ = (UniqueConstraint("session_id", "tag_kind", "normalized_tag"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    tag_kind: Mapped[str] = mapped_column(String(30), index=True)
    normalized_tag: Mapped[str] = mapped_column(String(80), index=True)
    display_tag: Mapped[str] = mapped_column(String(80))
    cluster_key: Mapped[str] = mapped_column(String(80), index=True)
    source_count: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class BehaviorSceneClusterRow(Base):
    """由 domain 标签概率分布描述的稳定行为场景簇。"""

    __tablename__ = "behavior_scene_clusters"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    tag_distribution_json: Mapped[str] = mapped_column(Text)
    source_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class BehaviorPatternRow(Base):
    """场景—行为—结果经验与反馈统计。"""

    __tablename__ = "behavior_patterns"
    __table_args__ = (
        UniqueConstraint("session_id", "pattern_hash"),
        Index("ix_behavior_patterns_session_id_id", "session_id", "id"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    scene_summary: Mapped[str] = mapped_column(Text)
    scene_tags_json: Mapped[str] = mapped_column(Text)
    need_tags_json: Mapped[str] = mapped_column(Text)
    other_traits_json: Mapped[str] = mapped_column(Text)
    tag_groups_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    tag_distribution_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    scene_cluster_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    action: Mapped[str] = mapped_column(Text)
    expected_outcome: Mapped[str] = mapped_column(Text)
    pattern_hash: Mapped[str] = mapped_column(String(64), index=True)
    actor_type: Mapped[str] = mapped_column(String(30), index=True)
    learning_type: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    occurrence_count: Mapped[int] = mapped_column(Integer)
    activation_count: Mapped[int] = mapped_column(Integer)
    success_count: Mapped[int] = mapped_column(Integer)
    failure_count: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    evidence_message_ids_json: Mapped[str] = mapped_column(Text)
    last_reinforced_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    last_selected_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_feedback_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    decay_count: Mapped[int] = mapped_column(Integer, default=0)
    last_maintained_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class BehaviorSelectionRow(Base):
    """行为 selector 的一次决策及其后续评价。"""

    __tablename__ = "behavior_selections"
    __table_args__ = (UniqueConstraint("turn_id", "behavior_id"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    turn_id: Mapped[str] = mapped_column(String(100), index=True)
    behavior_id: Mapped[str] = mapped_column(ForeignKey("behavior_patterns.id"), index=True)
    scene_summary: Mapped[str] = mapped_column(Text)
    scene_tags_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), index=True)
    assistant_message_ids_json: Mapped[str] = mapped_column(Text)
    feedback_message_ids_json: Mapped[str] = mapped_column(Text)
    evaluation_attempts: Mapped[int] = mapped_column(Integer)
    adopted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    feedback_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    score_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class SocialLearningRunRow(Base):
    """三类学习批次的可恢复状态。"""

    __tablename__ = "social_learning_runs"
    __table_args__ = (UniqueConstraint("source_fingerprint"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    source_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    source_message_ids_json: Mapped[str] = mapped_column(Text)
    source_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    data_epoch: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), index=True)
    produced_jargon_ids_json: Mapped[str] = mapped_column(Text)
    produced_expression_ids_json: Mapped[str] = mapped_column(Text)
    produced_behavior_ids_json: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class ScheduleRow(Base):
    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    created_by: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(120))
    instruction: Mapped[str] = mapped_column(Text)
    source_text: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(100))
    dtstart: Mapped[datetime] = mapped_column(UTCDateTime())
    rrule: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ScheduleRunRow(Base):
    __tablename__ = "schedule_runs"
    __table_args__ = (UniqueConstraint("schedule_id", "scheduled_for"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("schedules.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    missed_occurrences: Mapped[int] = mapped_column(Integer, default=0)
    outbound_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ToolExecutionRow(Base):
    __tablename__ = "tool_executions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(200), index=True)
    tool_name: Mapped[str] = mapped_column(String(100), index=True)
    arguments_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    turn_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    schedule_run_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ModelAttemptRow(Base):
    __tablename__ = "model_attempts"
    __table_args__ = (
        Index("ix_model_attempts_session_started", "session_id", "started_at"),
        Index("ix_model_attempts_task_started", "task", "started_at"),
        Index("ix_model_attempts_invocation_attempt", "invocation_id", "attempt_number"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    invocation_id: Mapped[str] = mapped_column(String(80), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    task: Mapped[str] = mapped_column(String(100), index=True)
    provider: Mapped[str] = mapped_column(String(100), index=True)
    profile: Mapped[str] = mapped_column(String(100), index=True)
    model: Mapped[str] = mapped_column(String(300))
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    turn_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    streamed: Mapped[bool] = mapped_column(Boolean)
    tool_call_count: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_source: Mapped[str] = mapped_column(String(20))
    latency_ms: Mapped[int] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, index=True)
    error_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cost_microusd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ExpressionAssetRow(Base):
    __tablename__ = "expression_assets"
    __table_args__ = (UniqueConstraint("character_id", "normalized_name"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    character_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(40))
    normalized_name: Mapped[str] = mapped_column(String(80))
    emotion: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(
        String(30), default=ExpressionSourceKind.GENERATED.value, index=True
    )
    generation_key: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True, index=True)
    source_portrait_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    storage_path: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(40))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ImageAnalysisRow(Base):
    __tablename__ = "image_analyses"
    __table_args__ = (UniqueConstraint("sha256", "provider_key", "prompt_version"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime_type: Mapped[str] = mapped_column(String(40))
    provider_key: Mapped[str] = mapped_column(String(500))
    prompt_version: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    emotions_json: Mapped[str] = mapped_column(Text)
    expression_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_expression: Mapped[bool] = mapped_column(Boolean, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class EngagementPolicyRow(Base):
    __tablename__ = "engagement_policies"

    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    proactive_enabled: Mapped[bool] = mapped_column(Boolean)
    drift_enabled: Mapped[bool] = mapped_column(Boolean)
    timezone: Mapped[str] = mapped_column(String(100))
    quiet_start: Mapped[str] = mapped_column(String(5))
    quiet_end: Mapped[str] = mapped_column(String(5))
    minimum_interval_minutes: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class GroupParticipationPolicyRow(Base):
    """群聊普通发言策略；配置 revision 与运行态 state_version 分离。"""

    __tablename__ = "group_participation_policies"

    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    mode: Mapped[str] = mapped_column(String(20))
    trigger_count: Mapped[int] = mapped_column(Integer)
    frequency_factor: Mapped[float] = mapped_column(Float)
    cooldown_seconds: Mapped[int] = mapped_column(Integer)
    idle_streak: Mapped[int] = mapped_column(Integer)
    idle_backoff_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_ordinary_reply_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_external_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    external_interval_ewma_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    external_interval_sample_count: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    state_version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class FeedSourceRow(Base):
    __tablename__ = "feed_sources"
    __table_args__ = (UniqueConstraint("session_id", "url"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, index=True)
    poll_interval_minutes: Mapped[int] = mapped_column(Integer)
    etag: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(500), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer)
    next_poll_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ProactiveCandidateRow(Base):
    __tablename__ = "proactive_candidates"
    __table_args__ = (UniqueConstraint("session_id", "source_kind", "source_key"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    source_kind: Mapped[str] = mapped_column(String(40), index=True)
    source_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    source_key: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    source_refs_json: Mapped[str] = mapped_column(Text)
    parent_candidate_ids_json: Mapped[str] = mapped_column(Text)
    aggregation_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ProactiveRunRow(Base):
    __tablename__ = "proactive_runs"
    __table_args__ = (
        Index(
            "ix_proactive_runs_session_status_id",
            "session_id",
            "status",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    stage: Mapped[str] = mapped_column(String(30), index=True)
    gate_reason: Mapped[str] = mapped_column(String(100))
    decision_code: Mapped[str] = mapped_column(String(100))
    decision_reason: Mapped[str] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidate_ids_json: Mapped[str] = mapped_column(Text)
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("proactive_candidates.id"), nullable=True, index=True
    )
    snapshot_at: Mapped[datetime] = mapped_column(UTCDateTime())
    snapshot_message_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    manual_triggered: Mapped[bool] = mapped_column(Boolean)
    outbound_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class DriftRunRow(Base):
    __tablename__ = "drift_runs"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    stage: Mapped[str] = mapped_column(String(30), index=True)
    activity: Mapped[str] = mapped_column(String(100))
    decision_reason: Mapped[str] = mapped_column(Text)
    resume_payload_json: Mapped[str] = mapped_column(Text)
    evidence_refs_json: Mapped[str] = mapped_column(Text)
    produced_candidate_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    snapshot_at: Mapped[datetime] = mapped_column(UTCDateTime())
    snapshot_message_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    resumed_from_run_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    auto_resume_count: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class BlacklistEntryRow(Base):
    """被本地拉黑的用户；维度与 IdentityRow 一致。"""

    __tablename__ = "blacklist_entries"
    __table_args__ = (UniqueConstraint("platform", "account_id", "external_user_id"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    platform: Mapped[str] = mapped_column(String(50), index=True)
    account_id: Mapped[str] = mapped_column(String(200), index=True)
    external_user_id: Mapped[str] = mapped_column(String(200), index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default=BlacklistSource.AGENT.value)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
