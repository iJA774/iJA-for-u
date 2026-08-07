"""黑话、群体表达和行为经验的独立学习闭环。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections import defaultdict
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from application.events import EventHub
from application.model_json import parse_model_json
from config import AppSettings
from config.settings import SocialLearningSettings
from domain.errors import (
    ConflictError,
    InputValidationError,
    InvalidModelResponseError,
    NotFoundError,
)
from domain.models import (
    BehaviorActorType,
    BehaviorLearningType,
    BehaviorPattern,
    BehaviorScenarioProfile,
    BehaviorSelection,
    BehaviorTagGroup,
    BehaviorTagKind,
    ChatType,
    ExtractionStatus,
    GroupExpressionPattern,
    JargonTerm,
    LearnedItemStatus,
    MessageRole,
    SessionView,
    SocialLearningRun,
    StoredMessage,
    utc_now,
)
from observability import model_observation_scope
from ports import EmbeddingProvider, ModelProvider, ModelRequest, SocialLearningRepository
from prompting import PromptAssembler

logger = logging.getLogger(__name__)

_ASCII_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_-]{1,31}(?![A-Za-z0-9_])")
_SPACE_RE = re.compile(r"\s+")
_TERM_EDGE_RE = re.compile(r"^[\s，。！？；：、,.!?;:'\"“”‘’（）()\[\]【】<>《》]+|"
                           r"[\s，。！？；：、,.!?;:'\"“”‘’（）()\[\]【】<>《》]+$")

_SEMANTIC_TAGS: dict[str, tuple[str, ...]] = {
    "求助": ("怎么办", "怎么弄", "如何", "帮忙", "求助", "不懂", "不会", "排查", "问题", "报错"),
    "询问": ("吗", "呢", "什么", "为什么", "多少", "是否", "能不能", "有没有", "请问"),
    "惊叹": ("离谱", "居然", "竟然", "卧槽", "我去", "天哪", "震惊", "太强", "牛"),
    "玩笑": ("哈哈", "hhh", "笑死", "233", "绷不住", "调侃", "开玩笑"),
    "赞同": ("确实", "同意", "对对对", "没错", "可以", "赞同"),
    "反对": ("不对", "不是", "不同意", "反驳", "质疑", "但是"),
    "感谢": ("谢谢", "感谢", "辛苦", "多谢"),
    "道歉": ("抱歉", "对不起", "不好意思"),
    "安慰": ("别难过", "没关系", "理解", "安慰", "抱抱", "会好的"),
    "夸赞": ("厉害", "真棒", "优秀", "太强", "好看", "牛"),
    "寒暄": ("你好", "早上好", "晚上好", "晚安", "在吗", "拜拜", "再见"),
    "技术": ("代码", "接口", "api", "配置", "服务", "模型", "数据库", "部署", "日志", "bug"),
    "信息不足": ("不清楚", "不知道", "没说", "缺少", "信息不足", "无法判断"),
}
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
_EXPRESSION_EMBEDDING_PROBES = (
    "iJA 表达检索探针：技术问题排查、报错截图、配置异常",
    "iJA 表达检索探针：轻松吐槽、接梗、日常群聊",
    "iJA 表达检索探针：情绪回应、安慰、拒绝、调侃",
)
_MAINTENANCE_PAGE_SIZE = 200


@dataclass(frozen=True)
class _ExpressionEmbeddingProfile:
    marker: str
    model_name: str
    dimension: int
    provider: EmbeddingProvider


@dataclass(frozen=True)
class _ExpressionVectorIndex:
    profile: _ExpressionEmbeddingProfile
    vectors: dict[str, list[float]]
    cluster_by_expression: dict[str, int]
    centers: list[list[float]]


class JargonCandidate(BaseModel):
    """模型提取的一条黑话候选。"""

    term: str = Field(min_length=1, max_length=64)
    source_message_ids: list[str] = Field(min_length=1, max_length=30)


class JargonCandidatePayload(BaseModel):
    candidates: list[JargonCandidate] = Field(default_factory=list, max_length=30)


class JargonInference(BaseModel):
    """模型对候选黑话的语义判断。"""

    jargon_id: str
    status: Literal["candidate", "active", "rejected"]
    meaning: str = Field(default="", max_length=2000)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def require_active_meaning(self) -> JargonInference:
        if self.status == "active" and not self.meaning.strip():
            raise ValueError("active 黑话必须包含 meaning")
        if self.status != "active" and self.meaning.strip() and self.status == "candidate":
            raise ValueError("证据不足的 candidate 不应保存猜测性 meaning")
        return self


class JargonInferencePayload(BaseModel):
    inferences: list[JargonInference] = Field(default_factory=list, max_length=30)


class ExpressionCandidate(BaseModel):
    """模型抽取的情境表达候选。"""

    situation: str = Field(min_length=1, max_length=200)
    style: str = Field(min_length=1, max_length=200)
    confidence: float = Field(default=0.7, ge=0, le=1)
    source_message_ids: list[str] = Field(min_length=1, max_length=30)


class ExpressionLearningPayload(BaseModel):
    patterns: list[ExpressionCandidate] = Field(default_factory=list, max_length=20)


class BehaviorTagCandidate(BaseModel):
    """学习模型给出的一个规范标签及其语义等价别名。"""

    tag_name: str = Field(min_length=1, max_length=80)
    tag_aliases: list[str] = Field(default_factory=list, max_length=8)


class BehaviorCandidate(BaseModel):
    """模型抽取的可反馈行为经验。"""

    scene_summary: str = Field(min_length=1, max_length=500)
    scene_tags: list[str | BehaviorTagCandidate] = Field(
        default_factory=list, max_length=30
    )
    need_tags: list[str | BehaviorTagCandidate] = Field(
        default_factory=list, max_length=10
    )
    other_traits: list[str | BehaviorTagCandidate] = Field(
        default_factory=list, max_length=20
    )
    action: str = Field(min_length=1, max_length=500)
    expected_outcome: str = Field(min_length=1, max_length=500)
    actor_type: BehaviorActorType
    learning_type: BehaviorLearningType
    confidence: float = Field(default=0.7, ge=0, le=1)
    source_message_ids: list[str] = Field(min_length=2, max_length=50)

    @model_validator(mode="after")
    def require_consistent_lane(self) -> BehaviorCandidate:
        if self.actor_type == BehaviorActorType.AGENT_SELF:
            if self.learning_type != BehaviorLearningType.SELF_REFLECTION:
                raise ValueError("agent_self 必须进入 self_reflection 通道")
        elif self.learning_type == BehaviorLearningType.SELF_REFLECTION:
            raise ValueError("self_reflection 的主体必须是 agent_self")
        return self


class BehaviorLearningPayload(BaseModel):
    patterns: list[BehaviorCandidate] = Field(default_factory=list, max_length=20)


class BehaviorScenarioPayload(BaseModel):
    """回复前模型返回的结构化场景画像。"""

    summary: str = Field(default="", max_length=500)
    tag_clusters: list[BehaviorTagCandidate] = Field(
        default_factory=list, max_length=20
    )
    need: BehaviorTagCandidate | None = None
    other_traits: list[BehaviorTagCandidate] = Field(
        default_factory=list, max_length=8
    )
    confidence: float = Field(default=0.0, ge=0, le=1)


class BehaviorFeedbackCandidate(BaseModel):
    """模型对一次行为选择的有证据评价。"""

    selection_id: str
    adopted: bool
    status: Literal["success", "partial_success", "failed"]
    score_delta: float = Field(default=0.0, allow_inf_nan=False)
    outcome: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    source_message_ids: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_reward_range(self) -> BehaviorFeedbackCandidate:
        """按反馈状态重映射奖励，避免模型用绝对数值支配经验权重。"""

        if self.status == "success":
            raw = self.score_delta if self.score_delta > 0 else 0.6
            self.score_delta = max(0.1, min(1.0, abs(raw)))
        elif self.status == "partial_success":
            raw = self.score_delta if self.score_delta > 0 else 0.25
            self.score_delta = max(0.05, min(0.35, abs(raw)))
        else:
            raw = self.score_delta if self.score_delta < 0 else -0.6
            self.score_delta = -max(0.1, min(1.0, abs(raw)))
        return self


class BehaviorFeedbackPayload(BaseModel):
    feedback: list[BehaviorFeedbackCandidate] = Field(default_factory=list, max_length=20)


class SocialLearningService:
    """拥有三类学习写入、情境选择与行为反馈的唯一应用层 owner。"""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: SocialLearningRepository,
        model: ModelProvider,
        prompting: PromptAssembler,
        events: EventHub,
        embedding: EmbeddingProvider | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.model = model
        self.embedding = embedding
        self.prompting = prompting
        self.events = events
        self._tasks: set[asyncio.Task[object]] = set()
        self._tasks_by_session: dict[str, set[asyncio.Task[object]]] = {}
        self._run_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._feedback_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._maintenance_lock = asyncio.Lock()
        self._expression_index_locks: defaultdict[str, asyncio.Lock] = defaultdict(
            asyncio.Lock
        )
        self._embedding_profile_lock = asyncio.Lock()
        self._embedding_profile: _ExpressionEmbeddingProfile | None = None
        self._embedding_profile_signature = ""
        self._maintenance_scheduled = False
        self._last_maintenance_scan_at: datetime | None = None

    def set_model(self, model: ModelProvider) -> None:
        """热切换后续学习与反馈评价使用的模型。"""

        self.model = model

    def set_embedding_provider(
        self, embedding: EmbeddingProvider | None
    ) -> None:
        """热切换表达索引使用的 embedding Provider，并废弃旧 profile 缓存。"""

        self.embedding = embedding
        self._embedding_profile = None
        self._embedding_profile_signature = ""

    async def start(self) -> None:
        """恢复中断前仍在执行的学习批次。"""

        if not self.settings.social_learning.enabled:
            return
        await self.run_maintenance()
        if self.settings.social_learning.behavior_graph_enabled:
            await self.store.backfill_behavior_scene_graph(
                reuse_threshold=(
                    self.settings.social_learning.behavior_scene_cluster_reuse_threshold
                )
            )
        if self.embedding is not None and self.settings.embedding.enabled:
            self._track(asyncio.create_task(self._backfill_expression_indexes()))
        for run in await self.store.list_recoverable_social_learning_runs():
            run.status = ExtractionStatus.PENDING
            run.updated_at = utc_now()
            await self.store.save_social_learning_run(run)
            self._track(
                asyncio.create_task(self._execute_run(run)),
                session_id=run.session_id,
            )

    async def stop(self) -> None:
        """等待已提交的学习与反馈任务完成。"""

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    @asynccontextmanager
    async def hold_writes(self, session_ids: list[str]):
        """等待并阻止指定 Session 的学习与反馈写入。"""

        async with AsyncExitStack() as stack:
            await stack.enter_async_context(self._maintenance_lock)
            for session_id in sorted(set(session_ids)):
                await stack.enter_async_context(self._run_locks[session_id])
                await stack.enter_async_context(self._feedback_locks[session_id])
                await stack.enter_async_context(
                    self._expression_index_locks[session_id]
                )
            yield

    async def maybe_enqueue(
        self, *, session: SessionView, source_run_id: str
    ) -> SocialLearningRun | None:
        """累计足够多的新用户证据后创建幂等后台学习批次。"""

        config = self.settings.social_learning
        if not config.enabled:
            return None
        self.schedule_maintenance()
        messages = await self.store.list_recallable_messages(
            session.id, config.context_messages
        )
        runs = await self.store.list_social_learning_runs(session.id, limit=100)
        handled_ids = {
            message_id
            for run in runs
            if run.status
            in {
                ExtractionStatus.PENDING,
                ExtractionStatus.RUNNING,
                ExtractionStatus.COMPLETED,
            }
            for message_id in run.source_message_ids
        }
        source_messages = [item for item in messages if item.id not in handled_ids]
        new_user_count = sum(
            item.role == MessageRole.USER for item in source_messages
        )
        if new_user_count < config.minimum_new_user_messages:
            return None
        source_ids = [item.id for item in source_messages]
        fingerprint = hashlib.sha256(
            f"{session.id}\0".encode()
            + "\0".join(source_ids).encode()
        ).hexdigest()
        run = SocialLearningRun(
            session_id=session.id,
            source_run_id=source_run_id,
            source_message_ids=source_ids,
            source_fingerprint=fingerprint,
            data_epoch=session.data_epoch,
        )
        run, created = await self.store.create_social_learning_run(run)
        if created:
            await self.events.publish(
                "social_learning.enqueued", run.model_dump(mode="json")
            )
            self._track(
                asyncio.create_task(self._execute_run(run)),
                session_id=run.session_id,
            )
        return run

    async def retry_run(self, run_id: str) -> SocialLearningRun:
        """人工重试明确失败且来源仍可召回的学习批次。"""

        run = await self.store.get_social_learning_run(run_id)
        if run is None:
            raise NotFoundError("社交学习批次不存在")
        if run.status != ExtractionStatus.FAILED:
            raise InputValidationError("只有失败的社交学习批次可以重试")
        if not await self.store.messages_are_recallable(
            run.session_id, run.source_message_ids
        ):
            raise InputValidationError(
                "来源消息已被清空或删除，不能重试该学习批次"
            )
        run.status = ExtractionStatus.PENDING
        run.error_code = None
        run.error_message = None
        run.updated_at = utc_now()
        await self.store.save_social_learning_run(run)
        await self.events.publish(
            "social_learning.retried", run.model_dump(mode="json")
        )
        self._track(
            asyncio.create_task(self._execute_run(run)),
            session_id=run.session_id,
        )
        return run

    def schedule_feedback(self, session: SessionView) -> None:
        """在新消息已归档后异步评价仍待反馈的行为选择。"""

        if not self.settings.social_learning.enabled:
            return
        self._track(
            asyncio.create_task(self._safe_evaluate_feedback(session)),
            session_id=session.id,
        )

    def schedule_maintenance(self) -> None:
        """异步触发有持久化游标保护的低频时效维护。"""

        if (
            self.settings.social_learning.enabled
            and not self._maintenance_scheduled
        ):
            self._maintenance_scheduled = True
            self._track(asyncio.create_task(self._run_scheduled_maintenance()))

    async def _run_scheduled_maintenance(self) -> None:
        """合并消息高峰中的重复维护请求。"""

        try:
            await self.run_maintenance()
        except Exception as exc:
            logger.exception(
                "社交学习时效维护失败",
                extra={
                    "error_code": getattr(
                        exc, "code", "social_learning_maintenance_failed"
                    )
                },
            )
            await self.events.publish(
                "social_learning.maintenance.failed",
                {
                    "error_code": getattr(
                        exc, "code", "social_learning_maintenance_failed"
                    )
                },
            )
        finally:
            self._maintenance_scheduled = False

    async def run_maintenance(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> dict[str, int]:
        """按独立强化时间衰减旧知识，并停用失效条目。"""

        config = self.settings.social_learning
        result = {
            "scanned_count": 0,
            "decayed_count": 0,
            "disabled_count": 0,
            "jargon_count": 0,
            "expression_count": 0,
            "behavior_count": 0,
        }
        if not config.enabled:
            return result

        maintenance_at = now or utc_now()
        async with self._maintenance_lock:
            if (
                not force
                and self._last_maintenance_scan_at is not None
                and maintenance_at - self._last_maintenance_scan_at
                < timedelta(hours=config.maintenance_interval_hours)
            ):
                return result
            for session in await self.store.list_sessions():
                async with self._run_locks[session.id]:
                    async with self._feedback_locks[session.id]:
                        after_id: str | None = None
                        while True:
                            page = await self.store.page_jargons_for_maintenance(
                                session.id,
                                after_id=after_id,
                                limit=_MAINTENANCE_PAGE_SIZE,
                            )
                            if not page:
                                break
                            jargon_updates: list[JargonTerm] = []
                            for item in page:
                                update, decayed, disabled = self._maintain_jargon(
                                    item,
                                    now=maintenance_at,
                                    config=config,
                                    force=force,
                                )
                                if update is None:
                                    continue
                                jargon_updates.append(update)
                                result["scanned_count"] += 1
                                result["jargon_count"] += 1
                                result["decayed_count"] += int(decayed)
                                result["disabled_count"] += int(disabled)
                            if jargon_updates:
                                await self.store.apply_social_learning_maintenance(
                                    jargons=jargon_updates,
                                    expressions=[],
                                    behaviors=[],
                                )
                            after_id = page[-1].id
                            if len(page) < _MAINTENANCE_PAGE_SIZE:
                                break

                        after_id = None
                        while True:
                            page = (
                                await self.store.page_group_expressions_for_maintenance(
                                    session.id,
                                    after_id=after_id,
                                    limit=_MAINTENANCE_PAGE_SIZE,
                                )
                            )
                            if not page:
                                break
                            expression_updates: list[GroupExpressionPattern] = []
                            for item in page:
                                update, decayed, disabled = (
                                    self._maintain_group_expression(
                                        item,
                                        now=maintenance_at,
                                        config=config,
                                        force=force,
                                    )
                                )
                                if update is None:
                                    continue
                                expression_updates.append(update)
                                result["scanned_count"] += 1
                                result["expression_count"] += 1
                                result["decayed_count"] += int(decayed)
                                result["disabled_count"] += int(disabled)
                            if expression_updates:
                                await self.store.apply_social_learning_maintenance(
                                    jargons=[],
                                    expressions=expression_updates,
                                    behaviors=[],
                                )
                            after_id = page[-1].id
                            if len(page) < _MAINTENANCE_PAGE_SIZE:
                                break

                        after_id = None
                        while True:
                            page = (
                                await self.store.page_behavior_patterns_for_maintenance(
                                    session.id,
                                    after_id=after_id,
                                    limit=_MAINTENANCE_PAGE_SIZE,
                                )
                            )
                            if not page:
                                break
                            behavior_updates: list[BehaviorPattern] = []
                            for item in page:
                                update, decayed, disabled = self._maintain_behavior(
                                    item,
                                    now=maintenance_at,
                                    config=config,
                                    force=force,
                                )
                                if update is None:
                                    continue
                                behavior_updates.append(update)
                                result["scanned_count"] += 1
                                result["behavior_count"] += 1
                                result["decayed_count"] += int(decayed)
                                result["disabled_count"] += int(disabled)
                            if behavior_updates:
                                await self.store.apply_social_learning_maintenance(
                                    jargons=[],
                                    expressions=[],
                                    behaviors=behavior_updates,
                                )
                            after_id = page[-1].id
                            if len(page) < _MAINTENANCE_PAGE_SIZE:
                                break
            self._last_maintenance_scan_at = maintenance_at

        await self.events.publish(
            "social_learning.maintenance.completed",
            {
                **result,
                "maintained_at": maintenance_at.isoformat(),
                "forced": force,
            },
        )
        return result

    async def prepare_reply_context(
        self,
        *,
        session: SessionView,
        messages: list[StoredMessage],
        turn_id: str,
    ) -> dict[str, object]:
        """按当前文本和情境选择专用回注，并持久化行为选择。"""

        if not self.settings.social_learning.enabled or not messages:
            return {}
        await self.run_maintenance()
        query_messages = messages[-8:]
        query_text = self.prompting.project_messages_text(
            session,
            query_messages,
        )
        semantic_tags = self._semantic_tags(query_text)
        context: dict[str, object] = {}

        jargon_items = []
        normalized_query = query_text.casefold()
        active_jargons = await self.store.list_jargons(
            session.id, active_only=True, limit=100
        )
        for item in active_jargons:
            if (
                item.confidence < self.settings.social_learning.minimum_confidence
                or not item.meaning.strip()
                or item.normalized_term not in normalized_query
            ):
                continue
            jargon_items.append(
                {
                    "jargon_id": item.id,
                    "term": item.term,
                    "meaning": item.meaning,
                    "confidence": item.confidence,
                }
            )
            if (
                len(jargon_items)
                >= self.settings.social_learning.jargon_injection_limit
            ):
                break
        if jargon_items:
            context["jargon_glossary"] = jargon_items

        reusable_jargons = [
            {
                "jargon_id": item.id,
                "term": item.term,
                "meaning": item.meaning,
                "confidence": item.confidence,
                "occurrence_count": item.occurrence_count,
                "usage_rule": "只在语义和语境都自然匹配时有限复用，不为展示学习结果而硬塞",
            }
            for item in active_jargons
            if item.meaning.strip()
            and item.confidence
            >= max(
                self.settings.social_learning.minimum_confidence,
                self.settings.social_learning.jargon_reuse_min_confidence,
            )
            and item.occurrence_count
            >= self.settings.social_learning.jargon_reuse_min_occurrences
        ][: self.settings.social_learning.jargon_reuse_limit]
        if reusable_jargons:
            context["jargon_style_guidance"] = reusable_jargons

        if session.chat_type in {ChatType.PRIVATE, ChatType.GROUP}:
            expression_candidates = await self.store.list_group_expressions(
                session.id, active_only=True, limit=200
            )
            eligible_expression_items = [
                item
                for item in expression_candidates
                if item.confidence
                >= self.settings.social_learning.minimum_confidence
            ]
            ranked_expressions: list[
                tuple[float, GroupExpressionPattern]
            ] | None = None
            used_vector_ranking = False
            if (
                eligible_expression_items
                and self.settings.social_learning.expression_vector_enabled
                and self.embedding is not None
                and self.settings.embedding.enabled
            ):
                try:
                    ranked_expressions = (
                        await self._rank_expressions_with_vectors(
                            session_id=session.id,
                            candidates=eligible_expression_items,
                            query_text=query_text,
                        )
                    )
                    used_vector_ranking = True
                except Exception as exc:
                    logger.exception(
                        "表达向量索引不可用，降级到本地表达选择器",
                        extra={
                            "session_id": session.id,
                            "turn_id": turn_id,
                            "error_code": getattr(
                                exc,
                                "code",
                                "expression_vector_index_failed",
                            ),
                        },
                    )
                    await self.events.publish(
                        "social_learning.expression_vector.failed",
                        {
                            "session_id": session.id,
                            "turn_id": turn_id,
                            "error_code": getattr(
                                exc,
                                "code",
                                "expression_vector_index_failed",
                            ),
                        },
                    )
            if ranked_expressions is None:
                ranked_expressions = sorted(
                    (
                        (
                            self._expression_match_score(
                                item.situation,
                                query_text=query_text,
                                query_tags=semantic_tags,
                                confidence=item.confidence,
                                occurrence_count=item.occurrence_count,
                                selection_count=item.selection_count,
                            ),
                            item,
                        )
                        for item in eligible_expression_items
                    ),
                    key=lambda pair: (pair[0], pair[1].id),
                    reverse=True,
                )
            eligible_expressions = [
                (score, item)
                for score, item in ranked_expressions
                if score
                >= self.settings.social_learning.expression_match_threshold
            ]
            selected_expressions = [
                item
                for _, item in eligible_expressions[
                    : self.settings.social_learning.expression_injection_limit
                ]
            ]
            if not used_vector_ranking:
                selected_expressions = self._select_diverse_expressions(
                    eligible_expressions,
                    limit=self.settings.social_learning.expression_injection_limit,
                )
            if selected_expressions:
                await self.store.mark_group_expressions_selected(
                    [item.id for item in selected_expressions]
                )
                guidance_key = (
                    "group_expression_guidance"
                    if session.chat_type == ChatType.GROUP
                    else "private_expression_guidance"
                )
                context[guidance_key] = [
                    {
                        "expression_id": item.id,
                        "situation": item.situation,
                        "style": item.style,
                    }
                    for item in selected_expressions
                ]

        behavior_candidates = await self.store.list_behavior_patterns(
            session.id, active_only=True, limit=200
        )
        scenario_profile = self._local_behavior_scenario_profile(
            session,
            query_messages,
            semantic_tags,
        )
        if (
            behavior_candidates
            and self.settings.social_learning.behavior_scene_analysis_enabled
        ):
            try:
                scenario_profile = await self._analyze_behavior_scenario(
                    session=session,
                    messages=query_messages,
                    fallback=scenario_profile,
                )
            except Exception as exc:
                logger.exception(
                    "行为场景画像失败，降级到本地语义标签",
                    extra={
                        "session_id": session.id,
                        "turn_id": turn_id,
                        "error_code": getattr(
                            exc, "code", "behavior_scene_analysis_failed"
                        ),
                    },
                )
                await self.events.publish(
                    "social_learning.behavior_scene.failed",
                    {
                        "session_id": session.id,
                        "turn_id": turn_id,
                        "error_code": getattr(
                            exc, "code", "behavior_scene_analysis_failed"
                        ),
                    },
                )
        graph_scores: dict[str, float] = {}
        if (
            behavior_candidates
            and scenario_profile.has_signal
            and self.settings.social_learning.behavior_graph_enabled
        ):
            try:
                graph_scores = (
                    await self.store.retrieve_behavior_graph_scores(
                        session_id=session.id,
                        profile=scenario_profile,
                        max_depth=(
                            self.settings.social_learning.behavior_graph_spread_depth
                        ),
                        direct_lock_threshold=(
                            self.settings.social_learning.behavior_graph_direct_lock_threshold
                        ),
                    )
                )
            except Exception as exc:
                logger.exception(
                    "行为标签图检索失败，降级到本地行为选择器",
                    extra={
                        "session_id": session.id,
                        "turn_id": turn_id,
                        "error_code": getattr(
                            exc, "code", "behavior_graph_retrieval_failed"
                        ),
                    },
                )
                await self.events.publish(
                    "social_learning.behavior_graph.failed",
                    {
                        "session_id": session.id,
                        "turn_id": turn_id,
                        "error_code": getattr(
                            exc, "code", "behavior_graph_retrieval_failed"
                        ),
                    },
                )
        ranked_behaviors = sorted(
            (
                (
                    self._blend_behavior_graph_score(
                        local_score=self._behavior_match_score(
                            item,
                            query_text=query_text,
                            query_tags=semantic_tags,
                        ),
                        graph_score=graph_scores.get(item.id),
                    ),
                    item,
                )
                for item in behavior_candidates
                if item.confidence >= self.settings.social_learning.minimum_confidence
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        selected_behaviors = [
            (score, item)
            for score, item in ranked_behaviors
            if score >= self.settings.social_learning.behavior_match_threshold
        ][: self.settings.social_learning.behavior_injection_limit]
        if selected_behaviors:
            scene_summary = (
                scenario_profile.summary
                or self._scene_summary(session, query_messages)
            )
            selected_scene_tags = sorted(
                {
                    tag
                    for group in scenario_profile.tag_groups
                    for tag in group.tags
                }
            )[:30]
            await self.store.create_behavior_selections(
                [
                    BehaviorSelection(
                        session_id=session.id,
                        turn_id=turn_id,
                        behavior_id=item.id,
                        scene_summary=scene_summary,
                        scene_tags=selected_scene_tags,
                    )
                    for _, item in selected_behaviors
                ]
            )
            context["behavior_guidance"] = [
                {
                    "behavior_id": item.id,
                    "scene": item.scene_summary,
                    "action": item.action,
                    "expected_outcome": item.expected_outcome,
                    "priority_score": round(score, 4),
                    "learning_type": item.learning_type.value,
                }
                for score, item in selected_behaviors
            ]
        return context

    async def record_reply_delivery(self, *, turn_id: str, message_id: str) -> None:
        """把真实送达并提交的助手消息接到行为评价链。"""

        if self.settings.social_learning.enabled:
            await self.store.attach_behavior_reply(
                turn_id=turn_id, message_id=message_id
            )

    async def _execute_run(self, run: SocialLearningRun) -> None:
        async with self._run_locks[run.session_id]:
            run.attempt_count += 1
            run.status = ExtractionStatus.RUNNING
            run.updated_at = utc_now()
            if not await self.store.save_social_learning_run(run):
                return
            completed = False
            try:
                session = await self.store.get_session(run.session_id)
                if session is None:
                    raise InvalidModelResponseError("学习批次对应 Session 不存在")
                if session.data_epoch != run.data_epoch:
                    raise ConflictError("社交学习批次的数据版本已失效")
                if not await self.store.messages_are_recallable(
                    run.session_id, run.source_message_ids
                ):
                    run.status = ExtractionStatus.COMPLETED
                    return
                context = await self.store.list_recallable_messages(
                    run.session_id,
                    self.settings.social_learning.context_messages,
                )
                allowed_source_ids = {item.id for item in context}
                if not set(run.source_message_ids).issubset(allowed_source_ids):
                    raise InvalidModelResponseError("学习上下文缺少本批次来源消息")

                with model_observation_scope(
                    task="social.learn",
                    profile="profile",
                    session_id=run.session_id,
                    run_id=run.id,
                ):
                    jargon_candidates, jargon_inferences = (
                        await self._prepare_jargon(
                            session, context
                        )
                    )
                    expressions = await self._prepare_expressions(
                        session, context
                    )
                    behaviors = await self._prepare_behaviors(
                        session, context
                    )
                current_session = await self.store.get_session(run.session_id)
                if (
                    current_session is None
                    or current_session.data_epoch != run.data_epoch
                ):
                    raise ConflictError("社交学习批次的数据版本已失效")
                await self.store.commit_social_learning_run(
                    run=run,
                    jargon_candidates=jargon_candidates,
                    jargon_inferences=jargon_inferences,
                    expressions=expressions,
                    behaviors=behaviors,
                    scene_cluster_reuse_threshold=(
                        self.settings.social_learning.behavior_scene_cluster_reuse_threshold
                    ),
                )
                completed = True
                if (
                    expressions
                    and self.embedding is not None
                    and self.settings.embedding.enabled
                    and self.settings.social_learning.expression_vector_enabled
                ):
                    self._track(
                        asyncio.create_task(
                            self._refresh_expression_index_for_session(
                                session.id
                            )
                        ),
                        session_id=session.id,
                    )
            except asyncio.CancelledError:
                run.status = ExtractionStatus.CANCELLED
                run.error_code = "session_lifecycle_cancelled"
                run.error_message = "学习批次因会话清空或删除被取消"
                raise
            except ConflictError as exc:
                run.status = ExtractionStatus.STALE
                run.error_code = "session_data_epoch_stale"
                run.error_message = str(exc)[:500]
            except Exception as exc:
                run.status = ExtractionStatus.FAILED
                run.error_code = getattr(
                    exc, "code", "social_learning_failed"
                )
                run.error_message = str(exc)[:500]
                logger.exception(
                    "社交学习批次失败",
                    extra={
                        "session_id": run.session_id,
                        "learning_run_id": run.id,
                        "attempt_count": run.attempt_count,
                    },
                )
                await self.events.publish(
                    "social_learning.failed",
                    {
                        "session_id": run.session_id,
                        "learning_run_id": run.id,
                        "error_code": run.error_code,
                    },
                )
            finally:
                run.updated_at = utc_now()
                await self.store.save_social_learning_run(run)
            if completed:
                await self.events.publish(
                    "social_learning.completed", run.model_dump(mode="json")
                )

    async def _prepare_jargon(
        self, session: SessionView, messages: list[StoredMessage]
    ) -> tuple[
        list[JargonTerm],
        list[tuple[str, str, LearnedItemStatus, float]],
    ]:
        payload = await self._complete_payload(
            self.prompting.build_jargon_learning(
                session=session, messages=messages
            ),
            JargonCandidatePayload,
        )
        by_id = {item.id: item for item in messages}
        user_ids = {
            item.id for item in messages if item.role == MessageRole.USER
        }
        staged: dict[str, JargonTerm] = {}
        for candidate in payload.candidates[
            : self.settings.social_learning.maximum_items_per_kind
        ]:
            term = self._normalize_term_surface(candidate.term)
            if not term:
                continue
            evidence_ids = [
                message_id
                for message_id in dict.fromkeys(candidate.source_message_ids)
                if message_id in user_ids
                and term.casefold() in by_id[message_id].plain_text.casefold()
            ]
            if not evidence_ids:
                continue
            normalized = term.casefold()
            latest_seen = max(
                by_id[message_id].created_at for message_id in evidence_ids
            )
            previous = staged.get(normalized)
            if previous is None:
                staged[normalized] = JargonTerm(
                    session_id=session.id,
                    term=term,
                    normalized_term=normalized,
                    occurrence_count=len(evidence_ids),
                    evidence_message_ids=evidence_ids,
                    last_seen_at=latest_seen,
                )
                continue
            combined_ids = list(
                dict.fromkeys(
                    [*previous.evidence_message_ids, *evidence_ids]
                )
            )[-100:]
            staged[normalized] = previous.model_copy(
                update={
                    "term": term,
                    "occurrence_count": len(combined_ids),
                    "evidence_message_ids": combined_ids,
                    "last_seen_at": max(previous.last_seen_at, latest_seen),
                    "updated_at": utc_now(),
                }
            )

        existing = await self.store.get_jargons_by_normalized_terms(
            session.id, set(staged)
        )
        candidates: list[JargonTerm] = []
        predicted: list[JargonTerm] = []
        for normalized, candidate in staged.items():
            current = existing.get(normalized)
            if current is None:
                candidates.append(candidate)
                predicted.append(candidate)
                continue
            new_evidence = [
                message_id
                for message_id in candidate.evidence_message_ids
                if message_id not in set(current.evidence_message_ids)
            ]
            candidates.append(
                candidate.model_copy(update={"id": current.id})
            )
            predicted.append(
                current.model_copy(
                    update={
                        "term": candidate.term,
                        "occurrence_count": (
                            current.occurrence_count + len(new_evidence)
                        ),
                        "evidence_message_ids": list(
                            dict.fromkeys(
                                [
                                    *current.evidence_message_ids,
                                    *new_evidence,
                                ]
                            )
                        )[-100:],
                        "last_seen_at": max(
                            current.last_seen_at,
                            candidate.last_seen_at,
                        ),
                        "updated_at": utc_now(),
                    }
                )
            )
        inferable = [
            item
            for item in predicted
            if self._jargon_inference_is_due(
                item,
                self.settings.social_learning.jargon_inference_thresholds,
            )
        ]
        if not inferable:
            return candidates, []
        inference_payload = await self._complete_payload(
            self.prompting.build_jargon_inference(
                session=session,
                messages=messages,
                candidates=[
                    {
                        "jargon_id": item.id,
                        "term": item.term,
                        "previous_meaning": item.meaning,
                        "occurrence_count": item.occurrence_count,
                        "evidence_message_ids": item.evidence_message_ids,
                    }
                    for item in inferable
                ],
            ),
            JargonInferencePayload,
        )
        allowed_ids = {item.id for item in inferable}
        inferences: list[
            tuple[str, str, LearnedItemStatus, float]
        ] = []
        for inference in inference_payload.inferences:
            if inference.jargon_id not in allowed_ids:
                raise InvalidModelResponseError("黑话推断引用了候选范围外的 ID")
            status = LearnedItemStatus(inference.status)
            confidence = inference.confidence
            if (
                status == LearnedItemStatus.ACTIVE
                and confidence
                < self.settings.social_learning.minimum_confidence
            ):
                status = LearnedItemStatus.CANDIDATE
            inferences.append(
                (
                    inference.jargon_id,
                    inference.meaning.strip()
                    if status == LearnedItemStatus.ACTIVE
                    else "",
                    status,
                    confidence,
                )
            )
        return candidates, inferences

    async def _prepare_expressions(
        self, session: SessionView, messages: list[StoredMessage]
    ) -> list[GroupExpressionPattern]:
        payload = await self._complete_payload(
            self.prompting.build_group_expression_learning(
                session=session, messages=messages
            ),
            ExpressionLearningPayload,
        )
        user_ids = {
            item.id for item in messages if item.role == MessageRole.USER
        }
        by_id = {item.id: item for item in messages}
        produced: list[GroupExpressionPattern] = []
        for candidate in payload.patterns[
            : self.settings.social_learning.maximum_items_per_kind
        ]:
            evidence_ids = [
                message_id
                for message_id in dict.fromkeys(candidate.source_message_ids)
                if message_id in user_ids
            ]
            if not evidence_ids:
                continue
            situation = self._normalize_phrase(candidate.situation)
            style = self._normalize_phrase(candidate.style)
            if not situation or not style:
                continue
            pattern_hash = self._stable_hash(situation, style)
            produced.append(
                GroupExpressionPattern(
                    session_id=session.id,
                    situation=situation,
                    style=style,
                    pattern_hash=pattern_hash,
                    status=(
                        LearnedItemStatus.ACTIVE
                        if candidate.confidence
                        >= self.settings.social_learning.minimum_confidence
                        else LearnedItemStatus.CANDIDATE
                    ),
                    confidence=candidate.confidence,
                    occurrence_count=len(evidence_ids),
                    evidence_message_ids=evidence_ids,
                    last_reinforced_at=max(
                        by_id[message_id].created_at
                        for message_id in evidence_ids
                    ),
                )
            )
        return produced

    async def _prepare_behaviors(
        self, session: SessionView, messages: list[StoredMessage]
    ) -> list[BehaviorPattern]:
        payload = await self._complete_payload(
            self.prompting.build_behavior_learning(
                session=session, messages=messages
            ),
            BehaviorLearningPayload,
        )
        by_id = {item.id: item for item in messages}
        produced: list[BehaviorPattern] = []
        for candidate in payload.patterns[
            : self.settings.social_learning.maximum_items_per_kind
        ]:
            evidence_ids = [
                message_id
                for message_id in dict.fromkeys(candidate.source_message_ids)
                if message_id in by_id
            ]
            if len(evidence_ids) < 2:
                continue
            evidence_roles = {by_id[message_id].role for message_id in evidence_ids}
            if (
                candidate.actor_type == BehaviorActorType.AGENT_SELF
                and MessageRole.ASSISTANT not in evidence_roles
            ):
                continue
            if (
                candidate.actor_type != BehaviorActorType.AGENT_SELF
                and MessageRole.USER not in evidence_roles
            ):
                continue
            scene_summary = self._normalize_phrase(candidate.scene_summary)
            action = self._normalize_phrase(candidate.action)
            expected_outcome = self._normalize_phrase(
                candidate.expected_outcome
            )
            if not scene_summary or not action or not expected_outcome:
                continue
            tag_groups = [
                *self._normalize_tag_candidates(
                    candidate.scene_tags, BehaviorTagKind.DOMAIN, 30
                ),
                *self._normalize_tag_candidates(
                    candidate.need_tags, BehaviorTagKind.NEED, 10
                ),
                *self._normalize_tag_candidates(
                    candidate.other_traits, BehaviorTagKind.ATTITUDE, 20
                ),
            ][:30]
            scene_tags = self._canonical_tags(
                tag_groups, BehaviorTagKind.DOMAIN
            )
            need_tags = self._canonical_tags(
                tag_groups, BehaviorTagKind.NEED
            )
            other_traits = self._canonical_tags(
                tag_groups, BehaviorTagKind.ATTITUDE
            )
            pattern_hash = self._stable_hash(
                candidate.actor_type.value,
                candidate.learning_type.value,
                scene_summary,
                action,
                expected_outcome,
            )
            produced.append(
                BehaviorPattern(
                    session_id=session.id,
                    scene_summary=scene_summary,
                    scene_tags=scene_tags,
                    need_tags=need_tags,
                    other_traits=other_traits,
                    tag_groups=tag_groups,
                    action=action,
                    expected_outcome=expected_outcome,
                    pattern_hash=pattern_hash,
                    actor_type=candidate.actor_type,
                    learning_type=candidate.learning_type,
                    status=(
                        LearnedItemStatus.ACTIVE
                        if candidate.confidence
                        >= self.settings.social_learning.minimum_confidence
                        else LearnedItemStatus.CANDIDATE
                    ),
                    confidence=candidate.confidence,
                    occurrence_count=len(evidence_ids),
                    evidence_message_ids=evidence_ids,
                    last_reinforced_at=max(
                        by_id[message_id].created_at
                        for message_id in evidence_ids
                    ),
                )
            )
        return produced

    async def _evaluate_pending_feedback(self, session: SessionView) -> None:
        async with self._feedback_locks[session.id]:
            selections = await self.store.list_pending_behavior_selections(
                session.id
            )
            selections = [
                item for item in selections if item.assistant_message_ids
            ]
            if not selections:
                return
            history = await self.store.list_recallable_messages(
                session.id, self.settings.social_learning.context_messages
            )
            by_id = {item.id: item for item in history}
            eligible = []
            timeline_ids: set[str] = set()
            references: list[dict[str, object]] = []
            for selection in selections:
                assistant_messages = [
                    by_id[message_id]
                    for message_id in selection.assistant_message_ids
                    if message_id in by_id
                ]
                if not assistant_messages:
                    continue
                selected_after = max(
                    item.created_at for item in assistant_messages
                )
                followups = [
                    item
                    for item in history
                    if item.role == MessageRole.USER
                    and item.created_at > selected_after
                ][:8]
                if not followups:
                    continue
                pattern = await self.store.get_behavior_pattern(
                    selection.behavior_id
                )
                if pattern is None:
                    continue
                eligible.append(selection)
                timeline_ids.update(selection.assistant_message_ids)
                timeline_ids.update(item.id for item in followups)
                references.append(
                    {
                        "selection_id": selection.id,
                        "behavior_id": pattern.id,
                        "scene": selection.scene_summary,
                        "action": pattern.action,
                        "expected_outcome": pattern.expected_outcome,
                    }
                )
            if not eligible:
                return
            timeline = [item for item in history if item.id in timeline_ids]
            with model_observation_scope(
                task="social.feedback",
                profile="profile",
                session_id=session.id,
            ):
                payload = await self._complete_payload(
                    self.prompting.build_behavior_feedback(
                        session=session,
                        messages=timeline,
                        references=references,
                    ),
                    BehaviorFeedbackPayload,
                )
            allowed_selection_ids = {item.id for item in eligible}
            allowed_message_ids = {item.id for item in timeline}
            received_ids: set[str] = set()
            for feedback in payload.feedback:
                if feedback.selection_id not in allowed_selection_ids:
                    raise InvalidModelResponseError(
                        "行为反馈引用了输入范围外的选择"
                    )
                if not set(feedback.source_message_ids).issubset(
                    allowed_message_ids
                ):
                    raise InvalidModelResponseError(
                        "行为反馈引用了输入范围外的消息"
                    )
                received_ids.add(feedback.selection_id)
                await self.store.save_behavior_feedback(
                    feedback.selection_id,
                    adopted=feedback.adopted,
                    feedback_status=feedback.status,
                    score_delta=feedback.score_delta,
                    outcome=feedback.outcome,
                    reason=feedback.reason,
                    feedback_message_ids=feedback.source_message_ids,
                )
            for selection in eligible:
                if selection.id not in received_ids:
                    await self.store.record_behavior_feedback_miss(
                        selection.id,
                        max_attempts=self.settings.social_learning.feedback_max_attempts,
                    )
            await self.events.publish(
                "behavior_feedback.evaluated",
                {
                    "session_id": session.id,
                    "selection_count": len(eligible),
                    "feedback_count": len(received_ids),
                },
            )

    async def _safe_evaluate_feedback(self, session: SessionView) -> None:
        """隔离反馈模型失败，保留 pending 选择供下一轮有限重试。"""

        try:
            await self._evaluate_pending_feedback(session)
        except Exception as exc:
            logger.exception(
                "行为反馈评价失败",
                extra={
                    "session_id": session.id,
                    "error_code": getattr(
                        exc, "code", "behavior_feedback_failed"
                    ),
                },
            )
            await self.events.publish(
                "behavior_feedback.failed",
                {
                    "session_id": session.id,
                    "error_code": getattr(
                        exc, "code", "behavior_feedback_failed"
                    ),
                },
            )

    async def _complete_payload(self, messages, schema):
        result = await self.model.complete(
            ModelRequest(
                messages=messages,
                model=self.settings.model.profile_name,
                temperature=0,
                max_tokens=self.settings.social_learning.model_max_tokens,
                json_mode=True,
            )
        )
        if not result.content:
            raise InvalidModelResponseError("学习模型没有返回文本")
        try:
            raw = parse_model_json(result.content)
            return schema.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise InvalidModelResponseError(
                f"学习模型结果不符合 schema: {exc}"
            ) from exc

    @staticmethod
    def _jargon_inference_is_due(
        item: JargonTerm, thresholds: list[int]
    ) -> bool:
        """仅在累计证据跨过新节点时重新推断，避免每批重复自证。"""

        next_threshold = next(
            (
                threshold
                for threshold in thresholds
                if threshold > item.last_inference_occurrence_count
            ),
            None,
        )
        if next_threshold is not None:
            return item.occurrence_count >= next_threshold
        return (
            item.status
            in {
                LearnedItemStatus.CANDIDATE,
                LearnedItemStatus.DISABLED,
            }
            and item.occurrence_count > item.last_inference_occurrence_count
        )

    @staticmethod
    def _maintenance_due(
        last_maintained_at: datetime | None,
        *,
        now: datetime,
        interval_hours: int,
        force: bool,
    ) -> bool:
        if force or last_maintained_at is None:
            return True
        return now - last_maintained_at >= timedelta(hours=interval_hours)

    @staticmethod
    def _inactive_days(now: datetime, activity_at: datetime) -> int:
        return max(0, (now - activity_at).days)

    @staticmethod
    def _decay_target_periods(
        inactive_days: int,
        *,
        decay_after_days: int,
        decay_period_days: int,
    ) -> int:
        if inactive_days < decay_after_days:
            return 0
        return 1 + (inactive_days - decay_after_days) // decay_period_days

    @classmethod
    def _maintain_jargon(
        cls,
        item: JargonTerm,
        *,
        now: datetime,
        config: SocialLearningSettings,
        force: bool,
    ) -> tuple[JargonTerm | None, bool, bool]:
        if not cls._maintenance_due(
            item.last_maintained_at,
            now=now,
            interval_hours=config.maintenance_interval_hours,
            force=force,
        ):
            return None, False, False

        inactive_days = cls._inactive_days(now, item.last_seen_at)
        target_periods = cls._decay_target_periods(
            inactive_days,
            decay_after_days=config.jargon_decay_after_days,
            decay_period_days=config.jargon_decay_period_days,
        )
        new_periods = max(0, target_periods - item.decay_count)
        confidence = max(
            0.0, item.confidence - new_periods * config.jargon_decay_step
        )
        status = item.status
        if (
            status == LearnedItemStatus.ACTIVE
            and confidence < config.minimum_confidence
        ):
            status = LearnedItemStatus.CANDIDATE
        if (
            status != LearnedItemStatus.DISABLED
            and inactive_days >= config.jargon_disable_after_days
            and (
                confidence < config.minimum_confidence
                or status
                in {
                    LearnedItemStatus.CANDIDATE,
                    LearnedItemStatus.REJECTED,
                }
            )
        ):
            status = LearnedItemStatus.DISABLED
        disabled = (
            item.status != LearnedItemStatus.DISABLED
            and status == LearnedItemStatus.DISABLED
        )
        return (
            item.model_copy(
                update={
                    "confidence": confidence,
                    "status": status,
                    "decay_count": max(item.decay_count, target_periods),
                    "last_maintained_at": now,
                    "updated_at": now,
                }
            ),
            new_periods > 0,
            disabled,
        )

    @classmethod
    def _maintain_group_expression(
        cls,
        item: GroupExpressionPattern,
        *,
        now: datetime,
        config: SocialLearningSettings,
        force: bool,
    ) -> tuple[GroupExpressionPattern | None, bool, bool]:
        if not cls._maintenance_due(
            item.last_maintained_at,
            now=now,
            interval_hours=config.maintenance_interval_hours,
            force=force,
        ):
            return None, False, False

        inactive_days = cls._inactive_days(now, item.last_reinforced_at)
        target_periods = cls._decay_target_periods(
            inactive_days,
            decay_after_days=config.expression_decay_after_days,
            decay_period_days=config.expression_decay_period_days,
        )
        new_periods = max(0, target_periods - item.decay_count)
        confidence = max(
            0.0,
            item.confidence - new_periods * config.expression_decay_step,
        )
        status = item.status
        if (
            status == LearnedItemStatus.ACTIVE
            and confidence < config.minimum_confidence
        ):
            status = LearnedItemStatus.CANDIDATE
        if (
            status != LearnedItemStatus.DISABLED
            and inactive_days >= config.expression_disable_after_days
            and confidence < config.minimum_confidence
        ):
            status = LearnedItemStatus.DISABLED
        disabled = (
            item.status != LearnedItemStatus.DISABLED
            and status == LearnedItemStatus.DISABLED
        )
        return (
            item.model_copy(
                update={
                    "confidence": confidence,
                    "status": status,
                    "decay_count": max(item.decay_count, target_periods),
                    "last_maintained_at": now,
                    "updated_at": now,
                }
            ),
            new_periods > 0,
            disabled,
        )

    @classmethod
    def _maintain_behavior(
        cls,
        item: BehaviorPattern,
        *,
        now: datetime,
        config: SocialLearningSettings,
        force: bool,
    ) -> tuple[BehaviorPattern | None, bool, bool]:
        if not cls._maintenance_due(
            item.last_maintained_at,
            now=now,
            interval_hours=config.maintenance_interval_hours,
            force=force,
        ):
            return None, False, False

        activity_times = [
            item.last_reinforced_at,
            item.last_selected_at,
            item.last_feedback_at,
        ]
        latest_activity = max(
            value for value in activity_times if value is not None
        )
        inactive_days = cls._inactive_days(now, latest_activity)
        decay_after_days: int | None = None
        decay_period_days: int | None = None
        decay_step = 0.0
        if item.occurrence_count <= 1 and item.activation_count <= 0:
            decay_after_days = config.behavior_unused_decay_after_days
            decay_period_days = config.behavior_unused_decay_after_days
            decay_step = 0.35
        elif item.activation_count > 0 and item.success_count <= 0:
            decay_after_days = config.behavior_unanswered_decay_after_days
            decay_period_days = config.behavior_unanswered_decay_after_days
            decay_step = 0.25
        elif (
            item.success_count > 0
            and item.failure_count <= item.success_count
        ):
            decay_after_days = config.behavior_positive_stale_decay_after_days
            decay_period_days = config.behavior_positive_stale_decay_after_days
            decay_step = 0.15

        target_periods = (
            cls._decay_target_periods(
                inactive_days,
                decay_after_days=decay_after_days,
                decay_period_days=decay_period_days,
            )
            if decay_after_days is not None and decay_period_days is not None
            else item.decay_count
        )
        new_periods = max(0, target_periods - item.decay_count)
        score = max(-5.0, min(5.0, item.score - new_periods * decay_step))
        status = item.status
        should_disable = (
            score <= -5.0
            and (item.failure_count >= 2 or item.activation_count >= 3)
            or item.occurrence_count <= 1
            and item.activation_count <= 0
            and inactive_days >= config.behavior_unused_disable_after_days
            and score <= -3.0
            or item.failure_count >= 3
            and item.success_count <= 0
            and score <= -4.0
        )
        if status != LearnedItemStatus.DISABLED and should_disable:
            status = LearnedItemStatus.DISABLED
        disabled = (
            item.status != LearnedItemStatus.DISABLED
            and status == LearnedItemStatus.DISABLED
        )
        return (
            item.model_copy(
                update={
                    "score": score,
                    "status": status,
                    "decay_count": max(item.decay_count, target_periods),
                    "last_maintained_at": now,
                    "updated_at": now,
                }
            ),
            new_periods > 0,
            disabled,
        )

    async def _backfill_expression_indexes(self) -> None:
        """启动后按 Session 重建缺失表达索引；失败只影响派生索引。"""

        for session_id in await self.store.list_group_expression_session_ids():
            try:
                await self._refresh_expression_index_for_session(session_id)
            except Exception as exc:
                logger.exception(
                    "表达向量索引后台回填失败",
                    extra={
                        "session_id": session_id,
                        "error_code": getattr(
                            exc, "code", "expression_vector_backfill_failed"
                        ),
                    },
                )
                await self.events.publish(
                    "social_learning.expression_vector.backfill_failed",
                    {
                        "session_id": session_id,
                        "error_code": getattr(
                            exc, "code", "expression_vector_backfill_failed"
                        ),
                    },
                )

    async def _refresh_expression_index_for_session(
        self, session_id: str
    ) -> None:
        candidates = await self.store.list_group_expressions(
            session_id, active_only=True, limit=1000
        )
        candidates = [
            item
            for item in candidates
            if item.confidence
            >= self.settings.social_learning.minimum_confidence
        ]
        if candidates:
            await self._ensure_expression_vector_index(
                session_id=session_id, candidates=candidates
            )

    async def _embedding_profile_for_expressions(
        self,
    ) -> _ExpressionEmbeddingProfile:
        """用固定探针识别后端实际 profile，避免同名异构向量混用。"""

        provider = self.embedding
        if provider is None or not self.settings.embedding.enabled:
            raise InputValidationError("表达向量索引未配置 embedding Provider")
        model_name = self.settings.embedding.name
        base_url = self.settings.embedding.base_url.rstrip("/")
        signature = self._stable_hash(
            base_url,
            model_name,
        )
        if (
            self._embedding_profile is not None
            and self._embedding_profile_signature == signature
        ):
            return self._embedding_profile
        async with self._embedding_profile_lock:
            if (
                self._embedding_profile is not None
                and self._embedding_profile_signature == signature
            ):
                return self._embedding_profile
            with model_observation_scope(
                task="embedding.expression_profile",
                profile="embedding",
            ):
                vectors = await provider.embed(
                    list(_EXPRESSION_EMBEDDING_PROBES)
                )
            normalized = self._validate_embedding_batch(
                vectors,
                expected_count=len(_EXPRESSION_EMBEDDING_PROBES),
                expected_dimension=None,
            )
            payload = {
                "version": 1,
                "model": model_name,
                "probes": [
                    [round(value, 6) for value in vector]
                    for vector in normalized
                ],
            }
            marker = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            profile = _ExpressionEmbeddingProfile(
                marker=marker,
                model_name=model_name,
                dimension=len(normalized[0]),
                provider=provider,
            )
            self._embedding_profile = profile
            self._embedding_profile_signature = signature
            return profile

    async def _ensure_expression_vector_index(
        self,
        *,
        session_id: str,
        candidates: list[GroupExpressionPattern],
    ) -> _ExpressionVectorIndex:
        """补齐内容指纹对应向量，并原子发布确定性 cosine K-means 结果。"""

        async with self._expression_index_locks[session_id]:
            profile = await self._embedding_profile_for_expressions()
            ordered = sorted(candidates, key=lambda item: item.id)
            content_hashes = {
                item.id: self._stable_hash(item.situation, item.style)
                for item in ordered
            }
            stored = await self.store.list_group_expression_embeddings(
                expression_ids=[item.id for item in ordered],
                profile_marker=profile.marker,
                content_hashes=content_hashes,
            )
            missing = [item for item in ordered if item.id not in stored]
            batch_size = (
                self.settings.social_learning.expression_vector_batch_size
            )
            for offset in range(0, len(missing), batch_size):
                batch = missing[offset : offset + batch_size]
                with model_observation_scope(
                    task="embedding.expression_backfill",
                    profile="embedding",
                    session_id=session_id,
                ):
                    produced = await profile.provider.embed(
                        [
                            self._expression_embedding_text(item)
                            for item in batch
                        ]
                    )
                vectors = self._validate_embedding_batch(
                    produced,
                    expected_count=len(batch),
                    expected_dimension=profile.dimension,
                )
                await self.store.save_group_expression_embeddings(
                    profile_marker=profile.marker,
                    model_name=profile.model_name,
                    items=[
                        (
                            item.id,
                            content_hashes[item.id],
                            vector,
                        )
                        for item, vector in zip(batch, vectors, strict=True)
                    ],
                )
                stored.update(
                    {
                        item.id: {
                            "vector": vector,
                            "cluster_id": None,
                            "cluster_fingerprint": "",
                        }
                        for item, vector in zip(batch, vectors, strict=True)
                    }
                )
            vectors_by_id: dict[str, list[float]] = {}
            for item in ordered:
                raw_vector = stored[item.id]["vector"]
                if not isinstance(raw_vector, list):
                    raise ValueError(
                        f"表达向量 {item.id} 不是 JSON array"
                    )
                vectors_by_id[item.id] = self._normalize_vector(
                    [float(value) for value in raw_vector]
                )
            index_fingerprint = hashlib.sha256(
                (
                    profile.marker
                    + "\0"
                    + "\0".join(
                        f"{item.id}:{content_hashes[item.id]}"
                        for item in ordered
                    )
                ).encode()
            ).hexdigest()
            cached_centers = (
                await self.store.list_group_expression_cluster_centers(
                    session_id=session_id,
                    profile_marker=profile.marker,
                    index_fingerprint=index_fingerprint,
                )
            )
            cluster_by_expression: dict[str, int] = {}
            for item in ordered:
                raw_cluster_id = stored[item.id]["cluster_id"]
                if (
                    isinstance(raw_cluster_id, int)
                    and stored[item.id]["cluster_fingerprint"]
                    == index_fingerprint
                ):
                    cluster_by_expression[item.id] = raw_cluster_id
            if (
                len(cluster_by_expression) != len(ordered)
                or not cached_centers
            ):
                cluster_count = min(
                    len(ordered),
                    self.settings.social_learning.expression_vector_cluster_max,
                    max(1, math.ceil(math.sqrt(len(ordered)))),
                )
                matrix = [vectors_by_id[item.id] for item in ordered]
                labels, centers = self._run_cosine_kmeans(
                    matrix, cluster_count=cluster_count
                )
                cluster_by_expression = {
                    item.id: labels[index]
                    for index, item in enumerate(ordered)
                }
                await self.store.save_group_expression_cluster_index(
                    session_id=session_id,
                    profile_marker=profile.marker,
                    model_name=profile.model_name,
                    index_fingerprint=index_fingerprint,
                    assignments=cluster_by_expression,
                    centers=centers,
                )
            else:
                centers = []
                for item in cached_centers:
                    raw_centroid = item["centroid"]
                    if not isinstance(raw_centroid, list):
                        raise ValueError("表达聚类中心不是 JSON array")
                    centers.append(
                        self._normalize_vector(
                            [float(value) for value in raw_centroid]
                        )
                    )
            return _ExpressionVectorIndex(
                profile=profile,
                vectors=vectors_by_id,
                cluster_by_expression=cluster_by_expression,
                centers=centers,
            )

    async def _rank_expressions_with_vectors(
        self,
        *,
        session_id: str,
        candidates: list[GroupExpressionPattern],
        query_text: str,
    ) -> list[tuple[float, GroupExpressionPattern]]:
        """簇召回后融合 item/cluster/lexical 分，并用向量 MMR 排序。"""

        index = await self._ensure_expression_vector_index(
            session_id=session_id, candidates=candidates
        )
        with model_observation_scope(
            task="embedding.expression_query",
            profile="embedding",
            session_id=session_id,
        ):
            query_vectors = self._validate_embedding_batch(
                await index.profile.provider.embed([query_text]),
                expected_count=1,
                expected_dimension=index.profile.dimension,
            )
        query_vector = self._normalize_vector(query_vectors[0])
        cluster_scores = [
            self._dot(center, query_vector) for center in index.centers
        ]
        cluster_order = sorted(
            range(len(cluster_scores)),
            key=lambda cluster_id: (
                cluster_scores[cluster_id],
                -cluster_id,
            ),
            reverse=True,
        )
        candidates_by_cluster: dict[
            int, list[GroupExpressionPattern]
        ] = defaultdict(list)
        for item in candidates:
            candidates_by_cluster[
                index.cluster_by_expression[item.id]
            ].append(item)
        pool: list[GroupExpressionPattern] = []
        selected_cluster_count = 0
        config = self.settings.social_learning
        for cluster_id in cluster_order:
            members = candidates_by_cluster.get(cluster_id, [])
            if not members:
                continue
            pool.extend(members)
            selected_cluster_count += 1
            if (
                selected_cluster_count
                >= config.expression_vector_cluster_pool_size
                and len(pool)
                >= config.expression_vector_candidate_pool_size
            ):
                break
        total_weight = (
            config.expression_vector_item_weight
            + config.expression_vector_cluster_weight
            + config.expression_vector_lexical_weight
        )
        query_features = self._text_features(query_text)
        scored: list[tuple[float, GroupExpressionPattern]] = []
        for item in pool:
            cluster_id = index.cluster_by_expression[item.id]
            item_similarity = self._dot(
                index.vectors[item.id], query_vector
            )
            lexical = self._overlap_score(
                query_features,
                self._text_features(f"{item.situation} {item.style}"),
            )
            score = (
                config.expression_vector_item_weight * item_similarity
                + config.expression_vector_cluster_weight
                * cluster_scores[cluster_id]
                + config.expression_vector_lexical_weight * lexical
            ) / total_weight
            reliability = 0.7 + 0.3 * item.confidence
            reinforcement = min(
                0.06, math.log1p(item.occurrence_count) * 0.015
            )
            fatigue = min(0.08, item.selection_count * 0.005)
            scored.append(
                (
                    max(0.0, score * reliability + reinforcement - fatigue),
                    item,
                )
            )
        scored.sort(key=lambda pair: (pair[0], pair[1].id), reverse=True)
        return self._vector_mmr_order(scored, index.vectors)

    def _vector_mmr_order(
        self,
        ranked: list[tuple[float, GroupExpressionPattern]],
        vectors: dict[str, list[float]],
    ) -> list[tuple[float, GroupExpressionPattern]]:
        """保持候选全集，同时让最前面的回注名额具备语义多样性。"""

        remaining = list(ranked)
        selected: list[tuple[float, GroupExpressionPattern]] = []
        lambda_value = (
            self.settings.social_learning.expression_vector_diversity_lambda
        )
        focus_count = min(
            len(remaining),
            max(
                self.settings.social_learning.expression_injection_limit * 3,
                self.settings.social_learning.expression_injection_limit,
            ),
        )
        while remaining and len(selected) < focus_count:
            best_index = 0
            best_key = (float("-inf"), "")
            for index, (relevance, item) in enumerate(remaining):
                redundancy = max(
                    (
                        self._dot(vectors[item.id], vectors[chosen.id])
                        for _, chosen in selected
                    ),
                    default=0.0,
                )
                mmr = (
                    lambda_value * relevance
                    - (1.0 - lambda_value) * redundancy
                )
                key = (mmr, item.id)
                if key > best_key:
                    best_index = index
                    best_key = key
            selected.append(remaining.pop(best_index))
        return [*selected, *remaining]

    @classmethod
    def _run_cosine_kmeans(
        cls,
        vectors: list[list[float]],
        *,
        cluster_count: int,
        max_iterations: int = 100,
    ) -> tuple[list[int], list[list[float]]]:
        """确定性 farthest-first cosine K-means；输入和中心均为单位向量。"""

        if not vectors:
            return [], []
        normalized = [cls._normalize_vector(item) for item in vectors]
        cluster_count = max(1, min(cluster_count, len(normalized)))
        if cluster_count == 1:
            return [0] * len(normalized), [
                cls._normalize_vector(cls._mean_vector(normalized))
            ]
        centroid_indices = [0]
        while len(centroid_indices) < cluster_count:
            remaining = [
                index
                for index in range(len(normalized))
                if index not in centroid_indices
            ]
            next_index = min(
                remaining,
                key=lambda index: (
                    max(
                        cls._dot(
                            normalized[index], normalized[selected]
                        )
                        for selected in centroid_indices
                    ),
                    index,
                ),
            )
            centroid_indices.append(next_index)
        centers = [list(normalized[index]) for index in centroid_indices]
        labels = [-1] * len(normalized)
        for _ in range(max_iterations):
            next_labels = [
                max(
                    range(cluster_count),
                    key=lambda cluster_id: (
                        cls._dot(vector, centers[cluster_id]),
                        -cluster_id,
                    ),
                )
                for vector in normalized
            ]
            counts = [
                next_labels.count(cluster_id)
                for cluster_id in range(cluster_count)
            ]
            for empty_cluster in (
                cluster_id
                for cluster_id, count in enumerate(counts)
                if count == 0
            ):
                donor = max(
                    (
                        index
                        for index, label in enumerate(next_labels)
                        if counts[label] > 1
                    ),
                    key=lambda index: (
                        1.0
                        - cls._dot(
                            normalized[index],
                            centers[next_labels[index]],
                        ),
                        -index,
                    ),
                )
                counts[next_labels[donor]] -= 1
                next_labels[donor] = empty_cluster
                counts[empty_cluster] += 1
            if next_labels == labels:
                break
            labels = next_labels
            centers = [
                cls._normalize_vector(
                    cls._mean_vector(
                        [
                            vector
                            for vector, label in zip(
                                normalized, labels, strict=True
                            )
                            if label == cluster_id
                        ]
                    )
                )
                for cluster_id in range(cluster_count)
            ]
        return labels, centers

    @staticmethod
    def _mean_vector(vectors: list[list[float]]) -> list[float]:
        if not vectors:
            raise ValueError("不能计算空向量集合的中心")
        return [
            sum(vector[index] for vector in vectors) / len(vectors)
            for index in range(len(vectors[0]))
        ]

    @staticmethod
    def _dot(left: list[float], right: list[float]) -> float:
        if len(left) != len(right):
            raise ValueError(
                f"向量维度不一致: left={len(left)}, right={len(right)}"
            )
        return sum(
            left_value * right_value
            for left_value, right_value in zip(left, right, strict=True)
        )

    @staticmethod
    def _normalize_vector(vector: list[float]) -> list[float]:
        if not vector or any(not math.isfinite(value) for value in vector):
            raise ValueError("embedding 必须是有限非空向量")
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0:
            raise ValueError("embedding 不能是零向量")
        return [value / norm for value in vector]

    @classmethod
    def _validate_embedding_batch(
        cls,
        vectors: list[list[float]],
        *,
        expected_count: int,
        expected_dimension: int | None,
    ) -> list[list[float]]:
        if len(vectors) != expected_count:
            raise ValueError(
                "embedding 返回数量错误: "
                f"expected={expected_count}, actual={len(vectors)}"
            )
        normalized: list[list[float]] = []
        dimension = expected_dimension
        for vector in vectors:
            numeric = [float(value) for value in vector]
            if dimension is None:
                dimension = len(numeric)
            if len(numeric) != dimension:
                raise ValueError(
                    "embedding 维度不一致: "
                    f"expected={dimension}, actual={len(numeric)}"
                )
            normalized.append(cls._normalize_vector(numeric))
        return normalized

    @staticmethod
    def _expression_embedding_text(
        item: GroupExpressionPattern,
    ) -> str:
        return f"情景：{item.situation}\n风格：{item.style}"

    async def _analyze_behavior_scenario(
        self,
        *,
        session: SessionView,
        messages: list[StoredMessage],
        fallback: BehaviorScenarioProfile,
    ) -> BehaviorScenarioProfile:
        vocabulary = await self.store.list_behavior_tag_vocabulary(
            session.id, limit=120
        )
        with model_observation_scope(
            task="social.behavior_scene",
            profile="profile",
            session_id=session.id,
        ):
            payload = await self._complete_payload(
                self.prompting.build_behavior_scene_analysis(
                    session=session,
                    messages=messages,
                    known_tags=vocabulary,
                ),
                BehaviorScenarioPayload,
            )
        groups = [
            *self._normalize_tag_candidates(
                payload.tag_clusters, BehaviorTagKind.DOMAIN, 20
            ),
            *self._normalize_tag_candidates(
                [payload.need] if payload.need is not None else [],
                BehaviorTagKind.NEED,
                1,
            ),
            *self._normalize_tag_candidates(
                payload.other_traits, BehaviorTagKind.ATTITUDE, 8
            ),
        ]
        known_keys = {
            (group.kind, group.tags[0].casefold()) for group in groups
        }
        groups.extend(
            group
            for group in fallback.tag_groups
            if (group.kind, group.tags[0].casefold()) not in known_keys
        )
        if not groups:
            return fallback
        return BehaviorScenarioProfile(
            summary=self._normalize_phrase(payload.summary)
            or fallback.summary,
            tag_groups=groups[:30],
            confidence=payload.confidence,
        )

    def _local_behavior_scenario_profile(
        self,
        session: SessionView,
        messages: list[StoredMessage],
        semantic_tags: set[str],
    ) -> BehaviorScenarioProfile:
        return BehaviorScenarioProfile(
            summary=self._scene_summary(session, messages),
            tag_groups=[
                BehaviorTagGroup(
                    kind=BehaviorTagKind.DOMAIN, tags=[tag]
                )
                for tag in sorted(semantic_tags)
            ],
            confidence=0.45 if semantic_tags else 0.0,
        )

    def _scene_summary(
        self,
        session: SessionView,
        messages: list[StoredMessage],
    ) -> str:
        """只从平台允许投影的用户消息生成场景摘要。"""

        last_user = next(
            (
                self.prompting.project_message_content(session, item).strip()
                for item in reversed(messages)
                if item.role == MessageRole.USER
                and self.prompting.project_message_content(
                    session,
                    item,
                ).strip()
            ),
            "当前对话场景",
        )
        return last_user[:500]

    @staticmethod
    def _blend_behavior_graph_score(
        *,
        local_score: float,
        graph_score: float | None,
    ) -> float:
        """图分只做召回增强，持久反馈和词面分仍保留否决能力。"""

        if graph_score is None or graph_score <= 0:
            return local_score
        normalized_graph = min(1.0, graph_score / 2.0)
        return 0.45 * local_score + 0.55 * normalized_graph

    @classmethod
    def _normalize_tag_candidates(
        cls,
        values: list[str | BehaviorTagCandidate],
        kind: BehaviorTagKind,
        limit: int,
    ) -> list[BehaviorTagGroup]:
        groups: list[BehaviorTagGroup] = []
        seen: set[str] = set()
        for value in values:
            raw_tags = (
                [value]
                if isinstance(value, str)
                else [value.tag_name, *value.tag_aliases]
            )
            tags: list[str] = []
            for raw_tag in raw_tags:
                normalized = cls._normalize_phrase(raw_tag)
                key = normalized.casefold()
                if (
                    not normalized
                    or len(normalized) > 80
                    or key in _GENERIC_BEHAVIOR_TAGS
                    or key in {item.casefold() for item in tags}
                ):
                    continue
                tags.append(normalized)
            if not tags:
                continue
            group_key = f"{kind.value}:{tags[0].casefold()}"
            if group_key in seen:
                continue
            seen.add(group_key)
            groups.append(BehaviorTagGroup(kind=kind, tags=tags[:8]))
            if len(groups) >= limit:
                break
        return groups

    @staticmethod
    def _canonical_tags(
        groups: list[BehaviorTagGroup], kind: BehaviorTagKind
    ) -> list[str]:
        return [group.tags[0] for group in groups if group.kind == kind]

    @classmethod
    def _select_diverse_expressions(
        cls,
        ranked_candidates: list[tuple[float, GroupExpressionPattern]],
        *,
        limit: int,
    ) -> list[GroupExpressionPattern]:
        """用轻量 MMR 抑制同义表达挤占全部回注名额。"""

        if limit <= 0:
            return []
        remaining = list(ranked_candidates)
        selected: list[tuple[float, GroupExpressionPattern]] = []
        relevance_weight = 0.78
        while remaining and len(selected) < limit:
            best_index = 0
            best_key = (float("-inf"), "")
            for index, (relevance, candidate) in enumerate(remaining):
                candidate_features = cls._text_features(
                    f"{candidate.situation} {candidate.style}"
                )
                redundancy = max(
                    (
                        cls._overlap_score(
                            candidate_features,
                            cls._text_features(
                                f"{chosen.situation} {chosen.style}"
                            ),
                        )
                        for _, chosen in selected
                    ),
                    default=0.0,
                )
                mmr_score = (
                    relevance_weight * relevance
                    - (1.0 - relevance_weight) * redundancy
                )
                key = (mmr_score, candidate.id)
                if key > best_key:
                    best_index = index
                    best_key = key
            selected.append(remaining.pop(best_index))
        return [item for _, item in selected]

    @classmethod
    def _expression_match_score(
        cls,
        situation: str,
        *,
        query_text: str,
        query_tags: set[str],
        confidence: float,
        occurrence_count: int,
        selection_count: int,
    ) -> float:
        candidate_tags = cls._semantic_tags(situation)
        semantic = cls._overlap_score(query_tags, candidate_tags)
        lexical = cls._overlap_score(
            cls._text_features(query_text), cls._text_features(situation)
        )
        reinforcement = min(0.12, math.log1p(occurrence_count) * 0.04)
        fatigue = min(0.08, selection_count * 0.005)
        return max(
            0.0,
            0.5 * semantic
            + 0.28 * lexical
            + 0.18 * confidence
            + reinforcement
            - fatigue,
        )

    @classmethod
    def _behavior_match_score(
        cls,
        pattern: BehaviorPattern,
        *,
        query_text: str,
        query_tags: set[str],
    ) -> float:
        scene_text = " ".join(
            [
                pattern.scene_summary,
                *pattern.scene_tags,
                *pattern.need_tags,
                *pattern.other_traits,
            ]
        )
        candidate_tags = cls._semantic_tags(scene_text) | set(
            pattern.scene_tags
        ) | set(pattern.need_tags)
        semantic = cls._overlap_score(query_tags, candidate_tags)
        lexical = cls._overlap_score(
            cls._text_features(query_text), cls._text_features(scene_text)
        )
        feedback_weight = max(-0.18, min(0.2, pattern.score * 0.05))
        success_bonus = min(0.12, pattern.success_count * 0.03)
        failure_penalty = min(0.2, pattern.failure_count * 0.05)
        cold_factor = (
            0.55
            if pattern.occurrence_count <= 2
            and pattern.activation_count == 0
            and pattern.success_count == 0
            else 1.0
        )
        score = (
            0.48 * semantic
            + 0.3 * lexical
            + 0.18 * pattern.confidence
            + feedback_weight
            + success_bonus
            - failure_penalty
        )
        return max(0.0, score * cold_factor)

    @staticmethod
    def _overlap_score(left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        shared = left & right
        return len(shared) / max(1, min(len(left), len(right)))

    @staticmethod
    def _text_features(text: str) -> set[str]:
        normalized = _SPACE_RE.sub(" ", text.casefold()).strip()
        features = {token.casefold() for token in _ASCII_TOKEN_RE.findall(normalized)}
        chinese = "".join(char for char in normalized if "\u4e00" <= char <= "\u9fff")
        features.update(
            chinese[index : index + 2]
            for index in range(max(0, len(chinese) - 1))
        )
        return features

    @staticmethod
    def _semantic_tags(text: str) -> set[str]:
        normalized = text.casefold()
        return {
            tag
            for tag, markers in _SEMANTIC_TAGS.items()
            if any(marker.casefold() in normalized for marker in markers)
        }

    @staticmethod
    def _normalize_term_surface(value: str) -> str:
        normalized = _TERM_EDGE_RE.sub("", _SPACE_RE.sub(" ", value).strip())
        if not normalized or len(normalized) > 64:
            return ""
        if normalized.casefold().startswith(("http://", "https://", "www.")):
            return ""
        if all(not char.isalnum() and not ("\u4e00" <= char <= "\u9fff") for char in normalized):
            return ""
        return normalized

    @staticmethod
    def _normalize_phrase(value: str) -> str:
        return _SPACE_RE.sub(" ", value).strip()

    @classmethod
    def _normalize_tags(cls, values: list[str], limit: int) -> list[str]:
        return list(
            dict.fromkeys(
                normalized
                for value in values
                if (normalized := cls._normalize_phrase(value))
            )
        )[:limit]

    @classmethod
    def _stable_hash(cls, *values: str) -> str:
        normalized = "\0".join(cls._normalize_phrase(value).casefold() for value in values)
        return hashlib.sha256(normalized.encode()).hexdigest()

    async def cancel_session_tasks(self, session_ids: list[str]) -> None:
        """取消并等待旧数据 epoch 上的学习与反馈任务。"""

        tasks = {
            task
            for session_id in session_ids
            for task in self._tasks_by_session.get(session_id, set())
            if not task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _track(
        self,
        task: asyncio.Task[object],
        *,
        session_id: str | None = None,
    ) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if session_id is None:
            return
        owned = self._tasks_by_session.setdefault(session_id, set())
        owned.add(task)

        def discard(completed: asyncio.Task[object]) -> None:
            current = self._tasks_by_session.get(session_id)
            if current is None:
                return
            current.discard(completed)
            if not current:
                self._tasks_by_session.pop(session_id, None)

        task.add_done_callback(discard)
