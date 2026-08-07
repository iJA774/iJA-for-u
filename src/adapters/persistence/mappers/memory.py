"""画像与长期记忆 Row 的纯映射。"""

from __future__ import annotations

import json

from domain.models import (
    ExtractionStatus,
    MemoryConsolidationRun,
    MemoryKind,
    MemoryRecord,
    MemorySourceChain,
    MemoryStatus,
    ProfileExtractionRun,
)

from ..schema import (
    MemoryConsolidationRunRow,
    MemoryRecordRow,
    ProfileExtractionRunRow,
)


def run_from_row(row: ProfileExtractionRunRow) -> ProfileExtractionRun:
    return ProfileExtractionRun(
        id=row.id,
        session_id=row.session_id,
        subject_id=row.subject_id,
        scope_key=row.scope_key,
        source_message_ids=json.loads(row.source_message_ids_json),
        data_epoch=row.data_epoch,
        status=ExtractionStatus(row.status),
        error_code=row.error_code,
        error_message=row.error_message,
        attempt_count=row.attempt_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def memory_to_row(memory: MemoryRecord) -> MemoryRecordRow:
    return MemoryRecordRow(
        id=memory.id,
        session_id=memory.session_id,
        scope_key=memory.scope_key,
        subject_id=memory.subject_id,
        kind=memory.kind.value,
        content=memory.content,
        content_hash=memory.content_hash,
        confidence=memory.confidence,
        importance=memory.importance,
        status=memory.status.value,
        source_chain=memory.source_chain.value,
        source_run_id=memory.source_run_id,
        source_message_ids_json=json.dumps(memory.source_message_ids, ensure_ascii=False),
        source_refs_json=json.dumps(memory.source_refs, ensure_ascii=False),
        supersedes_id=memory.supersedes_id,
        happened_at=memory.happened_at,
        reinforcement=memory.reinforcement,
        recall_count=memory.recall_count,
        last_recalled_at=memory.last_recalled_at,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


def memory_from_row(row: MemoryRecordRow) -> MemoryRecord:
    return MemoryRecord(
        id=row.id,
        session_id=row.session_id,
        scope_key=row.scope_key,
        subject_id=row.subject_id,
        kind=MemoryKind(row.kind),
        content=row.content,
        content_hash=row.content_hash,
        confidence=row.confidence,
        importance=row.importance,
        status=MemoryStatus(row.status),
        source_chain=MemorySourceChain(row.source_chain),
        source_run_id=row.source_run_id,
        source_message_ids=json.loads(row.source_message_ids_json),
        source_refs=json.loads(row.source_refs_json),
        supersedes_id=row.supersedes_id,
        happened_at=row.happened_at,
        reinforcement=row.reinforcement,
        recall_count=row.recall_count,
        last_recalled_at=row.last_recalled_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def memory_run_to_row(
    run: MemoryConsolidationRun,
) -> MemoryConsolidationRunRow:
    return MemoryConsolidationRunRow(
        id=run.id,
        session_id=run.session_id,
        scope_key=run.scope_key,
        source_chain=run.source_chain.value,
        source_run_id=run.source_run_id,
        source_message_ids_json=json.dumps(run.source_message_ids, ensure_ascii=False),
        source_fingerprint=run.source_fingerprint,
        data_epoch=run.data_epoch,
        status=run.status.value,
        produced_memory_ids_json=json.dumps(run.produced_memory_ids, ensure_ascii=False),
        error_code=run.error_code,
        error_message=run.error_message,
        attempt_count=run.attempt_count,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def memory_run_from_row(
    row: MemoryConsolidationRunRow,
) -> MemoryConsolidationRun:
    return MemoryConsolidationRun(
        id=row.id,
        session_id=row.session_id,
        scope_key=row.scope_key,
        source_chain=MemorySourceChain(row.source_chain),
        source_run_id=row.source_run_id,
        source_message_ids=json.loads(row.source_message_ids_json),
        source_fingerprint=row.source_fingerprint,
        data_epoch=row.data_epoch,
        status=ExtractionStatus(row.status),
        produced_memory_ids=json.loads(row.produced_memory_ids_json),
        error_code=row.error_code,
        error_message=row.error_message,
        attempt_count=row.attempt_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
