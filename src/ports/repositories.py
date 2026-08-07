"""应用服务按 bounded context 依赖的仓储协议。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from domain.models import (
    BehaviorPattern,
    BehaviorSelection,
    BlacklistEntry,
    DeliveryReceipt,
    DriftRun,
    EngagementPolicy,
    ExpressionAsset,
    FeedSource,
    GroupExpressionPattern,
    GroupParticipationPolicy,
    ImageAnalysis,
    InboundMessage,
    JargonTerm,
    MemoryConsolidationRun,
    MemoryRecord,
    MessageComponent,
    ModelAttempt,
    OutboundMessage,
    ProactiveCandidate,
    ProactiveRun,
    ProfileExtractionRun,
    ProfileFact,
    ScheduleRun,
    ScheduleTask,
    SessionView,
    SocialLearningRun,
    StoredMessage,
    ToolExecution,
    TurnDecision,
)


class SessionMessageReader(Protocol):
    """多个领域共享的只读 Session/消息投影。"""

    async def get_session(self, session_id: str) -> SessionView | None: ...

    async def session_epoch_is_current(
        self,
        session_id: str,
        data_epoch: int,
    ) -> bool: ...

    async def list_sessions(self) -> list[SessionView]: ...

    async def get_message(self, message_id: str) -> StoredMessage | None: ...

    async def list_recallable_messages(
        self,
        session_id: str,
        limit: int = 100,
        *,
        received_before: datetime | None = None,
    ) -> list[StoredMessage]: ...

    async def page_recallable_messages(
        self,
        session_id: str,
        *,
        limit: int,
        cursor_created_at: datetime | None = None,
        cursor_message_id: str | None = None,
        query_text: str | None = None,
    ) -> list[StoredMessage]: ...

    async def get_recallable_message(
        self,
        session_id: str,
        message_id: str,
    ) -> StoredMessage | None: ...

    async def messages_are_recallable(
        self,
        session_id: str,
        message_ids: list[str],
    ) -> bool: ...

    async def list_pending_messages(
        self,
        session_id: str,
        *,
        received_before: datetime | None = None,
    ) -> list[StoredMessage]: ...


@runtime_checkable
class ChatRepository(SessionMessageReader, Protocol):
    """Session、消息、Turn 与响应式出站的仓储边界。"""

    async def create_session(self, *args: Any, **kwargs: Any) -> SessionView: ...

    async def get_session_by_route(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> SessionView | None: ...

    async def update_session_members(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> SessionView: ...

    async def append_inbound(
        self,
        message: InboundMessage,
    ) -> tuple[StoredMessage, bool]: ...

    async def list_messages(
        self,
        session_id: str,
        limit: int = 100,
        *,
        received_before: datetime | None = None,
    ) -> list[StoredMessage]: ...

    async def search_session_messages(
        self,
        session_id: str,
        *,
        query_text: str,
        limit: int = 100,
    ) -> list[StoredMessage]: ...

    async def get_session_message_window(
        self,
        session_id: str,
        message_id: str,
        *,
        before: int = 50,
        after: int = 50,
    ) -> list[StoredMessage]: ...

    async def get_session_attachment(
        self,
        session_id: str,
        attachment_id: str,
    ) -> MessageComponent | None: ...

    async def save_decision(
        self,
        decision: TurnDecision,
        message_ids: list[str],
    ) -> None: ...

    async def save_reactive_turn_with_memory_run(
        self,
        decision: TurnDecision,
        message_ids: list[str],
        run: MemoryConsolidationRun,
    ) -> tuple[MemoryConsolidationRun, bool]: ...

    async def save_group_reactive_turn(
        self,
        decision: TurnDecision,
        message_ids: list[str],
        *,
        expected_state_version: int,
        external_at: datetime,
        observed_at: datetime,
        run: MemoryConsolidationRun | None,
    ) -> tuple[MemoryConsolidationRun | None, bool]: ...

    async def get_decision(self, decision_id: str) -> TurnDecision | None: ...

    async def list_turn_source_messages(
        self,
        session_id: str,
        decision_id: str,
    ) -> list[StoredMessage]: ...

    async def has_committed_reactive_reply(
        self,
        session_id: str,
        decision_id: str,
    ) -> bool: ...

    async def save_delivery(
        self,
        message: OutboundMessage,
        receipt: DeliveryReceipt,
    ) -> None: ...

    async def get_delivery(
        self,
        outbound_id: str,
    ) -> DeliveryReceipt | None: ...

    async def get_outbound_message(
        self,
        outbound_id: str,
    ) -> OutboundMessage | None: ...

    async def list_reactive_outbound_batch(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[OutboundMessage]: ...

    async def list_recoverable_reactive_outbound_batches(
        self,
    ) -> list[list[OutboundMessage]]: ...

    async def commit_outbound(
        self,
        message: OutboundMessage,
        sender_name: str,
    ) -> StoredMessage: ...

    async def advance_session_data_epochs(
        self,
        session_ids: list[str],
    ) -> dict[str, int]: ...

    async def clear_chat_content(
        self,
        session_id: str | None,
    ) -> dict[str, object]: ...

    async def list_referenced_attachment_paths(
        self,
        session_id: str | None = None,
    ) -> set[str]: ...

    async def delete_session(self, session_id: str) -> dict[str, object]: ...

    async def delete_all_local_user_data(self) -> dict[str, object]: ...


@runtime_checkable
class ProfileMemoryRepository(SessionMessageReader, Protocol):
    """画像事实、长期记忆、Embedding 与归档 Run 的仓储边界。"""

    async def list_facts(
        self,
        scope_key: str | None = None,
    ) -> list[ProfileFact]: ...

    async def save_extraction_run(self, run: ProfileExtractionRun) -> bool: ...

    async def get_extraction_run(
        self,
        run_id: str,
    ) -> ProfileExtractionRun | None: ...

    async def list_recoverable_runs(self) -> list[ProfileExtractionRun]: ...

    async def commit_profile_extraction(
        self,
        *,
        run: ProfileExtractionRun,
        facts: list[ProfileFact],
    ) -> tuple[list[ProfileFact], list[MemoryRecord]]: ...

    async def clear_memory_state(
        self,
        session_id: str | None,
    ) -> dict[str, object]: ...

    async def get_memory(self, memory_id: str) -> MemoryRecord | None: ...

    async def find_active_memory(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> MemoryRecord | None: ...

    async def list_memories(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[MemoryRecord]: ...

    async def search_active_memory_candidates(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[MemoryRecord] | None: ...

    async def page_memories(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[MemoryRecord]: ...

    async def page_memories_missing_embedding(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[MemoryRecord]: ...

    async def save_memory(self, memory: MemoryRecord) -> MemoryRecord: ...

    async def save_memories_batch_if_epoch(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[MemoryRecord]: ...

    async def record_memory_recall(self, memory_ids: list[str]) -> None: ...

    async def list_memory_embeddings(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, list[float]]: ...

    async def save_memory_embeddings(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None: ...

    async def create_memory_consolidation_run(
        self,
        run: MemoryConsolidationRun,
    ) -> tuple[MemoryConsolidationRun, bool]: ...

    async def save_memory_consolidation_run(
        self,
        run: MemoryConsolidationRun,
    ) -> MemoryConsolidationRun | None: ...

    async def get_memory_consolidation_run(
        self,
        run_id: str,
    ) -> MemoryConsolidationRun | None: ...

    async def list_recoverable_memory_runs(
        self,
    ) -> list[MemoryConsolidationRun]: ...


@runtime_checkable
class SocialLearningRepository(SessionMessageReader, Protocol):
    """黑话、群体表达、行为经验与整批学习事务的仓储边界。"""

    async def list_jargons(self, *args: Any, **kwargs: Any) -> list[JargonTerm]: ...

    async def page_jargons_for_maintenance(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[JargonTerm]: ...

    async def get_jargons_by_normalized_terms(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, JargonTerm]: ...

    async def list_group_expressions(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[GroupExpressionPattern]: ...

    async def page_group_expressions_for_maintenance(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[GroupExpressionPattern]: ...

    async def list_group_expression_session_ids(self) -> list[str]: ...

    async def list_group_expression_embeddings(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, dict[str, object]]: ...

    async def save_group_expression_embeddings(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None: ...

    async def list_group_expression_cluster_centers(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, object]]: ...

    async def save_group_expression_cluster_index(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None: ...

    async def mark_group_expressions_selected(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None: ...

    async def get_behavior_pattern(
        self,
        behavior_id: str,
    ) -> BehaviorPattern | None: ...

    async def list_behavior_patterns(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[BehaviorPattern]: ...

    async def page_behavior_patterns_for_maintenance(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[BehaviorPattern]: ...

    async def list_behavior_tag_vocabulary(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, str]]: ...

    async def backfill_behavior_scene_graph(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> int: ...

    async def retrieve_behavior_graph_scores(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, float]: ...

    async def apply_social_learning_maintenance(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None: ...

    async def create_behavior_selections(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[BehaviorSelection]: ...

    async def attach_behavior_reply(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None: ...

    async def list_pending_behavior_selections(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[BehaviorSelection]: ...

    async def save_behavior_feedback(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> BehaviorSelection: ...

    async def record_behavior_feedback_miss(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> BehaviorSelection: ...

    async def create_social_learning_run(
        self,
        run: SocialLearningRun,
    ) -> tuple[SocialLearningRun, bool]: ...

    async def save_social_learning_run(self, run: SocialLearningRun) -> bool: ...

    async def commit_social_learning_run(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> SocialLearningRun: ...

    async def list_social_learning_runs(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[SocialLearningRun]: ...

    async def get_social_learning_run(
        self,
        run_id: str,
    ) -> SocialLearningRun | None: ...

    async def list_recoverable_social_learning_runs(
        self,
    ) -> list[SocialLearningRun]: ...


@runtime_checkable
class OperationsRepository(SessionMessageReader, Protocol):
    """调度、主动行为、素材、工具与控制面状态的仓储边界。"""

    async def list_blacklist(self) -> list[BlacklistEntry]: ...

    async def get_blacklist(self, entry_id: str) -> BlacklistEntry | None: ...

    async def is_blacklisted(self, *args: Any, **kwargs: Any) -> bool: ...

    async def add_blacklist(self, entry: BlacklistEntry) -> BlacklistEntry: ...

    async def remove_blacklist(self, entry_id: str) -> BlacklistEntry: ...

    async def list_expressions(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[ExpressionAsset]: ...

    async def get_expression(
        self,
        expression_id: str,
    ) -> ExpressionAsset | None: ...

    async def get_expression_by_name(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> ExpressionAsset | None: ...

    async def get_expression_by_generation_key(
        self,
        generation_key: str,
    ) -> ExpressionAsset | None: ...

    async def create_expression(self, asset: ExpressionAsset) -> ExpressionAsset: ...

    async def update_expression_rename(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> ExpressionAsset: ...

    async def delete_expression(self, expression_id: str) -> ExpressionAsset: ...

    async def delete_expressions_for_character(
        self,
        character_id: str,
    ) -> list[ExpressionAsset]: ...

    async def delete_portrait_bound_expressions(
        self,
        character_id: str,
    ) -> list[ExpressionAsset]: ...

    async def record_expression_usage(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None: ...

    async def get_image_analysis(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> ImageAnalysis | None: ...

    async def save_image_analysis(self, analysis: ImageAnalysis) -> ImageAnalysis: ...

    async def save_tool_execution(self, execution: ToolExecution) -> None: ...

    async def list_tool_executions(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[ToolExecution]: ...

    async def save_model_attempt(self, attempt: ModelAttempt) -> None: ...

    async def create_schedule(self, schedule: ScheduleTask) -> ScheduleTask: ...

    async def get_schedule(self, schedule_id: str) -> ScheduleTask | None: ...

    async def list_schedules(
        self,
        session_id: str | None = None,
    ) -> list[ScheduleTask]: ...

    async def count_active_schedules(self, session_id: str) -> int: ...

    async def update_schedule(
        self,
        schedule: ScheduleTask,
        expected_revision: int,
    ) -> ScheduleTask: ...

    async def save_schedule_runtime(self, schedule: ScheduleTask) -> None: ...

    async def create_schedule_run(
        self,
        run: ScheduleRun,
    ) -> tuple[ScheduleRun, bool]: ...

    async def save_schedule_run(self, run: ScheduleRun) -> None: ...

    async def list_recoverable_schedule_runs(self) -> list[ScheduleRun]: ...

    async def list_engagement_policies(self) -> list[EngagementPolicy]: ...

    async def get_engagement_policy(
        self,
        session_id: str,
    ) -> EngagementPolicy | None: ...

    async def save_engagement_policy(
        self,
        policy: EngagementPolicy,
    ) -> EngagementPolicy: ...

    async def get_group_participation_policy(
        self,
        session_id: str,
    ) -> GroupParticipationPolicy: ...

    async def update_group_participation_policy(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> GroupParticipationPolicy: ...

    async def list_feed_sources(
        self,
        session_id: str | None = None,
    ) -> list[FeedSource]: ...

    async def list_due_feed_sources(self, *args: Any, **kwargs: Any) -> list[FeedSource]: ...

    async def get_feed_source(self, feed_id: str) -> FeedSource | None: ...

    async def create_feed_source(self, source: FeedSource) -> FeedSource: ...

    async def save_feed_source(self, source: FeedSource) -> FeedSource: ...

    async def delete_feed_source(self, feed_id: str) -> FeedSource: ...

    async def create_proactive_candidate(
        self,
        candidate: ProactiveCandidate,
    ) -> tuple[ProactiveCandidate, bool]: ...

    async def get_proactive_candidate(
        self,
        candidate_id: str,
    ) -> ProactiveCandidate | None: ...

    async def list_proactive_candidates(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[ProactiveCandidate]: ...

    async def save_proactive_candidate(
        self,
        candidate: ProactiveCandidate,
    ) -> ProactiveCandidate: ...

    async def save_proactive_run(self, run: ProactiveRun) -> ProactiveRun: ...

    async def save_proactive_run_with_candidates(
        self,
        run: ProactiveRun,
        candidates: list[ProactiveCandidate],
    ) -> ProactiveRun: ...

    async def prepare_proactive_delivery(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None: ...

    async def drop_prepared_proactive_delivery(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None: ...

    async def get_proactive_run(self, run_id: str) -> ProactiveRun | None: ...

    async def list_proactive_runs(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[ProactiveRun]: ...

    async def page_sent_proactive_runs_for_memory_reconciliation(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[ProactiveRun]: ...

    async def get_recallable_proactive_message_by_run(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> StoredMessage | None: ...

    async def list_recoverable_proactive_runs(self) -> list[ProactiveRun]: ...

    async def last_sent_proactive_run(
        self,
        session_id: str,
    ) -> ProactiveRun | None: ...

    async def save_drift_run(self, run: DriftRun) -> DriftRun: ...

    async def complete_drift_run_with_candidate(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[DriftRun, ProactiveCandidate, bool]: ...

    async def list_drift_runs(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> list[DriftRun]: ...

    async def latest_drift_run(self, session_id: str) -> DriftRun | None: ...

    async def list_recoverable_drift_runs(self) -> list[DriftRun]: ...

    async def list_recoverable_outbound_messages(
        self,
    ) -> list[OutboundMessage]: ...

    async def get_delivery(
        self,
        outbound_id: str,
    ) -> DeliveryReceipt | None: ...

    async def save_delivery(
        self,
        message: OutboundMessage,
        receipt: DeliveryReceipt,
    ) -> None: ...

    async def prepare_outbound_batch(
        self,
        messages: list[OutboundMessage],
    ) -> list[DeliveryReceipt]: ...

    async def transition_delivery_to_prepared(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> DeliveryReceipt: ...

    async def commit_outbound(
        self,
        message: OutboundMessage,
        sender_name: str,
    ) -> StoredMessage: ...


@runtime_checkable
class ApplicationRepository(
    ChatRepository,
    ProfileMemoryRepository,
    SocialLearningRepository,
    OperationsRepository,
    Protocol,
):
    """核心编排器的组合端口；仍由同一个 SQLite Store 实现。"""
