import asyncio
import json

import pytest

from adapters.model import FakeModelProvider
from adapters.persistence import DatabaseStore
from application.memory import (
    _bm25_relevance,
    _explicit_correction_requested,
    memory_content_hash,
)
from bootstrap import build_runtime
from config import EmbeddingSettings
from domain.errors import InputValidationError, NotFoundError
from domain.models import (
    ChatType,
    EngagementPolicy,
    FactStatus,
    InboundMessage,
    MemoryKind,
    MemoryRecord,
    MemorySourceChain,
    MemoryStatus,
    MessageComponent,
    Participant,
    ProfileFact,
    scope_key_for,
)
from ports import ModelRequest, ModelResult, ModelToolCall


class FailFirstMemoryConsolidationModel(FakeModelProvider):
    """首次记忆归档返回坏 JSON，用于验证可审计重试。"""

    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False

    async def complete(self, request: ModelRequest) -> ModelResult:
        system_text = "\n".join(
            item.content or "" for item in request.messages if item.role == "system"
        )
        if "# 长期记忆归档任务" in system_text and not self.failed_once:
            self.failed_once = True
            self.requests.append(request)
            return ModelResult(content="{")
        return await super().complete(request)


class CorrectThenConsolidateModel(FakeModelProvider):
    """验证同一 Turn 先提交显式纠正，再运行后台归档。"""

    def __init__(self, memory_id: str) -> None:
        super().__init__()
        self.memory_id = memory_id
        self.tool_result_seen = False
        self.consolidated_before_tool = False

    async def complete(self, request: ModelRequest) -> ModelResult:
        system_text = "\n".join(
            item.content or "" for item in request.messages if item.role == "system"
        )
        if "# 画像事实提取任务" in system_text:
            return ModelResult(content='{"facts":[]}')
        if "# 长期记忆归档任务" in system_text:
            self.consolidated_before_tool = not self.tool_result_seen
            return ModelResult(content='{"memories":[],"retract_memory_ids":[]}')
        if request.messages and request.messages[-1].role == "tool":
            self.tool_result_seen = True
            return ModelResult(content="已经按你说的纠正。")
        return ModelResult(
            tool_calls=[
                ModelToolCall(
                    id="correct-memory-call",
                    name="correct_memory",
                    arguments=json.dumps(
                        {
                            "memory_id": self.memory_id,
                            "corrected_content": "用户喜欢红茶",
                            "reason": "用户本轮明确纠正",
                        },
                        ensure_ascii=False,
                    ),
                )
            ]
        )

class InvalidMemoryBatchModel(FakeModelProvider):
    """返回前半合法、后半越域的批次，验证归档不会部分提交。"""

    async def complete(self, request: ModelRequest) -> ModelResult:
        system_text = "\n".join(
            item.content or "" for item in request.messages if item.role == "system"
        )
        if "# 长期记忆归档任务" not in system_text:
            return await super().complete(request)
        self.requests.append(request)
        payload = self._last_untrusted_payload(request)
        source_id = payload["source_message_ids"][0]
        return ModelResult(
            content=json.dumps(
                {
                    "memories": [
                        {
                            "kind": "preference",
                            "subject_id": "user-invalid-batch",
                            "content": "喜欢民谣",
                            "confidence": 0.9,
                            "importance": 0.8,
                            "source_message_ids": [source_id],
                            "supersedes_memory_id": None,
                        },
                        {
                            "kind": "profile",
                            "subject_id": "outside-session",
                            "content": "越域主体",
                            "confidence": 0.9,
                            "importance": 0.8,
                            "source_message_ids": [source_id],
                            "supersedes_memory_id": None,
                        },
                    ],
                    "retract_memory_ids": [],
                },
                ensure_ascii=False,
            )
        )


class TwoMemoryBatchModel(FakeModelProvider):
    """返回两条合法候选，用于验证存储故障不会留下半批提交。"""

    async def complete(self, request: ModelRequest) -> ModelResult:
        system_text = "\n".join(
            item.content or "" for item in request.messages if item.role == "system"
        )
        if "# 长期记忆归档任务" not in system_text:
            return await super().complete(request)
        payload = self._last_untrusted_payload(request)
        source_id = payload["source_message_ids"][0]
        return ModelResult(
            content=json.dumps(
                {
                    "memories": [
                        {
                            "kind": "preference",
                            "subject_id": "user-atomic-batch",
                            "content": "喜欢爵士乐",
                            "confidence": 0.9,
                            "importance": 0.8,
                            "source_message_ids": [source_id],
                            "supersedes_memory_id": None,
                        },
                        {
                            "kind": "preference",
                            "subject_id": "user-atomic-batch",
                            "content": "喜欢古典乐",
                            "confidence": 0.9,
                            "importance": 0.8,
                            "source_message_ids": [source_id],
                            "supersedes_memory_id": None,
                        },
                    ],
                    "retract_memory_ids": [],
                },
                ensure_ascii=False,
            )
        )


class SemanticEmbeddingProvider:
    """用稳定语义簇模拟远端 embedding，覆盖混合召回而不访问网络。"""

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[list[float]]:
        vectors = []
        for text in texts:
            vectors.append([1.0, 0.0] if any(term in text for term in ("烘焙", "甜点")) else [0.0, 1.0])
        return vectors

    async def close(self) -> None:
        return None


class CountingEmbeddingProvider:
    """记录分页索引处理量，验证全部记忆都建立派生向量。"""

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        return [[float(index + 1), 1.0] for index, _text in enumerate(texts)]

    async def close(self) -> None:
        return None


class UnauthorizedMemoryMutationModel(FakeModelProvider):
    """夹带未授权替代和撤回，验证只拒绝副作用而不丢弃合法候选。"""

    def __init__(self, *, supersedes_id: str, retract_id: str) -> None:
        super().__init__()
        self.supersedes_id = supersedes_id
        self.retract_id = retract_id

    async def complete(self, request: ModelRequest) -> ModelResult:
        system_text = "\n".join(
            item.content or "" for item in request.messages if item.role == "system"
        )
        if "# 长期记忆归档任务" not in system_text:
            return await super().complete(request)
        self.requests.append(request)
        payload = self._last_untrusted_payload(request)
        source_id = payload["source_message_ids"][0]
        return ModelResult(
            content=json.dumps(
                {
                    "memories": [
                        {
                            "kind": "preference",
                            "subject_id": "user-unauthorized-mutations",
                            "content": "用户喜欢手冲咖啡",
                            "confidence": 0.95,
                            "importance": 0.8,
                            "source_message_ids": [source_id],
                            "supersedes_memory_id": self.supersedes_id,
                        }
                    ],
                    "retract_memory_ids": [self.retract_id],
                },
                ensure_ascii=False,
            )
        )


class RhetoricalQuestionMutationModel(FakeModelProvider):
    """复现普通“不是……吗”被误判为纠正、进而导致归档失败的问题。"""

    def __init__(self, *, supersedes_id: str, subject_id: str) -> None:
        super().__init__()
        self.supersedes_id = supersedes_id
        self.subject_id = subject_id

    async def complete(self, request: ModelRequest) -> ModelResult:
        system_text = "\n".join(
            item.content or "" for item in request.messages if item.role == "system"
        )
        if "# 长期记忆归档任务" not in system_text:
            return await super().complete(request)
        payload = self._last_untrusted_payload(request)
        return ModelResult(
            content=json.dumps(
                {
                    "memories": [
                        {
                            "kind": "preference",
                            "subject_id": self.subject_id,
                            "content": "用户想了解杭州的特色菜",
                            "confidence": 0.9,
                            "importance": 0.7,
                            "source_message_ids": [payload["source_message_ids"][0]],
                            "supersedes_memory_id": self.supersedes_id,
                        }
                    ],
                    "retract_memory_ids": [],
                },
                ensure_ascii=False,
            )
        )


async def _private(runtime, suffix: str):
    return await runtime.chat.create_session(
        chat_type=ChatType.PRIVATE,
        display_name=f"记忆私聊-{suffix}",
        external_chat_id=f"memory-{suffix}",
        participants=[Participant(external_user_id=f"user-{suffix}", display_name="小明")],
    )


async def _cancel_debounce(runtime, session_id: str) -> None:
    task = runtime.chat._debounce_tasks[session_id]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_correction_phrase_detection_rejects_rhetorical_question() -> None:
    assert not _explicit_correction_requested(
        "嚯，杭州不是号称美食荒漠吗，我记得那里有道著名的黑暗料理"
    )
    assert _explicit_correction_requested("我不是喜欢红茶，而是喜欢咖啡")
    assert _explicit_correction_requested("更正一下，我现在不再喝咖啡")


def test_bm25_relevance_preserves_term_frequency_and_query_coverage() -> None:
    repeated, single, unrelated = _bm25_relevance(
        "python asyncio",
        [
            "python python python asyncio concurrency",
            "python gardening",
            "classical music",
        ],
    )

    assert repeated > single > unrelated
    assert unrelated == 0


@pytest.mark.asyncio
async def test_memory_scope_retrieval_correction_and_retraction(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        first = await _private(runtime, "first")
        second = await _private(runtime, "second")
        original = await runtime.memory.remember(
            session=first,
            content="用户喜欢手冲咖啡",
            kind=MemoryKind.PREFERENCE,
            subject_id="user-first",
            source_chain=MemorySourceChain.MANUAL,
        )
        await runtime.memory.remember(
            session=second,
            content="用户喜欢红茶",
            kind=MemoryKind.PREFERENCE,
            subject_id="user-second",
            source_chain=MemorySourceChain.MANUAL,
        )

        recalled = await runtime.memory.retrieve(
            session=first, query="咖啡偏好", limit=10
        )
        assert [item.id for item in recalled] == [original.id]
        assert all(item.scope_key.endswith(first.id) for item in recalled)

        with pytest.raises(InputValidationError, match="与原记忆相同"):
            await runtime.memory.correct(
                session=first,
                memory_id=original.id,
                corrected_content=" 用户喜欢手冲咖啡 ",
                reason="无实际变化",
            )
        corrected = await runtime.memory.correct(
            session=first,
            memory_id=original.id,
            corrected_content="用户现在更喜欢红茶，不再喝咖啡",
            reason="用户明确纠正",
        )
        assert corrected.supersedes_id == original.id
        saved_original = await runtime.store.get_memory(original.id)
        assert saved_original is not None
        assert saved_original.status == MemoryStatus.SUPERSEDED

        retracted = await runtime.memory.forget(
            session=first,
            memory_id=corrected.id,
            reason="用户要求忘记",
        )
        assert retracted.status == MemoryStatus.RETRACTED
        assert await runtime.memory.retrieve(
            session=first, query="饮品偏好", limit=10
        ) == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_retrieval_avoids_unrelated_memory_and_projects_scoped_summary(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await _private(runtime, "retrieval-relevance")
        memory = await runtime.memory.remember(
            session=session,
            content="用户喜欢在周末听爵士乐",
            kind=MemoryKind.PREFERENCE,
            subject_id="user-retrieval-relevance",
            source_chain=MemorySourceChain.MANUAL,
        )
        summary = await runtime.memory.remember(
            session=session,
            content="本周用户正在准备搬家，已确认周六上午看房。" * 50,
            kind=MemoryKind.SUMMARY,
            subject_id="user-retrieval-relevance",
            source_chain=MemorySourceChain.MANUAL,
        )

        assert await runtime.memory.retrieve(
            session=session,
            query="今天北京天气如何",
            record_recall=False,
        ) == []
        assert [item.id for item in await runtime.memory.retrieve(
            session=session,
            query="你还记得我的偏好吗",
            record_recall=False,
        )] == [memory.id, summary.id]
        summaries = await runtime.memory.latest_conversation_summaries(session=session)
        assert [item.id for item in summaries] == [summary.id]
        assert summaries[0].content.endswith("…")
        assert len(summaries[0].content) < len(summary.content)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_hybrid_retrieval_uses_vector_rrf_and_persists_embeddings(settings) -> None:
    hybrid_settings = settings.model_copy(deep=True)
    hybrid_settings.embedding = EmbeddingSettings(
        enabled=True,
        base_url="https://embedding.example/v1",
        api_key="secret-for-test-only",
        name="test-embedding",
    )
    runtime = build_runtime(
        hybrid_settings,
        model_override=FakeModelProvider(),
        embedding_override=SemanticEmbeddingProvider(),
    )
    await runtime.start()
    try:
        session = await _private(runtime, "hybrid-retrieval")
        semantic = await runtime.memory.remember(
            session=session,
            content="用户最近开始学习烘焙",
            kind=MemoryKind.PREFERENCE,
            subject_id="user-hybrid-retrieval",
            source_chain=MemorySourceChain.MANUAL,
        )
        await runtime.memory.remember(
            session=session,
            content="用户每周末去跑步",
            kind=MemoryKind.EVENT,
            subject_id="user-hybrid-retrieval",
            source_chain=MemorySourceChain.MANUAL,
        )

        recalled = await runtime.memory.retrieve(
            session=session, query="有什么甜点入门建议", record_recall=False
        )

        assert recalled[0].id == semantic.id
        vectors = await runtime.store.list_memory_embeddings(
            [item.id for item in recalled], model_name="test-embedding"
        )
        assert semantic.id in vectors
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_episode_and_relationship_queries_use_structured_retrieval_lanes(
    settings,
) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await _private(runtime, "episode-relationship")
        episode = await runtime.memory.remember(
            session=session,
            content="去年秋天在西湖骑行时轮胎坏了，Agent 陪用户把车推回了家。",
            kind=MemoryKind.EPISODE,
            subject_id="user-episode-relationship",
            source_chain=MemorySourceChain.MANUAL,
        )
        relationship = await runtime.memory.remember(
            session=session,
            content="小明把 Agent 当作可以坦诚沟通的老朋友。",
            kind=MemoryKind.RELATIONSHIP,
            subject_id="user-episode-relationship",
            source_chain=MemorySourceChain.MANUAL,
        )
        await runtime.memory.remember(
            session=session,
            content="小明偏好低糖饮品。",
            kind=MemoryKind.PREFERENCE,
            subject_id="user-episode-relationship",
            source_chain=MemorySourceChain.MANUAL,
        )

        episode_result = await runtime.memory.retrieve(
            session=session,
            query="我们上次一起经历了什么？",
            record_recall=False,
        )
        relationship_result = await runtime.memory.retrieve(
            session=session,
            query="咱俩现在是什么关系？",
            record_recall=False,
        )

        assert episode_result[0].id == episode.id
        assert relationship_result[0].id == relationship.id
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_embedding_backfill_pages_through_more_than_one_index_page(
    settings,
) -> None:
    indexed_settings = settings.model_copy(deep=True)
    indexed_settings.embedding = EmbeddingSettings(
        enabled=True,
        base_url="https://embedding.example/v1",
        api_key="secret-for-test-only",
        name="paged-embedding",
    )
    embedding = CountingEmbeddingProvider()
    runtime = build_runtime(
        indexed_settings,
        model_override=FakeModelProvider(),
        embedding_override=embedding,
    )
    await runtime.start()
    try:
        await runtime.memory.stop()
        session = await _private(runtime, "paged-index")
        records = [
            MemoryRecord(
                session_id=session.id,
                scope_key=scope_key_for(session),
                subject_id="user-paged-index",
                kind=MemoryKind.EVENT,
                content=f"第 {index} 条可索引记忆",
                content_hash=memory_content_hash(f"第 {index} 条可索引记忆"),
                source_chain=MemorySourceChain.MANUAL,
            )
            for index in range(205)
        ]
        await runtime.store.save_memories_batch(records)

        await runtime.memory._backfill_embeddings()

        vectors = await runtime.store.list_memory_embeddings(
            [item.id for item in records],
            model_name="paged-embedding",
        )
        assert len(vectors) == 205
        assert sum(embedding.batch_sizes) == 205
        assert max(embedding.batch_sizes) <= 64
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_clear_memory_preserves_transcript_but_blocks_recall(
    settings,
) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        first = await _private(runtime, "clear-first")
        second = await _private(runtime, "clear-second")
        old_message, _ = await runtime.store.append_inbound(
            InboundMessage(
                platform=first.platform,
                account_id=first.account_id,
                external_message_id="clear-old-message",
                external_chat_id=first.external_chat_id,
                sender_id="user-clear-first",
                sender_name="小明",
                chat_type=ChatType.PRIVATE,
                components=[MessageComponent.text_component("我喜欢爵士乐")],
            )
        )
        await runtime.store.save_fact(
            ProfileFact(
                subject_id="user-clear-first",
                scope_key=scope_key_for(first),
                category="偏好",
                content="喜欢爵士乐",
                confidence=0.9,
                source_message_ids=[old_message.id],
            )
        )
        await runtime.memory.remember(
            session=first,
            content="用户喜欢爵士乐",
            kind=MemoryKind.PREFERENCE,
            subject_id="user-clear-first",
            source_chain=MemorySourceChain.REACTIVE,
            source_message_ids=[old_message.id],
        )
        second_memory = await runtime.memory.remember(
            session=second,
            content="用户喜欢红茶",
            kind=MemoryKind.PREFERENCE,
            subject_id="user-clear-second",
            source_chain=MemorySourceChain.MANUAL,
        )

        cleared = await runtime.memory.clear(session_id=first.id)

        assert cleared["operation"] == "clear_long_term_memory"
        assert cleared["memory_count"] == 1
        assert cleared["profile_fact_count"] == 1
        # 长期记忆与聊天正文是两个独立删除域；旧正文只供用户查看，不再召回。
        assert [item.id for item in await runtime.store.list_messages(first.id)] == [
            old_message.id
        ]
        assert await runtime.store.list_recallable_messages(first.id) == []
        assert await runtime.memory.retrieve(
            session=first, query="爵士乐", limit=10
        ) == []
        assert await runtime.store.get_memory(second_memory.id) is not None
        # 被清空边界隔离的旧消息仍拒绝重新派生记忆。
        with pytest.raises(InputValidationError, match="清空边界"):
            await runtime.memory.remember(
                session=first,
                content="试图从旧聊天恢复",
                kind=MemoryKind.EVENT,
                source_chain=MemorySourceChain.REACTIVE,
                source_message_ids=[old_message.id],
            )
        saved_second = await runtime.store.get_memory(second_memory.id)
        assert saved_second is not None
        assert saved_second.status == MemoryStatus.ACTIVE

        all_cleared = await runtime.memory.clear()
        assert all_cleared["session_count"] == 2
        assert await runtime.store.get_memory(second_memory.id) is None
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_delete_session_removes_session_and_all_associated_data(
    settings,
) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await _private(runtime, "delete-session")
        message, _ = await runtime.store.append_inbound(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="delete-session-message",
                external_chat_id=session.external_chat_id,
                sender_id="user-delete-session",
                sender_name="小明",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component("记录会被一起删除")],
            )
        )
        await runtime.memory.remember(
            session=session,
            content="会被删除的记忆",
            kind=MemoryKind.EVENT,
            subject_id="user-delete-session",
            source_chain=MemorySourceChain.MANUAL,
            source_message_ids=[message.id],
        )
        await runtime.store.save_fact(
            ProfileFact(
                subject_id="user-delete-session",
                scope_key=scope_key_for(session),
                category="偏好",
                content="会被删除的画像",
                confidence=0.9,
                source_message_ids=[message.id],
            )
        )

        result = await runtime.chat.delete_session(session.id)

        assert result["session_id"] == session.id
        assert await runtime.store.get_session(session.id) is None
        assert await runtime.store.list_messages(session.id) == []
        assert await runtime.store.list_decisions(session.id) == []
        assert await runtime.store.list_memories(session_id=session.id) == []
        assert await runtime.store.list_facts(scope_key_for(session)) == []
        # 删除已不存在的会话应明确失败。
        with pytest.raises(NotFoundError):
            await runtime.chat.delete_session(session.id)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_conflicting_profile_facts_leave_memory_recall_until_resolved(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await _private(runtime, "profile-conflict")
        first_message, _ = await runtime.store.append_inbound(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="profile-conflict-1",
                external_chat_id=session.external_chat_id,
                sender_id="user-profile-conflict",
                sender_name="小明",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component("我喜欢浅烘咖啡")],
            )
        )
        first_fact = await runtime.store.save_fact(
            ProfileFact(
                subject_id="user-profile-conflict",
                scope_key=scope_key_for(session),
                category="偏好",
                content="喜欢浅烘咖啡",
                confidence=0.95,
                source_message_ids=[first_message.id],
            )
        )
        memory = await runtime.memory.sync_profile_fact(
            session=session,
            fact=first_fact,
            source_run_id="extract-first",
        )
        assert memory is not None

        second_message, _ = await runtime.store.append_inbound(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="profile-conflict-2",
                external_chat_id=session.external_chat_id,
                sender_id="user-profile-conflict",
                sender_name="小明",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component("我不喜欢浅烘咖啡")],
            )
        )
        second_fact = await runtime.store.save_fact(
            ProfileFact(
                subject_id="user-profile-conflict",
                scope_key=scope_key_for(session),
                category="偏好",
                content="不喜欢浅烘咖啡",
                confidence=0.95,
                source_message_ids=[second_message.id],
            )
        )
        assert second_fact.status == FactStatus.CONFLICTED
        assert (
            await runtime.memory.sync_profile_fact(
                session=session,
                fact=second_fact,
                source_run_id="extract-second",
            )
            is None
        )

        conflicted = await runtime.store.get_memory(memory.id)
        assert conflicted is not None
        assert conflicted.status == MemoryStatus.CONFLICTED
        assert await runtime.memory.retrieve(
            session=session,
            query="浅烘咖啡偏好",
            limit=10,
        ) == []

        corrected = await runtime.memory.correct(
            session=session,
            memory_id=memory.id,
            corrected_content="用户不喜欢浅烘咖啡",
            reason="用户确认新版本",
        )
        assert corrected.status == MemoryStatus.ACTIVE
        assert corrected.supersedes_id == memory.id
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_explicit_memory_tool_finishes_before_background_consolidation(settings) -> None:
    bootstrap_model = FakeModelProvider()
    runtime = build_runtime(settings, model_override=bootstrap_model)
    await runtime.start()
    try:
        session = await _private(runtime, "tool-order")
        original = await runtime.memory.remember(
            session=session,
            content="用户喜欢咖啡",
            kind=MemoryKind.PREFERENCE,
            subject_id="user-tool-order",
            source_chain=MemorySourceChain.MANUAL,
        )
        model = CorrectThenConsolidateModel(original.id)
        runtime.model = model
        runtime.chat.model = model
        runtime.memory.set_model(model)
        await runtime.chat.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="memory-tool-order-message",
                external_chat_id=session.external_chat_id,
                sender_id="user-tool-order",
                sender_name="小明",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component("不是咖啡，我喜欢红茶")],
            )
        )
        await _cancel_debounce(runtime, session.id)
        await runtime.chat.process_session(session.id)
        await runtime.memory.stop()

        assert model.tool_result_seen is True
        assert model.consolidated_before_tool is False
        saved = await runtime.store.get_memory(original.id)
        assert saved is not None
        assert saved.status == MemoryStatus.SUPERSEDED
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_reactive_chat_retrieves_memory_and_archives_user_evidence(settings) -> None:
    model = FakeModelProvider()
    runtime = build_runtime(settings, model_override=model)
    await runtime.start()
    try:
        session = await _private(runtime, "reactive")
        await runtime.memory.remember(
            session=session,
            content="用户准备在周末完成相册整理",
            kind=MemoryKind.COMMITMENT,
            subject_id="user-reactive",
            source_chain=MemorySourceChain.MANUAL,
        )
        await runtime.chat.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="memory-reactive-message",
                external_chat_id=session.external_chat_id,
                sender_id="user-reactive",
                sender_name="小明",
                chat_type=session.chat_type,
                components=[
                    MessageComponent.text_component(
                        "你还记得我周末要做什么吗？我喜欢浅烘咖啡。"
                    )
                ],
            )
        )
        await _cancel_debounce(runtime, session.id)
        await runtime.chat.process_session(session.id)
        await runtime.memory.stop()

        chat_requests = [request for request in model.requests if not request.json_mode]
        assert chat_requests
        system_text = chat_requests[0].messages[0].content or ""
        assert "用户准备在周末完成相册整理" in system_text
        memories = await runtime.store.list_memories(
            session_id=session.id, statuses={MemoryStatus.ACTIVE}
        )
        assert any("浅烘咖啡" in item.content for item in memories)
        runs = await runtime.store.list_memory_consolidation_runs(session.id)
        assert runs[0].status.value == "completed"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_group_silence_still_runs_memory_archival(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.GROUP,
            display_name="记忆群聊",
            external_chat_id="memory-group",
            participants=[Participant(external_user_id="group-user", display_name="小明")],
        )
        await runtime.chat.ingest(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="memory-group-message",
                external_chat_id=session.external_chat_id,
                sender_id="group-user",
                sender_name="小明",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component("我喜欢爵士乐")],
            )
        )
        await _cancel_debounce(runtime, session.id)
        await runtime.chat.process_session(session.id)
        await runtime.memory.stop()

        decisions = await runtime.store.list_decisions(session.id)
        assert decisions[0].action.value == "silence"
        memories = await runtime.store.list_memories(
            session_id=session.id, statuses={MemoryStatus.ACTIVE}
        )
        assert any(item.content == "喜欢爵士乐" for item in memories)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_invalid_memory_batch_does_not_leave_partial_records(settings) -> None:
    runtime = build_runtime(settings, model_override=InvalidMemoryBatchModel())
    await runtime.start()
    try:
        session = await _private(runtime, "invalid-batch")
        source, _ = await runtime.store.append_inbound(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="memory-invalid-batch-message",
                external_chat_id=session.external_chat_id,
                sender_id="user-invalid-batch",
                sender_name="小明",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component("我喜欢民谣")],
            )
        )
        run = await runtime.memory.enqueue_consolidation(
            session=session,
            source_messages=[source],
            source_chain=MemorySourceChain.REACTIVE,
            source_run_id="turn-invalid-batch",
        )
        assert run is not None
        await runtime.memory.stop()

        saved = await runtime.store.get_memory_consolidation_run(run.id)
        assert saved is not None
        assert saved.status.value == "failed"
        assert "当前会话外的主体" in (saved.error_message or "")
        assert await runtime.store.list_memories(session_id=session.id) == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_database_failure_rolls_back_entire_memory_batch(settings, monkeypatch) -> None:
    runtime = build_runtime(settings, model_override=TwoMemoryBatchModel())
    await runtime.start()
    try:
        session = await _private(runtime, "atomic-batch")
        source, _ = await runtime.store.append_inbound(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="memory-atomic-batch-message",
                external_chat_id=session.external_chat_id,
                sender_id="user-atomic-batch",
                sender_name="小明",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component("我喜欢爵士和古典乐")],
            )
        )
        original = DatabaseStore._save_memory_in_transaction
        calls = 0

        async def fail_second_write(db, memory) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("模拟第二条记忆写入故障")
            await original(db, memory)

        monkeypatch.setattr(
            DatabaseStore,
            "_save_memory_in_transaction",
            staticmethod(fail_second_write),
        )
        run = await runtime.memory.enqueue_consolidation(
            session=session,
            source_messages=[source],
            source_chain=MemorySourceChain.REACTIVE,
            source_run_id="turn-atomic-batch",
        )
        assert run is not None
        await runtime.memory.stop()

        saved = await runtime.store.get_memory_consolidation_run(run.id)
        assert saved is not None
        assert saved.status.value == "failed"
        assert await runtime.store.list_memories(session_id=session.id) == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_unauthorized_model_mutations_are_rejected_without_losing_safe_candidate(
    settings, monkeypatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        "application.memory.logger.warning",
        lambda message, *args, **kwargs: warnings.append(message),
    )
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await _private(runtime, "unauthorized-mutations")
        old_preference = await runtime.memory.remember(
            session=session,
            content="用户喜欢红茶",
            kind=MemoryKind.PREFERENCE,
            subject_id="user-unauthorized-mutations",
            source_chain=MemorySourceChain.MANUAL,
        )
        old_event = await runtime.memory.remember(
            session=session,
            content="用户下周准备旅行",
            kind=MemoryKind.EVENT,
            subject_id="user-unauthorized-mutations",
            source_chain=MemorySourceChain.MANUAL,
        )
        model = UnauthorizedMemoryMutationModel(
            supersedes_id=old_preference.id,
            retract_id=old_event.id,
        )
        runtime.memory.set_model(model)
        source, _ = await runtime.store.append_inbound(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="memory-unauthorized-mutations-message",
                external_chat_id=session.external_chat_id,
                sender_id="user-unauthorized-mutations",
                sender_name="小明",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component("我喜欢手冲咖啡")],
            )
        )

        run = await runtime.memory.enqueue_consolidation(
            session=session,
            source_messages=[source],
            source_chain=MemorySourceChain.REACTIVE,
            source_run_id="turn-unauthorized-mutations",
        )
        assert run is not None
        await runtime.memory.stop()

        saved = await runtime.store.get_memory_consolidation_run(run.id)
        assert saved is not None
        assert saved.status.value == "completed"
        persisted_preference = await runtime.store.get_memory(old_preference.id)
        persisted_event = await runtime.store.get_memory(old_event.id)
        assert persisted_preference is not None
        assert persisted_preference.status == MemoryStatus.ACTIVE
        assert persisted_preference.content == old_preference.content
        assert persisted_event is not None
        assert persisted_event.status == MemoryStatus.ACTIVE
        assert persisted_event.content == old_event.content
        active = await runtime.store.list_memories(
            session_id=session.id, statuses={MemoryStatus.ACTIVE}
        )
        new_memory = next(item for item in active if item.content == "用户喜欢手冲咖啡")
        assert new_memory.supersedes_id is None
        assert "已拒绝未经用户授权的自动记忆替代" in warnings
        assert "已拒绝未经用户授权的自动记忆撤回" in warnings
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_rhetorical_question_cannot_authorize_mismatched_memory_replacement(
    settings,
) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await _private(runtime, "rhetorical-question")
        subject_id = "user-rhetorical-question"
        old = await runtime.memory.remember(
            session=session,
            content="用户希望别人称呼自己为小明",
            kind=MemoryKind.PROFILE,
            subject_id=subject_id,
            source_chain=MemorySourceChain.MANUAL,
        )
        runtime.memory.set_model(
            RhetoricalQuestionMutationModel(
                supersedes_id=old.id,
                subject_id=subject_id,
            )
        )
        source, _ = await runtime.store.append_inbound(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="memory-rhetorical-question-message",
                external_chat_id=session.external_chat_id,
                sender_id=subject_id,
                sender_name="小明",
                chat_type=session.chat_type,
                components=[
                    MessageComponent.text_component(
                        "嚯，杭州不是号称美食荒漠吗，我记得那里有道著名的黑暗料理"
                    )
                ],
            )
        )

        run = await runtime.memory.enqueue_consolidation(
            session=session,
            source_messages=[source],
            source_chain=MemorySourceChain.REACTIVE,
            source_run_id="turn-rhetorical-question",
        )
        assert run is not None
        await runtime.memory.stop()

        saved = await runtime.store.get_memory_consolidation_run(run.id)
        unchanged = await runtime.store.get_memory(old.id)
        assert saved is not None
        assert saved.status.value == "completed"
        assert unchanged is not None
        assert unchanged.status == MemoryStatus.ACTIVE
        produced = await runtime.store.get_memory(saved.produced_memory_ids[0])
        assert produced is not None
        assert produced.supersedes_id is None
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_deferred_memory_run_is_persisted_before_execution(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await _private(runtime, "deferred")
        source, _ = await runtime.store.append_inbound(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="memory-deferred-message",
                external_chat_id=session.external_chat_id,
                sender_id="user-deferred",
                sender_name="小明",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component("我喜欢无糖豆浆")],
            )
        )
        run = await runtime.memory.enqueue_consolidation(
            session=session,
            source_messages=[source],
            source_chain=MemorySourceChain.REACTIVE,
            source_run_id="turn-memory-deferred",
            defer_execution=True,
        )
        assert run is not None
        await asyncio.sleep(0)

        pending = await runtime.store.get_memory_consolidation_run(run.id)
        assert pending is not None
        assert pending.status.value == "pending"
        assert pending.attempt_count == 0

        await runtime.memory.start_consolidation(run.id)
        await runtime.memory.stop()
        completed = await runtime.store.get_memory_consolidation_run(run.id)
        assert completed is not None
        assert completed.status.value == "completed"
        assert completed.attempt_count == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_memory_consolidation_is_idempotent_and_failed_run_can_retry(
    settings,
) -> None:
    model = FailFirstMemoryConsolidationModel()
    runtime = build_runtime(settings, model_override=model)
    await runtime.start()
    try:
        session = await _private(runtime, "retry")
        source, created = await runtime.store.append_inbound(
            InboundMessage(
                platform=session.platform,
                account_id=session.account_id,
                external_message_id="memory-retry-message",
                external_chat_id=session.external_chat_id,
                sender_id="user-retry",
                sender_name="小明",
                chat_type=session.chat_type,
                components=[MessageComponent.text_component("我喜欢深烘咖啡")],
            )
        )
        assert created is True
        first = await runtime.memory.enqueue_consolidation(
            session=session,
            source_messages=[source],
            source_chain=MemorySourceChain.REACTIVE,
            source_run_id="turn-memory-retry",
        )
        assert first is not None
        await runtime.memory.stop()
        failed = await runtime.store.get_memory_consolidation_run(first.id)
        assert failed is not None
        assert failed.status.value == "failed"
        assert failed.attempt_count == 1

        duplicate = await runtime.memory.enqueue_consolidation(
            session=session,
            source_messages=[source],
            source_chain=MemorySourceChain.REACTIVE,
            source_run_id="turn-memory-retry",
        )
        assert duplicate is not None
        assert duplicate.id == first.id
        assert len(await runtime.store.list_memory_consolidation_runs(session.id)) == 1

        await runtime.memory.retry_consolidation(first.id)
        await runtime.memory.stop()
        completed = await runtime.store.get_memory_consolidation_run(first.id)
        assert completed is not None
        assert completed.status.value == "completed"
        assert completed.attempt_count == 2
        memories = await runtime.store.list_memories(
            session_id=session.id, statuses={MemoryStatus.ACTIVE}
        )
        assert [item.content for item in memories] == ["喜欢深烘咖啡"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_drift_can_prepare_topic_from_scoped_memory_without_sending(settings) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        session = await _private(runtime, "drift-memory")
        memory = await runtime.memory.remember(
            session=session,
            content="用户最近在准备第一次半程马拉松",
            kind=MemoryKind.EVENT,
            subject_id="user-drift-memory",
            source_chain=MemorySourceChain.MANUAL,
        )
        await runtime.store.save_engagement_policy(
            EngagementPolicy(
                session_id=session.id,
                proactive_enabled=True,
                drift_enabled=True,
            )
        )

        run = await runtime.engagement.run_drift(session.id, force=True)

        assert run.status.value == "completed"
        assert run.activity == "conversation_topic_preparation"
        assert memory.id in run.evidence_refs
        assert await runtime.store.list_messages(session.id) == []
    finally:
        await runtime.stop()
