from datetime import UTC, datetime, timedelta

from application.social_learning import (
    BehaviorFeedbackCandidate,
    SocialLearningService,
)
from config.settings import SocialLearningSettings
from domain.models import (
    BehaviorActorType,
    BehaviorLearningType,
    BehaviorPattern,
    GroupExpressionPattern,
    JargonTerm,
    LearnedItemStatus,
)


def _behavior(**updates) -> BehaviorPattern:
    values = {
        "session_id": "session-learning",
        "scene_summary": "对方带着问题请求技术排障",
        "scene_tags": ["求助", "技术排障"],
        "need_tags": ["追问关键信息"],
        "other_traits": ["困惑"],
        "action": "先确认信息边界，再追问一个关键配置点",
        "expected_outcome": "对方补充配置，排查方向更明确",
        "pattern_hash": "a" * 64,
        "actor_type": BehaviorActorType.AGENT_SELF,
        "learning_type": BehaviorLearningType.SELF_REFLECTION,
        "confidence": 0.8,
    }
    values.update(updates)
    return BehaviorPattern.model_validate(values)


def test_behavior_selector_uses_scene_and_feedback_weight() -> None:
    query = "接口一直报错，我该怎么排查这个配置问题？"
    query_tags = SocialLearningService._semantic_tags(query)
    successful = _behavior(score=2.0, success_count=3)
    failed = _behavior(
        id="behavior_failed",
        pattern_hash="b" * 64,
        score=-2.0,
        failure_count=3,
    )

    successful_score = SocialLearningService._behavior_match_score(
        successful,
        query_text=query,
        query_tags=query_tags,
    )
    failed_score = SocialLearningService._behavior_match_score(
        failed,
        query_text=query,
        query_tags=query_tags,
    )

    assert successful_score > failed_score
    assert successful_score > 0.16


def test_behavior_selector_rejects_unrelated_scene() -> None:
    pattern = _behavior()
    query = "晚安，明天见"
    score = SocialLearningService._behavior_match_score(
        pattern,
        query_text=query,
        query_tags=SocialLearningService._semantic_tags(query),
    )
    assert score < 0.16


def test_jargon_inference_only_runs_after_crossing_new_evidence_stage() -> None:
    item = JargonTerm(
        session_id="session-learning",
        term="yyds",
        normalized_term="yyds",
        occurrence_count=4,
        last_inference_occurrence_count=2,
    )

    assert SocialLearningService._jargon_inference_is_due(
        item, [2, 4, 8, 25, 100]
    )
    assert not SocialLearningService._jargon_inference_is_due(
        item.model_copy(update={"last_inference_occurrence_count": 4}),
        [2, 4, 8, 25, 100],
    )
    assert SocialLearningService._jargon_inference_is_due(
        item.model_copy(
            update={
                "occurrence_count": 101,
                "last_inference_occurrence_count": 100,
                "status": LearnedItemStatus.DISABLED,
            }
        ),
        [2, 4, 8, 25, 100],
    )


def test_stale_expression_decays_and_is_disabled() -> None:
    now = datetime(2026, 7, 29, tzinfo=UTC)
    pattern = GroupExpressionPattern(
        session_id="session-learning",
        situation="群友分享突然发生的离谱事情",
        style="用短促反问表达惊讶",
        pattern_hash="c" * 64,
        confidence=0.7,
        last_reinforced_at=now - timedelta(days=190),
    )

    updated, decayed, disabled = (
        SocialLearningService._maintain_group_expression(
            pattern,
            now=now,
            config=SocialLearningSettings(),
            force=True,
        )
    )

    assert updated is not None
    assert decayed
    assert disabled
    assert updated.status == LearnedItemStatus.DISABLED
    assert updated.confidence < 0.55
    assert updated.decay_count == 6


def test_stale_singleton_behavior_decays_until_disabled() -> None:
    now = datetime(2026, 7, 29, tzinfo=UTC)
    pattern = _behavior(
        occurrence_count=1,
        activation_count=0,
        last_reinforced_at=now - timedelta(days=140),
    )

    updated, decayed, disabled = SocialLearningService._maintain_behavior(
        pattern,
        now=now,
        config=SocialLearningSettings(),
        force=True,
    )

    assert updated is not None
    assert decayed
    assert disabled
    assert updated.status == LearnedItemStatus.DISABLED
    assert updated.score == -3.5


def test_expression_selector_prefers_diversity_after_relevance() -> None:
    first = GroupExpressionPattern(
        id="expression-first",
        session_id="session-learning",
        situation="遇到离谱消息时",
        style="短促反问表达震惊",
        pattern_hash="d" * 64,
    )
    duplicate = GroupExpressionPattern(
        id="expression-duplicate",
        session_id="session-learning",
        situation="遇到离谱消息时",
        style="短促反问表达震惊",
        pattern_hash="e" * 64,
    )
    diverse = GroupExpressionPattern(
        id="expression-diverse",
        session_id="session-learning",
        situation="对方认真求助时",
        style="先确认问题再追问信息",
        pattern_hash="f" * 64,
    )

    selected = SocialLearningService._select_diverse_expressions(
        [(0.9, first), (0.89, duplicate), (0.75, diverse)],
        limit=2,
    )

    assert [item.id for item in selected] == [
        "expression-first",
        "expression-diverse",
    ]


def test_behavior_feedback_score_is_normalized_by_status() -> None:
    feedback = BehaviorFeedbackCandidate(
        selection_id="selection-1",
        response_to_message_id="message-1",
        attribution="behavior",
        signal="direct",
        adopted=True,
        status="success",
        score_delta=-99,
        outcome="用户继续补充信息",
        reason="行为产生了预期推进",
        source_message_ids=["message-1"],
    )

    assert feedback.score_delta == 0.6


def test_content_praise_does_not_reward_expression_behavior() -> None:
    """答案有用与话术有效分别归因。"""
    feedback = BehaviorFeedbackCandidate(
        selection_id="s", response_to_message_id="a", adopted=True, attribution="content",
        signal="direct", status="success", score_delta=0.9,
        outcome="答案正确", reason="只能证明内容有帮助", source_message_ids=["a", "u"],
    )
    assert feedback.score_delta == 0
    continuation = BehaviorFeedbackCandidate(
        selection_id="s", response_to_message_id="a", adopted=True, attribution="behavior",
        signal="continuation", status="success", score_delta=0.9,
        outcome="继续同话题", reason="没有明确评价", source_message_ids=["a", "u"],
    )
    assert continuation.score_delta == 0.1


def test_cosine_kmeans_is_deterministic_and_keeps_every_cluster_non_empty() -> None:
    vectors = [
        [1.0, 0.0],
        [0.99, 0.01],
        [0.0, 1.0],
        [0.01, 0.99],
    ]

    first_labels, first_centers = SocialLearningService._run_cosine_kmeans(
        vectors, cluster_count=2
    )
    second_labels, second_centers = (
        SocialLearningService._run_cosine_kmeans(
            vectors, cluster_count=2
        )
    )

    assert first_labels == second_labels
    assert first_centers == second_centers
    assert set(first_labels) == {0, 1}
    assert first_labels[0] == first_labels[1]
    assert first_labels[2] == first_labels[3]
