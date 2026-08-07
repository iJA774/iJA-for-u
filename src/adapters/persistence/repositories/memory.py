"""画像事实、长期记忆、Embedding 与归档 Run 的 SQLite 仓储实现。"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from domain.errors import ConflictError, InputValidationError, NotFoundError
from domain.models import (
    ExtractionStatus,
    FactStatus,
    MemoryConsolidationRun,
    MemoryKind,
    MemoryRecord,
    MemorySourceChain,
    MemoryStatus,
    ProfileExtractionRun,
    ProfileFact,
    new_id,
    utc_now,
)

from ..schema import (
    MemoryConsolidationRunRow,
    MemoryEmbeddingRow,
    MemoryRecordRow,
    MessageRow,
    ProfileExtractionRunRow,
    ProfileFactRow,
    ProfileFactSourceRow,
    SessionRow,
)
from ..search import memory_fts_query as _memory_fts_query
from ._base import RepositoryMixinSupport


class ProfileMemoryRepositoryMixin(RepositoryMixinSupport):
    """实现画像、记忆及其可恢复后台 Run 的同库事务。"""

    async def list_facts(self, scope_key: str | None = None) -> list[ProfileFact]:
        async with self.session_factory() as db:
            query = select(ProfileFactRow).order_by(ProfileFactRow.updated_at.desc())
            if scope_key is not None:
                query = query.where(ProfileFactRow.scope_key == scope_key)
            rows = (await db.execute(query)).scalars().all()
            facts: list[ProfileFact] = []
            for row in rows:
                sources = (
                    (
                        await db.execute(
                            select(ProfileFactSourceRow).where(ProfileFactSourceRow.fact_id == row.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                facts.append(
                    ProfileFact(
                        id=row.id,
                        subject_id=row.subject_id,
                        scope_key=row.scope_key,
                        category=row.category,
                        content=row.content,
                        confidence=row.confidence,
                        status=FactStatus(row.status),
                        source_message_ids=[source.message_id for source in sources],
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                    )
                )
            return facts

    async def save_fact(self, fact: ProfileFact) -> ProfileFact:
        async with self.session_factory() as db:
            _, separator, session_id = fact.scope_key.partition(":")
            if not separator or not session_id:
                raise InputValidationError("画像事实缺少合法会话可见域")
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise NotFoundError("画像事实对应会话不存在")
            if not fact.source_message_ids:
                raise InputValidationError("新增画像事实必须包含来源消息")
            source_query = select(MessageRow).where(
                MessageRow.id.in_(fact.source_message_ids),
                MessageRow.session_id == session_id,
            )
            if session.memory_cleared_at is not None:
                source_query = source_query.where(MessageRow.created_at > session.memory_cleared_at)
            source_rows = (await db.execute(source_query)).scalars().all()
            if len({row.id for row in source_rows}) != len(set(fact.source_message_ids)):
                raise InputValidationError("画像事实来源消息已被记忆清空边界隔离")
            same_category = (
                (
                    await db.execute(
                        select(ProfileFactRow).where(
                            ProfileFactRow.scope_key == fact.scope_key,
                            ProfileFactRow.subject_id == fact.subject_id,
                            ProfileFactRow.category == fact.category,
                            ProfileFactRow.status != FactStatus.RETRACTED.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for existing in same_category:
                if existing.content == fact.content:
                    sources = (
                        (
                            await db.execute(
                                select(ProfileFactSourceRow).where(
                                    ProfileFactSourceRow.fact_id == existing.id
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    known_source_ids = {source.message_id for source in sources}
                    for message_id in fact.source_message_ids:
                        if message_id not in known_source_ids:
                            db.add(
                                ProfileFactSourceRow(
                                    id=new_id("source"), fact_id=existing.id, message_id=message_id
                                )
                            )
                    existing.confidence = max(existing.confidence, fact.confidence)
                    existing.updated_at = utc_now()
                    await db.commit()
                    return ProfileFact(
                        id=existing.id,
                        subject_id=existing.subject_id,
                        scope_key=existing.scope_key,
                        category=existing.category,
                        content=existing.content,
                        confidence=max(existing.confidence, fact.confidence),
                        status=FactStatus(existing.status),
                        source_message_ids=sorted(known_source_ids | set(fact.source_message_ids)),
                        created_at=existing.created_at,
                        updated_at=existing.updated_at,
                    )
                existing.status = FactStatus.CONFLICTED.value
                fact.status = FactStatus.CONFLICTED
            db.add(
                ProfileFactRow(
                    id=fact.id,
                    subject_id=fact.subject_id,
                    scope_key=fact.scope_key,
                    category=fact.category,
                    content=fact.content,
                    confidence=fact.confidence,
                    status=fact.status.value,
                    created_at=fact.created_at,
                    updated_at=fact.updated_at,
                )
            )
            for message_id in fact.source_message_ids:
                db.add(ProfileFactSourceRow(id=new_id("source"), fact_id=fact.id, message_id=message_id))
            await db.commit()
        return fact

    async def commit_profile_extraction(
        self,
        *,
        run: ProfileExtractionRun,
        facts: list[ProfileFact],
    ) -> tuple[list[ProfileFact], list[MemoryRecord]]:
        """在一个事务内提交画像事实、统一记忆投影与 Run 完成态。"""

        async with self._unit_of_work.transaction() as db:
            session = await self._require_current_data_epoch(
                db,
                session_id=run.session_id,
                data_epoch=run.data_epoch,
            )
            run_row = await db.get(ProfileExtractionRunRow, run.id)
            if run_row is None or run_row.data_epoch != run.data_epoch:
                raise ConflictError("画像提取任务已被清空或替换")

            requested_sources = {message_id for fact in facts for message_id in fact.source_message_ids}
            if not requested_sources.issubset(set(run.source_message_ids)):
                raise InputValidationError("画像事实引用了本次任务范围外的消息")
            if requested_sources:
                source_query = select(MessageRow).where(
                    MessageRow.id.in_(requested_sources),
                    MessageRow.session_id == run.session_id,
                )
                if session.memory_cleared_at is not None:
                    source_query = source_query.where(MessageRow.created_at > session.memory_cleared_at)
                source_rows = (await db.execute(source_query)).scalars().all()
                if {row.id for row in source_rows} != requested_sources:
                    raise ConflictError("画像来源消息已被清空或删除")

            saved_facts: list[ProfileFact] = []
            changed_memories: dict[str, MemoryRecord] = {}
            now = utc_now()
            for fact in facts:
                same_category = (
                    (
                        await db.execute(
                            select(ProfileFactRow).where(
                                ProfileFactRow.scope_key == fact.scope_key,
                                ProfileFactRow.subject_id == fact.subject_id,
                                ProfileFactRow.category == fact.category,
                                ProfileFactRow.status != FactStatus.RETRACTED.value,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                fact_row = next(
                    (existing for existing in same_category if existing.content == fact.content),
                    None,
                )
                if fact_row is None:
                    if same_category:
                        for existing in same_category:
                            existing.status = FactStatus.CONFLICTED.value
                        fact.status = FactStatus.CONFLICTED
                    fact_row = ProfileFactRow(
                        id=fact.id,
                        subject_id=fact.subject_id,
                        scope_key=fact.scope_key,
                        category=fact.category,
                        content=fact.content,
                        confidence=fact.confidence,
                        status=fact.status.value,
                        created_at=fact.created_at,
                        updated_at=fact.updated_at,
                    )
                    db.add(fact_row)
                    known_sources: set[str] = set()
                else:
                    known_sources = set(
                        (
                            await db.execute(
                                select(ProfileFactSourceRow.message_id).where(
                                    ProfileFactSourceRow.fact_id == fact_row.id
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    fact_row.confidence = max(fact_row.confidence, fact.confidence)
                    fact_row.updated_at = now
                for message_id in fact.source_message_ids:
                    if message_id in known_sources:
                        continue
                    db.add(
                        ProfileFactSourceRow(
                            id=new_id("source"),
                            fact_id=fact_row.id,
                            message_id=message_id,
                        )
                    )
                    known_sources.add(message_id)
                await db.flush()
                saved_fact = ProfileFact(
                    id=fact_row.id,
                    subject_id=fact_row.subject_id,
                    scope_key=fact_row.scope_key,
                    category=fact_row.category,
                    content=fact_row.content,
                    confidence=fact_row.confidence,
                    status=FactStatus(fact_row.status),
                    source_message_ids=sorted(known_sources),
                    created_at=fact_row.created_at,
                    updated_at=fact_row.updated_at,
                )
                saved_facts.append(saved_fact)

                if saved_fact.status == FactStatus.CONFLICTED:
                    conflicting_ids = {
                        item.id
                        for item in (
                            (
                                await db.execute(
                                    select(ProfileFactRow).where(
                                        ProfileFactRow.scope_key == saved_fact.scope_key,
                                        ProfileFactRow.subject_id == saved_fact.subject_id,
                                        ProfileFactRow.category == saved_fact.category,
                                        ProfileFactRow.status == FactStatus.CONFLICTED.value,
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                    }
                    memory_rows = (
                        (
                            await db.execute(
                                select(MemoryRecordRow).where(
                                    MemoryRecordRow.session_id == run.session_id,
                                    MemoryRecordRow.scope_key == saved_fact.scope_key,
                                    MemoryRecordRow.status.in_(
                                        {
                                            MemoryStatus.ACTIVE.value,
                                            MemoryStatus.CONFLICTED.value,
                                        }
                                    ),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for memory_row in memory_rows:
                        refs = set(json.loads(memory_row.source_refs_json))
                        if not refs.intersection(conflicting_ids):
                            continue
                        refs.add(f"profile_conflict:{saved_fact.category}")
                        memory_row.status = MemoryStatus.CONFLICTED.value
                        memory_row.source_refs_json = json.dumps(sorted(refs), ensure_ascii=False)
                        memory_row.updated_at = now
                        changed_memories[memory_row.id] = self._memory_from_row(memory_row)
                    continue

                clean_content = " ".join(saved_fact.content.strip().split())
                content_hash = hashlib.sha256(clean_content.encode("utf-8")).hexdigest()
                kind = (
                    MemoryKind.PREFERENCE
                    if "偏好" in saved_fact.category or "preference" in saved_fact.category.casefold()
                    else MemoryKind.PROFILE
                )
                memory_row = (
                    await db.execute(
                        select(MemoryRecordRow).where(
                            MemoryRecordRow.scope_key == saved_fact.scope_key,
                            MemoryRecordRow.kind == kind.value,
                            MemoryRecordRow.subject_id == saved_fact.subject_id,
                            MemoryRecordRow.content_hash == content_hash,
                            MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
                        )
                    )
                ).scalar_one_or_none()
                if memory_row is None:
                    memory = MemoryRecord(
                        session_id=run.session_id,
                        scope_key=saved_fact.scope_key,
                        subject_id=saved_fact.subject_id,
                        kind=kind,
                        content=clean_content,
                        content_hash=content_hash,
                        confidence=saved_fact.confidence,
                        importance=0.7,
                        source_chain=MemorySourceChain.REACTIVE,
                        source_run_id=run.id,
                        source_message_ids=saved_fact.source_message_ids,
                        source_refs=[saved_fact.id],
                    )
                    memory_row = self._memory_to_row(memory)
                    db.add(memory_row)
                else:
                    old_message_ids = set(json.loads(memory_row.source_message_ids_json))
                    old_refs = set(json.loads(memory_row.source_refs_json))
                    new_message_ids = old_message_ids | set(saved_fact.source_message_ids)
                    new_refs = old_refs | {saved_fact.id}
                    if (
                        new_message_ids != old_message_ids
                        or new_refs != old_refs
                        or memory_row.source_run_id != run.id
                    ):
                        memory_row.reinforcement += 1
                    memory_row.confidence = max(memory_row.confidence, saved_fact.confidence)
                    memory_row.importance = max(memory_row.importance, 0.7)
                    memory_row.source_run_id = run.id
                    memory_row.source_message_ids_json = json.dumps(
                        sorted(new_message_ids), ensure_ascii=False
                    )
                    memory_row.source_refs_json = json.dumps(sorted(new_refs), ensure_ascii=False)
                    memory_row.updated_at = now
                await db.flush()
                changed_memories[memory_row.id] = self._memory_from_row(memory_row)

            run_row.status = ExtractionStatus.COMPLETED.value
            run_row.error_code = None
            run_row.error_message = None
            run_row.attempt_count = run.attempt_count
            run_row.updated_at = now
            return saved_facts, list(changed_memories.values())

    async def save_extraction_run(self, run: ProfileExtractionRun) -> bool:
        """创建 pending Run 或更新当前 epoch Run，永不复活已删除任务。"""

        async with self.session_factory() as db:
            row = await db.get(ProfileExtractionRunRow, run.id)
            if row is None:
                if run.status != ExtractionStatus.PENDING:
                    return False
                await self._require_current_data_epoch(
                    db,
                    session_id=run.session_id,
                    data_epoch=run.data_epoch,
                )
                row = ProfileExtractionRunRow(
                    id=run.id,
                    session_id=run.session_id,
                    subject_id=run.subject_id,
                    scope_key=run.scope_key,
                    source_message_ids_json=json.dumps(run.source_message_ids),
                    data_epoch=run.data_epoch,
                    status=run.status.value,
                    error_code=run.error_code,
                    error_message=run.error_message,
                    attempt_count=run.attempt_count,
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                )
                db.add(row)
            else:
                if row.data_epoch != run.data_epoch:
                    return False
                session = await db.get(SessionRow, run.session_id)
                if session is None or session.data_epoch != run.data_epoch:
                    return False
                row.status = run.status.value
                row.error_code = run.error_code
                row.error_message = run.error_message
                row.attempt_count = run.attempt_count
                row.updated_at = run.updated_at
            await db.commit()
            return True

    async def get_extraction_run(self, run_id: str) -> ProfileExtractionRun | None:
        async with self.session_factory() as db:
            row = await db.get(ProfileExtractionRunRow, run_id)
            return self._run_from_row(row) if row else None

    async def list_extraction_runs(self) -> list[ProfileExtractionRun]:
        async with self.session_factory() as db:
            query = select(ProfileExtractionRunRow).order_by(ProfileExtractionRunRow.updated_at.desc())
            return [self._run_from_row(row) for row in (await db.execute(query)).scalars().all()]

    async def list_recoverable_runs(self) -> list[ProfileExtractionRun]:
        async with self.session_factory() as db:
            query = select(ProfileExtractionRunRow).where(
                ProfileExtractionRunRow.status.in_(
                    [ExtractionStatus.PENDING.value, ExtractionStatus.RUNNING.value]
                )
            )
            return [self._run_from_row(row) for row in (await db.execute(query)).scalars().all()]

    async def get_memory(self, memory_id: str) -> MemoryRecord | None:
        """按内部 ID 读取长期记忆。"""

        async with self.session_factory() as db:
            row = await db.get(MemoryRecordRow, memory_id)
            return self._memory_from_row(row) if row is not None else None

    async def find_active_memory(
        self,
        *,
        scope_key: str,
        kind: MemoryKind,
        subject_id: str | None,
        content_hash: str,
    ) -> MemoryRecord | None:
        """读取同域、同类别、同主体的活跃等价记忆。"""

        async with self.session_factory() as db:
            row = (
                (
                    await db.execute(
                        select(MemoryRecordRow).where(
                            MemoryRecordRow.scope_key == scope_key,
                            MemoryRecordRow.kind == kind.value,
                            MemoryRecordRow.subject_id == subject_id,
                            MemoryRecordRow.content_hash == content_hash,
                            MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
                        )
                    )
                )
                .scalars()
                .first()
            )
            return self._memory_from_row(row) if row is not None else None

    async def list_memories(
        self,
        *,
        scope_key: str | None = None,
        session_id: str | None = None,
        statuses: set[MemoryStatus] | None = None,
        kinds: set[MemoryKind] | None = None,
        limit: int = 500,
    ) -> list[MemoryRecord]:
        """按严格可见域列出记忆；调用方不得用空 scope 做聊天注入。"""

        async with self.session_factory() as db:
            query = select(MemoryRecordRow)
            if scope_key is not None:
                query = query.where(MemoryRecordRow.scope_key == scope_key)
            if session_id is not None:
                query = query.where(MemoryRecordRow.session_id == session_id)
            if statuses is not None:
                query = query.where(
                    MemoryRecordRow.status.in_([item.value for item in statuses])
                )
            if kinds is not None:
                query = query.where(
                    MemoryRecordRow.kind.in_([item.value for item in kinds])
                )
            query = query.order_by(
                MemoryRecordRow.importance.desc(),
                MemoryRecordRow.updated_at.desc(),
            ).limit(limit)
            rows = (await db.execute(query)).scalars().all()
            return [self._memory_from_row(row) for row in rows]

    async def search_active_memory_candidates(
        self,
        *,
        scope_key: str,
        query_text: str,
        limit: int = 1000,
    ) -> list[MemoryRecord] | None:
        """按严格 scope 预筛活跃记忆；返回 None 表示调用方必须完整降级。"""

        if limit < 1 or limit > 5000:
            raise InputValidationError("记忆检索候选条数必须在 1 到 5000 之间")
        fts_query = _memory_fts_query(query_text)
        if not self._memory_fts_available or fts_query is None:
            return None
        async with self.session_factory() as db:
            memory_ids = list(
                (
                    await db.execute(
                        text(
                            """
                            SELECT memory_id
                            FROM memory_search_fts
                            WHERE memory_search_fts MATCH :fts_query
                              AND scope_key = :scope_key
                              AND status = :status
                            ORDER BY bm25(memory_search_fts), memory_id
                            LIMIT :limit
                            """
                        ),
                        {
                            "fts_query": fts_query,
                            "scope_key": scope_key,
                            "status": MemoryStatus.ACTIVE.value,
                            "limit": limit,
                        },
                    )
                )
                .scalars()
                .all()
            )
            if not memory_ids:
                return []
            rows = (
                await db.execute(
                    select(MemoryRecordRow).where(
                        MemoryRecordRow.id.in_(memory_ids),
                        MemoryRecordRow.scope_key == scope_key,
                        MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
                    )
                )
            ).scalars()
            by_id = {row.id: row for row in rows}
            return [self._memory_from_row(by_id[memory_id]) for memory_id in memory_ids if memory_id in by_id]

    async def page_memories(
        self,
        *,
        scope_key: str | None = None,
        statuses: set[MemoryStatus] | None = None,
        kinds: set[MemoryKind] | None = None,
        after_id: str | None = None,
        limit: int = 200,
    ) -> list[MemoryRecord]:
        """按稳定主键游标分页读取记忆，供完整检索和后台派生索引使用。"""

        if limit < 1 or limit > 1000:
            raise InputValidationError("记忆分页条数必须在 1 到 1000 之间")
        async with self.session_factory() as db:
            query = select(MemoryRecordRow)
            if scope_key is not None:
                query = query.where(MemoryRecordRow.scope_key == scope_key)
            if statuses is not None:
                query = query.where(
                    MemoryRecordRow.status.in_([item.value for item in statuses])
                )
            if kinds is not None:
                query = query.where(
                    MemoryRecordRow.kind.in_([item.value for item in kinds])
                )
            if after_id is not None:
                query = query.where(MemoryRecordRow.id > after_id)
            rows = (
                (
                    await db.execute(
                        query.order_by(MemoryRecordRow.id.asc()).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [self._memory_from_row(row) for row in rows]

    async def page_memories_missing_embedding(
        self,
        *,
        model_name: str,
        after_id: str | None = None,
        limit: int = 200,
    ) -> list[MemoryRecord]:
        """分页读取缺少当前模型最新向量的活跃记忆。"""

        if limit < 1 or limit > 1000:
            raise InputValidationError("向量索引分页条数必须在 1 到 1000 之间")
        async with self.session_factory() as db:
            query = (
                select(MemoryRecordRow)
                .outerjoin(
                    MemoryEmbeddingRow,
                    MemoryEmbeddingRow.memory_id == MemoryRecordRow.id,
                )
                .where(
                    MemoryRecordRow.status == MemoryStatus.ACTIVE.value,
                    or_(
                        MemoryEmbeddingRow.memory_id.is_(None),
                        MemoryEmbeddingRow.model_name != model_name,
                        MemoryEmbeddingRow.content_hash != MemoryRecordRow.content_hash,
                    ),
                )
            )
            if after_id is not None:
                query = query.where(MemoryRecordRow.id > after_id)
            rows = (
                (
                    await db.execute(
                        query.order_by(MemoryRecordRow.id.asc()).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [self._memory_from_row(row) for row in rows]

    async def save_memory(self, memory: MemoryRecord) -> MemoryRecord:
        """新增或更新一条记忆；状态替换由应用层显式决定。"""

        async with self.session_factory() as db:
            await self._save_memory_in_transaction(db, memory)
            await db.commit()
            return memory

    async def save_memories_batch(self, memories: list[MemoryRecord]) -> list[MemoryRecord]:
        """在同一事务中提交一组已验证的记忆状态转换。"""

        unique = {memory.id: memory for memory in memories}
        if not unique:
            return []
        async with self.session_factory() as db:
            for memory in unique.values():
                await self._save_memory_in_transaction(db, memory)
            await db.commit()
        return list(unique.values())

    async def save_memories_batch_if_epoch(
        self,
        memories: list[MemoryRecord],
        *,
        session_id: str,
        data_epoch: int,
    ) -> list[MemoryRecord]:
        """仅在 Session 数据版本未变化时提交整组记忆状态转换。"""

        unique = {memory.id: memory for memory in memories}
        if not unique:
            return []
        async with self.session_factory() as db:
            await self._require_current_data_epoch(
                db,
                session_id=session_id,
                data_epoch=data_epoch,
            )
            for memory in unique.values():
                if memory.session_id != session_id:
                    raise InputValidationError("记忆批次跨越 Session")
                await self._save_memory_in_transaction(db, memory)
            await db.commit()
        return list(unique.values())

    @staticmethod
    async def _save_memory_in_transaction(
        db: AsyncSession, memory: MemoryRecord
    ) -> None:
        """在调用方事务中 upsert 一条记忆。"""

        row = await db.get(MemoryRecordRow, memory.id)
        values = RepositoryMixinSupport._memory_to_row(memory)
        if row is None:
            db.add(values)
            return
        for column in (
            "session_id",
            "scope_key",
            "subject_id",
            "kind",
            "content",
            "content_hash",
            "confidence",
            "importance",
            "status",
            "source_chain",
            "source_run_id",
            "source_message_ids_json",
            "source_refs_json",
            "supersedes_id",
            "happened_at",
            "reinforcement",
            "recall_count",
            "last_recalled_at",
            "updated_at",
        ):
            setattr(row, column, getattr(values, column))

    async def record_memory_recall(self, memory_ids: list[str]) -> None:
        """只为最终注入 Prompt 的记忆累计召回统计。"""

        if not memory_ids:
            return
        now = utc_now()
        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(MemoryRecordRow).where(MemoryRecordRow.id.in_(memory_ids))
                )
            ).scalars().all()
            for row in rows:
                row.recall_count += 1
                row.last_recalled_at = now
            await db.commit()

    async def list_memory_embeddings(
        self, memory_ids: list[str], *, model_name: str
    ) -> dict[str, list[float]]:
        """读取当前 embedding 模型且正文未变化的派生向量。"""

        if not memory_ids:
            return {}
        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(MemoryEmbeddingRow)
                    .join(
                        MemoryRecordRow,
                        MemoryEmbeddingRow.memory_id == MemoryRecordRow.id,
                    )
                    .where(
                        MemoryEmbeddingRow.memory_id.in_(memory_ids),
                        MemoryEmbeddingRow.model_name == model_name,
                        MemoryEmbeddingRow.content_hash == MemoryRecordRow.content_hash,
                    )
                )
            ).scalars().all()
            return {
                row.memory_id: [float(value) for value in json.loads(row.vector_json)]
                for row in rows
            }

    async def save_memory_embeddings(
        self, *, model_name: str, entries: list[tuple[MemoryRecord, list[float]]]
    ) -> None:
        """批量覆盖派生向量；向量不属于权威记忆事实。"""

        if not entries:
            return
        now = utc_now()
        async with self.session_factory() as db:
            for memory, vector in entries:
                if not vector:
                    raise InputValidationError("记忆向量不能为空")
                row = await db.get(MemoryEmbeddingRow, memory.id)
                if row is None:
                    db.add(
                        MemoryEmbeddingRow(
                            memory_id=memory.id,
                            model_name=model_name,
                            content_hash=memory.content_hash,
                            dimension=len(vector),
                            vector_json=json.dumps(vector, separators=(",", ":")),
                            updated_at=now,
                        )
                    )
                else:
                    row.model_name = model_name
                    row.content_hash = memory.content_hash
                    row.dimension = len(vector)
                    row.vector_json = json.dumps(vector, separators=(",", ":"))
                    row.updated_at = now
            await db.commit()

    async def create_memory_consolidation_run(
        self, run: MemoryConsolidationRun
    ) -> tuple[MemoryConsolidationRun, bool]:
        """按来源指纹创建归档任务，保证重启与重复事件幂等。"""

        async with self.session_factory() as db:
            await self._require_current_data_epoch(
                db,
                session_id=run.session_id,
                data_epoch=run.data_epoch,
            )
            existing = (
                await db.execute(
                    select(MemoryConsolidationRunRow).where(
                        MemoryConsolidationRunRow.source_fingerprint == run.source_fingerprint
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return self._memory_run_from_row(existing), False
            db.add(self._memory_run_to_row(run))
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                existing = (
                    await db.execute(
                        select(MemoryConsolidationRunRow).where(
                            MemoryConsolidationRunRow.source_fingerprint == run.source_fingerprint
                        )
                    )
                ).scalar_one()
                return self._memory_run_from_row(existing), False
            return run, True

    async def save_memory_consolidation_run(
        self, run: MemoryConsolidationRun
    ) -> MemoryConsolidationRun | None:
        """更新当前 epoch 的既有 Run；删除后的 finalizer 不得重新插入。"""

        async with self.session_factory() as db:
            row = await db.get(MemoryConsolidationRunRow, run.id)
            if row is None:
                return None
            if row.data_epoch != run.data_epoch:
                return None
            session = await db.get(SessionRow, run.session_id)
            if session is None or session.data_epoch != run.data_epoch:
                return None
            values = self._memory_run_to_row(run)
            for column in (
                "status",
                "produced_memory_ids_json",
                "error_code",
                "error_message",
                "attempt_count",
                "updated_at",
            ):
                setattr(row, column, getattr(values, column))
            await db.commit()
            return run

    async def get_memory_consolidation_run(
        self, run_id: str
    ) -> MemoryConsolidationRun | None:
        async with self.session_factory() as db:
            row = await db.get(MemoryConsolidationRunRow, run_id)
            return self._memory_run_from_row(row) if row is not None else None

    async def list_memory_consolidation_runs(
        self, session_id: str | None = None, *, limit: int = 500
    ) -> list[MemoryConsolidationRun]:
        async with self.session_factory() as db:
            query = select(MemoryConsolidationRunRow)
            if session_id is not None:
                query = query.where(
                    MemoryConsolidationRunRow.session_id == session_id
                )
            query = query.order_by(
                MemoryConsolidationRunRow.updated_at.desc()
            ).limit(limit)
            rows = (await db.execute(query)).scalars().all()
            return [self._memory_run_from_row(row) for row in rows]

    async def list_recoverable_memory_runs(self) -> list[MemoryConsolidationRun]:
        async with self.session_factory() as db:
            query = select(MemoryConsolidationRunRow).where(
                MemoryConsolidationRunRow.status.in_(
                    [ExtractionStatus.PENDING.value, ExtractionStatus.RUNNING.value]
                )
            )
            rows = (await db.execute(query)).scalars().all()
            return [self._memory_run_from_row(row) for row in rows]
