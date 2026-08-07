"""调度、素材、观测与主动行为 Row 的纯映射。"""

from __future__ import annotations

import json

from domain.models import (
    BlacklistEntry,
    BlacklistSource,
    CandidateSourceKind,
    DriftRun,
    DriftRunStatus,
    DriftStage,
    EngagementPolicy,
    ExpressionAsset,
    ExpressionSourceKind,
    FeedSource,
    GroupParticipationMode,
    GroupParticipationPolicy,
    ImageAnalysis,
    ModelAttempt,
    ProactiveCandidate,
    ProactiveCandidateStatus,
    ProactiveRun,
    ProactiveRunStatus,
    ProactiveStage,
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleStatus,
    ScheduleTask,
    ToolExecution,
    ToolExecutionStatus,
)

from ..schema import (
    BlacklistEntryRow,
    DriftRunRow,
    EngagementPolicyRow,
    ExpressionAssetRow,
    FeedSourceRow,
    GroupParticipationPolicyRow,
    ImageAnalysisRow,
    ModelAttemptRow,
    ProactiveCandidateRow,
    ProactiveRunRow,
    ScheduleRow,
    ScheduleRunRow,
    ToolExecutionRow,
)


def blacklist_from_row(row: BlacklistEntryRow) -> BlacklistEntry:
    return BlacklistEntry(
        id=row.id,
        platform=row.platform,
        account_id=row.account_id,
        external_user_id=row.external_user_id,
        display_name=row.display_name,
        reason=row.reason,
        source=BlacklistSource(row.source),
        session_id=row.session_id,
        created_at=row.created_at,
    )


def blacklist_to_row(entry: BlacklistEntry) -> BlacklistEntryRow:
    return BlacklistEntryRow(
        id=entry.id,
        platform=entry.platform,
        account_id=entry.account_id,
        external_user_id=entry.external_user_id,
        display_name=entry.display_name,
        reason=entry.reason,
        source=entry.source.value,
        session_id=entry.session_id,
        created_at=entry.created_at,
    )


def schedule_to_row(schedule: ScheduleTask) -> ScheduleRow:
    return ScheduleRow(
        id=schedule.id,
        session_id=schedule.session_id,
        created_by=schedule.created_by,
        title=schedule.title,
        instruction=schedule.instruction,
        source_text=schedule.source_text,
        timezone=schedule.timezone,
        dtstart=schedule.dtstart,
        rrule=schedule.rrule,
        status=schedule.status.value,
        revision=schedule.revision,
        next_run_at=schedule.next_run_at,
        last_run_at=schedule.last_run_at,
        consecutive_failures=schedule.consecutive_failures,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


def schedule_from_row(row: ScheduleRow) -> ScheduleTask:
    return ScheduleTask(
        id=row.id,
        session_id=row.session_id,
        created_by=row.created_by,
        title=row.title,
        instruction=row.instruction,
        source_text=row.source_text,
        timezone=row.timezone,
        dtstart=row.dtstart,
        rrule=row.rrule,
        status=ScheduleStatus(row.status),
        revision=row.revision,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        consecutive_failures=row.consecutive_failures,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def schedule_run_to_row(run: ScheduleRun) -> ScheduleRunRow:
    return ScheduleRunRow(
        id=run.id,
        schedule_id=run.schedule_id,
        session_id=run.session_id,
        scheduled_for=run.scheduled_for,
        status=run.status.value,
        missed_occurrences=run.missed_occurrences,
        outbound_id=run.outbound_id,
        error_code=run.error_code,
        error_message=run.error_message,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def schedule_run_from_row(row: ScheduleRunRow) -> ScheduleRun:
    return ScheduleRun(
        id=row.id,
        schedule_id=row.schedule_id,
        session_id=row.session_id,
        scheduled_for=row.scheduled_for,
        status=ScheduleRunStatus(row.status),
        missed_occurrences=row.missed_occurrences,
        outbound_id=row.outbound_id,
        error_code=row.error_code,
        error_message=row.error_message,
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def tool_execution_from_row(row: ToolExecutionRow) -> ToolExecution:
    return ToolExecution(
        id=row.id,
        session_id=row.session_id,
        tool_call_id=row.tool_call_id,
        tool_name=row.tool_name,
        arguments=json.loads(row.arguments_json),
        result=json.loads(row.result_json) if row.result_json else None,
        status=ToolExecutionStatus(row.status),
        turn_id=row.turn_id,
        schedule_run_id=row.schedule_run_id,
        error_code=row.error_code,
        error_message=row.error_message,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def model_attempt_from_row(row: ModelAttemptRow) -> ModelAttempt:
    return ModelAttempt(
        id=row.id,
        invocation_id=row.invocation_id,
        attempt_number=row.attempt_number,
        task=row.task,
        provider=row.provider,
        profile=row.profile,
        model=row.model,
        session_id=row.session_id,
        turn_id=row.turn_id,
        run_id=row.run_id,
        streamed=row.streamed,
        tool_call_count=row.tool_call_count,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        total_tokens=row.total_tokens,
        usage_source=row.usage_source,
        latency_ms=row.latency_ms,
        success=row.success,
        error_type=row.error_type,
        error_code=row.error_code,
        cost_microusd=row.cost_microusd,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def expression_from_row(row: ExpressionAssetRow) -> ExpressionAsset:
    return ExpressionAsset(
        id=row.id,
        character_id=row.character_id,
        name=row.name,
        normalized_name=row.normalized_name,
        emotion=row.emotion,
        description=row.description,
        source_kind=ExpressionSourceKind(row.source_kind),
        generation_key=row.generation_key,
        source_portrait_sha256=row.source_portrait_sha256,
        storage_path=row.storage_path,
        mime_type=row.mime_type,
        size=row.size,
        sha256=row.sha256,
        width=row.width,
        height=row.height,
        use_count=row.use_count,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


def image_analysis_from_row(row: ImageAnalysisRow) -> ImageAnalysis:
    return ImageAnalysis(
        sha256=row.sha256,
        mime_type=row.mime_type,
        provider_key=row.provider_key,
        prompt_version=row.prompt_version,
        description=row.description,
        emotions=json.loads(row.emotions_json),
        expression_name=row.expression_name,
        is_expression=row.is_expression,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def policy_from_row(row: EngagementPolicyRow) -> EngagementPolicy:
    return EngagementPolicy(
        session_id=row.session_id,
        proactive_enabled=row.proactive_enabled,
        drift_enabled=row.drift_enabled,
        timezone=row.timezone,
        quiet_start=row.quiet_start,
        quiet_end=row.quiet_end,
        minimum_interval_minutes=row.minimum_interval_minutes,
        updated_at=row.updated_at,
    )


def group_participation_policy_from_row(
    row: GroupParticipationPolicyRow,
) -> GroupParticipationPolicy:
    return GroupParticipationPolicy(
        session_id=row.session_id,
        mode=GroupParticipationMode(row.mode),
        trigger_count=row.trigger_count,
        frequency_factor=row.frequency_factor,
        cooldown_seconds=row.cooldown_seconds,
        idle_streak=row.idle_streak,
        idle_backoff_until=row.idle_backoff_until,
        last_ordinary_reply_at=row.last_ordinary_reply_at,
        last_external_message_at=row.last_external_message_at,
        external_interval_ewma_seconds=row.external_interval_ewma_seconds,
        external_interval_sample_count=row.external_interval_sample_count,
        revision=row.revision,
        state_version=row.state_version,
        updated_at=row.updated_at,
    )


def feed_to_row(source: FeedSource) -> FeedSourceRow:
    return FeedSourceRow(**source.model_dump())


def feed_from_row(row: FeedSourceRow) -> FeedSource:
    return FeedSource(
        id=row.id,
        session_id=row.session_id,
        url=row.url,
        title=row.title,
        enabled=row.enabled,
        poll_interval_minutes=row.poll_interval_minutes,
        etag=row.etag,
        last_modified=row.last_modified,
        consecutive_failures=row.consecutive_failures,
        next_poll_at=row.next_poll_at,
        last_polled_at=row.last_polled_at,
        error_code=row.error_code,
        error_message=row.error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def candidate_to_row(candidate: ProactiveCandidate) -> ProactiveCandidateRow:
    return ProactiveCandidateRow(
        id=candidate.id,
        session_id=candidate.session_id,
        source_kind=candidate.source_kind.value,
        source_id=candidate.source_id,
        source_key=candidate.source_key,
        title=candidate.title,
        summary=candidate.summary,
        url=candidate.url,
        published_at=candidate.published_at,
        source_refs_json=json.dumps(candidate.source_refs, ensure_ascii=False),
        parent_candidate_ids_json=json.dumps(candidate.parent_candidate_ids, ensure_ascii=False),
        aggregation_key=candidate.aggregation_key,
        status=candidate.status.value,
        decision_reason=candidate.decision_reason,
        attempt_count=candidate.attempt_count,
        available_at=candidate.available_at,
        next_attempt_at=candidate.next_attempt_at,
        expires_at=candidate.expires_at,
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
    )


def candidate_from_row(row: ProactiveCandidateRow) -> ProactiveCandidate:
    return ProactiveCandidate(
        id=row.id,
        session_id=row.session_id,
        source_kind=CandidateSourceKind(row.source_kind),
        source_id=row.source_id,
        source_key=row.source_key,
        title=row.title,
        summary=row.summary,
        url=row.url,
        published_at=row.published_at,
        source_refs=json.loads(row.source_refs_json),
        parent_candidate_ids=json.loads(row.parent_candidate_ids_json),
        aggregation_key=row.aggregation_key,
        status=ProactiveCandidateStatus(row.status),
        decision_reason=row.decision_reason,
        attempt_count=row.attempt_count,
        available_at=row.available_at,
        next_attempt_at=row.next_attempt_at,
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def update_proactive_candidate_row(
    row: ProactiveCandidateRow,
    values: ProactiveCandidateRow,
) -> None:
    """只更新候选可变字段，保留来源身份和创建时间。"""

    for column in (
        "status",
        "decision_reason",
        "attempt_count",
        "available_at",
        "next_attempt_at",
        "expires_at",
        "updated_at",
        "source_refs_json",
        "parent_candidate_ids_json",
        "aggregation_key",
    ):
        setattr(row, column, getattr(values, column))


def proactive_run_to_row(run: ProactiveRun) -> ProactiveRunRow:
    return ProactiveRunRow(
        id=run.id,
        session_id=run.session_id,
        status=run.status.value,
        stage=run.stage.value,
        gate_reason=run.gate_reason,
        decision_code=run.decision_code,
        decision_reason=run.decision_reason,
        score=run.score,
        candidate_ids_json=json.dumps(run.candidate_ids, ensure_ascii=False),
        candidate_id=run.candidate_id,
        snapshot_at=run.snapshot_at,
        snapshot_message_id=run.snapshot_message_id,
        manual_triggered=run.manual_triggered,
        outbound_id=run.outbound_id,
        error_code=run.error_code,
        error_message=run.error_message,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def update_proactive_run_row(
    row: ProactiveRunRow,
    values: ProactiveRunRow,
) -> None:
    """更新主动运行的权威状态机字段。"""

    for column in (
        "status",
        "stage",
        "gate_reason",
        "decision_code",
        "decision_reason",
        "score",
        "candidate_ids_json",
        "candidate_id",
        "snapshot_at",
        "snapshot_message_id",
        "manual_triggered",
        "outbound_id",
        "error_code",
        "error_message",
        "updated_at",
    ):
        setattr(row, column, getattr(values, column))


def proactive_run_from_row(row: ProactiveRunRow) -> ProactiveRun:
    return ProactiveRun(
        id=row.id,
        session_id=row.session_id,
        status=ProactiveRunStatus(row.status),
        stage=ProactiveStage(row.stage),
        gate_reason=row.gate_reason,
        decision_code=row.decision_code,
        decision_reason=row.decision_reason,
        score=row.score,
        candidate_ids=json.loads(row.candidate_ids_json),
        candidate_id=row.candidate_id,
        snapshot_at=row.snapshot_at,
        snapshot_message_id=row.snapshot_message_id,
        manual_triggered=row.manual_triggered,
        outbound_id=row.outbound_id,
        error_code=row.error_code,
        error_message=row.error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def drift_run_to_row(run: DriftRun) -> DriftRunRow:
    return DriftRunRow(
        id=run.id,
        session_id=run.session_id,
        status=run.status.value,
        stage=run.stage.value,
        activity=run.activity,
        decision_reason=run.decision_reason,
        resume_payload_json=json.dumps(run.resume_payload, ensure_ascii=False),
        evidence_refs_json=json.dumps(run.evidence_refs, ensure_ascii=False),
        produced_candidate_id=run.produced_candidate_id,
        snapshot_at=run.snapshot_at,
        snapshot_message_id=run.snapshot_message_id,
        resumed_from_run_id=run.resumed_from_run_id,
        auto_resume_count=run.auto_resume_count,
        error_code=run.error_code,
        error_message=run.error_message,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def drift_run_from_row(row: DriftRunRow) -> DriftRun:
    return DriftRun(
        id=row.id,
        session_id=row.session_id,
        status=DriftRunStatus(row.status),
        stage=DriftStage(row.stage),
        activity=row.activity,
        decision_reason=row.decision_reason,
        resume_payload=json.loads(row.resume_payload_json),
        evidence_refs=json.loads(row.evidence_refs_json),
        produced_candidate_id=row.produced_candidate_id,
        snapshot_at=row.snapshot_at,
        snapshot_message_id=row.snapshot_message_id,
        resumed_from_run_id=row.resumed_from_run_id,
        auto_resume_count=row.auto_resume_count,
        error_code=row.error_code,
        error_message=row.error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
