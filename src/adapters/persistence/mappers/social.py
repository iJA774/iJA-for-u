"""社交学习 Row 的纯映射。"""

from __future__ import annotations

import json
import math

from domain.models import (
    BehaviorActorType,
    BehaviorLearningType,
    BehaviorPattern,
    BehaviorSelection,
    BehaviorSelectionStatus,
    BehaviorTagGroup,
    ExtractionStatus,
    GroupExpressionPattern,
    JargonTerm,
    LearnedItemStatus,
    SocialLearningRun,
)

from ..schema import (
    BehaviorPatternRow,
    BehaviorSelectionRow,
    GroupExpressionPatternRow,
    JargonTermRow,
    SocialLearningRunRow,
)


def load_probability_mapping(raw: str, *, context: str) -> dict[str, float]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{context} 必须是 JSON object")
    result: dict[str, float] = {}
    for tag, value in parsed.items():
        numeric = float(value)
        if not isinstance(tag, str) or not tag or not math.isfinite(numeric):
            raise ValueError(f"{context} 包含非法标签或概率")
        if numeric > 0:
            result[tag] = numeric
    return result


def jargon_to_row(item: JargonTerm) -> JargonTermRow:
    return JargonTermRow(
        id=item.id,
        session_id=item.session_id,
        term=item.term,
        normalized_term=item.normalized_term,
        meaning=item.meaning,
        status=item.status.value,
        confidence=item.confidence,
        occurrence_count=item.occurrence_count,
        inference_count=item.inference_count,
        last_inference_occurrence_count=item.last_inference_occurrence_count,
        evidence_message_ids_json=json.dumps(item.evidence_message_ids, ensure_ascii=False),
        last_seen_at=item.last_seen_at,
        last_inferred_at=item.last_inferred_at,
        decay_count=item.decay_count,
        last_maintained_at=item.last_maintained_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def jargon_from_row(row: JargonTermRow) -> JargonTerm:
    return JargonTerm(
        id=row.id,
        session_id=row.session_id,
        term=row.term,
        normalized_term=row.normalized_term,
        meaning=row.meaning,
        status=LearnedItemStatus(row.status),
        confidence=row.confidence,
        occurrence_count=row.occurrence_count,
        inference_count=row.inference_count,
        last_inference_occurrence_count=row.last_inference_occurrence_count,
        evidence_message_ids=json.loads(row.evidence_message_ids_json),
        last_seen_at=row.last_seen_at,
        last_inferred_at=row.last_inferred_at,
        decay_count=row.decay_count,
        last_maintained_at=row.last_maintained_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def group_expression_to_row(
    item: GroupExpressionPattern,
) -> GroupExpressionPatternRow:
    return GroupExpressionPatternRow(
        id=item.id,
        session_id=item.session_id,
        situation=item.situation,
        style=item.style,
        pattern_hash=item.pattern_hash,
        status=item.status.value,
        confidence=item.confidence,
        occurrence_count=item.occurrence_count,
        selection_count=item.selection_count,
        evidence_message_ids_json=json.dumps(item.evidence_message_ids, ensure_ascii=False),
        last_reinforced_at=item.last_reinforced_at,
        last_selected_at=item.last_selected_at,
        decay_count=item.decay_count,
        last_maintained_at=item.last_maintained_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def group_expression_from_row(
    row: GroupExpressionPatternRow,
) -> GroupExpressionPattern:
    return GroupExpressionPattern(
        id=row.id,
        session_id=row.session_id,
        situation=row.situation,
        style=row.style,
        pattern_hash=row.pattern_hash,
        status=LearnedItemStatus(row.status),
        confidence=row.confidence,
        occurrence_count=row.occurrence_count,
        selection_count=row.selection_count,
        evidence_message_ids=json.loads(row.evidence_message_ids_json),
        last_reinforced_at=row.last_reinforced_at,
        last_selected_at=row.last_selected_at,
        decay_count=row.decay_count,
        last_maintained_at=row.last_maintained_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def behavior_pattern_to_row(item: BehaviorPattern) -> BehaviorPatternRow:
    return BehaviorPatternRow(
        id=item.id,
        session_id=item.session_id,
        scene_summary=item.scene_summary,
        scene_tags_json=json.dumps(item.scene_tags, ensure_ascii=False),
        need_tags_json=json.dumps(item.need_tags, ensure_ascii=False),
        other_traits_json=json.dumps(item.other_traits, ensure_ascii=False),
        tag_groups_json=json.dumps(
            [group.model_dump(mode="json") for group in item.tag_groups],
            ensure_ascii=False,
        ),
        tag_distribution_json=json.dumps(
            item.tag_distribution,
            ensure_ascii=False,
            sort_keys=True,
        ),
        scene_cluster_id=item.scene_cluster_id,
        action=item.action,
        expected_outcome=item.expected_outcome,
        pattern_hash=item.pattern_hash,
        actor_type=item.actor_type.value,
        learning_type=item.learning_type.value,
        status=item.status.value,
        confidence=item.confidence,
        occurrence_count=item.occurrence_count,
        activation_count=item.activation_count,
        success_count=item.success_count,
        failure_count=item.failure_count,
        score=item.score,
        evidence_message_ids_json=json.dumps(item.evidence_message_ids, ensure_ascii=False),
        last_reinforced_at=item.last_reinforced_at,
        last_selected_at=item.last_selected_at,
        last_feedback_at=item.last_feedback_at,
        decay_count=item.decay_count,
        last_maintained_at=item.last_maintained_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def behavior_pattern_from_row(
    row: BehaviorPatternRow,
) -> BehaviorPattern:
    return BehaviorPattern(
        id=row.id,
        session_id=row.session_id,
        scene_summary=row.scene_summary,
        scene_tags=json.loads(row.scene_tags_json),
        need_tags=json.loads(row.need_tags_json),
        other_traits=json.loads(row.other_traits_json),
        tag_groups=[BehaviorTagGroup.model_validate(item) for item in json.loads(row.tag_groups_json)],
        tag_distribution=load_probability_mapping(
            row.tag_distribution_json,
            context=f"行为经验 {row.id} 标签分布",
        ),
        scene_cluster_id=row.scene_cluster_id,
        action=row.action,
        expected_outcome=row.expected_outcome,
        pattern_hash=row.pattern_hash,
        actor_type=BehaviorActorType(row.actor_type),
        learning_type=BehaviorLearningType(row.learning_type),
        status=LearnedItemStatus(row.status),
        confidence=row.confidence,
        occurrence_count=row.occurrence_count,
        activation_count=row.activation_count,
        success_count=row.success_count,
        failure_count=row.failure_count,
        score=row.score,
        evidence_message_ids=json.loads(row.evidence_message_ids_json),
        last_reinforced_at=row.last_reinforced_at,
        last_selected_at=row.last_selected_at,
        last_feedback_at=row.last_feedback_at,
        decay_count=row.decay_count,
        last_maintained_at=row.last_maintained_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def behavior_selection_to_row(
    item: BehaviorSelection,
) -> BehaviorSelectionRow:
    return BehaviorSelectionRow(
        id=item.id,
        session_id=item.session_id,
        turn_id=item.turn_id,
        behavior_id=item.behavior_id,
        scene_summary=item.scene_summary,
        scene_tags_json=json.dumps(item.scene_tags, ensure_ascii=False),
        status=item.status.value,
        assistant_message_ids_json=json.dumps(item.assistant_message_ids, ensure_ascii=False),
        feedback_message_ids_json=json.dumps(item.feedback_message_ids, ensure_ascii=False),
        evaluation_attempts=item.evaluation_attempts,
        adopted=item.adopted,
        feedback_status=item.feedback_status,
        score_delta=item.score_delta,
        outcome=item.outcome,
        reason=item.reason,
        selected_at=item.selected_at,
        evaluated_at=item.evaluated_at,
    )


def behavior_selection_from_row(
    row: BehaviorSelectionRow,
) -> BehaviorSelection:
    return BehaviorSelection(
        id=row.id,
        session_id=row.session_id,
        turn_id=row.turn_id,
        behavior_id=row.behavior_id,
        scene_summary=row.scene_summary,
        scene_tags=json.loads(row.scene_tags_json),
        status=BehaviorSelectionStatus(row.status),
        assistant_message_ids=json.loads(row.assistant_message_ids_json),
        feedback_message_ids=json.loads(row.feedback_message_ids_json),
        evaluation_attempts=row.evaluation_attempts,
        adopted=row.adopted,
        feedback_status=row.feedback_status,
        score_delta=row.score_delta,
        outcome=row.outcome,
        reason=row.reason,
        selected_at=row.selected_at,
        evaluated_at=row.evaluated_at,
    )


def social_learning_run_to_row(
    run: SocialLearningRun,
) -> SocialLearningRunRow:
    return SocialLearningRunRow(
        id=run.id,
        session_id=run.session_id,
        source_run_id=run.source_run_id,
        source_message_ids_json=json.dumps(run.source_message_ids, ensure_ascii=False),
        source_fingerprint=run.source_fingerprint,
        data_epoch=run.data_epoch,
        status=run.status.value,
        produced_jargon_ids_json=json.dumps(run.produced_jargon_ids, ensure_ascii=False),
        produced_expression_ids_json=json.dumps(run.produced_expression_ids, ensure_ascii=False),
        produced_behavior_ids_json=json.dumps(run.produced_behavior_ids, ensure_ascii=False),
        error_code=run.error_code,
        error_message=run.error_message,
        attempt_count=run.attempt_count,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def social_learning_run_from_row(
    row: SocialLearningRunRow,
) -> SocialLearningRun:
    return SocialLearningRun(
        id=row.id,
        session_id=row.session_id,
        source_run_id=row.source_run_id,
        source_message_ids=json.loads(row.source_message_ids_json),
        source_fingerprint=row.source_fingerprint,
        data_epoch=row.data_epoch,
        status=ExtractionStatus(row.status),
        produced_jargon_ids=json.loads(row.produced_jargon_ids_json),
        produced_expression_ids=json.loads(row.produced_expression_ids_json),
        produced_behavior_ids=json.loads(row.produced_behavior_ids_json),
        error_code=row.error_code,
        error_message=row.error_message,
        attempt_count=row.attempt_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
