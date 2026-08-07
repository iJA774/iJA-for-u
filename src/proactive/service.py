"""主动来源、硬门控、结构化判断与无出站 Drift 的应用服务。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, model_validator

from adapters.persistence import PersonaStore
from application.events import EventHub
from application.memory import MemoryService
from application.model_json import parse_model_json
from application.outbound import OutboundCoordinator
from config import AppSettings
from domain.errors import (
    InputValidationError,
    InvalidModelResponseError,
    NotFoundError,
)
from domain.models import (
    CandidateSourceKind,
    ChatType,
    DeliveryReceipt,
    DeliveryStatus,
    DriftRun,
    DriftRunStatus,
    DriftStage,
    EngagementPolicy,
    FeedSource,
    MemoryRecord,
    MemorySourceChain,
    MessageComponent,
    MessageOrigin,
    MessageRole,
    OutboundMessage,
    ProactiveCandidate,
    ProactiveCandidateStatus,
    ProactiveRun,
    ProactiveRunStatus,
    ProactiveStage,
    SessionView,
    StoredMessage,
    scope_key_for,
    utc_now,
)
from observability import model_observation_scope
from ports import ApplicationRepository, ModelProvider, ModelRequest
from ports.egress import EgressEnvelope, EgressHandler
from proactive.rss import CandidateSource, require_public_destination
from prompting import PromptAssembler

logger = logging.getLogger(__name__)
_RECONCILIATION_PAGE_SIZE = 200


async def _default_egress(envelope: EgressEnvelope) -> str:
    """无 egress 插件时的直通透传。"""

    return envelope.text


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}


def _canonical_rss_key(
    *,
    url: str,
    title: str,
    summary: str,
    published_at: datetime | None,
    source_key: str,
) -> str:
    """跨 Feed 生成内容身份，避免同一链接因来源键不同重复触达。"""

    if url:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() in {"http", "https"} and parts.hostname:
            host = parts.hostname.lower()
            port = parts.port
            if port and not (
                (parts.scheme.lower() == "http" and port == 80)
                or (parts.scheme.lower() == "https" and port == 443)
            ):
                host = f"{host}:{port}"
            query = urlencode(
                sorted(
                    (key, value)
                    for key, value in parse_qsl(
                        parts.query, keep_blank_values=True
                    )
                    if not key.lower().startswith("utm_")
                    and key.lower() not in _TRACKING_QUERY_KEYS
                )
            )
            canonical_url = urlunsplit(
                (
                    parts.scheme.lower(),
                    host,
                    parts.path or "/",
                    query,
                    "",
                )
            )
            return "url:" + hashlib.sha256(
                canonical_url.encode("utf-8")
            ).hexdigest()
    material = json.dumps(
        {
            "title": title.strip(),
            "summary": summary.strip(),
            "published_at": (
                _as_utc(published_at).isoformat() if published_at else ""
            ),
            # 无链接条目以内容本身去重；source_key 只在内容完全空时兜底。
            "fallback": source_key if not title.strip() and not summary.strip() else "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "item:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


class ProactiveJudgePayload(BaseModel):
    """主动判断模型的最小可校验输出。"""

    action: Literal["send", "skip"]
    candidate_id: str | None = None
    score: float = Field(ge=0, le=1)
    reason_code: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_candidate(self) -> ProactiveJudgePayload:
        if self.action == "send" and not self.candidate_id:
            raise ValueError("send 必须包含 candidate_id")
        if self.action == "skip" and self.candidate_id is not None:
            raise ValueError("skip 不得包含 candidate_id")
        return self


class ProactiveComposePayload(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class DriftSelectPayload(BaseModel):
    activity: Literal[
        "candidate_aggregation", "conversation_topic_preparation", "idle"
    ]
    reason: str = Field(min_length=1, max_length=500)


class DriftActivityPayload(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=4000)
    parent_candidate_ids: list[str] = Field(default_factory=list, max_length=20)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)


class EngagementService:
    """管理私聊主动策略、来源、Proactive 与 Drift。"""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: ApplicationRepository,
        model: ModelProvider,
        personas: PersonaStore,
        prompting: PromptAssembler,
        outbound: OutboundCoordinator,
        events: EventHub,
        rss: CandidateSource,
        session_lock: Callable[[str], asyncio.Lock],
        memory: MemoryService,
    ) -> None:
        self.settings = settings
        self.store = store
        self.model = model
        self.personas = personas
        self.prompting = prompting
        self.outbound = outbound
        self.events = events
        self.rss = rss
        self.session_lock = session_lock
        self.memory = memory
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._egress_filter: EgressHandler = _default_egress

    def set_model(self, model: ModelProvider) -> None:
        """热切换后续主动与 Drift 模型。"""

        self.model = model

    def set_egress_filter(self, filter: EgressHandler) -> None:
        """热切换输出过滤管线；无 egress 插件时为直通透传。"""

        self._egress_filter = filter

    async def recover_proactive_runs(self) -> None:
        """恢复可重放准备态，并把发送中的不确定状态收敛为终态。"""

        for run in await self.store.list_recoverable_proactive_runs():
            try:
                if run.status == ProactiveRunStatus.RUNNING:
                    run.status = ProactiveRunStatus.FAILED
                    run.stage = ProactiveStage.FINISHED
                    run.error_code = "proactive_interrupted"
                    run.error_message = (
                        "进程在主动消息准备完成前退出；未发生发送，可重新判断候选"
                    )
                    run.updated_at = utc_now()
                    await self.store.save_proactive_run(run)
                    await self._try_publish_run(run)
                    continue
                await self._recover_prepared_proactive(run)
            except Exception:
                logger.exception(
                    "主动运行恢复失败",
                    extra={
                        "session_id": run.session_id,
                        "proactive_run_id": run.id,
                        "stage": run.stage.value,
                    },
                )
        await self._reconcile_sent_proactive_memories()

    async def _reconcile_sent_proactive_memories(self) -> None:
        """幂等补齐已发送运行的派生记忆，覆盖发送提交后的崩溃窗口。"""

        if not self.settings.memory.enabled:
            return
        for policy in await self.store.list_engagement_policies():
            try:
                session = await self._require_session(policy.session_id)
                # 群聊同样会持久化关闭状态的 engagement policy，但主动触达及其派生记忆仅属于私聊。
                if session.chat_type != ChatType.PRIVATE:
                    continue
                recorded_run_ids: set[str] = set()
                memory_after_id: str | None = None
                while True:
                    memories = await self.store.page_memories(
                        scope_key=scope_key_for(session),
                        after_id=memory_after_id,
                        limit=_RECONCILIATION_PAGE_SIZE,
                    )
                    if not memories:
                        break
                    recorded_run_ids.update(
                        memory.source_run_id
                        for memory in memories
                        if memory.source_chain == MemorySourceChain.PROACTIVE
                        and memory.source_run_id
                    )
                    # 等价内容可能合并进 reactive 记忆；结构化来源引用仍能证明已对账。
                    recorded_run_ids.update(
                        source_ref.removeprefix("proactive_run:")
                        for memory in memories
                        for source_ref in memory.source_refs
                        if source_ref.startswith("proactive_run:")
                    )
                    memory_after_id = memories[-1].id
                    if len(memories) < _RECONCILIATION_PAGE_SIZE:
                        break

                run_after_id: str | None = None
                while True:
                    sent_runs = (
                        await self.store.page_sent_proactive_runs_for_memory_reconciliation(
                            session.id,
                            after_id=run_after_id,
                            limit=_RECONCILIATION_PAGE_SIZE,
                        )
                    )
                    if not sent_runs:
                        break
                    for run in sent_runs:
                        if run.id in recorded_run_ids:
                            continue
                        candidate = await self.store.get_proactive_candidate(
                            run.candidate_id or ""
                        )
                        committed = (
                            await self.store.get_recallable_proactive_message_by_run(
                                session.id,
                                run.id,
                            )
                        )
                        if candidate is None or committed is None:
                            logger.error(
                                "主动已发送运行缺少记忆对账证据",
                                extra={
                                    "session_id": session.id,
                                    "proactive_run_id": run.id,
                                    "candidate_id": run.candidate_id,
                                    "has_message": committed is not None,
                                },
                            )
                            continue
                        await self._record_proactive_memory(
                            session, run, candidate, committed
                        )
                        recorded_run_ids.add(run.id)
                    run_after_id = sent_runs[-1].id
                    if len(sent_runs) < _RECONCILIATION_PAGE_SIZE:
                        break
            except Exception:
                logger.exception(
                    "主动已发送记忆对账失败",
                    extra={"session_id": policy.session_id},
                )

    async def _recover_prepared_proactive(self, run: ProactiveRun) -> None:
        """只对完整 PREPARED 重新门控；发送开始后绝不自动重发。"""

        candidate = (
            await self.store.get_proactive_candidate(run.candidate_id)
            if run.candidate_id
            else None
        )
        message = (
            await self.store.get_outbound_message(run.outbound_id)
            if run.outbound_id
            else None
        )
        receipt = (
            await self.store.get_delivery(run.outbound_id)
            if run.outbound_id
            else None
        )
        if candidate is None or message is None or receipt is None:
            # 旧版本可能先保存 run 再保存出站。该窗口明确没有完整发送意图，
            # 因而候选应重新开放，不得误记成已发送或永久失败。
            run.status = ProactiveRunStatus.FAILED
            run.stage = ProactiveStage.FINISHED
            run.error_code = "incomplete_prepared_state"
            run.error_message = "主动准备态缺少候选或完整出站记录，未尝试发送"
            run.updated_at = utc_now()
            candidates = []
            if candidate is not None:
                self._defer_candidate(
                    candidate,
                    "incomplete_prepared_state",
                    minutes=5,
                )
                candidates.append(candidate)
            await self.store.save_proactive_run_with_candidates(
                run, candidates
            )
            await self._publish_proactive_state(run, candidates)
            return

        session = await self._require_private(run.session_id)
        if receipt.status == DeliveryStatus.PREPARED:
            async with self.session_lock(run.session_id):
                gate_reason = await self._post_prepare_gate_reason(run)
                if gate_reason:
                    await self._drop_prepared_proactive(
                        run, candidate, message, gate_reason
                    )
                    return
                await self._deliver_prepared_proactive(
                    session, run, candidate, message
                )
            return

        # OutboundCoordinator 在本方法前把 DISPATCHING 收敛为 UNKNOWN，
        # SENT 则幂等提交到可见历史；这里仅补齐主动运行与候选终态。
        committed = None
        if receipt.status in {
            DeliveryStatus.DISPATCHING,
            DeliveryStatus.SENT,
        }:
            receipt, committed = await self.outbound.deliver(session, message)
        await self._settle_proactive_delivery(
            session, run, candidate, receipt, committed
        )

    async def has_eligible_candidates(self, session_id: str) -> bool:
        return bool(await self._eligible_candidates(session_id, utc_now()))

    async def get_policy(self, session_id: str) -> EngagementPolicy:
        session = await self._require_session(session_id)
        policy = await self.store.get_engagement_policy(session_id)
        if policy is not None:
            return policy
        policy = EngagementPolicy(
            session_id=session_id,
            proactive_enabled=session.chat_type == ChatType.PRIVATE,
        )
        return await self.store.save_engagement_policy(policy)

    async def update_policy(
        self,
        session_id: str,
        *,
        proactive_enabled: bool,
        drift_enabled: bool,
        timezone: str,
        quiet_start: str,
        quiet_end: str,
        minimum_interval_minutes: int,
    ) -> EngagementPolicy:
        session = await self._require_session(session_id)
        if session.chat_type != ChatType.PRIVATE and (
            proactive_enabled or drift_enabled
        ):
            raise InputValidationError("群聊不能开启主动触达或 Drift")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise InputValidationError("主动策略 timezone 必须是有效 IANA 时区") from exc
        policy = EngagementPolicy(
            session_id=session_id,
            proactive_enabled=proactive_enabled,
            drift_enabled=drift_enabled,
            timezone=timezone,
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            minimum_interval_minutes=minimum_interval_minutes,
            updated_at=utc_now(),
        )
        saved = await self.store.save_engagement_policy(policy)
        await self.events.publish(
            "engagement.policy.updated", saved.model_dump(mode="json")
        )
        return saved

    async def create_feed(
        self,
        session_id: str,
        *,
        url: str,
        title: str = "",
        poll_interval_minutes: int | None = None,
    ) -> FeedSource:
        await self._require_private(session_id)
        normalized = await require_public_destination(url)
        now = utc_now()
        source = FeedSource(
            session_id=session_id,
            url=normalized,
            title=title.strip(),
            poll_interval_minutes=(
                poll_interval_minutes
                or self.settings.proactive.default_poll_interval_minutes
            ),
            next_poll_at=now,
            created_at=now,
            updated_at=now,
        )
        saved = await self.store.create_feed_source(source)
        await self.events.publish("feed.updated", saved.model_dump(mode="json"))
        return saved

    async def submit_external_candidate(
        self,
        session_id: str,
        *,
        source_kind: CandidateSourceKind,
        source_key: str,
        title: str,
        summary: str,
        url: str,
        source_ref: str,
        published_at: datetime | None = None,
    ) -> tuple[ProactiveCandidate, bool]:
        """接收 alert/content/context 类型来源并幂等写入候选池。"""

        await self._require_private(session_id)
        if source_kind not in {
            CandidateSourceKind.ALERT,
            CandidateSourceKind.CONTENT,
            CandidateSourceKind.CONTEXT,
        }:
            raise InputValidationError("外部主动来源只允许 alert、content 或 context")
        clean_ref = source_ref.strip()
        if ":" not in clean_ref or any(char.isspace() for char in clean_ref):
            raise InputValidationError("source_ref 必须是无空白的命名空间引用")
        clean_url = url.strip()
        if clean_url:
            clean_url = await require_public_destination(clean_url)
        now = utc_now()
        candidate = ProactiveCandidate(
            session_id=session_id,
            source_kind=source_kind,
            source_key=source_key.strip(),
            title=title.strip(),
            summary=summary.strip(),
            url=clean_url,
            published_at=published_at,
            source_refs=[clean_ref],
            expires_at=now
            + timedelta(days=self.settings.proactive.candidate_retention_days),
            created_at=now,
            updated_at=now,
        )
        saved, created = await self.store.create_proactive_candidate(candidate)
        await self.events.publish(
            "proactive.candidate.updated", saved.model_dump(mode="json")
        )
        return saved, created

    async def update_feed(
        self,
        feed_id: str,
        *,
        url: str,
        title: str,
        enabled: bool,
        poll_interval_minutes: int,
    ) -> FeedSource:
        source = await self._require_feed(feed_id)
        await self._require_private(source.session_id)
        normalized = await require_public_destination(url)
        if normalized != source.url:
            source.etag = None
            source.last_modified = None
            source.next_poll_at = utc_now()
        source.url = normalized
        source.title = title.strip()
        source.enabled = enabled
        source.poll_interval_minutes = poll_interval_minutes
        source.updated_at = utc_now()
        saved = await self.store.save_feed_source(source)
        await self.events.publish("feed.updated", saved.model_dump(mode="json"))
        return saved

    async def delete_feed(self, feed_id: str) -> FeedSource:
        source = await self.store.delete_feed_source(feed_id)
        await self.events.publish("feed.deleted", source.model_dump(mode="json"))
        return source

    async def refresh_feed(self, feed_id: str) -> FeedSource:
        source = await self._require_feed(feed_id)
        await self.poll_feed(source)
        refreshed = await self._require_feed(feed_id)
        return refreshed

    async def poll_due_feeds(self) -> None:
        sources = await self.store.list_due_feed_sources(utc_now())
        semaphore = asyncio.Semaphore(
            self.settings.proactive.max_concurrency
        )

        async def poll(source: FeedSource) -> None:
            async with semaphore:
                await self.poll_feed(source)

        await asyncio.gather(*(poll(source) for source in sources))

    async def poll_feed(self, source: FeedSource) -> int:
        """先持久化候选，再推进 Feed 条件请求状态。"""

        now = utc_now()
        try:
            result = await self.rss.fetch(source)
            created_count = 0
            if not result.not_modified:
                for item in result.items:
                    candidate = ProactiveCandidate(
                        session_id=source.session_id,
                        source_kind=CandidateSourceKind.RSS,
                        source_id=source.id,
                        source_key=_canonical_rss_key(
                            url=item.url,
                            title=item.title,
                            summary=item.summary,
                            published_at=item.published_at,
                            source_key=item.source_key,
                        ),
                        title=item.title,
                        summary=item.summary,
                        url=item.url,
                        published_at=item.published_at,
                        source_refs=[f"feed:{source.id}:{item.source_key}"],
                        expires_at=now
                        + timedelta(
                            days=self.settings.proactive.candidate_retention_days
                        ),
                    )
                    saved, created = await self.store.create_proactive_candidate(
                        candidate
                    )
                    if created:
                        created_count += 1
                    await self.events.publish(
                        "proactive.candidate.updated",
                        saved.model_dump(mode="json"),
                    )
            source.title = result.title or source.title
            source.etag = result.etag
            source.last_modified = result.last_modified
            source.consecutive_failures = 0
            source.error_code = None
            source.error_message = None
            source.last_polled_at = now
            source.next_poll_at = now + timedelta(
                minutes=source.poll_interval_minutes
            )
            source.updated_at = now
            await self.store.save_feed_source(source)
            await self.events.publish(
                "feed.updated", source.model_dump(mode="json")
            )
            return created_count
        except Exception as exc:
            source.consecutive_failures += 1
            delay_minutes = min(
                360,
                source.poll_interval_minutes
                * (2 ** min(source.consecutive_failures, 4)),
            )
            source.error_code = getattr(exc, "code", "feed_fetch_failed")
            source.error_message = str(exc)[:500]
            source.last_polled_at = now
            source.next_poll_at = now + timedelta(minutes=delay_minutes)
            source.updated_at = now
            await self.store.save_feed_source(source)
            await self.events.publish(
                "feed.updated", source.model_dump(mode="json")
            )
            logger.warning(
                "RSS/Atom 抓取失败",
                extra={
                    "session_id": source.session_id,
                    "feed_id": source.id,
                    "error_code": source.error_code,
                },
            )
            return 0

    async def run_proactive(
        self, session_id: str, *, force: bool = False
    ) -> ProactiveRun:
        """执行一次硬门控、结构化判断和最多一条文本投递。"""

        lock = self._run_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            return await self._run_proactive_locked(session_id, force=force)

    async def _run_proactive_locked(
        self, session_id: str, *, force: bool
    ) -> ProactiveRun:
        session = await self._require_private(session_id)
        policy = await self.get_policy(session_id)
        snapshot_at = utc_now()
        history = await self.store.list_recallable_messages(session_id, 20)
        last_user = next(
            (item for item in reversed(history) if item.role == MessageRole.USER),
            None,
        )
        run = ProactiveRun(
            session_id=session_id,
            status=ProactiveRunStatus.RUNNING,
            stage=ProactiveStage.GATING,
            snapshot_at=snapshot_at,
            snapshot_message_id=last_user.id if last_user else None,
            manual_triggered=force,
        )
        candidates = await self._eligible_candidates(
            session_id, snapshot_at, force=force
        )
        run.candidate_ids = [item.id for item in candidates]
        await self.store.save_proactive_run(run)
        await self._try_publish_run(run)
        gate_reason = await self._gate_reason(
            policy, snapshot_at, has_candidates=bool(candidates), force=force
        )
        if gate_reason:
            run.status = ProactiveRunStatus.GATED
            run.stage = ProactiveStage.FINISHED
            run.gate_reason = gate_reason
            run.decision_code = gate_reason
            run.decision_reason = gate_reason
            if candidates and gate_reason not in {"disabled", "no_candidates"}:
                for candidate in candidates:
                    self._defer_candidate(
                        candidate, gate_reason, minutes=30
                    )
            run.updated_at = utc_now()
            await self.store.save_proactive_run_with_candidates(
                run, candidates
            )
            await self._publish_proactive_state(run, candidates)
            return run

        facts = await self.store.list_facts(scope_key_for(session))
        memory_query = "\n".join(
            f"{item.title}\n{item.summary}" for item in candidates
        )
        memories = await self.memory.retrieve(
            session=session,
            query=memory_query,
            context_budget=True,
        )
        recent_proactive = [
            item
            for item in await self.store.list_recallable_messages(session_id, 100)
            if item.origin == MessageOrigin.PROACTIVE
        ][-5:]
        local_now = snapshot_at.astimezone(ZoneInfo(policy.timezone))
        try:
            run.stage = ProactiveStage.JUDGING
            run.updated_at = utc_now()
            await self.store.save_proactive_run(run)
            await self._try_publish_run(run)
            with model_observation_scope(
                task="proactive.judge",
                session_id=session_id,
                turn_id=run.id,
                run_id=run.id,
            ):
                judge_result = await self.model.complete(
                    ModelRequest(
                        messages=self.prompting.build_proactive_judge(
                            session=session,
                            facts=facts,
                            messages=history,
                            recent_proactive=recent_proactive,
                            candidates=candidates,
                            request_time=local_now.isoformat(),
                            timezone=policy.timezone,
                            memories=memories,
                        ),
                        model=self.settings.model.name,
                        temperature=0,
                        max_tokens=min(500, self.settings.model.max_tokens),
                        json_mode=self.settings.model.supports_json_object,
                    )
                )
            if not judge_result.content:
                raise InvalidModelResponseError("主动判断模型没有返回 JSON")
            decision = ProactiveJudgePayload.model_validate(
                parse_model_json(judge_result.content)
            )
            run.decision_code = decision.reason_code
            run.decision_reason = decision.reason
            run.score = decision.score
            candidate_map = {item.id: item for item in candidates}
            if (
                decision.candidate_id is not None
                and decision.candidate_id not in candidate_map
            ):
                raise InvalidModelResponseError("主动判断引用了本轮外的候选")
            if (
                decision.action == "skip"
                or decision.score < self.settings.proactive.judge_threshold
            ):
                reason = (
                    decision.reason
                    if decision.action == "skip"
                    else "模型分数低于发送阈值"
                )
                for candidate in candidates:
                    candidate.status = ProactiveCandidateStatus.SKIPPED
                    candidate.decision_reason = reason
                    candidate.updated_at = utc_now()
                run.status = ProactiveRunStatus.SKIPPED
                run.stage = ProactiveStage.FINISHED
                run.decision_code = (
                    decision.reason_code
                    if decision.action == "skip"
                    else "below_threshold"
                )
                run.decision_reason = reason
                run.score = decision.score
                run.updated_at = utc_now()
                await self.store.save_proactive_run_with_candidates(
                    run, candidates
                )
                await self._publish_proactive_state(run, candidates)
                return run
            candidate = candidate_map[decision.candidate_id or ""]
            run.candidate_id = candidate.id
            run.stage = ProactiveStage.COMPOSING
            run.updated_at = utc_now()
            await self.store.save_proactive_run(run)
            await self._try_publish_run(run)
            compose_messages = self.prompting.build_proactive_compose(
                session=session,
                persona=self.personas.get_for_chat_type(session.chat_type),
                candidate=candidate,
                request_time=local_now.isoformat(),
                timezone=policy.timezone,
                memories=memories,
            )
            with model_observation_scope(
                task="proactive.compose",
                session_id=session_id,
                turn_id=run.id,
                run_id=run.id,
            ):
                compose_result = await self.model.complete(
                    ModelRequest(
                        messages=compose_messages,
                        model=self.settings.model.name,
                        temperature=self.settings.model.temperature,
                        max_tokens=min(500, self.settings.model.max_tokens),
                        json_mode=self.settings.model.supports_json_object,
                    )
                )
            if not compose_result.content:
                raise InvalidModelResponseError("主动文案模型没有返回 JSON")
            composed = ProactiveComposePayload.model_validate(
                parse_model_json(compose_result.content)
            )
        except (ValidationError, ValueError, InvalidModelResponseError) as exc:
            return await self._fail_proactive(run, candidates, exc)
        except Exception as exc:
            return await self._fail_proactive(run, candidates, exc)

        try:
            filtered_text = await self._egress_filter(
                EgressEnvelope(
                    session_id=session_id,
                    turn_id=run.id,
                    text=composed.text,
                    prompt_messages=compose_messages,
                    model=self.model,
                    model_name=self.settings.model.name,
                    temperature=self.settings.model.temperature,
                    max_tokens=min(500, self.settings.model.max_tokens),
                )
            )
            if filtered_text != composed.text:
                composed = composed.model_copy(update={"text": filtered_text})
        except Exception as exc:
            logger.exception(
                "主动消息输出过滤异常，已阻止发送原始文案",
                extra={"session_id": session_id, "proactive_run_id": run.id},
            )
            return await self._fail_proactive(run, candidates, exc)

        run.stage = ProactiveStage.PREPARING
        run.updated_at = utc_now()
        await self.store.save_proactive_run(run)
        await self._try_publish_run(run)
        outbound = OutboundMessage(
            id="out_proactive_"
            + hashlib.sha256(run.id.encode()).hexdigest()[:32],
            session_id=session_id,
            components=[MessageComponent.text_component(composed.text)],
            origin=MessageOrigin.PROACTIVE,
            origin_run_id=run.id,
            source_refs=[candidate.id, *candidate.source_refs],
        )
        run.status = ProactiveRunStatus.PREPARED
        run.stage = ProactiveStage.PREPARED
        run.outbound_id = outbound.id
        run.updated_at = utc_now()
        candidate.status = ProactiveCandidateStatus.PREPARED
        candidate.decision_reason = "已保留给当前主动运行"
        candidate.updated_at = run.updated_at
        await self.store.prepare_proactive_delivery(run, candidate, outbound)
        await self._publish_proactive_state(run, [candidate])
        await self._try_publish_delivery(
            DeliveryReceipt(
                outbound_id=outbound.id,
                status=DeliveryStatus.PREPARED,
            )
        )

        async with self.session_lock(session_id):
            gate_reason = await self._post_prepare_gate_reason(run)
            if gate_reason:
                await self._drop_prepared_proactive(
                    run, candidate, outbound, gate_reason
                )
                return run
            await self._deliver_prepared_proactive(
                session, run, candidate, outbound
            )
        return run

    async def run_drift(
        self, session_id: str, *, force: bool = False
    ) -> DriftRun:
        """运行一次最多两次模型调用、永不直接发送的 Drift。"""

        # Proactive 和 Drift 会读写同一候选池；同一私聊只能有一个后台链形成快照。
        lock = self._run_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            return await self._run_drift_locked(session_id, force=force)

    async def recover_drift_runs(self) -> None:
        """把重启前的 RUNNING 收敛到可审计、可有界续接的暂停点。"""

        for run in await self.store.list_recoverable_drift_runs():
            run.status = DriftRunStatus.PAUSED
            run.auto_resume_count = min(
                self.settings.drift.max_auto_resumes,
                run.auto_resume_count + 1,
            )
            run.error_code = "drift_interrupted"
            run.error_message = "进程在 Drift 完成前退出，已从最后持久化阶段恢复"
            run.resume_payload = self._drift_resume_payload(run)
            run.updated_at = utc_now()
            await self.store.save_drift_run(run)
            await self._try_publish_drift(run)
            logger.warning(
                "Drift 中断运行已恢复为暂停点",
                extra={
                    "session_id": run.session_id,
                    "drift_run_id": run.id,
                    "stage": run.stage.value,
                    "auto_resume_count": run.auto_resume_count,
                },
            )

    async def _run_drift_locked(
        self, session_id: str, *, force: bool
    ) -> DriftRun:
        session = await self._require_private(session_id)
        policy = await self.get_policy(session_id)
        if not policy.drift_enabled:
            raise InputValidationError("当前私聊未启用 Drift")
        now = utc_now()
        history = await self.store.list_recallable_messages(session_id, 20)
        pending = await self.store.list_pending_messages(session_id)
        # 人工 run-now 只绕过空闲/频率门控，不能抢占尚未处理的用户输入。
        if pending:
            raise InputValidationError("当前存在待处理用户消息，不能运行 Drift")
        last_user = next(
            (item for item in reversed(history) if item.role == MessageRole.USER),
            None,
        )
        if (
            not force
            and last_user is not None
            and now - _as_utc(last_user.created_at)
            < timedelta(hours=self.settings.drift.idle_hours)
        ):
            raise InputValidationError("私聊尚未达到 Drift 空闲时长")
        latest = await self.store.latest_drift_run(session_id)
        if (
            latest is not None
            and latest.status == DriftRunStatus.PAUSED
            and latest.auto_resume_count >= self.settings.drift.max_auto_resumes
            and not force
        ):
            raise InputValidationError(
                "Drift 已达到自动续接上限，需要人工“立即运行”"
            )
        if (
            not force
            and latest is not None
            and latest.status
            in {DriftRunStatus.COMPLETED, DriftRunStatus.PAUSED}
            and now - _as_utc(latest.updated_at)
            < timedelta(hours=self.settings.drift.minimum_interval_hours)
        ):
            raise InputValidationError("Drift 尚在最小运行间隔内")
        active_candidates = await self._eligible_candidates(session_id, now)
        if active_candidates and not force:
            raise InputValidationError("存在待判断的主动候选，暂不进入 Drift")

        facts = await self.store.list_facts(scope_key_for(session))
        memories = await self.memory.retrieve(
            session=session,
            query=self.prompting.project_messages_text(session, history),
            context_budget=True,
        )
        candidate_history = await self.store.list_proactive_candidates(
            session_id,
            limit=1000,
        )
        consumed_parent_ids = {
            parent_id
            for item in candidate_history
            if item.source_kind == CandidateSourceKind.DRIFT_AGGREGATION
            for parent_id in item.parent_candidate_ids
        }
        used_conversation_refs = {
            ref
            for item in candidate_history
            if item.source_kind == CandidateSourceKind.DRIFT_CONVERSATION
            for ref in item.source_refs
        }
        skipped = [
            item
            for item in candidate_history
            if item.status == ProactiveCandidateStatus.SKIPPED
            if item.source_kind == CandidateSourceKind.RSS
            and item.id not in consumed_parent_ids
        ][:20]
        recent_runs = await self.store.list_drift_runs(session_id, limit=5)
        payload = {
            "request_time": now.astimezone(ZoneInfo(policy.timezone)).isoformat(),
            "timezone": policy.timezone,
            "messages": self.prompting._message_payload(
                history,
                session=session,
            ),
            "facts": [
                {
                    "fact_id": fact.id,
                    "category": fact.category,
                    "content": fact.content,
                }
                for fact in facts
            ],
            "memories": self.prompting._memory_payload(memories),
            "rss_candidates": [
                {
                    **self.prompting._candidate_payload(item),
                    "source_refs": item.source_refs,
                }
                for item in skipped
            ],
            "used_drift_evidence_refs": sorted(used_conversation_refs),
            "resume_payload": (
                latest.resume_payload
                if latest is not None
                and latest.status == DriftRunStatus.PAUSED
                else {}
            ),
            "recent_drift_runs": [
                {
                    "run_id": item.id,
                    "status": item.status.value,
                    "activity": item.activity,
                    "decision_reason": item.decision_reason,
                    "produced_candidate_id": item.produced_candidate_id,
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in reversed(recent_runs)
            ],
        }
        resume_activity: Literal[
            "candidate_aggregation", "conversation_topic_preparation"
        ] | None = None
        if latest is not None and latest.status == DriftRunStatus.PAUSED:
            candidate_activity = str(latest.resume_payload.get("activity") or "")
            resume_stage = str(latest.resume_payload.get("stage") or "")
            if (
                candidate_activity == "candidate_aggregation"
                and resume_stage == "activity_execution"
            ):
                resume_activity = "candidate_aggregation"
            elif (
                candidate_activity == "conversation_topic_preparation"
                and resume_stage == "activity_execution"
            ):
                resume_activity = "conversation_topic_preparation"
        if resume_activity == "candidate_aggregation" and len(skipped) < 2:
            resume_activity = None
        if (
            resume_activity == "conversation_topic_preparation"
            and not history
            and not facts
            and not memories
        ):
            resume_activity = None
        resume_count = (
            latest.auto_resume_count
            if latest is not None and latest.status == DriftRunStatus.PAUSED
            else 0
        )
        if force and resume_count >= self.settings.drift.max_auto_resumes:
            # 人工运行代表一次新的明确授权；失败后重新获得一轮有限自动续接预算。
            resume_count = 0
        run = DriftRun(
            session_id=session_id,
            stage=(
                DriftStage.EXECUTING
                if resume_activity
                else DriftStage.SELECTING
            ),
            activity=resume_activity or "",
            snapshot_at=now,
            snapshot_message_id=last_user.id if last_user is not None else None,
            resumed_from_run_id=(
                latest.id
                if latest is not None
                and latest.status == DriftRunStatus.PAUSED
                else None
            ),
            auto_resume_count=resume_count,
            resume_payload=(
                latest.resume_payload
                if latest is not None and resume_activity
                else {}
            ),
        )
        await self.store.save_drift_run(run)
        await self._try_publish_drift(run)
        try:
            if resume_activity:
                selection = DriftSelectPayload(
                    activity=resume_activity,
                    reason="从已暂停的原子活动继续",
                )
            else:
                with model_observation_scope(
                    task="drift.select",
                    session_id=session_id,
                    turn_id=run.id,
                    run_id=run.id,
                ):
                    selection_result = await self.model.complete(
                        ModelRequest(
                            messages=self.prompting.build_drift_select(payload),
                            model=self.settings.model.name,
                            temperature=0,
                            max_tokens=min(
                                300,
                                self.settings.model.max_tokens,
                            ),
                            json_mode=self.settings.model.supports_json_object,
                        )
                    )
                if not selection_result.content:
                    raise InvalidModelResponseError("Drift 选择模型没有返回 JSON")
                selection = DriftSelectPayload.model_validate(
                    parse_model_json(selection_result.content)
                )
            run.activity = selection.activity
            run.decision_reason = selection.reason
            if selection.activity == "idle":
                run.status = DriftRunStatus.COMPLETED
                run.stage = DriftStage.FINISHED
                run.resume_payload = {}
                run.updated_at = utc_now()
                await self.store.save_drift_run(run)
                await self._try_publish_drift(run)
                return run
            run.stage = DriftStage.EXECUTING
            run.resume_payload = self._drift_resume_payload(run)
            run.updated_at = utc_now()
            # 活动选择是独立 checkpoint；重启/瞬时失败不再重复选择。
            await self.store.save_drift_run(run)
            await self._try_publish_drift(run)
            with model_observation_scope(
                task="drift.activity",
                session_id=session_id,
                turn_id=run.id,
                run_id=run.id,
            ):
                activity_result = await self.model.complete(
                    ModelRequest(
                        messages=self.prompting.build_drift_activity(
                            selection.activity, payload
                        ),
                        model=self.settings.model.name,
                        temperature=self.settings.model.temperature,
                        max_tokens=min(600, self.settings.model.max_tokens),
                        json_mode=self.settings.model.supports_json_object,
                    )
                )
            if not activity_result.content:
                raise InvalidModelResponseError("Drift 活动模型没有返回 JSON")
            activity = DriftActivityPayload.model_validate(
                parse_model_json(activity_result.content)
            )
            candidate = self._validate_drift_candidate(
                run,
                selection.activity,
                activity,
                skipped,
                history,
                facts,
                memories,
            )
            run.stage = DriftStage.COMMITTING
            run.evidence_refs = candidate.source_refs
            run.resume_payload = self._drift_resume_payload(run)
            run.updated_at = utc_now()
            await self.store.save_drift_run(run)

            async with self.session_lock(session_id):
                current_history = await self.store.list_recallable_messages(
                    session_id, 20
                )
                current_last_user = next(
                    (
                        item
                        for item in reversed(current_history)
                        if item.role == MessageRole.USER
                    ),
                    None,
                )
                current_policy = await self.get_policy(session_id)
                snapshot_stale = (
                    (current_last_user.id if current_last_user else None)
                    != run.snapshot_message_id
                    or bool(await self.store.list_pending_messages(session_id))
                )
                if snapshot_stale or not current_policy.drift_enabled:
                    run.status = DriftRunStatus.GATED
                    run.stage = DriftStage.FINISHED
                    run.error_code = (
                        "snapshot_stale"
                        if snapshot_stale
                        else "drift_disabled"
                    )
                    run.error_message = (
                        "Drift 执行期间出现新的用户消息，已丢弃过期结果"
                        if snapshot_stale
                        else "Drift 执行期间策略已关闭，已丢弃结果"
                    )
                    run.resume_payload = {}
                    run.updated_at = utc_now()
                    await self.store.save_drift_run(run)
                    await self._try_publish_drift(run)
                    return run

                run.status = DriftRunStatus.COMPLETED
                run.stage = DriftStage.FINISHED
                run.resume_payload = {}
                run.error_code = None
                run.error_message = None
                run.updated_at = utc_now()
                _, saved, created = (
                    await self.store.complete_drift_run_with_candidate(
                        run, candidate
                    )
                )
            if created:
                await self._try_publish_candidate(saved)
            await self._try_publish_drift(run)
            return run
        except Exception as exc:
            run.status = DriftRunStatus.PAUSED
            run.auto_resume_count = min(
                self.settings.drift.max_auto_resumes,
                run.auto_resume_count + 1,
            )
            run.error_code = getattr(exc, "code", "drift_failed")
            run.error_message = str(exc)[:500]
            run.resume_payload = self._drift_resume_payload(run)
            run.updated_at = utc_now()
            await self.store.save_drift_run(run)
            await self._try_publish_drift(run)
            logger.warning(
                "Drift 运行暂停",
                extra={
                    "session_id": session_id,
                    "drift_run_id": run.id,
                    "stage": run.stage.value,
                    "error_code": run.error_code,
                    "auto_resume_count": run.auto_resume_count,
                },
                exc_info=True,
            )
            return run

    @staticmethod
    def _drift_resume_payload(run: DriftRun) -> dict[str, object]:
        """从权威阶段生成最小续接投影，不保存消息正文或模型隐藏状态。"""

        if run.activity in {
            "candidate_aggregation",
            "conversation_topic_preparation",
        }:
            return {
                "activity": run.activity,
                "stage": "activity_execution",
                "next": "重新读取当前域证据，只执行已选原子活动并重新校验",
            }
        return {
            "activity": "",
            "stage": "activity_selection",
            "next": "重新读取当前域证据并选择一个原子活动",
        }

    async def _eligible_candidates(
        self, session_id: str, now: datetime, *, force: bool = False
    ) -> list[ProactiveCandidate]:
        candidates = await self.store.list_proactive_candidates(
            session_id,
            statuses={
                ProactiveCandidateStatus.PENDING,
                ProactiveCandidateStatus.DEFERRED,
            },
            limit=100,
        )
        eligible: list[ProactiveCandidate] = []
        for candidate in candidates:
            if _as_utc(candidate.expires_at) <= now:
                candidate.status = ProactiveCandidateStatus.EXPIRED
                candidate.decision_reason = "候选超过保留期限"
                candidate.updated_at = now
                await self.store.save_proactive_candidate(candidate)
                await self._publish_candidate(candidate)
                continue
            # 人工 run-now 是一次新的、明确授权的运行，可以提前重新判断已推迟
            # 候选；过期仍是不可绕过的事实边界。
            if not force and _as_utc(candidate.available_at) > now:
                continue
            if not force and candidate.next_attempt_at is not None and _as_utc(
                candidate.next_attempt_at
            ) > now:
                continue
            eligible.append(candidate)
        source_priority = {
            CandidateSourceKind.ALERT: 4,
            CandidateSourceKind.CONTENT: 3,
            CandidateSourceKind.RSS: 3,
            CandidateSourceKind.CONTEXT: 2,
            CandidateSourceKind.DRIFT_AGGREGATION: 1,
            CandidateSourceKind.DRIFT_CONVERSATION: 1,
        }
        eligible.sort(
            key=lambda item: (
                source_priority[item.source_kind],
                _as_utc(item.published_at)
                if item.published_at is not None
                else _as_utc(item.created_at),
            ),
            reverse=True,
        )
        return eligible[: self.settings.proactive.max_candidates_per_tick]

    async def _gate_reason(
        self,
        policy: EngagementPolicy,
        now: datetime,
        *,
        has_candidates: bool,
        force: bool,
    ) -> str:
        if not policy.proactive_enabled:
            return "disabled"
        if not has_candidates:
            return "no_candidates"
        if await self.store.list_pending_messages(policy.session_id):
            return "pending_user_message"
        if force:
            return ""
        zone = ZoneInfo(policy.timezone)
        local = now.astimezone(zone)
        if self._in_quiet_hours(local.time(), policy.quiet_start, policy.quiet_end):
            return "quiet_hours"
        last_sent = await self.store.last_sent_proactive_run(policy.session_id)
        if (
            last_sent is not None
            and now - _as_utc(last_sent.updated_at)
            < timedelta(minutes=policy.minimum_interval_minutes)
        ):
            return "cooldown"
        return ""

    @staticmethod
    def _in_quiet_hours(
        current: time, quiet_start: str, quiet_end: str
    ) -> bool:
        start = time.fromisoformat(quiet_start)
        end = time.fromisoformat(quiet_end)
        if start == end:
            return True
        if start < end:
            return start <= current < end
        return current >= start or current < end

    async def _post_prepare_gate_reason(self, run: ProactiveRun) -> str:
        """发送前基于最新权威状态重新门控，不信任模型前快照。"""

        policy = await self.get_policy(run.session_id)
        if not policy.proactive_enabled:
            return "disabled"
        current_history = await self.store.list_recallable_messages(
            run.session_id, 20
        )
        current_last_user = next(
            (
                item
                for item in reversed(current_history)
                if item.role == MessageRole.USER
            ),
            None,
        )
        if (
            current_last_user.id if current_last_user else None
        ) != run.snapshot_message_id:
            return "new_user_message"
        if await self.store.list_pending_messages(run.session_id):
            return "pending_user_message"
        if run.manual_triggered:
            return ""
        return await self._gate_reason(
            policy,
            utc_now(),
            has_candidates=True,
            force=False,
        )

    async def _drop_prepared_proactive(
        self,
        run: ProactiveRun,
        candidate: ProactiveCandidate,
        message: OutboundMessage,
        gate_reason: str,
    ) -> None:
        """在不可逆发送前撤销准备态，并保留未来重新判断机会。"""

        explanations = {
            "disabled": "主动触达已关闭",
            "new_user_message": "判断期间出现了新的用户消息",
            "pending_user_message": "存在尚未处理的用户消息",
            "quiet_hours": "进入免打扰时段",
            "cooldown": "尚未达到主动触达最小间隔",
        }
        run.status = ProactiveRunStatus.GATED
        run.stage = ProactiveStage.FINISHED
        run.gate_reason = gate_reason
        run.decision_code = gate_reason
        run.decision_reason = explanations.get(gate_reason, gate_reason)
        run.updated_at = utc_now()
        self._defer_candidate(candidate, gate_reason, minutes=30)
        receipt = DeliveryReceipt(
            outbound_id=message.id,
            status=DeliveryStatus.DROPPED,
            error_code=gate_reason,
            error_message=run.decision_reason,
        )
        await self.store.drop_prepared_proactive_delivery(
            run, candidate, message, receipt
        )
        await self._publish_proactive_state(run, [candidate])
        await self._try_publish_delivery(receipt)

    async def _deliver_prepared_proactive(
        self,
        session: SessionView,
        run: ProactiveRun,
        candidate: ProactiveCandidate,
        message: OutboundMessage,
    ) -> None:
        """推进唯一不可逆发送 owner，并把结果收敛到主动终态。"""

        run.stage = ProactiveStage.DELIVERING
        run.updated_at = utc_now()
        await self.store.save_proactive_run(run)
        await self._try_publish_run(run)
        receipt, committed = await self.outbound.deliver(session, message)
        await self._settle_proactive_delivery(
            session, run, candidate, receipt, committed
        )

    async def _settle_proactive_delivery(
        self,
        session: SessionView,
        run: ProactiveRun,
        candidate: ProactiveCandidate,
        receipt: DeliveryReceipt,
        committed: StoredMessage | None,
    ) -> None:
        """原子提交运行与候选终态；仅 SENT 产生长期记忆。"""

        run.stage = ProactiveStage.FINISHED
        run.updated_at = utc_now()
        candidate.updated_at = run.updated_at
        if receipt.status == DeliveryStatus.SENT and committed is not None:
            run.status = ProactiveRunStatus.SENT
            run.error_code = None
            run.error_message = None
            candidate.status = ProactiveCandidateStatus.SENT
            candidate.decision_reason = run.decision_reason
        elif receipt.status == DeliveryStatus.UNKNOWN:
            run.status = ProactiveRunStatus.UNKNOWN
            run.error_code = receipt.error_code
            run.error_message = receipt.error_message
            candidate.status = ProactiveCandidateStatus.FAILED
            candidate.decision_reason = "投递结果不确定，禁止自动重发"
            candidate.next_attempt_at = None
        else:
            run.status = ProactiveRunStatus.FAILED
            run.error_code = receipt.error_code or "delivery_failed"
            run.error_message = receipt.error_message
            self._defer_candidate(
                candidate,
                run.error_code,
                minutes=60,
                increment_attempt=True,
            )
        await self.store.save_proactive_run_with_candidates(run, [candidate])
        await self._publish_proactive_state(run, [candidate])
        if run.status == ProactiveRunStatus.SENT and committed is not None:
            await self._record_proactive_memory(
                session, run, candidate, committed
            )

    async def _record_proactive_memory(
        self,
        session: SessionView,
        run: ProactiveRun,
        candidate: ProactiveCandidate,
        committed: StoredMessage,
    ) -> None:
        """幂等记录已发送主动消息的长期记忆。"""

        await self.memory.try_record_chain_outcome(
            session=session,
            source_chain=MemorySourceChain.PROACTIVE,
            source_run_id=run.id,
            content=f"主动分享了「{candidate.title}」：{candidate.summary}",
            source_message_ids=[committed.id],
            source_refs=[
                f"proactive_run:{run.id}",
                candidate.id,
                *candidate.source_refs,
            ],
            importance=0.35,
        )

    async def _fail_proactive(
        self,
        run: ProactiveRun,
        candidates: list[ProactiveCandidate],
        exc: Exception,
    ) -> ProactiveRun:
        run.status = ProactiveRunStatus.FAILED
        run.stage = ProactiveStage.FINISHED
        run.error_code = getattr(exc, "code", "proactive_model_failed")
        run.error_message = str(exc)[:500]
        run.updated_at = utc_now()
        for candidate in candidates:
            self._defer_candidate(
                candidate,
                run.error_code or "proactive_model_failed",
                minutes=60,
                increment_attempt=True,
            )
        await self.store.save_proactive_run_with_candidates(run, candidates)
        await self._publish_proactive_state(run, candidates)
        return run

    @staticmethod
    def _defer_candidate(
        candidate: ProactiveCandidate,
        reason: str,
        *,
        minutes: int,
        increment_attempt: bool = False,
    ) -> None:
        """计算单个候选的有界退避终态，持久化由调用方统一拥有。"""

        now = utc_now()
        if increment_attempt:
            candidate.attempt_count += 1
        candidate.status = (
            ProactiveCandidateStatus.FAILED
            if candidate.attempt_count >= 3
            else ProactiveCandidateStatus.DEFERRED
        )
        candidate.decision_reason = reason
        candidate.next_attempt_at = (
            None
            if candidate.status == ProactiveCandidateStatus.FAILED
            else now + timedelta(minutes=minutes)
        )
        candidate.updated_at = now

    async def _defer_candidates(
        self,
        candidates: list[ProactiveCandidate],
        reason: str,
        *,
        minutes: int,
        increment_attempt: bool = False,
    ) -> None:
        for candidate in candidates:
            self._defer_candidate(
                candidate,
                reason,
                minutes=minutes,
                increment_attempt=increment_attempt,
            )
            await self.store.save_proactive_candidate(candidate)
            await self._try_publish_candidate(candidate)

    def _validate_drift_candidate(
        self,
        run: DriftRun,
        activity_name: str,
        activity: DriftActivityPayload,
        skipped: list[ProactiveCandidate],
        history: list[StoredMessage],
        facts: list,
        memories: list[MemoryRecord],
    ) -> ProactiveCandidate:
        now = utc_now()
        if activity_name == "candidate_aggregation":
            allowed = {item.id for item in skipped}
            parents = set(activity.parent_candidate_ids)
            if len(parents) < 2 or not parents <= allowed:
                raise InvalidModelResponseError(
                    "Drift 聚合引用了无效或不足两个父候选"
                )
            selected = [item for item in skipped if item.id in parents]
            evidence_refs = set(activity.evidence_refs)
            allowed_refs = {ref for item in selected for ref in item.source_refs}
            if (
                not evidence_refs
                or not evidence_refs <= allowed_refs
                or any(not evidence_refs.intersection(item.source_refs) for item in selected)
            ):
                raise InvalidModelResponseError(
                    "Drift 聚合必须为每个父候选引用至少一个有效来源"
                )
            aggregation_key = hashlib.sha256(
                "\x1f".join(sorted(parents)).encode()
            ).hexdigest()
            return ProactiveCandidate(
                session_id=run.session_id,
                source_kind=CandidateSourceKind.DRIFT_AGGREGATION,
                source_id=run.id,
                source_key=aggregation_key,
                title=activity.title,
                summary=activity.summary,
                source_refs=sorted(evidence_refs),
                parent_candidate_ids=sorted(parents),
                aggregation_key=aggregation_key,
                available_at=now + timedelta(minutes=1),
                expires_at=now
                + timedelta(days=self.settings.proactive.candidate_retention_days),
            )
        if activity.parent_candidate_ids:
            raise InvalidModelResponseError("Drift 对话话题不得引用父候选")
        allowed_refs = (
            {item.id for item in history}
            | {fact.id for fact in facts}
            | {memory.id for memory in memories}
        )
        evidence_refs = set(activity.evidence_refs)
        if not evidence_refs or not evidence_refs <= allowed_refs:
            raise InvalidModelResponseError("Drift 话题候选缺少有效证据引用")
        # 同一证据快照只准备一个话题，避免模型改写标题后绕过去重。
        source_key = hashlib.sha256(
            json.dumps(
                {
                    "activity": "conversation_topic_preparation",
                    "evidence": sorted(evidence_refs),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return ProactiveCandidate(
            session_id=run.session_id,
            source_kind=CandidateSourceKind.DRIFT_CONVERSATION,
            source_id=run.id,
            source_key=source_key,
            title=activity.title,
            summary=activity.summary,
            source_refs=sorted(evidence_refs),
            available_at=now + timedelta(minutes=1),
            expires_at=now
            + timedelta(days=self.settings.proactive.candidate_retention_days),
        )

    async def _require_session(self, session_id: str):
        session = await self.store.get_session(session_id)
        if session is None:
            raise NotFoundError("会话不存在")
        return session

    async def _require_private(self, session_id: str):
        session = await self._require_session(session_id)
        if session.chat_type != ChatType.PRIVATE:
            raise InputValidationError("该能力仅支持私聊")
        return session

    async def _require_feed(self, feed_id: str) -> FeedSource:
        source = await self.store.get_feed_source(feed_id)
        if source is None:
            raise NotFoundError("订阅源不存在")
        return source

    async def _publish_candidate(self, candidate: ProactiveCandidate) -> None:
        await self.events.publish(
            "proactive.candidate.updated", candidate.model_dump(mode="json")
        )

    async def _publish_run(self, run: ProactiveRun) -> None:
        await self.events.publish(
            "proactive.run.updated", run.model_dump(mode="json")
        )

    async def _try_publish_run(self, run: ProactiveRun) -> None:
        """主动运行事件是派生通知，不反向破坏已提交的权威状态。"""

        try:
            await self._publish_run(run)
        except Exception:
            logger.exception(
                "主动运行事件发布失败",
                extra={
                    "session_id": run.session_id,
                    "proactive_run_id": run.id,
                },
            )

    async def _try_publish_delivery(
        self, receipt: DeliveryReceipt
    ) -> None:
        """尽力发布统一投递状态；数据库记录仍是权威事实。"""

        try:
            await self.events.publish(
                "delivery.updated", receipt.model_dump(mode="json")
            )
        except Exception:
            logger.exception(
                "主动投递事件发布失败",
                extra={"outbound_id": receipt.outbound_id},
            )

    async def _publish_proactive_state(
        self,
        run: ProactiveRun,
        candidates: list[ProactiveCandidate],
    ) -> None:
        """提交后发布主动运行和候选投影，单个通知失败不影响其他通知。"""

        for candidate in candidates:
            await self._try_publish_candidate(candidate)
        await self._try_publish_run(run)

    async def _publish_drift(self, run: DriftRun) -> None:
        await self.events.publish(
            "drift.run.updated", run.model_dump(mode="json")
        )

    async def _try_publish_drift(self, run: DriftRun) -> None:
        """事件投影失败不得反向改写已经提交的 Drift 权威状态。"""

        try:
            await self._publish_drift(run)
        except Exception:
            logger.exception(
                "Drift 运行事件发布失败",
                extra={"session_id": run.session_id, "drift_run_id": run.id},
            )

    async def _try_publish_candidate(
        self, candidate: ProactiveCandidate
    ) -> None:
        """候选事件是派生通知，数据库提交成功后不因通知失败回滚语义。"""

        try:
            await self._publish_candidate(candidate)
        except Exception:
            logger.exception(
                "Drift 候选事件发布失败",
                extra={
                    "session_id": candidate.session_id,
                    "candidate_id": candidate.id,
                },
            )
