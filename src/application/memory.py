"""长期记忆归档、检索、纠正与四链路共享投影。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections import Counter
from contextlib import AsyncExitStack
from datetime import UTC, datetime

from pydantic import BaseModel, Field, ValidationError

from application.events import EventHub
from application.model_json import parse_model_json
from config import AppSettings
from domain.errors import (
    ConflictError,
    InputValidationError,
    InvalidModelResponseError,
    NotFoundError,
)
from domain.models import (
    ExtractionStatus,
    FactStatus,
    MemoryConsolidationRun,
    MemoryKind,
    MemoryRecord,
    MemorySourceChain,
    MemoryStatus,
    ProfileFact,
    SessionView,
    StoredMessage,
    scope_key_for,
    utc_now,
)
from observability import model_observation_scope
from ports import EmbeddingProvider, ModelProvider, ModelRequest, ProfileMemoryRepository
from prompting import PromptAssembler
from prompting.budget import estimate_text_tokens

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]+", re.IGNORECASE)
_EPISODE_QUERY_RE = re.compile(
    r"(?:上次|那次|当时|后来|曾经|一起|经历|发生|做过|去过|回忆|什么时候)"
)
_RELATIONSHIP_QUERY_RE = re.compile(
    r"(?:关系|咱俩|我们俩|朋友|家人|同事|信任|相处|认识|边界|之间)"
)
_MEMORY_PAGE_SIZE = 200
_MEMORY_FTS_CANDIDATE_LIMIT = 1000


def memory_content_hash(content: str) -> str:
    """生成与排版无关的稳定内容摘要。"""

    normalized = " ".join(content.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _tokens(text: str) -> list[str]:
    """为中英文生成无需外部索引的稳定词与汉字 n-gram。"""

    result: list[str] = []
    for match in _WORD_RE.finditer(text.casefold()):
        token = match.group(0)
        if token.isascii():
            if len(token) > 1:
                result.append(token)
            continue
        characters = list(token)
        result.extend(character for character in characters if character.strip())
        for size in (2, 3):
            result.extend(
                "".join(characters[index : index + size])
                for index in range(max(0, len(characters) - size + 1))
            )
    return result


def _bm25_relevance(query: str, documents: list[str]) -> list[float]:
    """计算带查询覆盖约束的 BM25 分数，并压缩到 0..1 供统一门槛使用。"""

    query_terms = set(_tokens(query))
    if not query_terms or not documents:
        return [0.0] * len(documents)
    tokenized = [_tokens(document) for document in documents]
    average_length = sum(len(tokens) for tokens in tokenized) / len(tokenized)
    document_frequency = {
        term: sum(term in set(tokens) for tokens in tokenized)
        for term in query_terms
    }
    scores: list[float] = []
    for tokens in tokenized:
        frequencies = Counter(tokens)
        raw_score = 0.0
        matched = 0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            matched += 1
            frequency_count = document_frequency[term]
            inverse_frequency = math.log(
                1
                + (len(documents) - frequency_count + 0.5)
                / (frequency_count + 0.5)
            )
            length_ratio = len(tokens) / max(1.0, average_length)
            denominator = frequency + 1.5 * (0.25 + 0.75 * length_ratio)
            raw_score += inverse_frequency * frequency * 2.5 / denominator
        coverage = matched / len(query_terms)
        scores.append(
            (raw_score / (raw_score + 1.5)) * math.sqrt(coverage)
            if raw_score
            else 0.0
        )
    return scores


def _intent_memory_kinds(query: str) -> set[MemoryKind]:
    """从显式回忆/关系问法中选择结构化召回 lane。"""

    kinds: set[MemoryKind] = set()
    if _EPISODE_QUERY_RE.search(query):
        kinds.update({MemoryKind.EPISODE, MemoryKind.EVENT})
    if _RELATIONSHIP_QUERY_RE.search(query):
        kinds.add(MemoryKind.RELATIONSHIP)
    return kinds


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算两个同维向量的余弦相似度；无效向量不产生命中。"""

    if not left or len(left) != len(right):
        return -1.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else -1.0


class ConsolidatedMemoryCandidate(BaseModel):
    """归档模型提出、尚未写入的记忆候选。"""

    kind: MemoryKind
    subject_id: str | None = Field(default=None, max_length=200)
    content: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    source_message_ids: list[str] = Field(min_length=1, max_length=100)
    supersedes_memory_id: str | None = Field(default=None, max_length=80)


class ConsolidationPayload(BaseModel):
    memories: list[ConsolidatedMemoryCandidate] = Field(default_factory=list, max_length=50)
    retract_memory_ids: list[str] = Field(default_factory=list, max_length=20)


def _explicit_correction_requested(source_text: str) -> bool:
    """只识别具有明确修改语义的措辞，避免把普通反问中的“不是”当授权。"""

    if re.search(r"(?:更正|纠正|改成|不再|其实是|实际是|应该是|准确地说)", source_text):
        return True
    # “不是杭州，是苏州”“并非讨厌，而是过敏”具有成对的新旧事实；
    # 单独的“杭州不是号称美食荒漠吗”只是反问，不能授权替代既有记忆。
    return bool(
        re.search(
            r"(?:不是|并非)[^。！？\n]{0,80}(?:[,，;；]\s*)?(?:而)?是",
            source_text,
        )
    )


class MemoryService:
    """长期记忆的唯一写入、版本、检索和后台任务 owner。"""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: ProfileMemoryRepository,
        model: ModelProvider,
        embedding: EmbeddingProvider | None = None,
        prompting: PromptAssembler,
        events: EventHub,
    ) -> None:
        self.settings = settings
        self.store = store
        self.model = model
        self.embedding = embedding
        self.prompting = prompting
        self.events = events
        self._tasks: set[asyncio.Task[None]] = set()
        self._tasks_by_session: dict[str, set[asyncio.Task[None]]] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._write_locks: dict[str, asyncio.Lock] = {}

    def set_model(self, model: ModelProvider) -> None:
        """热切换后续归档任务使用的模型。"""

        self.model = model

    def set_embedding_provider(self, embedding: EmbeddingProvider | None) -> None:
        """热切换独立 embedding Provider；停用后仅使用本地关键词 lane。"""

        self.embedding = embedding

    async def start(self) -> None:
        """恢复崩溃前 pending/running 的归档任务。"""

        if not self.settings.memory.enabled:
            return
        for run in await self.store.list_recoverable_memory_runs():
            run.status = ExtractionStatus.PENDING
            run.updated_at = utc_now()
            await self.store.save_memory_consolidation_run(run)
            self._track(
                asyncio.create_task(self._execute_consolidation(run)),
                session_id=run.session_id,
            )
        if self.embedding is not None and self.settings.embedding.enabled:
            self._track(asyncio.create_task(self._backfill_embeddings()))

    async def stop(self) -> None:
        """等待已开始的本地归档写入完成。"""

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def enqueue_consolidation(
        self,
        *,
        session: SessionView,
        source_messages: list[StoredMessage],
        source_chain: MemorySourceChain,
        source_run_id: str | None,
        defer_execution: bool = False,
    ) -> MemoryConsolidationRun | None:
        """为一批权威消息持久化幂等归档任务，并可延迟到显式工具完成后执行。"""

        run = self.build_consolidation_run(
            session=session,
            source_messages=source_messages,
            source_chain=source_chain,
            source_run_id=source_run_id,
        )
        if run is None:
            return None
        saved, created = await self.store.create_memory_consolidation_run(run)
        if created:
            await self.events.publish(
                "memory.consolidation.updated", saved.model_dump(mode="json")
            )
        if not defer_execution and saved.status == ExtractionStatus.PENDING:
            self._track(
                asyncio.create_task(self._execute_consolidation(saved)),
                session_id=saved.session_id,
            )
        return saved

    def build_consolidation_run(
        self,
        *,
        session: SessionView,
        source_messages: list[StoredMessage],
        source_chain: MemorySourceChain,
        source_run_id: str | None,
    ) -> MemoryConsolidationRun | None:
        """构造尚未持久化的归档 outbox，使调用方可与 Turn 原子提交。"""

        if not self.settings.memory.enabled or not source_messages:
            return None
        message_ids = sorted({item.id for item in source_messages})
        fingerprint_payload = {
            "session_id": session.id,
            "source_chain": source_chain.value,
            "source_run_id": source_run_id,
            "message_ids": message_ids,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return MemoryConsolidationRun(
            session_id=session.id,
            scope_key=scope_key_for(session),
            source_chain=source_chain,
            source_run_id=source_run_id,
            source_message_ids=message_ids,
            source_fingerprint=fingerprint,
            data_epoch=session.data_epoch,
        )

    async def publish_enqueued_consolidation(
        self, run: MemoryConsolidationRun, *, created: bool
    ) -> None:
        """发布由外层原子事务创建的归档任务事件。"""

        if created:
            await self.events.publish(
                "memory.consolidation.updated", run.model_dump(mode="json")
            )

    async def start_consolidation(self, run_id: str) -> None:
        """启动已持久化的待处理归档任务；重复启动保持幂等。"""

        run = await self.store.get_memory_consolidation_run(run_id)
        if run is None:
            raise NotFoundError("记忆归档任务不存在")
        if run.status == ExtractionStatus.PENDING:
            self._track(
                asyncio.create_task(self._execute_consolidation(run)),
                session_id=run.session_id,
            )

    async def retry_consolidation(self, run_id: str) -> MemoryConsolidationRun:
        run = await self.store.get_memory_consolidation_run(run_id)
        if run is None:
            raise NotFoundError("记忆归档任务不存在")
        if run.status not in {ExtractionStatus.FAILED, ExtractionStatus.PENDING}:
            raise InputValidationError("只有失败或待处理的记忆归档任务可以重试")
        run.status = ExtractionStatus.PENDING
        run.error_code = None
        run.error_message = None
        run.updated_at = utc_now()
        await self.store.save_memory_consolidation_run(run)
        self._track(
            asyncio.create_task(self._execute_consolidation(run)),
            session_id=run.session_id,
        )
        return run

    async def _execute_consolidation(self, run: MemoryConsolidationRun) -> None:
        lock = self._run_locks.setdefault(run.id, asyncio.Lock())
        try:
            async with lock:
                current = await self.store.get_memory_consolidation_run(run.id)
                if current is None or current.status == ExtractionStatus.COMPLETED:
                    return
                run = current
                run.status = ExtractionStatus.RUNNING
                run.attempt_count += 1
                run.updated_at = utc_now()
                await self.store.save_memory_consolidation_run(run)
                await self.events.publish(
                    "memory.consolidation.updated", run.model_dump(mode="json")
                )
                try:
                    session = await self.store.get_session(run.session_id)
                    if session is None:
                        raise NotFoundError("记忆归档任务对应会话不存在")
                    if session.data_epoch != run.data_epoch:
                        raise ConflictError("记忆归档任务的数据版本已失效")
                    if not await self.store.messages_are_recallable(
                        run.session_id, run.source_message_ids
                    ):
                        run.produced_memory_ids = []
                        run.status = ExtractionStatus.COMPLETED
                        run.error_code = None
                        run.error_message = None
                        return
                    source_messages = [
                        message
                        for message_id in run.source_message_ids
                        if (
                            message := await self.store.get_message(message_id)
                        )
                        is not None
                    ]
                    if {item.id for item in source_messages} != set(
                        run.source_message_ids
                    ):
                        raise InputValidationError("记忆归档任务的来源消息不完整")
                    context = await self.store.list_recallable_messages(
                        run.session_id,
                        self.settings.memory.consolidation_context_messages,
                    )
                    existing = await self.store.list_memories(
                        scope_key=run.scope_key,
                        statuses={MemoryStatus.ACTIVE},
                        limit=100,
                    )
                    existing = self._fit_context(existing)
                    with model_observation_scope(
                        task="memory.consolidate",
                        profile="profile",
                        session_id=run.session_id,
                        run_id=run.id,
                    ):
                        result = await self.model.complete(
                            ModelRequest(
                                messages=self.prompting.build_memory_consolidation(
                                    session=session,
                                    source_messages=source_messages,
                                    recent_messages=context,
                                    existing_memories=existing,
                                ),
                                model=self.settings.model.profile_name,
                                temperature=0,
                                max_tokens=min(
                                    1600,
                                    self.settings.model.max_tokens,
                                ),
                                json_mode=True,
                            )
                        )
                    if not result.content:
                        raise InvalidModelResponseError("记忆归档模型没有返回 JSON")
                    try:
                        payload = ConsolidationPayload.model_validate(
                            parse_model_json(result.content)
                        )
                    except (json.JSONDecodeError, ValidationError) as exc:
                        raise InvalidModelResponseError(
                            f"记忆归档结果不符合 schema: {exc}"
                        ) from exc
                    current_session = await self.store.get_session(
                        run.session_id
                    )
                    if (
                        current_session is None
                        or current_session.data_epoch != run.data_epoch
                    ):
                        raise ConflictError("记忆归档任务的数据版本已失效")
                    run.produced_memory_ids = await self._apply_payload(
                        run=run,
                        session=current_session,
                        payload=payload,
                        existing=existing,
                    )
                    run.status = ExtractionStatus.COMPLETED
                    run.error_code = None
                    run.error_message = None
                except asyncio.CancelledError:
                    run.status = ExtractionStatus.CANCELLED
                    run.error_code = "session_lifecycle_cancelled"
                    run.error_message = "归档任务因会话清空或删除被取消"
                    raise
                except ConflictError as exc:
                    run.status = ExtractionStatus.STALE
                    run.error_code = "session_data_epoch_stale"
                    run.error_message = str(exc)[:500]
                except Exception as exc:
                    run.status = ExtractionStatus.FAILED
                    run.error_code = getattr(
                        exc, "code", "memory_consolidation_failed"
                    )
                    run.error_message = str(exc)[:500]
                    logger.exception(
                        "长期记忆归档失败",
                        extra={
                            "session_id": run.session_id,
                            "memory_run_id": run.id,
                            "source_chain": run.source_chain.value,
                        },
                    )
                finally:
                    run.updated_at = utc_now()
                    await self.store.save_memory_consolidation_run(run)
                    await self.events.publish(
                        "memory.consolidation.updated", run.model_dump(mode="json")
                    )
        finally:
            # 每个 Run 只允许一个锁 owner；完成后释放索引，避免长期运行持续增长。
            if not lock.locked():
                self._run_locks.pop(run.id, None)

    async def _apply_payload(
        self,
        *,
        run: MemoryConsolidationRun,
        session: SessionView,
        payload: ConsolidationPayload,
        existing: list[MemoryRecord],
    ) -> list[str]:
        allowed_sources = set(run.source_message_ids)
        allowed_subjects = {
            item.external_user_id for item in session.participants
        } | {"agent"}
        existing_map = {item.id: item for item in existing}
        source_messages = [
            message
            for message_id in run.source_message_ids
            if (message := await self.store.get_message(message_id)) is not None
        ]
        source_text = "\n".join(item.plain_text for item in source_messages)
        correction_requested = _explicit_correction_requested(source_text)
        retraction_requested = bool(
            re.search(r"(?:忘记|删掉|删除|不要记|别记|撤回)", source_text)
        )
        produced: list[str] = []
        candidates = list(
            payload.memories[: self.settings.memory.automatic_max_items_per_run]
        )
        superseded_ids: set[str] = set()
        # 先验证整份模型输出，再执行任何写入，避免尾部坏候选留下部分提交。
        for index, candidate in enumerate(candidates):
            if not set(candidate.source_message_ids).issubset(allowed_sources):
                raise InvalidModelResponseError("记忆候选引用了本次任务范围外的消息")
            if candidate.subject_id is not None and candidate.subject_id not in allowed_subjects:
                raise InvalidModelResponseError("记忆候选引用了当前会话外的主体")
            if not candidate.content.strip():
                raise InvalidModelResponseError("记忆候选内容不能为空白")
            if candidate.confidence < self.settings.memory.minimum_confidence:
                continue
            if candidate.supersedes_memory_id is not None:
                if not correction_requested:
                    # 模型输出是不可信计划；没有用户授权时仅拒绝替代副作用，
                    # 候选本身仍由来源、主体和置信度规则独立校验并安全归档。
                    logger.warning(
                        "已拒绝未经用户授权的自动记忆替代",
                        extra={
                            "session_id": run.session_id,
                            "memory_run_id": run.id,
                            "candidate_index": index,
                        },
                    )
                    candidate = candidate.model_copy(
                        update={"supersedes_memory_id": None}
                    )
                    candidates[index] = candidate
                    continue
                supersedes = existing_map.get(candidate.supersedes_memory_id)
                if supersedes is None or supersedes.scope_key != run.scope_key:
                    raise InvalidModelResponseError("记忆候选替代了本次可见域外的记忆")
                if (
                    supersedes.kind != candidate.kind
                    or supersedes.subject_id != candidate.subject_id
                ):
                    raise InvalidModelResponseError(
                        "纠正候选必须与被替代记忆保持类别和主体一致"
                    )
                superseded_ids.add(supersedes.id)
        retract_ids = set(payload.retract_memory_ids)
        if retract_ids and not retraction_requested:
            # 撤回是破坏性副作用，模型误判不能让合法归档失败或触碰既有记忆。
            logger.warning(
                "已拒绝未经用户授权的自动记忆撤回",
                extra={
                    "session_id": run.session_id,
                    "memory_run_id": run.id,
                    "rejected_count": len(retract_ids),
                },
            )
            retract_ids.clear()
        if superseded_ids & retract_ids:
            raise InvalidModelResponseError("同一记忆不能在一次归档中同时替代和撤回")
        for memory_id in retract_ids:
            memory = existing_map.get(memory_id)
            if memory is None or memory.scope_key != run.scope_key:
                raise InvalidModelResponseError("归档模型撤回了本次可见域外的记忆")

        # 候选已经通过语义和权限校验；以下“等价合并、替代、撤回”必须作为一个
        # 数据库事务提交。否则中途写入故障会留下半批状态，而 run 会显示失败。
        key = scope_key_for(session)
        lock = self._write_locks.setdefault(key, asyncio.Lock())
        async with lock:
            current = await self._load_memories(
                scope_key=key,
                statuses={MemoryStatus.ACTIVE, MemoryStatus.CONFLICTED},
            )
            current_map = {item.id: item for item in current}
            active_by_identity = {
                (item.kind, item.subject_id, item.content_hash): item
                for item in current
                if item.status == MemoryStatus.ACTIVE
            }
            changed: dict[str, MemoryRecord] = {}
            now = utc_now()

            def stage(memory: MemoryRecord) -> None:
                changed[memory.id] = memory

            for candidate in candidates:
                if candidate.confidence < self.settings.memory.minimum_confidence:
                    continue
                if not await self.store.messages_are_recallable(
                    session.id, candidate.source_message_ids
                ):
                    raise InputValidationError("来源消息已被记忆清空边界隔离")
                supersedes = (
                    current_map.get(candidate.supersedes_memory_id)
                    if candidate.supersedes_memory_id is not None
                    else None
                )
                if candidate.supersedes_memory_id is not None and (
                    supersedes is None
                    or supersedes.status not in {
                        MemoryStatus.ACTIVE,
                        MemoryStatus.CONFLICTED,
                    }
                ):
                    raise InvalidModelResponseError("纠正候选替代了已失效的记忆")
                clean_content = " ".join(candidate.content.strip().split())
                content_hash = memory_content_hash(clean_content)
                identity = (candidate.kind, candidate.subject_id, content_hash)
                equivalent = active_by_identity.get(identity)
                incoming_message_ids = set(candidate.source_message_ids)
                incoming_refs = {run.id}
                if equivalent is not None:
                    if supersedes is not None:
                        if equivalent.id == supersedes.id:
                            raise InputValidationError("纠正后的内容与原记忆相同")
                        supersedes.status = MemoryStatus.SUPERSEDED
                        supersedes.source_refs = sorted(
                            set(supersedes.source_refs)
                            | {f"replaced_by:{equivalent.id}"}
                        )
                        supersedes.updated_at = now
                        stage(supersedes)
                        if equivalent.supersedes_id is None:
                            equivalent.supersedes_id = supersedes.id
                    has_new_evidence = bool(
                        incoming_message_ids - set(equivalent.source_message_ids)
                        or incoming_refs - set(equivalent.source_refs)
                        or (
                            run.source_run_id
                            and run.source_run_id != equivalent.source_run_id
                        )
                    )
                    equivalent.confidence = max(equivalent.confidence, candidate.confidence)
                    equivalent.importance = max(equivalent.importance, candidate.importance)
                    if has_new_evidence:
                        equivalent.reinforcement += 1
                    equivalent.source_message_ids = sorted(
                        set(equivalent.source_message_ids) | incoming_message_ids
                    )
                    equivalent.source_refs = sorted(
                        set(equivalent.source_refs) | incoming_refs
                    )
                    equivalent.updated_at = now
                    stage(equivalent)
                    produced.append(equivalent.id)
                    continue

                memory = MemoryRecord(
                    session_id=session.id,
                    scope_key=key,
                    subject_id=candidate.subject_id,
                    kind=candidate.kind,
                    content=clean_content,
                    content_hash=content_hash,
                    confidence=candidate.confidence,
                    importance=candidate.importance,
                    source_chain=run.source_chain,
                    source_run_id=run.source_run_id or run.id,
                    source_message_ids=sorted(incoming_message_ids),
                    source_refs=sorted(incoming_refs),
                    supersedes_id=supersedes.id if supersedes is not None else None,
                )
                if supersedes is not None:
                    supersedes.status = MemoryStatus.SUPERSEDED
                    supersedes.source_refs = sorted(
                        set(supersedes.source_refs) | {f"replaced_by:{memory.id}"}
                    )
                    supersedes.updated_at = now
                    stage(supersedes)
                active_by_identity[identity] = memory
                current_map[memory.id] = memory
                stage(memory)
                produced.append(memory.id)

            for memory_id in retract_ids:
                memory = current_map.get(memory_id)
                if memory is None or memory.status not in {
                    MemoryStatus.ACTIVE,
                    MemoryStatus.CONFLICTED,
                }:
                    raise InvalidModelResponseError("归档模型撤回了已失效的记忆")
                memory.status = MemoryStatus.RETRACTED
                memory.source_refs = sorted(set(memory.source_refs) | {run.id})
                memory.updated_at = now
                stage(memory)

            await self.store.save_memories_batch_if_epoch(
                list(changed.values()),
                session_id=run.session_id,
                data_epoch=run.data_epoch,
            )
        for memory in changed.values():
            await self._publish_memory(memory)
        return list(dict.fromkeys(produced))

    async def sync_profile_fact(
        self,
        *,
        session: SessionView,
        fact: ProfileFact,
        source_run_id: str,
    ) -> MemoryRecord | None:
        """把活跃画像投影到统一记忆，并隔离未解决的画像冲突。"""

        if fact.status == FactStatus.CONFLICTED:
            # 画像表是兼容派生投影。相同主体/类别出现互斥内容时，所有关联的
            # 长期记忆先退出活跃召回，等待用户通过可审计纠正确定新版本。
            conflicted_fact_ids = {
                item.id
                for item in await self.store.list_facts(scope_key_for(session))
                if item.subject_id == fact.subject_id
                and item.category == fact.category
                and item.status == FactStatus.CONFLICTED
            }
            key = scope_key_for(session)
            lock = self._write_locks.setdefault(key, asyncio.Lock())
            async with lock:
                memories = await self._load_active_memories(key)
                for memory in memories:
                    if not conflicted_fact_ids.intersection(memory.source_refs):
                        continue
                    memory.status = MemoryStatus.CONFLICTED
                    memory.source_refs = sorted(
                        set(memory.source_refs) | {f"profile_conflict:{fact.category}"}
                    )
                    memory.updated_at = utc_now()
                    await self.store.save_memory(memory)
                    await self._publish_memory(memory)
            return None

        kind = (
            MemoryKind.PREFERENCE
            if "偏好" in fact.category or "preference" in fact.category.casefold()
            else MemoryKind.PROFILE
        )
        return await self.remember(
            session=session,
            content=fact.content,
            kind=kind,
            subject_id=fact.subject_id,
            confidence=fact.confidence,
            importance=0.7,
            source_chain=MemorySourceChain.REACTIVE,
            source_run_id=source_run_id,
            source_message_ids=fact.source_message_ids,
            source_refs=[fact.id],
        )

    async def record_chain_outcome(
        self,
        *,
        session: SessionView,
        source_chain: MemorySourceChain,
        source_run_id: str,
        content: str,
        source_message_ids: list[str] | None = None,
        source_refs: list[str] | None = None,
        importance: float = 0.45,
    ) -> MemoryRecord | None:
        """在真实 sent/完成后记录链路结果；内部候选不调用此入口。"""

        if not self.settings.memory.enabled or not content.strip():
            return None
        return await self.remember(
            session=session,
            content=content,
            kind=MemoryKind.EVENT,
            confidence=1,
            importance=importance,
            source_chain=source_chain,
            source_run_id=source_run_id,
            source_message_ids=source_message_ids or [],
            source_refs=source_refs or [],
            happened_at=utc_now(),
        )

    async def try_record_chain_outcome(
        self,
        *,
        session: SessionView,
        source_chain: MemorySourceChain,
        source_run_id: str,
        content: str,
        source_message_ids: list[str] | None = None,
        source_refs: list[str] | None = None,
        importance: float = 0.45,
    ) -> MemoryRecord | None:
        """在 sent 后尽力写入派生记忆，失败可观测但不篡改发送终态。"""

        try:
            return await self.record_chain_outcome(
                session=session,
                source_chain=source_chain,
                source_run_id=source_run_id,
                content=content,
                source_message_ids=source_message_ids,
                source_refs=source_refs,
                importance=importance,
            )
        except Exception as exc:
            logger.exception(
                "链路结果写入长期记忆失败",
                extra={
                    "session_id": session.id,
                    "source_chain": source_chain.value,
                    "source_run_id": source_run_id,
                    "error_code": getattr(exc, "code", "memory_chain_write_failed"),
                },
            )
            await self.events.publish(
                "memory.chain.failed",
                {
                    "session_id": session.id,
                    "source_chain": source_chain.value,
                    "source_run_id": source_run_id,
                    "error_code": getattr(
                        exc, "code", "memory_chain_write_failed"
                    ),
                },
            )
            return None

    async def remember(
        self,
        *,
        session: SessionView,
        content: str,
        kind: MemoryKind,
        source_chain: MemorySourceChain,
        subject_id: str | None = None,
        confidence: float = 1,
        importance: float = 0.7,
        source_run_id: str | None = None,
        source_message_ids: list[str] | None = None,
        source_refs: list[str] | None = None,
        supersedes: MemoryRecord | None = None,
        happened_at: datetime | None = None,
    ) -> MemoryRecord:
        """串行化同域等价写入，并拒绝生命周期边界前冻结的 Session。"""

        # 同一可见域串行写入，使“查重→增强/替代→保存”成为单一状态转换。
        key = scope_key_for(session)
        lock = self._write_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._require_current_session_epoch(session)
            if source_message_ids and not await self.store.messages_are_recallable(
                session.id, source_message_ids
            ):
                raise InputValidationError(
                    "来源消息已被记忆清空边界隔离，不能重新写入记忆"
                )
            return await self._remember_unlocked(
                session=session,
                content=content,
                kind=kind,
                source_chain=source_chain,
                subject_id=subject_id,
                confidence=confidence,
                importance=importance,
                source_run_id=source_run_id,
                source_message_ids=source_message_ids,
                source_refs=source_refs,
                supersedes=supersedes,
                happened_at=happened_at,
            )

    async def clear(
        self, *, session_id: str | None = None
    ) -> dict[str, object]:
        """物理删除长期记忆、画像和社交学习，保留聊天正文与聊天审计。

        ``memory_cleared_at`` 推进到当前时间，使保留的旧正文只供用户查看，
        不再进入 Agent 上下文或重新派生被清空的知识。
        """

        sessions = (
            [await self.session_for_tool(session_id)]
            if session_id is not None
            else await self.store.list_sessions()
        )
        keys = sorted(scope_key_for(item) for item in sessions)
        async with AsyncExitStack() as stack:
            for key in keys:
                lock = self._write_locks.setdefault(key, asyncio.Lock())
                await stack.enter_async_context(lock)
            result = await self.store.clear_memory_state(session_id)
        await self.events.publish("memory.cleared", result)
        return result

    async def _remember_unlocked(
        self,
        *,
        session: SessionView,
        content: str,
        kind: MemoryKind,
        source_chain: MemorySourceChain,
        subject_id: str | None = None,
        confidence: float = 1,
        importance: float = 0.7,
        source_run_id: str | None = None,
        source_message_ids: list[str] | None = None,
        source_refs: list[str] | None = None,
        supersedes: MemoryRecord | None = None,
        happened_at: datetime | None = None,
    ) -> MemoryRecord:
        """在等价写锁内写入；等价项只增强，纠正项保留替代链。"""

        clean_content = " ".join(content.strip().split())
        if not clean_content:
            raise InputValidationError("记忆内容不能为空")
        scope_key = scope_key_for(session)
        content_hash = memory_content_hash(clean_content)
        existing = await self.store.find_active_memory(
            scope_key=scope_key,
            kind=kind,
            subject_id=subject_id,
            content_hash=content_hash,
        )
        now = utc_now()
        if existing is not None:
            changed: dict[str, MemoryRecord] = {}
            if supersedes is not None:
                if supersedes.scope_key != scope_key or supersedes.status not in {
                    MemoryStatus.ACTIVE,
                    MemoryStatus.CONFLICTED,
                }:
                    raise InputValidationError("只能替代当前会话可见域内的活跃或冲突记忆")
                if existing.id == supersedes.id:
                    raise InputValidationError("纠正后的内容与原记忆相同")
                supersedes.status = MemoryStatus.SUPERSEDED
                supersedes.source_refs = sorted(
                    set(supersedes.source_refs) | {f"replaced_by:{existing.id}"}
                )
                supersedes.updated_at = now
                changed[supersedes.id] = supersedes
                if existing.supersedes_id is None:
                    existing.supersedes_id = supersedes.id
                source_refs = [
                    *(source_refs or []),
                    f"supersedes:{supersedes.id}",
                ]
            incoming_message_ids = set(source_message_ids or [])
            incoming_refs = set(source_refs or [])
            has_new_evidence = bool(
                incoming_message_ids - set(existing.source_message_ids)
                or incoming_refs - set(existing.source_refs)
                or (
                    source_run_id
                    and source_run_id != existing.source_run_id
                )
            )
            existing.confidence = max(existing.confidence, confidence)
            existing.importance = max(existing.importance, importance)
            if has_new_evidence:
                existing.reinforcement += 1
            existing.source_message_ids = sorted(
                set(existing.source_message_ids) | incoming_message_ids
            )
            existing.source_refs = sorted(
                set(existing.source_refs) | incoming_refs
            )
            existing.updated_at = now
            changed[existing.id] = existing
            await self._save_memory_changes_if_current(
                session=session,
                memories=list(changed.values()),
            )
            for changed_memory in changed.values():
                await self._publish_memory(changed_memory)
            return existing
        memory = MemoryRecord(
            session_id=session.id,
            scope_key=scope_key,
            subject_id=subject_id,
            kind=kind,
            content=clean_content,
            content_hash=content_hash,
            confidence=confidence,
            importance=importance,
            source_chain=source_chain,
            source_run_id=source_run_id,
            source_message_ids=sorted(set(source_message_ids or [])),
            source_refs=sorted(set(source_refs or [])),
            supersedes_id=supersedes.id if supersedes is not None else None,
            happened_at=happened_at,
        )
        if supersedes is not None:
            if supersedes.scope_key != scope_key or supersedes.status not in {
                MemoryStatus.ACTIVE,
                MemoryStatus.CONFLICTED,
            }:
                raise InputValidationError("只能替代当前会话可见域内的活跃或冲突记忆")
            supersedes.status = MemoryStatus.SUPERSEDED
            supersedes.updated_at = now
        changes = [memory] if supersedes is None else [supersedes, memory]
        await self._save_memory_changes_if_current(
            session=session,
            memories=changes,
        )
        for changed_memory in changes:
            await self._publish_memory(changed_memory)
        return memory

    async def retrieve(
        self,
        *,
        session: SessionView,
        query: str,
        limit: int | None = None,
        record_recall: bool = True,
        context_budget: bool = False,
    ) -> list[MemoryRecord]:
        """在完整当前域内融合 BM25、结构化语义 lane 与可选向量。"""

        if not self.settings.memory.enabled:
            return []
        query_tokens = set(_tokens(query))
        generic_query = bool(
            re.search(r"(?:还记得|记不记得|之前|以前|历史|记忆|回顾)", query)
        )
        intent_kinds = _intent_memory_kinds(query)
        records = await self._load_retrieval_memories(
            scope_key=scope_key_for(session),
            query=query,
            generic_query=generic_query,
            intent_kinds=intent_kinds,
        )
        bm25_scores = _bm25_relevance(
            query,
            [record.content for record in records],
        )
        bm25_by_id = {
            record.id: value
            for record, value in zip(records, bm25_scores, strict=True)
        }
        normalized_query = " ".join(query.casefold().split())
        now = utc_now()

        def score(record: MemoryRecord) -> tuple[float, float]:
            lexical = bm25_by_id.get(record.id, 0.0)
            substring = (
                0.35
                if normalized_query
                and (
                    normalized_query in record.content.casefold()
                    or record.content.casefold() in normalized_query
                )
                else 0
            )
            age_days = max(
                0.0,
                (now - _as_utc(record.happened_at or record.updated_at)).total_seconds()
                / 86400,
            )
            recency = 1 / (1 + age_days / 30)
            kind_boost = {
                MemoryKind.PROFILE: 0.18,
                MemoryKind.PREFERENCE: 0.2,
                MemoryKind.COMMITMENT: 0.18,
                MemoryKind.RELATIONSHIP: 0.16,
                MemoryKind.PROCEDURE: 0.12,
                MemoryKind.EVENT: 0.05,
                MemoryKind.EPISODE: 0.14,
                MemoryKind.SUMMARY: 0.08,
            }[record.kind]
            intent_boost = 0.22 if record.kind in intent_kinds else 0.0
            total = (
                lexical * 0.55
                + substring
                + record.importance * 0.22
                + record.confidence * 0.12
                + recency * 0.08
                + min(0.08, math.log2(record.reinforcement + 1) * 0.02)
                + kind_boost
                + intent_boost
            )
            # BM25/子串是普通问题的相关性门槛；显式情节/关系问法另走结构化 lane。
            return total, max(lexical, substring)

        scored = [(record, *score(record)) for record in records]
        scored.sort(key=lambda item: (item[1], item[0].updated_at), reverse=True)
        if query_tokens and not generic_query:
            keyword_records = [
                item
                for item, total, lexical in scored
                if lexical >= self.settings.memory.retrieval_min_score
                and total >= self.settings.memory.retrieval_min_score
            ]
        else:
            keyword_records = [item for item, _total, _lexical in scored]
        intent_records = [
            item for item, _total, _lexical in scored if item.kind in intent_kinds
        ]
        ranked = await self._fuse_retrieval_lanes(
            query=query,
            keyword_records=keyword_records,
            intent_records=intent_records,
            records=records,
            session_id=session.id,
        )
        selected = self._select_for_injection(
            ranked, limit or self.settings.memory.retrieval_limit
        )
        if context_budget:
            selected = self._fit_context(selected)
        if record_recall:
            await self.store.record_memory_recall([item.id for item in selected])
            for item in selected:
                item.recall_count += 1
                item.last_recalled_at = now
        return selected

    async def _load_retrieval_memories(
        self,
        *,
        scope_key: str,
        query: str,
        generic_query: bool,
        intent_kinds: set[MemoryKind],
    ) -> list[MemoryRecord]:
        """先做可重建候选预筛，同时保留向量、泛化问法与结构化 lane 的完整语义。"""

        if (
            generic_query
            or not query.strip()
            or (self.embedding is not None and self.settings.embedding.enabled)
        ):
            return await self._load_active_memories(scope_key)
        candidates = await self.store.search_active_memory_candidates(
            scope_key=scope_key,
            query_text=query,
            limit=_MEMORY_FTS_CANDIDATE_LIMIT,
        )
        if candidates is None:
            return await self._load_active_memories(scope_key)
        if not intent_kinds:
            return candidates
        intent_records = await self._load_memories(
            scope_key=scope_key,
            statuses={MemoryStatus.ACTIVE},
            kinds=intent_kinds,
        )
        by_id = {item.id: item for item in candidates}
        by_id.update((item.id, item) for item in intent_records)
        return list(by_id.values())

    async def _load_active_memories(self, scope_key: str) -> list[MemoryRecord]:
        """用稳定游标读取当前域全部活跃记忆，避免固定上限改变召回语义。"""

        return await self._load_memories(
            scope_key=scope_key,
            statuses={MemoryStatus.ACTIVE},
        )

    async def _load_memories(
        self,
        *,
        scope_key: str,
        statuses: set[MemoryStatus],
        kinds: set[MemoryKind] | None = None,
    ) -> list[MemoryRecord]:
        """用稳定游标完整读取指定状态的当前域记忆。"""

        result: list[MemoryRecord] = []
        after_id: str | None = None
        while True:
            page = await self.store.page_memories(
                scope_key=scope_key,
                statuses=statuses,
                kinds=kinds,
                after_id=after_id,
                limit=_MEMORY_PAGE_SIZE,
            )
            if not page:
                return result
            result.extend(page)
            after_id = page[-1].id
            if len(page) < _MEMORY_PAGE_SIZE:
                return result

    async def _fuse_retrieval_lanes(
        self,
        *,
        query: str,
        keyword_records: list[MemoryRecord],
        intent_records: list[MemoryRecord],
        records: list[MemoryRecord],
        session_id: str,
    ) -> list[MemoryRecord]:
        """精确 scope 内融合 BM25、情节/关系意图与向量 rank。"""

        keyword_rank = {item.id: index + 1 for index, item in enumerate(keyword_records)}
        intent_rank = {item.id: index + 1 for index, item in enumerate(intent_records)}
        vector_rank: dict[str, int] = {}
        if self.embedding is not None and self.settings.embedding.enabled and query.strip():
            vectors = await self._ensure_embeddings(
                records,
                session_id=session_id,
            )
            try:
                with model_observation_scope(
                    task="embedding.memory_query",
                    profile="embedding",
                    session_id=session_id,
                ):
                    query_vector = (await self.embedding.embed([query]))[0]
            except Exception as exc:
                logger.warning(
                    "记忆向量查询失败，已降级为关键词召回",
                    extra={"error_type": type(exc).__name__},
                )
                query_vector = []
            ranked_vectors = [
                (record, _cosine_similarity(query_vector, vectors.get(record.id, [])))
                for record in records if record.id in vectors
            ]
            ranked_vectors.sort(key=lambda item: (item[1], item[0].updated_at), reverse=True)
            for record, similarity in ranked_vectors:
                threshold = self.settings.memory.vector_score_thresholds.get(
                    record.kind.value, self.settings.memory.vector_score_threshold
                )
                if similarity >= threshold:
                    vector_rank[record.id] = len(vector_rank) + 1
        if not vector_rank and not intent_rank:
            return keyword_records
        by_id = {item.id: item for item in records}
        fused: list[tuple[MemoryRecord, float]] = []
        for memory_id in set(keyword_rank) | set(intent_rank) | set(vector_rank):
            score = 0.0
            if memory_id in vector_rank:
                score += 1 / (self.settings.memory.rrf_k + vector_rank[memory_id])
            if memory_id in keyword_rank:
                score += self.settings.memory.keyword_rrf_weight / (
                    self.settings.memory.rrf_k + keyword_rank[memory_id]
                )
            if memory_id in intent_rank:
                score += 0.9 / (
                    self.settings.memory.rrf_k + intent_rank[memory_id]
                )
            fused.append((by_id[memory_id], score))
        fused.sort(key=lambda item: (-item[1], -item[0].importance, item[0].id))
        return [item for item, _score in fused]

    async def _ensure_embeddings(
        self,
        records: list[MemoryRecord],
        *,
        session_id: str | None = None,
    ) -> dict[str, list[float]]:
        """按当前模型回填缺失或正文变化的向量，失败不阻断关键词召回。"""

        if self.embedding is None or not self.settings.embedding.enabled:
            return {}
        vectors = await self.store.list_memory_embeddings(
            [item.id for item in records], model_name=self.settings.embedding.name
        )
        missing = [item for item in records if item.id not in vectors]
        if not missing:
            return vectors
        try:
            for offset in range(0, len(missing), 64):
                batch = missing[offset : offset + 64]
                with model_observation_scope(
                    task="embedding.memory_backfill",
                    profile="embedding",
                    session_id=session_id,
                ):
                    produced = await self.embedding.embed(
                        [item.content for item in batch]
                    )
                if len(produced) != len(batch):
                    raise InvalidModelResponseError("Embedding 返回数量与记忆数量不一致")
                await self.store.save_memory_embeddings(
                    model_name=self.settings.embedding.name,
                    entries=list(zip(batch, produced, strict=True)),
                )
                vectors.update(
                    {item.id: vector for item, vector in zip(batch, produced, strict=True)}
                )
        except Exception as exc:
            logger.warning("记忆向量回填失败，已降级为关键词召回", extra={"error_type": type(exc).__name__})
        return vectors

    async def _backfill_embeddings(self) -> None:
        """按游标分批回填全部活跃记忆，不因固定上限留下永久缺口。"""

        if self.embedding is None or not self.settings.embedding.enabled:
            return
        after_id: str | None = None
        while True:
            records = await self.store.page_memories_missing_embedding(
                model_name=self.settings.embedding.name,
                after_id=after_id,
                limit=_MEMORY_PAGE_SIZE,
            )
            if not records:
                return
            vectors = await self._ensure_embeddings(records)
            if any(item.id not in vectors for item in records):
                logger.warning(
                    "记忆向量分页回填未完成，等待下次启动或配置切换重试",
                    extra={
                        "model_name": self.settings.embedding.name,
                        "page_size": len(records),
                    },
                )
                return
            after_id = records[-1].id

    def _select_for_injection(self, records: list[MemoryRecord], limit: int) -> list[MemoryRecord]:
        """按类型分配注入名额，避免单类低价值记忆淹没上下文。"""

        result: list[MemoryRecord] = []
        procedure_preference = 0
        event_profile = 0
        episode_relationship = 0
        other_by_kind: dict[MemoryKind, int] = {}
        for record in records:
            if record.kind in {MemoryKind.PROCEDURE, MemoryKind.PREFERENCE}:
                if procedure_preference >= self.settings.memory.inject_procedure_preference_limit:
                    continue
                procedure_preference += 1
            elif record.kind in {MemoryKind.EVENT, MemoryKind.PROFILE}:
                if event_profile >= self.settings.memory.inject_event_profile_limit:
                    continue
                event_profile += 1
            elif record.kind in {MemoryKind.EPISODE, MemoryKind.RELATIONSHIP}:
                if (
                    episode_relationship
                    >= self.settings.memory.inject_episode_relationship_limit
                ):
                    continue
                episode_relationship += 1
            elif other_by_kind.get(record.kind, 0) >= self.settings.memory.retrieval_per_kind_limit:
                continue
            else:
                other_by_kind[record.kind] = other_by_kind.get(record.kind, 0) + 1
            result.append(record)
            if len(result) >= limit:
                break
        return result

    async def latest_conversation_summaries(
        self, *, session: SessionView, limit: int = 1
    ) -> list[MemoryRecord]:
        """投影当前域最近的滚动摘要，避免长会话只能依赖截断历史。"""

        if not self.settings.memory.enabled:
            return []
        records = await self.store.list_memories(
            scope_key=scope_key_for(session),
            statuses={MemoryStatus.ACTIVE},
            kinds={MemoryKind.SUMMARY},
            limit=limit,
        )
        projected: list[MemoryRecord] = []
        for record in records:
            content = record.content
            while content and estimate_text_tokens(content) > 320:
                content = content[: max(1, len(content) - max(1, len(content) // 8))]
            projected.append(
                record.model_copy(
                    update={"content": content.rstrip() + ("…" if content != record.content else "")}
                )
            )
        if projected:
            await self.store.record_memory_recall([item.id for item in projected])
        return projected

    async def forget(
        self, *, session: SessionView, memory_id: str, reason: str
    ) -> MemoryRecord:
        """在当前 Session epoch 内可审计撤回，不物理删除权威记录。"""

        key = scope_key_for(session)
        lock = self._write_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._require_current_session_epoch(session)
            memory = await self._require_visible_memory(session, memory_id)
            if memory.status not in {MemoryStatus.ACTIVE, MemoryStatus.CONFLICTED}:
                raise InputValidationError("只有活跃或冲突记忆可以撤回")
            memory.status = MemoryStatus.RETRACTED
            memory.source_chain = MemorySourceChain.MANUAL
            memory.source_refs = sorted(
                set(memory.source_refs) | {f"reason:{reason.strip()}"}
            )
            memory.updated_at = utc_now()
            await self._save_memory_changes_if_current(
                session=session,
                memories=[memory],
            )
        await self._publish_memory(memory)
        return memory

    async def correct(
        self,
        *,
        session: SessionView,
        memory_id: str,
        corrected_content: str,
        reason: str,
    ) -> MemoryRecord:
        """在当前 Session epoch 内原子替代旧记忆，保留完整纠正链。"""

        key = scope_key_for(session)
        lock = self._write_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._require_current_session_epoch(session)
            old = await self._require_visible_memory(session, memory_id)
            if memory_content_hash(corrected_content) == old.content_hash:
                raise InputValidationError("纠正后的内容与原记忆相同")
            return await self._remember_unlocked(
                session=session,
                content=corrected_content,
                kind=old.kind,
                subject_id=old.subject_id,
                confidence=1,
                importance=max(0.8, old.importance),
                source_chain=MemorySourceChain.MANUAL,
                source_refs=[f"reason:{reason.strip()}"],
                supersedes=old,
                happened_at=utc_now(),
            )

    async def _require_current_session_epoch(self, session: SessionView) -> None:
        """拒绝 clear/delete 前冻结的 Session 快照继续产生长期记忆副作用。"""

        if await self.store.session_epoch_is_current(session.id, session.data_epoch):
            return
        raise ConflictError(
            "会话数据版本已失效，不能写入长期记忆",
            details={
                "session_id": session.id,
                "expected_epoch": session.data_epoch,
            },
        )

    async def _save_memory_changes_if_current(
        self,
        *,
        session: SessionView,
        memories: list[MemoryRecord],
    ) -> None:
        """在一个事务内复核 epoch 并提交整组记忆状态转换。"""

        try:
            await self.store.save_memories_batch_if_epoch(
                memories,
                session_id=session.id,
                data_epoch=session.data_epoch,
            )
        except ConflictError as exc:
            raise ConflictError(
                "会话数据版本已失效，不能写入长期记忆",
                details=exc.details,
            ) from exc

    async def _require_visible_memory(
        self, session: SessionView, memory_id: str
    ) -> MemoryRecord:
        memory = await self.store.get_memory(memory_id)
        if memory is None or memory.scope_key != scope_key_for(session):
            raise NotFoundError("记忆不存在或不属于当前会话可见域")
        return memory

    async def session_for_tool(self, session_id: str) -> SessionView:
        """为工具适配层读取当前会话；不存在时明确失败。"""

        session = await self.store.get_session(session_id)
        if session is None:
            raise NotFoundError("会话不存在")
        return session

    async def _publish_memory(self, memory: MemoryRecord) -> None:
        await self.events.publish("memory.updated", memory.model_dump(mode="json"))

    def _fit_context(self, memories: list[MemoryRecord]) -> list[MemoryRecord]:
        """按字符与保守 token 双预算裁剪临时投影，不修改权威记忆正文。"""

        remaining_chars = self.settings.memory.max_context_chars
        remaining_tokens = self.settings.memory.max_context_tokens
        result: list[MemoryRecord] = []
        for memory in memories:
            if remaining_chars <= 0 or remaining_tokens <= 0:
                break
            char_overhead = 160
            token_overhead = 48
            available_chars = max(0, remaining_chars - char_overhead)
            available_tokens = max(0, remaining_tokens - token_overhead)
            if available_chars <= 0 or available_tokens <= 0:
                break
            projected = memory
            if (
                len(memory.content) > available_chars
                or estimate_text_tokens(memory.content) > available_tokens
            ):
                if result:
                    break
                clipped = memory.content[: max(1, available_chars - 1)].rstrip()
                while clipped and estimate_text_tokens(clipped + "…") > available_tokens:
                    clipped = clipped[: max(1, len(clipped) - max(1, len(clipped) // 8))]
                projected = memory.model_copy(
                    update={
                        "content": clipped.rstrip() + "…"
                    }
                )
            result.append(projected)
            remaining_chars -= min(len(projected.content), available_chars) + char_overhead
            remaining_tokens -= estimate_text_tokens(projected.content) + token_overhead
        return result

    async def cancel_session_tasks(self, session_ids: list[str]) -> None:
        """取消并等待属于旧 Session epoch 的归档任务。"""

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
        task: asyncio.Task[None],
        *,
        session_id: str | None = None,
    ) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if session_id is None:
            return
        owned = self._tasks_by_session.setdefault(session_id, set())
        owned.add(task)

        def discard(completed: asyncio.Task[None]) -> None:
            current = self._tasks_by_session.get(session_id)
            if current is None:
                return
            current.discard(completed)
            if not current:
                self._tasks_by_session.pop(session_id, None)

        task.add_done_callback(discard)
