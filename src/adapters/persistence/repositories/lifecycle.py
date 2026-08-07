"""四种用户数据生命周期语义的跨域 SQLite 事务。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.errors import NotFoundError
from domain.models import utc_now

from ..schema import (
    BehaviorPatternRow,
    BehaviorSceneClusterRow,
    BehaviorSceneTagAliasRow,
    BehaviorSelectionRow,
    BlacklistEntryRow,
    DriftRunRow,
    EngagementPolicyRow,
    ExpressionAssetRow,
    FeedSourceRow,
    GroupExpressionClusterCenterRow,
    GroupExpressionEmbeddingRow,
    GroupExpressionPatternRow,
    GroupParticipationPolicyRow,
    IdentityRow,
    ImageAnalysisRow,
    JargonTermRow,
    MemoryConsolidationRunRow,
    MemoryEmbeddingRow,
    MemoryRecordRow,
    MessageRow,
    ModelAttemptRow,
    OutboundAttemptRow,
    ProactiveCandidateRow,
    ProactiveRunRow,
    ProfileExtractionRunRow,
    ProfileFactRow,
    ProfileFactSourceRow,
    ScheduleRow,
    ScheduleRunRow,
    SessionMemberRow,
    SessionRow,
    SocialLearningRunRow,
    ToolExecutionRow,
    TurnDecisionRow,
)
from ..serialization import parse_components as _parse_components
from ._base import RepositoryMixinSupport


class DataLifecycleRepositoryMixin(RepositoryMixinSupport):
    """集中拥有跨聚合清理，保证每种语义只提交一次。"""

    @staticmethod
    async def _delete_matching(
        db: AsyncSession,
        model: Any,
        *criteria: Any,
    ) -> int:
        """物理删除匹配行并返回精确数量，供删除结果形成无正文审计快照。"""

        query = select(model)
        if criteria:
            query = query.where(*criteria)
        rows = (await db.execute(query)).scalars().all()
        for row in rows:
            await db.delete(row)
        return len(rows)

    @staticmethod
    async def _deletion_scope(
        db: AsyncSession,
        session_id: str | None,
    ) -> tuple[list[SessionRow], list[str], list[str]]:
        """解析删除作用域；指定 Session 不存在时拒绝伪造成功。"""

        query = select(SessionRow)
        if session_id is not None:
            query = query.where(SessionRow.id == session_id)
        sessions = list((await db.execute(query)).scalars().all())
        if session_id is not None and not sessions:
            raise NotFoundError("会话不存在")
        session_ids = [item.id for item in sessions]
        scope_keys = [f"{item.chat_type}:{item.id}" for item in sessions]
        return sessions, session_ids, scope_keys

    async def clear_memory_state(self, session_id: str | None = None) -> dict[str, object]:
        """只清空长期记忆、画像与社交学习，保留聊天正文和聊天审计。

        清空边界会阻止保留的旧聊天再次进入 Agent 上下文或重新派生记忆；
        Turn 决策、工具执行与出站审计仍引用真实存在的消息行，不产生悬空引用。
        """

        now = utc_now()
        async with self.session_factory() as db:
            sessions, session_ids, scope_keys = await self._deletion_scope(db, session_id)
            for item in sessions:
                item.memory_cleared_at = now

            counts: dict[str, int] = {}
            if session_ids:
                fact_ids = select(ProfileFactRow.id).where(ProfileFactRow.scope_key.in_(scope_keys))
                counts["profile_fact_source_count"] = await self._delete_matching(
                    db,
                    ProfileFactSourceRow,
                    ProfileFactSourceRow.fact_id.in_(fact_ids),
                )
                counts["profile_fact_count"] = await self._delete_matching(
                    db,
                    ProfileFactRow,
                    ProfileFactRow.scope_key.in_(scope_keys),
                )

                memory_ids = select(MemoryRecordRow.id).where(MemoryRecordRow.session_id.in_(session_ids))
                counts["memory_embedding_count"] = await self._delete_matching(
                    db,
                    MemoryEmbeddingRow,
                    MemoryEmbeddingRow.memory_id.in_(memory_ids),
                )
                expression_ids = select(GroupExpressionPatternRow.id).where(
                    GroupExpressionPatternRow.session_id.in_(session_ids)
                )
                counts["group_expression_embedding_count"] = await self._delete_matching(
                    db,
                    GroupExpressionEmbeddingRow,
                    GroupExpressionEmbeddingRow.expression_id.in_(expression_ids),
                )
                for label, model in (
                    ("behavior_selection_count", BehaviorSelectionRow),
                    ("behavior_pattern_count", BehaviorPatternRow),
                    ("behavior_scene_cluster_count", BehaviorSceneClusterRow),
                    ("behavior_scene_tag_alias_count", BehaviorSceneTagAliasRow),
                    ("group_expression_count", GroupExpressionPatternRow),
                    (
                        "expression_cluster_center_count",
                        GroupExpressionClusterCenterRow,
                    ),
                    ("jargon_count", JargonTermRow),
                    ("social_learning_run_count", SocialLearningRunRow),
                    ("extraction_run_count", ProfileExtractionRunRow),
                    ("memory_count", MemoryRecordRow),
                    ("consolidation_run_count", MemoryConsolidationRunRow),
                ):
                    counts[label] = await self._delete_matching(
                        db,
                        model,
                        model.session_id.in_(session_ids),
                    )
            await db.commit()
            return {
                "operation": "clear_long_term_memory",
                "scope": "session" if session_id is not None else "all",
                "session_id": session_id,
                "session_count": len(session_ids),
                **counts,
                "cleared_at": now.isoformat(),
            }

    async def clear_chat_content(self, session_id: str | None = None) -> dict[str, object]:
        """清空聊天正文和正文型审计，保留会话配置与长期知识摘要。

        保留的记忆、画像和社交学习条目会移除消息 ID、Run ID 等来源引用；
        主动、漂移与周期运行保留自带快照，但解除消息/出站 ID 引用。
        """

        now = utc_now()
        empty_json = json.dumps([], ensure_ascii=False)
        async with self.session_factory() as db:
            sessions, session_ids, _ = await self._deletion_scope(db, session_id)
            for item in sessions:
                item.memory_cleared_at = now

            counts: dict[str, int] = {}
            if session_ids:
                message_ids = select(MessageRow.id).where(MessageRow.session_id.in_(session_ids))
                counts["profile_fact_source_count"] = await self._delete_matching(
                    db,
                    ProfileFactSourceRow,
                    ProfileFactSourceRow.message_id.in_(message_ids),
                )
                for label, model in (
                    ("behavior_selection_count", BehaviorSelectionRow),
                    ("social_learning_run_count", SocialLearningRunRow),
                    ("extraction_run_count", ProfileExtractionRunRow),
                    ("consolidation_run_count", MemoryConsolidationRunRow),
                    ("tool_execution_count", ToolExecutionRow),
                    ("decision_count", TurnDecisionRow),
                    ("outbound_count", OutboundAttemptRow),
                ):
                    counts[label] = await self._delete_matching(
                        db,
                        model,
                        model.session_id.in_(session_ids),
                    )

                memory_rows = (
                    (
                        await db.execute(
                            select(MemoryRecordRow).where(MemoryRecordRow.session_id.in_(session_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in memory_rows:
                    row.source_message_ids_json = empty_json
                    row.source_refs_json = empty_json
                    row.source_run_id = None
                    row.updated_at = now
                counts["memory_provenance_scrubbed_count"] = len(memory_rows)

                for label, model in (
                    ("jargon_provenance_scrubbed_count", JargonTermRow),
                    (
                        "group_expression_provenance_scrubbed_count",
                        GroupExpressionPatternRow,
                    ),
                    (
                        "behavior_provenance_scrubbed_count",
                        BehaviorPatternRow,
                    ),
                ):
                    rows = (
                        (await db.execute(select(model).where(model.session_id.in_(session_ids))))
                        .scalars()
                        .all()
                    )
                    for row in rows:
                        row.evidence_message_ids_json = empty_json
                        row.updated_at = now
                    counts[label] = len(rows)

                proactive_rows = (
                    (
                        await db.execute(
                            select(ProactiveRunRow).where(ProactiveRunRow.session_id.in_(session_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in proactive_rows:
                    row.snapshot_message_id = None
                    row.outbound_id = None
                    row.updated_at = now
                counts["proactive_audit_detached_count"] = len(proactive_rows)

                drift_rows = (
                    (await db.execute(select(DriftRunRow).where(DriftRunRow.session_id.in_(session_ids))))
                    .scalars()
                    .all()
                )
                for row in drift_rows:
                    row.snapshot_message_id = None
                    row.updated_at = now
                counts["drift_audit_detached_count"] = len(drift_rows)

                schedule_rows = (
                    (
                        await db.execute(
                            select(ScheduleRunRow).where(ScheduleRunRow.session_id.in_(session_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in schedule_rows:
                    row.outbound_id = None
                    row.updated_at = now
                counts["schedule_audit_detached_count"] = len(schedule_rows)

                model_attempt_rows = (
                    (
                        await db.execute(
                            select(ModelAttemptRow).where(ModelAttemptRow.session_id.in_(session_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in model_attempt_rows:
                    row.turn_id = None
                    row.run_id = None
                counts["model_attempt_audit_detached_count"] = len(model_attempt_rows)
                counts["message_count"] = await self._delete_matching(
                    db,
                    MessageRow,
                    MessageRow.session_id.in_(session_ids),
                )

            await db.commit()
            return {
                "operation": "clear_chat_content",
                "scope": "session" if session_id is not None else "all",
                "session_id": session_id,
                "session_count": len(session_ids),
                **counts,
                "cleared_at": now.isoformat(),
            }

    async def list_referenced_attachment_paths(
        self,
        session_id: str | None = None,
    ) -> set[str]:
        """返回指定作用域消息与待发送正文引用的全部受控附件路径。"""

        async with self.session_factory() as db:
            message_query = select(MessageRow.components_json)
            outbound_query = select(OutboundAttemptRow.components_json)
            if session_id is not None:
                message_query = message_query.where(MessageRow.session_id == session_id)
                outbound_query = outbound_query.where(OutboundAttemptRow.session_id == session_id)
            message_components = (await db.execute(message_query)).scalars().all()
            outbound_components = (await db.execute(outbound_query)).scalars().all()
        paths: set[str] = set()
        for raw in (*message_components, *outbound_components):
            for component in _parse_components(raw):
                if component.storage_path:
                    paths.add(component.storage_path)
        return paths

    async def delete_session(self, session_id: str) -> dict[str, object]:
        """物理删除整个会话及其全部关联数据，会话从列表消失。

        调用方负责取消在途 Turn 与后台任务；本方法只保证数据库一致性，
        先删所有引用 sessions.id 的子表，最后删除会话行与成员行。独立的
        本地黑名单保留，但解除 Session 外键；仅删除不再被其他会话使用的身份。
        """

        async with self.session_factory() as db:
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise NotFoundError("会话不存在")
            scope_key = f"{session.chat_type}:{session.id}"
            member_identity_ids = list(
                (
                    await db.execute(
                        select(SessionMemberRow.identity_id).where(SessionMemberRow.session_id == session_id)
                    )
                )
                .scalars()
                .all()
            )
            counts: dict[str, int] = {}
            blacklist_rows = (
                (
                    await db.execute(
                        select(BlacklistEntryRow).where(BlacklistEntryRow.session_id == session_id)
                    )
                )
                .scalars()
                .all()
            )
            for row in blacklist_rows:
                row.session_id = None
            counts["blacklist_detached_count"] = len(blacklist_rows)

            fact_ids = select(ProfileFactRow.id).where(ProfileFactRow.scope_key == scope_key)
            counts["profile_fact_source_count"] = await self._delete_matching(
                db,
                ProfileFactSourceRow,
                ProfileFactSourceRow.fact_id.in_(fact_ids),
            )
            counts["behavior_selection_count"] = await self._delete_matching(
                db,
                BehaviorSelectionRow,
                BehaviorSelectionRow.session_id == session_id,
            )
            counts["behavior_pattern_count"] = await self._delete_matching(
                db,
                BehaviorPatternRow,
                BehaviorPatternRow.session_id == session_id,
            )
            counts["behavior_scene_cluster_count"] = await self._delete_matching(
                db,
                BehaviorSceneClusterRow,
                BehaviorSceneClusterRow.session_id == session_id,
            )
            counts["behavior_scene_tag_alias_count"] = await self._delete_matching(
                db,
                BehaviorSceneTagAliasRow,
                BehaviorSceneTagAliasRow.session_id == session_id,
            )
            expression_ids = select(GroupExpressionPatternRow.id).where(
                GroupExpressionPatternRow.session_id == session_id
            )
            counts["group_expression_embedding_count"] = await self._delete_matching(
                db,
                GroupExpressionEmbeddingRow,
                GroupExpressionEmbeddingRow.expression_id.in_(expression_ids),
            )
            counts["expression_cluster_center_count"] = await self._delete_matching(
                db,
                GroupExpressionClusterCenterRow,
                GroupExpressionClusterCenterRow.session_id == session_id,
            )
            counts["group_expression_count"] = await self._delete_matching(
                db,
                GroupExpressionPatternRow,
                GroupExpressionPatternRow.session_id == session_id,
            )
            for label, model in (
                ("jargon_count", JargonTermRow),
                ("social_learning_run_count", SocialLearningRunRow),
                ("message_count", MessageRow),
                ("decision_count", TurnDecisionRow),
                ("tool_execution_count", ToolExecutionRow),
                ("outbound_count", OutboundAttemptRow),
                ("extraction_run_count", ProfileExtractionRunRow),
                ("model_attempt_count", ModelAttemptRow),
                ("memory_count", MemoryRecordRow),
                ("consolidation_run_count", MemoryConsolidationRunRow),
                ("engagement_policy_count", EngagementPolicyRow),
                ("group_participation_policy_count", GroupParticipationPolicyRow),
                ("feed_count", FeedSourceRow),
                ("drift_run_count", DriftRunRow),
            ):
                if model is MemoryRecordRow:
                    memory_ids = select(MemoryRecordRow.id).where(MemoryRecordRow.session_id == session_id)
                    counts["memory_embedding_count"] = await self._delete_matching(
                        db,
                        MemoryEmbeddingRow,
                        MemoryEmbeddingRow.memory_id.in_(memory_ids),
                    )
                counts[label] = await self._delete_matching(
                    db,
                    model,
                    model.session_id == session_id,
                )
            counts["profile_fact_count"] = await self._delete_matching(
                db,
                ProfileFactRow,
                ProfileFactRow.scope_key == scope_key,
            )
            # proactive_runs 引用 candidates，必须先删 Run 再删候选。
            counts["proactive_run_count"] = await self._delete_matching(
                db,
                ProactiveRunRow,
                ProactiveRunRow.session_id == session_id,
            )
            counts["proactive_candidate_count"] = await self._delete_matching(
                db,
                ProactiveCandidateRow,
                ProactiveCandidateRow.session_id == session_id,
            )
            counts["schedule_run_count"] = await self._delete_matching(
                db,
                ScheduleRunRow,
                ScheduleRunRow.session_id == session_id,
            )
            counts["schedule_count"] = await self._delete_matching(
                db,
                ScheduleRow,
                ScheduleRow.session_id == session_id,
            )
            counts["member_count"] = await self._delete_matching(
                db,
                SessionMemberRow,
                SessionMemberRow.session_id == session_id,
            )
            await db.execute(delete(SessionRow).where(SessionRow.id == session_id))
            await db.flush()

            orphan_identity_count = 0
            for identity_id in member_identity_ids:
                remaining = (
                    await db.execute(
                        select(SessionMemberRow.id)
                        .where(SessionMemberRow.identity_id == identity_id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if remaining is None:
                    identity = await db.get(IdentityRow, identity_id)
                    if identity is not None:
                        await db.delete(identity)
                        orphan_identity_count += 1
            counts["orphan_identity_count"] = orphan_identity_count
            await db.commit()
            return {
                "operation": "delete_session",
                "session_id": session_id,
                **counts,
                "deleted_at": utc_now().isoformat(),
            }

    async def delete_all_local_user_data(self) -> dict[str, object]:
        """物理删除数据库中的全部用户数据，保留 schema 与应用配置。"""

        async with self.session_factory() as db:
            counts: dict[str, int] = {}
            for label, model in (
                ("profile_fact_source_count", ProfileFactSourceRow),
                ("behavior_selection_count", BehaviorSelectionRow),
                ("memory_embedding_count", MemoryEmbeddingRow),
                (
                    "group_expression_embedding_count",
                    GroupExpressionEmbeddingRow,
                ),
                ("schedule_run_count", ScheduleRunRow),
                ("proactive_run_count", ProactiveRunRow),
            ):
                counts[label] = await self._delete_matching(db, model)

            for label, model in (
                ("message_count", MessageRow),
                ("decision_count", TurnDecisionRow),
                ("outbound_count", OutboundAttemptRow),
                ("profile_fact_count", ProfileFactRow),
                ("extraction_run_count", ProfileExtractionRunRow),
                ("memory_count", MemoryRecordRow),
                ("consolidation_run_count", MemoryConsolidationRunRow),
                ("jargon_count", JargonTermRow),
                ("group_expression_count", GroupExpressionPatternRow),
                (
                    "expression_cluster_center_count",
                    GroupExpressionClusterCenterRow,
                ),
                ("behavior_scene_tag_alias_count", BehaviorSceneTagAliasRow),
                ("behavior_scene_cluster_count", BehaviorSceneClusterRow),
                ("behavior_pattern_count", BehaviorPatternRow),
                ("social_learning_run_count", SocialLearningRunRow),
                ("tool_execution_count", ToolExecutionRow),
                ("model_attempt_count", ModelAttemptRow),
                ("expression_asset_count", ExpressionAssetRow),
                ("image_analysis_count", ImageAnalysisRow),
                ("engagement_policy_count", EngagementPolicyRow),
                ("group_participation_policy_count", GroupParticipationPolicyRow),
                ("feed_count", FeedSourceRow),
                ("proactive_candidate_count", ProactiveCandidateRow),
                ("drift_run_count", DriftRunRow),
                ("schedule_count", ScheduleRow),
                ("blacklist_count", BlacklistEntryRow),
                ("member_count", SessionMemberRow),
                ("session_count", SessionRow),
                ("identity_count", IdentityRow),
            ):
                counts[label] = await self._delete_matching(db, model)
            await db.commit()
            return {
                "operation": "delete_all_local_user_data",
                **counts,
                "deleted_at": utc_now().isoformat(),
            }
