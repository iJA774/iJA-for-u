"""社交学习三条 lane、维护索引与反馈闭环的 SQLite 仓储实现。"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from domain.errors import ConflictError, InputValidationError, NotFoundError
from domain.models import (
    BehaviorPattern,
    BehaviorScenarioProfile,
    BehaviorSelection,
    BehaviorSelectionStatus,
    BehaviorTagGroup,
    BehaviorTagKind,
    ExtractionStatus,
    GroupExpressionPattern,
    JargonTerm,
    LearnedItemStatus,
    SocialLearningRun,
    new_id,
    utc_now,
)

from ..schema import (
    BehaviorPatternRow,
    BehaviorSceneClusterRow,
    BehaviorSceneTagAliasRow,
    BehaviorSelectionRow,
    GroupExpressionClusterCenterRow,
    GroupExpressionEmbeddingRow,
    GroupExpressionPatternRow,
    JargonTermRow,
    MessageRow,
    SessionRow,
    SocialLearningRunRow,
)
from ..serialization import parse_finite_vector as _parse_finite_vector
from ._base import RepositoryMixinSupport

_GENERIC_BEHAVIOR_TAGS = {
    "聊天",
    "用户",
    "消息",
    "问题",
    "对方",
    "群聊",
    "行为",
    "回应",
    "回复",
    "交流",
    "互动",
}


class SocialLearningRepositoryMixin(RepositoryMixinSupport):
    """实现黑话、表达、行为图谱与学习 Run 的同库事务。"""

    async def save_jargon_candidate(self, candidate: JargonTerm) -> JargonTerm:
        """按 Session 与规范词形合并黑话证据，保留已有释义。"""

        async with self.session_factory() as db:
            saved = await self._save_jargon_candidate_in_transaction(db, candidate)
            await db.commit()
            return saved

    async def _save_jargon_candidate_in_transaction(
        self,
        db: AsyncSession,
        candidate: JargonTerm,
    ) -> JargonTerm:
        """在调用方事务内合并黑话证据。"""

        row = (
            await db.execute(
                select(JargonTermRow).where(
                    JargonTermRow.session_id == candidate.session_id,
                    JargonTermRow.normalized_term == candidate.normalized_term,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = self._jargon_to_row(candidate)
            db.add(row)
        else:
            existing_evidence = json.loads(row.evidence_message_ids_json)
            existing_ids = set(existing_evidence)
            new_evidence = [
                message_id for message_id in candidate.evidence_message_ids if message_id not in existing_ids
            ]
            evidence = list(dict.fromkeys([*existing_evidence, *new_evidence]))[-100:]
            row.term = candidate.term
            row.evidence_message_ids_json = json.dumps(evidence, ensure_ascii=False)
            row.occurrence_count += len(new_evidence)
            row.last_seen_at = max(row.last_seen_at, candidate.last_seen_at)
            row.updated_at = candidate.updated_at
            if new_evidence:
                row.decay_count = 0
                row.last_maintained_at = None
        await db.flush()
        return self._jargon_from_row(row)

    async def save_jargon_inference(
        self,
        jargon_id: str,
        *,
        meaning: str,
        status: LearnedItemStatus,
        confidence: float,
    ) -> JargonTerm:
        """提交一次黑话语义推断，候选发现与语义确认保持不同写入口。"""

        async with self.session_factory() as db:
            saved = await self._save_jargon_inference_in_transaction(
                db,
                jargon_id,
                meaning=meaning,
                status=status,
                confidence=confidence,
            )
            await db.commit()
            return saved

    async def _save_jargon_inference_in_transaction(
        self,
        db: AsyncSession,
        jargon_id: str,
        *,
        meaning: str,
        status: LearnedItemStatus,
        confidence: float,
    ) -> JargonTerm:
        row = await db.get(JargonTermRow, jargon_id)
        if row is None:
            raise NotFoundError("黑话条目不存在")
        now = utc_now()
        row.meaning = meaning
        row.status = status.value
        row.confidence = confidence
        row.inference_count += 1
        row.last_inference_occurrence_count = row.occurrence_count
        row.last_inferred_at = now
        row.decay_count = 0
        row.last_maintained_at = None
        row.updated_at = now
        await db.flush()
        return self._jargon_from_row(row)

    async def get_jargon(self, jargon_id: str) -> JargonTerm | None:
        async with self.session_factory() as db:
            row = await db.get(JargonTermRow, jargon_id)
            return self._jargon_from_row(row) if row is not None else None

    async def list_jargons(
        self,
        session_id: str,
        *,
        active_only: bool = False,
        limit: int = 500,
    ) -> list[JargonTerm]:
        async with self.session_factory() as db:
            query = select(JargonTermRow).where(
                JargonTermRow.session_id == session_id
            )
            if active_only:
                query = query.where(
                    JargonTermRow.status == LearnedItemStatus.ACTIVE.value
                )
            query = query.order_by(
                JargonTermRow.occurrence_count.desc(),
                JargonTermRow.confidence.desc(),
                JargonTermRow.updated_at.desc(),
            ).limit(limit)
            rows = (await db.execute(query)).scalars().all()
            return [self._jargon_from_row(row) for row in rows]

    async def page_jargons_for_maintenance(
        self,
        session_id: str,
        *,
        after_id: str | None = None,
        limit: int = 200,
    ) -> list[JargonTerm]:
        """按不可变主键分页读取黑话，避免维护写回 updated_at 后跳页。"""

        if limit < 1 or limit > 1000:
            raise InputValidationError("黑话维护分页条数必须在 1 到 1000 之间")
        async with self.session_factory() as db:
            query = select(JargonTermRow).where(JargonTermRow.session_id == session_id)
            if after_id is not None:
                query = query.where(JargonTermRow.id > after_id)
            rows = (await db.execute(query.order_by(JargonTermRow.id.asc()).limit(limit))).scalars()
            return [self._jargon_from_row(row) for row in rows]

    async def get_jargons_by_normalized_terms(
        self,
        session_id: str,
        normalized_terms: set[str],
    ) -> dict[str, JargonTerm]:
        """按本批规范词形精确读取，避免准备阶段依赖固定数量扫描。"""

        if not normalized_terms:
            return {}
        async with self.session_factory() as db:
            rows = (
                (
                    await db.execute(
                        select(JargonTermRow).where(
                            JargonTermRow.session_id == session_id,
                            JargonTermRow.normalized_term.in_(normalized_terms),
                        )
                    )
                )
                .scalars()
                .all()
            )
            return {row.normalized_term: self._jargon_from_row(row) for row in rows}

    async def save_group_expression(self, pattern: GroupExpressionPattern) -> GroupExpressionPattern:
        """幂等合并同一 Session 的情境表达证据。"""

        async with self.session_factory() as db:
            saved = await self._save_group_expression_in_transaction(db, pattern)
            await db.commit()
            return saved

    async def _save_group_expression_in_transaction(
        self,
        db: AsyncSession,
        pattern: GroupExpressionPattern,
    ) -> GroupExpressionPattern:
        row = (
            await db.execute(
                select(GroupExpressionPatternRow).where(
                    GroupExpressionPatternRow.session_id == pattern.session_id,
                    GroupExpressionPatternRow.pattern_hash == pattern.pattern_hash,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = self._group_expression_to_row(pattern)
            db.add(row)
        else:
            existing_evidence = json.loads(row.evidence_message_ids_json)
            existing_ids = set(existing_evidence)
            new_evidence = [
                message_id for message_id in pattern.evidence_message_ids if message_id not in existing_ids
            ]
            evidence = list(dict.fromkeys([*existing_evidence, *new_evidence]))[-100:]
            row.evidence_message_ids_json = json.dumps(evidence, ensure_ascii=False)
            row.occurrence_count += len(new_evidence)
            row.confidence = max(row.confidence, pattern.confidence)
            row.updated_at = pattern.updated_at
            if new_evidence:
                row.status = pattern.status.value
                row.last_reinforced_at = max(row.last_reinforced_at, pattern.last_reinforced_at)
                row.decay_count = 0
                row.last_maintained_at = None
        await db.flush()
        return self._group_expression_from_row(row)

    async def list_group_expressions(
        self,
        session_id: str,
        *,
        active_only: bool = False,
        limit: int = 500,
    ) -> list[GroupExpressionPattern]:
        async with self.session_factory() as db:
            query = select(GroupExpressionPatternRow).where(
                GroupExpressionPatternRow.session_id == session_id
            )
            if active_only:
                query = query.where(
                    GroupExpressionPatternRow.status
                    == LearnedItemStatus.ACTIVE.value
                )
            query = query.order_by(
                GroupExpressionPatternRow.occurrence_count.desc(),
                GroupExpressionPatternRow.confidence.desc(),
                GroupExpressionPatternRow.updated_at.desc(),
            ).limit(limit)
            rows = (await db.execute(query)).scalars().all()
            return [self._group_expression_from_row(row) for row in rows]

    async def page_group_expressions_for_maintenance(
        self,
        session_id: str,
        *,
        after_id: str | None = None,
        limit: int = 200,
    ) -> list[GroupExpressionPattern]:
        """按不可变主键分页读取群表达，供完整时效维护使用。"""

        if limit < 1 or limit > 1000:
            raise InputValidationError("群表达维护分页条数必须在 1 到 1000 之间")
        async with self.session_factory() as db:
            query = select(GroupExpressionPatternRow).where(
                GroupExpressionPatternRow.session_id == session_id
            )
            if after_id is not None:
                query = query.where(GroupExpressionPatternRow.id > after_id)
            rows = (
                await db.execute(query.order_by(GroupExpressionPatternRow.id.asc()).limit(limit))
            ).scalars()
            return [self._group_expression_from_row(row) for row in rows]

    async def list_group_expression_session_ids(self) -> list[str]:
        """列出存在表达库的 Session，供派生向量后台回填。"""

        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(GroupExpressionPatternRow.session_id)
                    .distinct()
                    .order_by(GroupExpressionPatternRow.session_id)
                )
            ).scalars().all()
            return list(rows)

    async def list_group_expression_embeddings(
        self,
        *,
        expression_ids: list[str],
        profile_marker: str,
        content_hashes: dict[str, str],
    ) -> dict[str, dict[str, object]]:
        """只返回 profile 与当前表达内容均匹配的派生向量。"""

        if not expression_ids:
            return {}
        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(GroupExpressionEmbeddingRow).where(
                        GroupExpressionEmbeddingRow.expression_id.in_(
                            expression_ids
                        ),
                        GroupExpressionEmbeddingRow.profile_marker
                        == profile_marker,
                    )
                )
            ).scalars().all()
            result: dict[str, dict[str, object]] = {}
            for row in rows:
                if content_hashes.get(row.expression_id) != row.content_hash:
                    continue
                result[row.expression_id] = {
                    "vector": _parse_finite_vector(
                        row.vector_json,
                        expected_dimension=row.dimension,
                        context=f"表达向量 {row.expression_id}",
                    ),
                    "cluster_id": row.cluster_id,
                    "cluster_fingerprint": row.cluster_fingerprint,
                }
            return result

    async def save_group_expression_embeddings(
        self,
        *,
        profile_marker: str,
        model_name: str,
        items: list[tuple[str, str, list[float]]],
    ) -> None:
        """保存新向量并清空旧簇归属；聚类快照由独立原子入口发布。"""

        if not items:
            return
        dimensions = {len(vector) for _, _, vector in items}
        if len(dimensions) != 1 or 0 in dimensions:
            raise InputValidationError("同批表达 embedding 维度必须一致且非空")
        now = utc_now()
        async with self.session_factory() as db:
            for expression_id, content_hash, vector in items:
                if any(not math.isfinite(float(value)) for value in vector):
                    raise InputValidationError("表达 embedding 包含非有限数")
                expression = await db.get(
                    GroupExpressionPatternRow, expression_id
                )
                if expression is None:
                    raise NotFoundError("表达条目不存在")
                row = await db.get(GroupExpressionEmbeddingRow, expression_id)
                if row is None:
                    row = GroupExpressionEmbeddingRow(
                        expression_id=expression_id,
                        profile_marker=profile_marker,
                        model_name=model_name,
                        content_hash=content_hash,
                        dimension=len(vector),
                        vector_json=json.dumps(vector),
                        cluster_id=None,
                        cluster_fingerprint="",
                        updated_at=now,
                    )
                    db.add(row)
                else:
                    row.profile_marker = profile_marker
                    row.model_name = model_name
                    row.content_hash = content_hash
                    row.dimension = len(vector)
                    row.vector_json = json.dumps(vector)
                    row.cluster_id = None
                    row.cluster_fingerprint = ""
                    row.updated_at = now
            await db.commit()

    async def list_group_expression_cluster_centers(
        self,
        *,
        session_id: str,
        profile_marker: str,
        index_fingerprint: str,
    ) -> list[dict[str, object]]:
        """读取指定完整索引快照的有序 K-means 中心。"""

        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(GroupExpressionClusterCenterRow)
                    .where(
                        GroupExpressionClusterCenterRow.session_id
                        == session_id,
                        GroupExpressionClusterCenterRow.profile_marker
                        == profile_marker,
                        GroupExpressionClusterCenterRow.index_fingerprint
                        == index_fingerprint,
                    )
                    .order_by(GroupExpressionClusterCenterRow.cluster_id)
                )
            ).scalars().all()
            if rows and [row.cluster_id for row in rows] != list(
                range(len(rows))
            ):
                raise ValueError(
                    f"Session {session_id} 的表达聚类中心编号不连续"
                )
            return [
                {
                    "cluster_id": row.cluster_id,
                    "centroid": _parse_finite_vector(
                        row.centroid_json,
                        expected_dimension=row.dimension,
                        context=f"表达簇中心 {row.id}",
                    ),
                    "member_count": row.member_count,
                }
                for row in rows
            ]

    async def save_group_expression_cluster_index(
        self,
        *,
        session_id: str,
        profile_marker: str,
        model_name: str,
        index_fingerprint: str,
        assignments: dict[str, int],
        centers: list[list[float]],
    ) -> None:
        """原子发布表达簇成员与中心，避免读到半个聚类快照。"""

        if not assignments or not centers:
            raise InputValidationError("表达聚类快照不能为空")
        dimensions = {len(center) for center in centers}
        if len(dimensions) != 1 or 0 in dimensions:
            raise InputValidationError("表达聚类中心维度必须一致且非空")
        member_counts = Counter(assignments.values())
        if set(member_counts) != set(range(len(centers))):
            raise InputValidationError("表达聚类存在空簇或越界簇")
        now = utc_now()
        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(
                        GroupExpressionEmbeddingRow,
                        GroupExpressionPatternRow,
                    )
                    .join(
                        GroupExpressionPatternRow,
                        GroupExpressionPatternRow.id
                        == GroupExpressionEmbeddingRow.expression_id,
                    )
                    .where(
                        GroupExpressionEmbeddingRow.expression_id.in_(
                            list(assignments)
                        )
                    )
                )
            ).all()
            if len(rows) != len(assignments):
                raise InputValidationError("表达聚类引用了缺失向量")
            for embedding_row, expression_row in rows:
                if (
                    expression_row.session_id != session_id
                    or embedding_row.profile_marker != profile_marker
                ):
                    raise InputValidationError(
                        "表达聚类跨越 Session 或 embedding profile 边界"
                    )
                embedding_row.cluster_id = assignments[
                    embedding_row.expression_id
                ]
                embedding_row.cluster_fingerprint = index_fingerprint
                embedding_row.updated_at = now
            await db.execute(
                delete(GroupExpressionClusterCenterRow).where(
                    GroupExpressionClusterCenterRow.session_id == session_id,
                )
            )
            for cluster_id, center in enumerate(centers):
                if any(not math.isfinite(value) for value in center):
                    raise InputValidationError("表达聚类中心包含非有限数")
                db.add(
                    GroupExpressionClusterCenterRow(
                        id=new_id("expression_cluster"),
                        session_id=session_id,
                        profile_marker=profile_marker,
                        model_name=model_name,
                        index_fingerprint=index_fingerprint,
                        cluster_id=cluster_id,
                        dimension=len(center),
                        centroid_json=json.dumps(center),
                        member_count=member_counts[cluster_id],
                        updated_at=now,
                    )
                )
            await db.commit()

    async def mark_group_expressions_selected(
        self, expression_ids: list[str]
    ) -> None:
        if not expression_ids:
            return
        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(GroupExpressionPatternRow).where(
                        GroupExpressionPatternRow.id.in_(expression_ids)
                    )
                )
            ).scalars().all()
            now = utc_now()
            for row in rows:
                row.selection_count += 1
                row.last_selected_at = now
                row.updated_at = now
            await db.commit()

    async def save_behavior_pattern(
        self,
        pattern: BehaviorPattern,
        *,
        scene_cluster_reuse_threshold: float = 0.72,
    ) -> BehaviorPattern:
        """幂等合并同一场景—动作—结果经验。"""

        async with self.session_factory() as db:
            saved = await self._save_behavior_pattern_in_transaction(
                db,
                pattern,
                scene_cluster_reuse_threshold=scene_cluster_reuse_threshold,
            )
            await db.commit()
            return saved

    async def _save_behavior_pattern_in_transaction(
        self,
        db: AsyncSession,
        pattern: BehaviorPattern,
        *,
        scene_cluster_reuse_threshold: float,
    ) -> BehaviorPattern:
        row = (
            await db.execute(
                select(BehaviorPatternRow).where(
                    BehaviorPatternRow.session_id == pattern.session_id,
                    BehaviorPatternRow.pattern_hash == pattern.pattern_hash,
                )
            )
        ).scalar_one_or_none()
        has_new_evidence = True
        if row is None:
            row = self._behavior_pattern_to_row(pattern)
            db.add(row)
            await db.flush()
        else:
            existing_evidence = json.loads(row.evidence_message_ids_json)
            existing_ids = set(existing_evidence)
            new_evidence = [
                message_id for message_id in pattern.evidence_message_ids if message_id not in existing_ids
            ]
            evidence = list(dict.fromkeys([*existing_evidence, *new_evidence]))[-100:]
            row.evidence_message_ids_json = json.dumps(evidence, ensure_ascii=False)
            row.occurrence_count += len(new_evidence)
            row.confidence = max(row.confidence, pattern.confidence)
            row.updated_at = pattern.updated_at
            has_new_evidence = bool(new_evidence)
            if new_evidence:
                if not (row.failure_count >= 3 and row.score <= -4.0):
                    row.status = pattern.status.value
                row.last_reinforced_at = max(row.last_reinforced_at, pattern.last_reinforced_at)
                row.decay_count = 0
                row.last_maintained_at = None
                row.scene_summary = pattern.scene_summary
                row.scene_tags_json = json.dumps(pattern.scene_tags, ensure_ascii=False)
                row.need_tags_json = json.dumps(pattern.need_tags, ensure_ascii=False)
                row.other_traits_json = json.dumps(pattern.other_traits, ensure_ascii=False)
                row.tag_groups_json = json.dumps(
                    [item.model_dump(mode="json") for item in pattern.tag_groups],
                    ensure_ascii=False,
                )
        if has_new_evidence and pattern.tag_groups:
            await self._upsert_behavior_scene_graph(
                db,
                row=row,
                pattern=pattern,
                reuse_threshold=scene_cluster_reuse_threshold,
            )
        await db.flush()
        return self._behavior_pattern_from_row(row)

    async def get_behavior_pattern(
        self, behavior_id: str
    ) -> BehaviorPattern | None:
        async with self.session_factory() as db:
            row = await db.get(BehaviorPatternRow, behavior_id)
            return self._behavior_pattern_from_row(row) if row is not None else None

    async def list_behavior_patterns(
        self,
        session_id: str,
        *,
        active_only: bool = False,
        limit: int = 500,
    ) -> list[BehaviorPattern]:
        async with self.session_factory() as db:
            query = select(BehaviorPatternRow).where(
                BehaviorPatternRow.session_id == session_id
            )
            if active_only:
                query = query.where(
                    BehaviorPatternRow.status == LearnedItemStatus.ACTIVE.value
                )
            query = query.order_by(
                BehaviorPatternRow.score.desc(),
                BehaviorPatternRow.success_count.desc(),
                BehaviorPatternRow.occurrence_count.desc(),
                BehaviorPatternRow.updated_at.desc(),
            ).limit(limit)
            rows = (await db.execute(query)).scalars().all()
            return [self._behavior_pattern_from_row(row) for row in rows]

    async def page_behavior_patterns_for_maintenance(
        self,
        session_id: str,
        *,
        after_id: str | None = None,
        limit: int = 200,
    ) -> list[BehaviorPattern]:
        """按不可变主键分页读取行为经验，供完整时效维护使用。"""

        if limit < 1 or limit > 1000:
            raise InputValidationError("行为维护分页条数必须在 1 到 1000 之间")
        async with self.session_factory() as db:
            query = select(BehaviorPatternRow).where(BehaviorPatternRow.session_id == session_id)
            if after_id is not None:
                query = query.where(BehaviorPatternRow.id > after_id)
            rows = (await db.execute(query.order_by(BehaviorPatternRow.id.asc()).limit(limit))).scalars()
            return [self._behavior_pattern_from_row(row) for row in rows]

    async def list_behavior_tag_vocabulary(
        self, session_id: str, *, limit: int = 120
    ) -> list[dict[str, str]]:
        """返回高证据标签词表，引导场景分析器复用既有规范名。"""

        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(BehaviorSceneTagAliasRow)
                    .where(
                        BehaviorSceneTagAliasRow.session_id == session_id
                    )
                    .order_by(
                        BehaviorSceneTagAliasRow.source_count.desc(),
                        BehaviorSceneTagAliasRow.updated_at.desc(),
                    )
                    .limit(limit)
                )
            ).scalars().all()
            return [
                {
                    "kind": row.tag_kind,
                    "tag": row.display_tag,
                    "cluster_key": row.cluster_key,
                }
                for row in rows
            ]

    async def backfill_behavior_scene_graph(
        self, *, reuse_threshold: float
    ) -> int:
        """用已有平面标签确定性补建行为图，不调用 LLM、不增加证据计数。"""

        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(BehaviorPatternRow)
                    .where(BehaviorPatternRow.scene_cluster_id.is_(None))
                    .order_by(BehaviorPatternRow.created_at)
                )
            ).scalars().all()
            rebuilt = 0
            for row in rows:
                pattern = self._behavior_pattern_from_row(row)
                groups = pattern.tag_groups
                if not groups:
                    groups = [
                        *[
                            BehaviorTagGroup(
                                kind=BehaviorTagKind.DOMAIN, tags=[tag]
                            )
                            for tag in pattern.scene_tags
                        ],
                        *[
                            BehaviorTagGroup(
                                kind=BehaviorTagKind.NEED, tags=[tag]
                            )
                            for tag in pattern.need_tags
                        ],
                        *[
                            BehaviorTagGroup(
                                kind=BehaviorTagKind.ATTITUDE, tags=[tag]
                            )
                            for tag in pattern.other_traits
                        ],
                    ][:30]
                if not groups:
                    continue
                pattern = pattern.model_copy(
                    update={"tag_groups": groups}
                )
                row.tag_groups_json = json.dumps(
                    [
                        group.model_dump(mode="json")
                        for group in groups
                    ],
                    ensure_ascii=False,
                )
                await self._upsert_behavior_scene_graph(
                    db,
                    row=row,
                    pattern=pattern,
                    reuse_threshold=reuse_threshold,
                )
                rebuilt += 1
            await db.commit()
            return rebuilt

    async def get_social_learning_index_data(
        self, session_id: str
    ) -> dict[str, object]:
        """返回不含向量正文的表达簇与行为图数据，供审计和调试。"""

        async with self.session_factory() as db:
            centers = (
                await db.execute(
                    select(GroupExpressionClusterCenterRow)
                    .where(
                        GroupExpressionClusterCenterRow.session_id
                        == session_id
                    )
                    .order_by(
                        GroupExpressionClusterCenterRow.updated_at.desc(),
                        GroupExpressionClusterCenterRow.cluster_id,
                    )
                )
            ).scalars().all()
            aliases = (
                await db.execute(
                    select(BehaviorSceneTagAliasRow)
                    .where(
                        BehaviorSceneTagAliasRow.session_id == session_id
                    )
                    .order_by(
                        BehaviorSceneTagAliasRow.tag_kind,
                        BehaviorSceneTagAliasRow.cluster_key,
                        BehaviorSceneTagAliasRow.normalized_tag,
                    )
                )
            ).scalars().all()
            scene_clusters = (
                await db.execute(
                    select(BehaviorSceneClusterRow)
                    .where(
                        BehaviorSceneClusterRow.session_id == session_id
                    )
                    .order_by(BehaviorSceneClusterRow.updated_at.desc())
                )
            ).scalars().all()
            return {
                "expression_clusters": [
                    {
                        "profile_marker": row.profile_marker,
                        "model_name": row.model_name,
                        "index_fingerprint": row.index_fingerprint,
                        "cluster_id": row.cluster_id,
                        "dimension": row.dimension,
                        "member_count": row.member_count,
                        "updated_at": row.updated_at.isoformat(),
                    }
                    for row in centers
                ],
                "behavior_tag_aliases": [
                    {
                        "kind": row.tag_kind,
                        "tag": row.display_tag,
                        "cluster_key": row.cluster_key,
                        "source_count": row.source_count,
                    }
                    for row in aliases
                ],
                "behavior_scene_clusters": [
                    {
                        "scene_cluster_id": row.id,
                        "tag_distribution": (
                            self._load_probability_mapping(
                                row.tag_distribution_json,
                                context=f"行为场景簇 {row.id}",
                            )
                        ),
                        "source_count": row.source_count,
                        "updated_at": row.updated_at.isoformat(),
                    }
                    for row in scene_clusters
                ],
            }

    async def retrieve_behavior_graph_scores(
        self,
        *,
        session_id: str,
        profile: BehaviorScenarioProfile,
        max_depth: int,
        direct_lock_threshold: float,
    ) -> dict[str, float]:
        """按同义标签簇、场景分布和有限跳扩散召回行为经验。"""

        if not profile.tag_groups:
            return {}
        async with self.session_factory() as db:
            aliases = (
                await db.execute(
                    select(BehaviorSceneTagAliasRow).where(
                        BehaviorSceneTagAliasRow.session_id == session_id
                    )
                )
            ).scalars().all()
            alias_lookup = {
                (row.tag_kind, row.normalized_tag): row.cluster_key
                for row in aliases
            }
            query_full = self._behavior_distribution_from_groups(
                profile.tag_groups,
                alias_lookup=alias_lookup,
            )
            query_domain = {
                tag: probability
                for tag, probability in query_full.items()
                if tag.startswith(f"{BehaviorTagKind.DOMAIN.value}:")
            }
            if not query_domain:
                return {}
            query_domain = self._normalize_distribution(query_domain)
            behaviors = (
                await db.execute(
                    select(BehaviorPatternRow).where(
                        BehaviorPatternRow.session_id == session_id,
                        BehaviorPatternRow.status
                        == LearnedItemStatus.ACTIVE.value,
                        BehaviorPatternRow.scene_cluster_id.is_not(None),
                    )
                )
            ).scalars().all()
            active_cluster_ids = {
                row.scene_cluster_id
                for row in behaviors
                if row.scene_cluster_id is not None
            }
            if not active_cluster_ids:
                return {}
            clusters = (
                await db.execute(
                    select(BehaviorSceneClusterRow).where(
                        BehaviorSceneClusterRow.session_id == session_id,
                        BehaviorSceneClusterRow.id.in_(active_cluster_ids),
                    )
                )
            ).scalars().all()
            cluster_distributions = {
                row.id: self._load_probability_mapping(
                    row.tag_distribution_json,
                    context=f"行为场景簇 {row.id}",
                )
                for row in clusters
            }
            frequency_weights = self._behavior_frequency_weights(
                list(cluster_distributions.values())
            )
            direct_cluster_scores = {
                cluster_id: round(score * 2.0, 4)
                for cluster_id, distribution in cluster_distributions.items()
                if (
                    score := self._weighted_distribution_overlap(
                        query_domain,
                        distribution,
                        frequency_weights,
                    )
                )
                >= 0.30
            }
            adjacency = self._behavior_tag_adjacency(
                list(cluster_distributions.values())
            )
            expanded = self._expand_behavior_tags(
                set(query_domain),
                adjacency,
                max_depth=max_depth,
            )
            total_query_weight = sum(
                weight * frequency_weights.get(tag, 1.0)
                for tag, weight in expanded.items()
            )
            spread_cluster_scores: dict[str, float] = {}
            if total_query_weight > 0:
                for cluster_id, distribution in cluster_distributions.items():
                    shared = set(expanded) & set(distribution)
                    if not shared:
                        continue
                    hit_weight = sum(
                        expanded[tag]
                        * frequency_weights.get(tag, 1.0)
                        for tag in shared
                    )
                    spread_cluster_scores[cluster_id] = round(
                        hit_weight / total_query_weight * 2.0, 4
                    )
            def behavior_scores(
                cluster_scores: dict[str, float],
            ) -> dict[str, float]:
                scores: dict[str, float] = {}
                for row in behaviors:
                    cluster_score = cluster_scores.get(
                        row.scene_cluster_id or "", 0.0
                    )
                    if cluster_score <= 0:
                        continue
                    history_bonus = (
                        1.0 + min(float(row.occurrence_count), 20.0) * 0.02
                    )
                    path_distribution = self._load_probability_mapping(
                        row.tag_distribution_json,
                        context=f"行为经验 {row.id} 标签分布",
                    )
                    path_overlap = self._weighted_distribution_overlap(
                        query_full,
                        path_distribution,
                        frequency_weights,
                    )
                    scores[row.id] = (
                        cluster_score * history_bonus
                        + 0.2 * path_overlap
                    )
                return scores

            direct_scores = behavior_scores(direct_cluster_scores)
            spread_scores = behavior_scores(spread_cluster_scores)
            if max(direct_scores.values(), default=0.0) >= direct_lock_threshold:
                result = dict(direct_scores)
                for behavior_id, score in spread_scores.items():
                    result[behavior_id] = max(
                        result.get(behavior_id, 0.0), score * 0.25
                    )
            else:
                result = spread_scores
            return dict(
                sorted(
                    result.items(),
                    key=lambda item: (item[1], item[0]),
                    reverse=True,
                )[:48]
            )

    async def _upsert_behavior_scene_graph(
        self,
        db: AsyncSession,
        *,
        row: BehaviorPatternRow,
        pattern: BehaviorPattern,
        reuse_threshold: float,
    ) -> None:
        """在行为经验同一事务内更新同义簇、场景簇和路径引用。"""

        alias_lookup: dict[tuple[str, str], str] = {}
        for group in pattern.tag_groups:
            display_by_normalized: dict[str, str] = {}
            for value in group.tags:
                normalized_tag = self._normalize_behavior_tag(value)
                if (
                    normalized_tag
                    and normalized_tag not in _GENERIC_BEHAVIOR_TAGS
                ):
                    display_by_normalized.setdefault(normalized_tag, value)
            normalized_tags = list(display_by_normalized)
            if not normalized_tags:
                continue
            existing = (
                await db.execute(
                    select(BehaviorSceneTagAliasRow).where(
                        BehaviorSceneTagAliasRow.session_id
                        == pattern.session_id,
                        BehaviorSceneTagAliasRow.tag_kind
                        == group.kind.value,
                        BehaviorSceneTagAliasRow.normalized_tag.in_(
                            normalized_tags
                        ),
                    )
                )
            ).scalars().all()
            existing_keys = sorted(
                {item.cluster_key for item in existing}
            )
            cluster_key = (
                existing_keys[0]
                if existing_keys
                else f"tag_cluster_{new_id('tag').split('_', 1)[1]}"
            )
            if len(existing_keys) > 1:
                merge_rows = (
                    await db.execute(
                        select(BehaviorSceneTagAliasRow).where(
                            BehaviorSceneTagAliasRow.session_id
                            == pattern.session_id,
                            BehaviorSceneTagAliasRow.tag_kind
                            == group.kind.value,
                            BehaviorSceneTagAliasRow.cluster_key.in_(
                                existing_keys[1:]
                            ),
                        )
                    )
                ).scalars().all()
                for merge_row in merge_rows:
                    merge_row.cluster_key = cluster_key
                await self._rewrite_behavior_tag_cluster_keys(
                    db,
                    session_id=pattern.session_id,
                    kind=group.kind,
                    target_key=cluster_key,
                    replaced_keys=set(existing_keys[1:]),
                )
            existing_by_tag = {
                item.normalized_tag: item for item in existing
            }
            now = utc_now()
            for normalized_tag in normalized_tags:
                display_tag = display_by_normalized[normalized_tag]
                alias_row = existing_by_tag.get(normalized_tag)
                if alias_row is None:
                    alias_row = BehaviorSceneTagAliasRow(
                        id=new_id("behavior_tag"),
                        session_id=pattern.session_id,
                        tag_kind=group.kind.value,
                        normalized_tag=normalized_tag,
                        display_tag=display_tag,
                        cluster_key=cluster_key,
                        source_count=1,
                        updated_at=now,
                    )
                    db.add(alias_row)
                else:
                    alias_row.cluster_key = cluster_key
                    alias_row.source_count += 1
                    alias_row.updated_at = now
                alias_lookup[
                    (group.kind.value, normalized_tag)
                ] = cluster_key
        await db.flush()
        persisted_aliases = (
            await db.execute(
                select(BehaviorSceneTagAliasRow).where(
                    BehaviorSceneTagAliasRow.session_id
                    == pattern.session_id
                )
            )
        ).scalars().all()
        alias_lookup.update(
            {
                (item.tag_kind, item.normalized_tag): item.cluster_key
                for item in persisted_aliases
            }
        )
        full_distribution = self._behavior_distribution_from_groups(
            pattern.tag_groups,
            alias_lookup=alias_lookup,
        )
        row.tag_distribution_json = json.dumps(
            full_distribution, ensure_ascii=False, sort_keys=True
        )
        domain_distribution = self._normalize_distribution(
            {
                tag: probability
                for tag, probability in full_distribution.items()
                if tag.startswith(f"{BehaviorTagKind.DOMAIN.value}:")
            }
        )
        if not domain_distribution:
            row.scene_cluster_id = None
            return
        clusters = (
            await db.execute(
                select(BehaviorSceneClusterRow).where(
                    BehaviorSceneClusterRow.session_id == pattern.session_id
                )
            )
        ).scalars().all()
        best_cluster: BehaviorSceneClusterRow | None = None
        best_overlap = 0.0
        for candidate in clusters:
            existing_distribution = self._load_probability_mapping(
                candidate.tag_distribution_json,
                context=f"行为场景簇 {candidate.id}",
            )
            overlap = sum(
                min(domain_distribution[tag], existing_distribution[tag])
                for tag in set(domain_distribution)
                & set(existing_distribution)
            )
            if overlap > best_overlap:
                best_cluster = candidate
                best_overlap = overlap
        now = utc_now()
        if best_cluster is None or best_overlap < reuse_threshold:
            best_cluster = BehaviorSceneClusterRow(
                id=new_id("behavior_scene_cluster"),
                session_id=pattern.session_id,
                tag_distribution_json=json.dumps(
                    domain_distribution,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                source_count=1,
                created_at=now,
                updated_at=now,
            )
            db.add(best_cluster)
        else:
            existing_distribution = self._load_probability_mapping(
                best_cluster.tag_distribution_json,
                context=f"行为场景簇 {best_cluster.id}",
            )
            weight = max(best_cluster.source_count, 1)
            merged = {
                tag: (
                    existing_distribution.get(tag, 0.0) * weight
                    + domain_distribution.get(tag, 0.0)
                )
                / (weight + 1.0)
                for tag in set(existing_distribution) | set(domain_distribution)
            }
            best_cluster.tag_distribution_json = json.dumps(
                self._normalize_distribution(merged),
                ensure_ascii=False,
                sort_keys=True,
            )
            best_cluster.source_count += 1
            best_cluster.updated_at = now
        await db.flush()
        row.scene_cluster_id = best_cluster.id

    async def _rewrite_behavior_tag_cluster_keys(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        kind: BehaviorTagKind,
        target_key: str,
        replaced_keys: set[str],
    ) -> None:
        """同义簇合并时同步改写全部派生分布，避免旧 key 变成孤岛。"""

        if not replaced_keys:
            return
        target_tag = f"{kind.value}:{target_key}"

        def rewritten(raw: str, *, context: str) -> str:
            distribution = self._load_probability_mapping(
                raw, context=context
            )
            changed = False
            merged_weight = distribution.get(target_tag, 0.0)
            for replaced_key in replaced_keys:
                old_tag = f"{kind.value}:{replaced_key}"
                if old_tag in distribution:
                    merged_weight += distribution.pop(old_tag)
                    changed = True
            if not changed:
                return raw
            distribution[target_tag] = merged_weight
            return json.dumps(
                self._normalize_distribution(distribution),
                ensure_ascii=False,
                sort_keys=True,
            )

        scene_rows = (
            await db.execute(
                select(BehaviorSceneClusterRow).where(
                    BehaviorSceneClusterRow.session_id == session_id
                )
            )
        ).scalars().all()
        for scene_row in scene_rows:
            scene_row.tag_distribution_json = rewritten(
                scene_row.tag_distribution_json,
                context=f"行为场景簇 {scene_row.id}",
            )
        behavior_rows = (
            await db.execute(
                select(BehaviorPatternRow).where(
                    BehaviorPatternRow.session_id == session_id
                )
            )
        ).scalars().all()
        for behavior_row in behavior_rows:
            behavior_row.tag_distribution_json = rewritten(
                behavior_row.tag_distribution_json,
                context=f"行为经验 {behavior_row.id} 标签分布",
            )

    @staticmethod
    def _normalize_behavior_tag(value: str) -> str:
        return " ".join(value.casefold().split()).strip()[:80]

    @classmethod
    def _behavior_distribution_from_groups(
        cls,
        groups: list[BehaviorTagGroup],
        *,
        alias_lookup: dict[tuple[str, str], str],
    ) -> dict[str, float]:
        kind_weights = {
            BehaviorTagKind.DOMAIN: 1.25,
            BehaviorTagKind.NEED: 1.25,
            BehaviorTagKind.ATTITUDE: 1.1,
        }
        weights: dict[str, float] = {}
        for group in groups:
            normalized = [
                cls._normalize_behavior_tag(value)
                for value in group.tags
                if cls._normalize_behavior_tag(value)
                and cls._normalize_behavior_tag(value)
                not in _GENERIC_BEHAVIOR_TAGS
            ]
            if not normalized:
                continue
            cluster_key = next(
                (
                    alias_lookup[(group.kind.value, tag)]
                    for tag in normalized
                    if (group.kind.value, tag) in alias_lookup
                ),
                normalized[0],
            )
            key = f"{group.kind.value}:{cluster_key}"
            weights[key] = max(
                weights.get(key, 0.0), kind_weights[group.kind]
            )
        return cls._normalize_distribution(weights)

    @staticmethod
    def _normalize_distribution(
        mapping: dict[str, float],
    ) -> dict[str, float]:
        positive = {
            tag: float(value)
            for tag, value in mapping.items()
            if tag and math.isfinite(float(value)) and float(value) > 0
        }
        total = sum(positive.values())
        if total <= 0:
            return {}
        return {
            tag: round(value / total, 8)
            for tag, value in sorted(positive.items())
        }

    @staticmethod
    def _behavior_frequency_weights(
        distributions: list[dict[str, float]],
    ) -> dict[str, float]:
        document_frequency: Counter[str] = Counter()
        for distribution in distributions:
            document_frequency.update(distribution)
        if not document_frequency:
            return {}
        count = max(len(distributions), 1)
        raw: dict[str, float] = {}
        for tag, frequency in document_frequency.items():
            ratio = frequency / count
            inverse = 1.0 + math.log(
                (count + 1.0) / (frequency + 1.0)
            )
            rare_reliability = 1.0 - math.exp(-frequency / 2.0)
            common_gate = 1.0 / (1.0 + (ratio / 0.08) ** 1.8)
            raw[tag] = max(
                0.05,
                (1.0 + math.log(inverse))
                * rare_reliability
                * common_gate,
            )
        average = sum(raw.values()) / len(raw)
        return (
            {tag: value / average for tag, value in raw.items()}
            if average > 0
            else {tag: 1.0 for tag in raw}
        )

    @staticmethod
    def _weighted_distribution_overlap(
        query: dict[str, float],
        candidate: dict[str, float],
        frequency_weights: dict[str, float],
    ) -> float:
        shared = set(query) & set(candidate)
        if not shared:
            return 0.0
        query_weight = sum(
            value * frequency_weights.get(tag, 1.0)
            for tag, value in query.items()
        )
        if query_weight <= 0:
            return 0.0
        hit = sum(
            min(query[tag], candidate[tag])
            * frequency_weights.get(tag, 1.0)
            for tag in shared
        )
        return max(0.0, min(1.0, hit / query_weight))

    @staticmethod
    def _behavior_tag_adjacency(
        distributions: list[dict[str, float]],
    ) -> dict[str, set[str]]:
        adjacency: dict[str, set[str]] = defaultdict(set)
        for distribution in distributions:
            tags = sorted(distribution)
            for index, left in enumerate(tags):
                adjacency.setdefault(left, set())
                for right in tags[index + 1 :]:
                    adjacency[left].add(right)
                    adjacency[right].add(left)
        return adjacency

    @staticmethod
    def _expand_behavior_tags(
        direct_tags: set[str],
        adjacency: dict[str, set[str]],
        *,
        max_depth: int,
    ) -> dict[str, float]:
        weights = {tag: 1.0 for tag in direct_tags}
        visited = set(direct_tags)
        frontier = set(direct_tags)
        for depth in range(1, max_depth + 1):
            next_frontier = {
                neighbor
                for tag in frontier
                for neighbor in adjacency.get(tag, set())
            } - visited
            for tag in next_frontier:
                weights[tag] = 0.5**depth
            visited.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break
        return weights

    async def apply_social_learning_maintenance(
        self,
        *,
        jargons: list[JargonTerm],
        expressions: list[GroupExpressionPattern],
        behaviors: list[BehaviorPattern],
    ) -> None:
        """原子写回维护器允许修改的衰减、状态和维护游标字段。"""

        async with self.session_factory() as db:
            for item in jargons:
                row = await db.get(JargonTermRow, item.id)
                if row is None:
                    continue
                if row.session_id != item.session_id:
                    raise InputValidationError("黑话维护结果跨越了 Session 边界")
                row.confidence = item.confidence
                row.status = item.status.value
                row.decay_count = item.decay_count
                row.last_maintained_at = item.last_maintained_at
                row.updated_at = item.updated_at
            for item in expressions:
                row = await db.get(GroupExpressionPatternRow, item.id)
                if row is None:
                    continue
                if row.session_id != item.session_id:
                    raise InputValidationError("表达维护结果跨越了 Session 边界")
                row.confidence = item.confidence
                row.status = item.status.value
                row.decay_count = item.decay_count
                row.last_maintained_at = item.last_maintained_at
                row.updated_at = item.updated_at
            for item in behaviors:
                row = await db.get(BehaviorPatternRow, item.id)
                if row is None:
                    continue
                if row.session_id != item.session_id:
                    raise InputValidationError("行为维护结果跨越了 Session 边界")
                row.score = item.score
                row.status = item.status.value
                row.decay_count = item.decay_count
                row.last_maintained_at = item.last_maintained_at
                row.updated_at = item.updated_at
            await db.commit()

    async def create_behavior_selections(
        self, selections: list[BehaviorSelection]
    ) -> list[BehaviorSelection]:
        """原子记录 selector 决策并增加行为激活计数。"""

        if not selections:
            return []
        async with self.session_factory() as db:
            created: list[BehaviorSelectionRow] = []
            now = utc_now()
            for selection_item in selections:
                existing = (
                    await db.execute(
                        select(BehaviorSelectionRow).where(
                            BehaviorSelectionRow.turn_id == selection_item.turn_id,
                            BehaviorSelectionRow.behavior_id
                            == selection_item.behavior_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    created.append(existing)
                    continue
                row = self._behavior_selection_to_row(selection_item)
                db.add(row)
                behavior = await db.get(
                    BehaviorPatternRow, selection_item.behavior_id
                )
                if behavior is None or behavior.session_id != selection_item.session_id:
                    raise InputValidationError("行为选择引用了当前 Session 外的条目")
                behavior.activation_count += 1
                behavior.last_selected_at = now
                behavior.decay_count = 0
                behavior.last_maintained_at = None
                behavior.updated_at = now
                created.append(row)
            await db.commit()
            for row in created:
                await db.refresh(row)
            return [self._behavior_selection_from_row(row) for row in created]

    async def attach_behavior_reply(
        self, *, turn_id: str, message_id: str
    ) -> None:
        """将真实提交的助手消息绑定到本轮行为选择。"""

        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(BehaviorSelectionRow).where(
                        BehaviorSelectionRow.turn_id == turn_id,
                        BehaviorSelectionRow.status
                        == BehaviorSelectionStatus.PENDING.value,
                    )
                )
            ).scalars().all()
            for row in rows:
                message_ids = list(
                    dict.fromkeys(
                        [
                            *json.loads(row.assistant_message_ids_json),
                            message_id,
                        ]
                    )
                )[:20]
                row.assistant_message_ids_json = json.dumps(
                    message_ids, ensure_ascii=False
                )
            await db.commit()

    async def list_pending_behavior_selections(
        self, session_id: str, *, limit: int = 20
    ) -> list[BehaviorSelection]:
        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(BehaviorSelectionRow)
                    .where(
                        BehaviorSelectionRow.session_id == session_id,
                        BehaviorSelectionRow.status
                        == BehaviorSelectionStatus.PENDING.value,
                    )
                    .order_by(BehaviorSelectionRow.selected_at)
                    .limit(limit)
                )
            ).scalars().all()
            return [self._behavior_selection_from_row(row) for row in rows]

    async def save_behavior_feedback(
        self,
        selection_id: str,
        *,
        adopted: bool,
        feedback_status: str,
        score_delta: float,
        outcome: str,
        reason: str,
        feedback_message_ids: list[str],
    ) -> BehaviorSelection:
        """原子提交选择评价并更新对应行为经验的奖励统计。"""

        if feedback_status not in {"success", "partial_success", "failed"}:
            raise InputValidationError("未知的行为反馈状态")
        async with self.session_factory() as db:
            row = await db.get(BehaviorSelectionRow, selection_id)
            if row is None:
                raise NotFoundError("行为选择不存在")
            if row.status != BehaviorSelectionStatus.PENDING.value:
                return self._behavior_selection_from_row(row)
            now = utc_now()
            row.status = BehaviorSelectionStatus.EVALUATED.value
            row.evaluation_attempts += 1
            row.adopted = adopted
            row.feedback_status = feedback_status
            bounded_score_delta = max(-1.0, min(1.0, score_delta))
            row.score_delta = bounded_score_delta
            row.outcome = outcome
            row.reason = reason
            row.feedback_message_ids_json = json.dumps(
                list(dict.fromkeys(feedback_message_ids))[:20],
                ensure_ascii=False,
            )
            row.evaluated_at = now
            behavior = await db.get(BehaviorPatternRow, row.behavior_id)
            if behavior is None:
                raise NotFoundError("行为选择对应的行为条目不存在")
            if adopted:
                if feedback_status == "success":
                    behavior.success_count += 1
                elif feedback_status == "failed":
                    behavior.failure_count += 1
                behavior.score = max(
                    -5.0, min(5.0, behavior.score + bounded_score_delta)
                )
                behavior.last_feedback_at = now
                behavior.decay_count = 0
                behavior.last_maintained_at = None
                behavior.updated_at = now
            await db.commit()
            await db.refresh(row)
            return self._behavior_selection_from_row(row)

    async def record_behavior_feedback_miss(
        self, selection_id: str, *, max_attempts: int
    ) -> BehaviorSelection:
        """记录一次证据不足；达到上限后终止评价，避免无限重试。"""

        async with self.session_factory() as db:
            row = await db.get(BehaviorSelectionRow, selection_id)
            if row is None:
                raise NotFoundError("行为选择不存在")
            if row.status == BehaviorSelectionStatus.PENDING.value:
                row.evaluation_attempts += 1
                if row.evaluation_attempts >= max_attempts:
                    row.status = BehaviorSelectionStatus.EXPIRED.value
                    row.evaluated_at = utc_now()
                await db.commit()
                await db.refresh(row)
            return self._behavior_selection_from_row(row)

    async def create_social_learning_run(self, run: SocialLearningRun) -> tuple[SocialLearningRun, bool]:
        """按来源指纹幂等创建学习批次。"""

        async with self.session_factory() as db:
            await self._require_current_data_epoch(
                db,
                session_id=run.session_id,
                data_epoch=run.data_epoch,
            )
            existing = (
                await db.execute(
                    select(SocialLearningRunRow).where(
                        SocialLearningRunRow.source_fingerprint == run.source_fingerprint
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return self._social_learning_run_from_row(existing), False
            row = self._social_learning_run_to_row(run)
            db.add(row)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                existing = (
                    await db.execute(
                        select(SocialLearningRunRow).where(
                            SocialLearningRunRow.source_fingerprint == run.source_fingerprint
                        )
                    )
                ).scalar_one()
                return self._social_learning_run_from_row(existing), False
            return run, True

    async def save_social_learning_run(self, run: SocialLearningRun) -> bool:
        """只更新仍存在且 epoch 匹配的 Run，禁止 finalizer 复活孤儿行。"""

        async with self.session_factory() as db:
            row = await db.get(SocialLearningRunRow, run.id)
            if row is None:
                return False
            if row.data_epoch != run.data_epoch:
                return False
            session = await db.get(SessionRow, run.session_id)
            if session is None or session.data_epoch != run.data_epoch:
                return False
            row.status = run.status.value
            row.produced_jargon_ids_json = json.dumps(run.produced_jargon_ids, ensure_ascii=False)
            row.produced_expression_ids_json = json.dumps(run.produced_expression_ids, ensure_ascii=False)
            row.produced_behavior_ids_json = json.dumps(run.produced_behavior_ids, ensure_ascii=False)
            row.error_code = run.error_code
            row.error_message = run.error_message
            row.attempt_count = run.attempt_count
            row.updated_at = run.updated_at
            await db.commit()
            return True

    async def commit_social_learning_run(
        self,
        *,
        run: SocialLearningRun,
        jargon_candidates: list[JargonTerm],
        jargon_inferences: list[tuple[str, str, LearnedItemStatus, float]],
        expressions: list[GroupExpressionPattern],
        behaviors: list[BehaviorPattern],
        scene_cluster_reuse_threshold: float,
    ) -> SocialLearningRun:
        """原子提交三条学习 lane 及 Run 完成态。"""

        async with self._unit_of_work.transaction() as db:
            session = await self._require_current_data_epoch(
                db,
                session_id=run.session_id,
                data_epoch=run.data_epoch,
            )
            run_row = await db.get(SocialLearningRunRow, run.id)
            if run_row is None or run_row.data_epoch != run.data_epoch:
                raise ConflictError("社交学习批次已被清空或替换")
            source_ids = set(run.source_message_ids)
            source_query = select(MessageRow.id).where(
                MessageRow.id.in_(source_ids),
                MessageRow.session_id == run.session_id,
            )
            if session.memory_cleared_at is not None:
                source_query = source_query.where(MessageRow.created_at > session.memory_cleared_at)
            current_source_ids = set((await db.execute(source_query)).scalars().all())
            if current_source_ids != source_ids:
                raise ConflictError("社交学习来源消息已被清空或删除")

            saved_jargons = [
                await self._save_jargon_candidate_in_transaction(db, item) for item in jargon_candidates
            ]
            saved_jargon_ids = {item.id for item in saved_jargons}
            for jargon_id, meaning, status, confidence in jargon_inferences:
                if jargon_id not in saved_jargon_ids:
                    raise InputValidationError("黑话推断不属于本批候选")
                await self._save_jargon_inference_in_transaction(
                    db,
                    jargon_id,
                    meaning=meaning,
                    status=status,
                    confidence=confidence,
                )
            saved_expressions = [
                await self._save_group_expression_in_transaction(db, item) for item in expressions
            ]
            saved_behaviors = [
                await self._save_behavior_pattern_in_transaction(
                    db,
                    item,
                    scene_cluster_reuse_threshold=(scene_cluster_reuse_threshold),
                )
                for item in behaviors
            ]

            run.produced_jargon_ids = [item.id for item in saved_jargons]
            run.produced_expression_ids = [item.id for item in saved_expressions]
            run.produced_behavior_ids = [item.id for item in saved_behaviors]
            run.status = ExtractionStatus.COMPLETED
            run.error_code = None
            run.error_message = None
            run.updated_at = utc_now()
            values = self._social_learning_run_to_row(run)
            for column in (
                "status",
                "produced_jargon_ids_json",
                "produced_expression_ids_json",
                "produced_behavior_ids_json",
                "error_code",
                "error_message",
                "attempt_count",
                "updated_at",
            ):
                setattr(run_row, column, getattr(values, column))
            return run

    async def list_social_learning_runs(
        self, session_id: str | None = None, *, limit: int = 500
    ) -> list[SocialLearningRun]:
        async with self.session_factory() as db:
            query = select(SocialLearningRunRow)
            if session_id is not None:
                query = query.where(
                    SocialLearningRunRow.session_id == session_id
                )
            rows = (
                await db.execute(
                    query.order_by(SocialLearningRunRow.created_at.desc()).limit(
                        limit
                    )
                )
            ).scalars().all()
            return [self._social_learning_run_from_row(row) for row in rows]

    async def get_social_learning_run(
        self, run_id: str
    ) -> SocialLearningRun | None:
        async with self.session_factory() as db:
            row = await db.get(SocialLearningRunRow, run_id)
            return (
                self._social_learning_run_from_row(row)
                if row is not None
                else None
            )

    async def list_recoverable_social_learning_runs(
        self,
    ) -> list[SocialLearningRun]:
        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(SocialLearningRunRow).where(
                        SocialLearningRunRow.status.in_(
                            [
                                ExtractionStatus.PENDING.value,
                                ExtractionStatus.RUNNING.value,
                            ]
                        )
                    )
                )
            ).scalars().all()
            return [self._social_learning_run_from_row(row) for row in rows]
