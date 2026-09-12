"""聊天、会话、出站与画像的唯一领域协议。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    """返回带时区的 UTC 当前时间。"""

    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """生成适合日志定位的内部 ID。"""

    return f"{prefix}_{uuid4().hex}"


class ChatType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"


class ParticipantRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class ComponentType(StrEnum):
    TEXT = "text"
    MENTION = "mention"
    QUOTE = "quote"
    IMAGE_REF = "image_ref"
    AUDIO_REF = "audio_ref"
    FILE_REF = "file_ref"


class ExpressionSourceKind(StrEnum):
    """表情素材进入图库的来源。"""

    GENERATED = "generated"
    UPLOADED = "uploaded"
    COLLECTED = "collected"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageOrigin(StrEnum):
    """可见消息或出站尝试来自哪条执行链。"""

    REACTIVE = "reactive"
    SCHEDULED = "scheduled"
    PROACTIVE = "proactive"
    UNKNOWN = "unknown"


class DecisionAction(StrEnum):
    REPLY = "reply"
    SILENCE = "silence"


class GroupParticipationMode(StrEnum):
    """群聊普通发言参与模式；@/引用等硬门不受其关闭。"""

    SILENT = "silent"
    NORMAL = "normal"
    FOCUSED = "focused"


class ReplyMode(StrEnum):
    """Planner 可指定、Replyer 只能执行的有限回复形态。"""

    DIRECT_ANSWER = "direct_answer"
    ACKNOWLEDGE_AND_CONTINUE = "acknowledge_and_continue"
    GROUP_FORCED_REPLY = "group_forced_reply"
    GROUP_CONCISE_PARTICIPATION = "group_concise_participation"


class ReplyTargetReason(StrEnum):
    """Planner 选择目标消息的可审计原因。"""

    DIRECT_MENTION = "direct_mention"
    DIRECT_QUOTE = "direct_quote"
    QUESTION = "question"
    REQUEST = "request"
    LATEST_PENDING = "latest_pending"


class ReplyEvidenceMode(StrEnum):
    """本轮回答对历史和实时证据的依赖。"""

    CONVERSATION_ONLY = "conversation_only"
    ADAPTIVE = "adaptive"
    HISTORY_SOURCE_REQUIRED = "history_source_required"
    FRESH_TOOL_REQUIRED = "fresh_tool_required"
    HISTORY_AND_FRESH_TOOL_REQUIRED = "history_and_fresh_tool_required"


class DeliveryStatus(StrEnum):
    BLOCKED = "blocked"
    PREPARED = "prepared"
    DISPATCHING = "dispatching"
    SENT = "sent"
    FAILED = "failed"
    DROPPED = "dropped"
    UNKNOWN = "unknown"


class FactStatus(StrEnum):
    ACTIVE = "active"
    CONFLICTED = "conflicted"
    RETRACTED = "retracted"


class MemoryKind(StrEnum):
    """长期记忆的语义类别，决定检索加权而非权限。"""

    PROFILE = "profile"
    PREFERENCE = "preference"
    EVENT = "event"
    EPISODE = "episode"
    COMMITMENT = "commitment"
    RELATIONSHIP = "relationship"
    PROCEDURE = "procedure"
    SUMMARY = "summary"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    CONFLICTED = "conflicted"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class MemorySourceChain(StrEnum):
    """产生记忆的链路；manual/imported 不代表可见消息来源。"""

    REACTIVE = "reactive"
    SCHEDULED = "scheduled"
    PROACTIVE = "proactive"
    DRIFT = "drift"
    MANUAL = "manual"
    IMPORTED = "imported"


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"


class LearnedItemStatus(StrEnum):
    """独立学习库条目的生命周期。"""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    REJECTED = "rejected"
    DISABLED = "disabled"


class BehaviorActorType(StrEnum):
    """行为模式的实际主体。"""

    OTHER_USER = "other_user"
    GROUP_COLLECTIVE = "group_collective"
    AGENT_SELF = "agent_self"
    UNKNOWN = "unknown"


class BehaviorLearningType(StrEnum):
    """行为经验来自观察还是自身反馈。"""

    OBSERVED = "observed_behavior"
    SELF_REFLECTION = "self_reflection"


class BehaviorSelectionStatus(StrEnum):
    """一次行为候选选择的反馈状态。"""

    PENDING = "pending"
    EVALUATED = "evaluated"
    EXPIRED = "expired"


class ScheduleStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    DELETED = "deleted"


class ScheduleRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ToolExecutionStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ProactiveCandidateStatus(StrEnum):
    PENDING = "pending"
    DEFERRED = "deferred"
    PREPARED = "prepared"
    SKIPPED = "skipped"
    SENT = "sent"
    EXPIRED = "expired"
    FAILED = "failed"


class ProactiveRunStatus(StrEnum):
    RUNNING = "running"
    GATED = "gated"
    SKIPPED = "skipped"
    PREPARED = "prepared"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ProactiveStage(StrEnum):
    """主动链持久化阶段；不可逆发送仍由 DeliveryStatus 独立拥有。"""

    GATING = "gating"
    JUDGING = "judging"
    COMPOSING = "composing"
    PREPARING = "preparing"
    PREPARED = "prepared"
    DELIVERING = "delivering"
    FINISHED = "finished"


class DriftRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    GATED = "gated"
    PAUSED = "paused"


class DriftStage(StrEnum):
    """Drift 的持久化阶段；重启只能从已提交的阶段继续。"""

    SELECTING = "selecting"
    EXECUTING = "executing"
    COMMITTING = "committing"
    FINISHED = "finished"


class CandidateSourceKind(StrEnum):
    ALERT = "alert"
    CONTENT = "content"
    CONTEXT = "context"
    RSS = "rss"
    DRIFT_AGGREGATION = "drift_aggregation"
    DRIFT_CONVERSATION = "drift_conversation"


class MessageComponent(BaseModel):
    """统一消息组件；不同类型只允许使用对应字段。"""

    type: ComponentType
    text: str | None = None
    target_id: str | None = None
    target_name: str | None = None
    message_id: str | None = None
    attachment_id: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    storage_path: str | None = None
    description: str | None = None
    is_expression: bool = False

    @model_validator(mode="after")
    def validate_by_type(self) -> MessageComponent:
        if self.is_expression and self.type != ComponentType.IMAGE_REF:
            raise ValueError("只有受控图片引用可以标记为表情包")
        if self.type == ComponentType.TEXT and not (self.text or "").strip():
            raise ValueError("文本组件不能为空")
        if self.type == ComponentType.MENTION and not (self.target_id or "").strip():
            raise ValueError("@ 组件必须包含 target_id")
        if self.type == ComponentType.QUOTE and not (self.message_id or "").strip():
            raise ValueError("引用组件必须包含 message_id")
        if self.type in {
            ComponentType.IMAGE_REF,
            ComponentType.AUDIO_REF,
            ComponentType.FILE_REF,
        }:
            required = [self.attachment_id, self.filename, self.mime_type, self.sha256, self.storage_path]
            if not all(str(value or "").strip() for value in required) or self.size is None:
                label = {
                    ComponentType.IMAGE_REF: "图片",
                    ComponentType.AUDIO_REF: "语音",
                    ComponentType.FILE_REF: "文件",
                }[self.type]
                raise ValueError(f"{label}引用缺少已验证的元数据")
        if self.type == ComponentType.IMAGE_REF and not (self.mime_type or "").startswith("image/"):
            raise ValueError("图片引用必须使用 image MIME")
        if self.type == ComponentType.AUDIO_REF and not (self.mime_type or "").startswith("audio/"):
            raise ValueError("语音引用必须使用 audio MIME")
        return self

    @classmethod
    def text_component(cls, text: str) -> MessageComponent:
        return cls(type=ComponentType.TEXT, text=text)


class Participant(BaseModel):
    """平台侧聊天参与者。"""

    external_user_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=100)
    role: ParticipantRole = ParticipantRole.MEMBER


class InboundMessage(BaseModel):
    """Channel 交给核心的规范化入站消息。"""

    platform: str = Field(min_length=1, max_length=50)
    account_id: str = Field(min_length=1, max_length=200)
    external_message_id: str = Field(min_length=1, max_length=300)
    external_chat_id: str = Field(min_length=1, max_length=300)
    sender_id: str = Field(min_length=1, max_length=200)
    sender_name: str = Field(min_length=1, max_length=100)
    chat_type: ChatType
    components: list[MessageComponent] = Field(min_length=1, max_length=30)
    received_at: datetime = Field(default_factory=utc_now)

    @property
    def plain_text(self) -> str:
        parts: list[str] = []
        for component in self.components:
            if component.type == ComponentType.TEXT:
                parts.append(component.text or "")
            elif component.type == ComponentType.MENTION:
                parts.append(f"@{component.target_name or component.target_id}")
            elif component.type == ComponentType.IMAGE_REF:
                parts.append(component.description or f"[图片:{component.filename}]")
            elif component.type == ComponentType.AUDIO_REF:
                parts.append(component.description or f"[语音:{component.filename}]")
            elif component.type == ComponentType.FILE_REF:
                parts.append(component.description or f"[文件:{component.filename}]")
        return " ".join(part.strip() for part in parts if part.strip()).strip()


class SessionView(BaseModel):
    """对 API 暴露的会话快照。"""

    id: str
    platform: str
    account_id: str
    external_chat_id: str
    chat_type: ChatType
    display_name: str
    participants: list[Participant] = Field(default_factory=list)
    revision: int = Field(default=1, ge=1)
    data_epoch: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime


class StoredMessage(BaseModel):
    """已提交到权威历史的消息。"""

    id: str
    session_id: str
    role: MessageRole
    sender_id: str
    sender_name: str
    external_message_id: str | None = None
    components: list[MessageComponent]
    created_at: datetime
    processed_turn_id: str | None = None
    origin: MessageOrigin = MessageOrigin.UNKNOWN
    origin_run_id: str | None = None
    source_refs: list[str] = Field(default_factory=list)

    @property
    def plain_text(self) -> str:
        return InboundMessage(
            platform="internal",
            account_id="internal",
            external_message_id=self.id,
            external_chat_id=self.session_id,
            sender_id=self.sender_id,
            sender_name=self.sender_name,
            chat_type=ChatType.PRIVATE,
            components=self.components,
            received_at=self.created_at,
        ).plain_text


class TurnDecision(BaseModel):
    """策略层对一批待处理消息的显式判定。"""

    id: str = Field(default_factory=lambda: new_id("turn"))
    session_id: str
    action: DecisionAction
    strategy: str
    score: int = Field(ge=0, le=100)
    threshold: int = Field(ge=0, le=100)
    reason: str
    score_detail: dict[str, Any] = Field(default_factory=dict)
    trigger_message_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class GroupParticipationPolicy(BaseModel):
    """每个群 Session 的参与配置与可恢复 idle 状态。"""

    session_id: str
    mode: GroupParticipationMode = GroupParticipationMode.NORMAL
    # 触发条数控制积压压力，评分倍率控制同等必要性下的发言倾向；二者必须独立，
    # 避免一个“频率”滑杆同时改变两种并不直观的行为。
    trigger_count: int = Field(default=3, ge=1, le=999999)
    frequency_factor: float = Field(default=0.9, ge=0, le=1)
    cooldown_seconds: int = Field(default=60, ge=0, le=3600)
    idle_streak: int = Field(default=0, ge=0)
    idle_backoff_until: datetime | None = None
    last_ordinary_reply_at: datetime | None = None
    last_external_message_at: datetime | None = None
    external_interval_ewma_seconds: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    external_interval_sample_count: int = Field(default=0, ge=0)
    revision: int = Field(default=1, ge=1)
    state_version: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_external_interval_state(self) -> GroupParticipationPolicy:
        """EWMA 必须和样本数同时存在或同时为空。"""

        if (self.external_interval_sample_count == 0) != (
            self.external_interval_ewma_seconds is None
        ):
            raise ValueError("外部消息间隔 EWMA 与样本数状态不一致")
        return self


class ReplyPlan(BaseModel):
    """Planner 冻结后交给 Replyer 的唯一生成契约。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    objective: str = Field(min_length=1, max_length=300)
    response_mode: ReplyMode
    target_message_id: str = Field(min_length=1, max_length=80)
    address_sender_id: str = Field(min_length=1, max_length=200)
    relevant_message_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    target_reason: ReplyTargetReason
    evidence_mode: ReplyEvidenceMode
    evidence_policy: str = Field(min_length=1, max_length=400)
    preferred_tools: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    ask_follow_up: bool = False
    max_visible_messages: int = Field(default=1, ge=1, le=6)
    max_total_chars: int = Field(default=4000, ge=1, le=20_000)

    @model_validator(mode="after")
    def validate_target_snapshot(self) -> ReplyPlan:
        """保证回复目标属于 Planner 冻结的相关消息集合。"""

        if self.target_message_id not in self.relevant_message_ids:
            raise ValueError("目标消息必须包含在相关消息快照中")
        if len(set(self.relevant_message_ids)) != len(self.relevant_message_ids):
            raise ValueError("相关消息快照不能包含重复 ID")
        if len(set(self.preferred_tools)) != len(self.preferred_tools):
            raise ValueError("建议工具不能重复")
        return self


class OutboundMessage(BaseModel):
    """尚未提交到可见历史的待投递消息。"""

    id: str = Field(default_factory=lambda: new_id("out"))
    session_id: str
    components: list[MessageComponent]
    reply_to_message_id: str | None = None
    origin: MessageOrigin = MessageOrigin.REACTIVE
    origin_run_id: str | None = None
    source_refs: list[str] = Field(default_factory=list, max_length=50)
    created_at: datetime = Field(default_factory=utc_now)


class ReplyDraft(BaseModel):
    """模型或工具生成、但尚未交给 Channel 的一条或多条可见消息。"""

    components: list[MessageComponent] = Field(min_length=1, max_length=2)
    follow_up_components: list[list[MessageComponent]] = Field(
        default_factory=list,
        max_length=5,
    )
    expression_id: str | None = None


class DeliveryReceipt(BaseModel):
    """Channel 对一次不可逆投递尝试的统一回执。"""

    outbound_id: str
    status: DeliveryStatus
    external_message_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    delivered_at: datetime | None = None


class PersonaPortrait(BaseModel):
    """角色基础形象的受控文件引用。"""

    storage_path: str = Field(min_length=1, max_length=2000)
    filename: str = Field(default="base_image.png", min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: str = Field(default="image/png", pattern=r"^image/(jpeg|png|gif|webp)$")
    size: int = Field(gt=0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    aspect_valid: bool = False


class ExpressionAsset(BaseModel):
    """当前角色可复用的表情源素材。"""

    id: str = Field(default_factory=lambda: new_id("expression"))
    character_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=40)
    normalized_name: str = Field(min_length=1, max_length=80)
    emotion: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    source_kind: ExpressionSourceKind = ExpressionSourceKind.GENERATED
    generation_key: str | None = Field(default=None, min_length=1, max_length=100)
    source_portrait_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    storage_path: str = Field(min_length=1, max_length=2000)
    mime_type: str = Field(default="image/png", pattern=r"^image/png$")
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    use_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    last_used_at: datetime | None = None


class ImageAnalysis(BaseModel):
    """按图片内容和模型版本缓存的非权威视觉理解结果。"""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: str = Field(pattern=r"^image/(jpeg|png|gif|webp)$")
    provider_key: str = Field(min_length=1, max_length=500)
    prompt_version: str = Field(default="v1", min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=1000)
    emotions: list[str] = Field(default_factory=list, max_length=12)
    expression_name: str | None = Field(default=None, min_length=1, max_length=40)
    is_expression: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Persona(BaseModel):
    """由身份配置与角色 Prompt 组成的当前人格投影。"""

    character_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    persona_prompt: str = Field(min_length=1, max_length=20_000)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    portrait: PersonaPortrait | None = None
    revision: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("人格名称不能为空")
        if any(ord(character) < 32 for character in stripped):
            raise ValueError("人格名称不得包含换行或控制字符")
        return stripped

    @field_validator("persona_prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("人格提示词不能为空")
        if "\x00" in stripped:
            raise ValueError("人格提示词不得包含空字符")
        return stripped


class ProfileFact(BaseModel):
    """带来源证据和可见域的画像事实；清聊天后来源可被隐私脱钩。"""

    id: str = Field(default_factory=lambda: new_id("fact"))
    subject_id: str
    scope_key: str
    category: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    status: FactStatus = FactStatus.ACTIVE
    source_message_ids: list[str] = Field(default_factory=list, max_length=100)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProfileExtractionRun(BaseModel):
    """可恢复的画像提取任务状态。"""

    id: str = Field(default_factory=lambda: new_id("extract"))
    session_id: str
    subject_id: str
    scope_key: str
    source_message_ids: list[str]
    data_epoch: int = Field(default=1, ge=1)
    status: ExtractionStatus = ExtractionStatus.PENDING
    error_code: str | None = None
    error_message: str | None = None
    attempt_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MemoryRecord(BaseModel):
    """带证据、版本关系与召回统计的长期记忆权威记录。"""

    id: str = Field(default_factory=lambda: new_id("memory"))
    session_id: str
    scope_key: str
    subject_id: str | None = Field(default=None, max_length=200)
    kind: MemoryKind
    content: str = Field(min_length=1, max_length=4000)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confidence: float = Field(default=1.0, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    status: MemoryStatus = MemoryStatus.ACTIVE
    source_chain: MemorySourceChain
    source_run_id: str | None = Field(default=None, max_length=100)
    source_message_ids: list[str] = Field(default_factory=list, max_length=100)
    source_refs: list[str] = Field(default_factory=list, max_length=100)
    supersedes_id: str | None = Field(default=None, max_length=80)
    happened_at: datetime | None = None
    reinforcement: int = Field(default=1, ge=1)
    recall_count: int = Field(default=0, ge=0)
    last_recalled_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MemoryConsolidationRun(BaseModel):
    """一次幂等、可恢复的对话记忆归档任务。"""

    id: str = Field(default_factory=lambda: new_id("memory_run"))
    session_id: str
    scope_key: str
    source_chain: MemorySourceChain
    source_run_id: str | None = Field(default=None, max_length=100)
    source_message_ids: list[str] = Field(min_length=1, max_length=100)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_epoch: int = Field(default=1, ge=1)
    status: ExtractionStatus = ExtractionStatus.PENDING
    produced_memory_ids: list[str] = Field(default_factory=list, max_length=100)
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=500)
    attempt_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class JargonTerm(BaseModel):
    """带语义、证据与置信度演进的 Session 黑话条目。"""

    id: str = Field(default_factory=lambda: new_id("jargon"))
    session_id: str
    term: str = Field(min_length=1, max_length=64)
    normalized_term: str = Field(min_length=1, max_length=128)
    meaning: str = Field(default="", max_length=2000)
    status: LearnedItemStatus = LearnedItemStatus.CANDIDATE
    confidence: float = Field(default=0.0, ge=0, le=1)
    occurrence_count: int = Field(default=1, ge=1)
    inference_count: int = Field(default=0, ge=0)
    last_inference_occurrence_count: int = Field(default=0, ge=0)
    evidence_message_ids: list[str] = Field(default_factory=list, max_length=100)
    last_seen_at: datetime = Field(default_factory=utc_now)
    last_inferred_at: datetime | None = None
    decay_count: int = Field(default=0, ge=0)
    last_maintained_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GroupExpressionPattern(BaseModel):
    """群聊中可复用的“情境→表达方式”记录。"""

    id: str = Field(default_factory=lambda: new_id("group_expression"))
    session_id: str
    situation: str = Field(min_length=1, max_length=200)
    style: str = Field(min_length=1, max_length=200)
    pattern_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: LearnedItemStatus = LearnedItemStatus.ACTIVE
    confidence: float = Field(default=0.6, ge=0, le=1)
    occurrence_count: int = Field(default=1, ge=1)
    selection_count: int = Field(default=0, ge=0)
    evidence_message_ids: list[str] = Field(default_factory=list, max_length=100)
    last_reinforced_at: datetime = Field(default_factory=utc_now)
    last_selected_at: datetime | None = None
    decay_count: int = Field(default=0, ge=0)
    last_maintained_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BehaviorTagKind(StrEnum):
    """行为场景标签的稳定语义维度。"""

    DOMAIN = "domain"
    NEED = "need"
    ATTITUDE = "attitude"


class BehaviorTagGroup(BaseModel):
    """一组语义等价标签；首项是规范名，其余是同义别名。"""

    kind: BehaviorTagKind
    tags: list[str] = Field(min_length=1, max_length=8)


class BehaviorScenarioProfile(BaseModel):
    """行为选择前由当前聊天生成的结构化场景画像。"""

    summary: str = Field(default="", max_length=500)
    tag_groups: list[BehaviorTagGroup] = Field(default_factory=list, max_length=30)
    confidence: float = Field(default=0.0, ge=0, le=1)

    @property
    def has_signal(self) -> bool:
        return bool(self.tag_groups)


class BehaviorPattern(BaseModel):
    """可检索、选择并接收反馈的场景—行为—结果经验。"""

    id: str = Field(default_factory=lambda: new_id("behavior"))
    session_id: str
    scene_summary: str = Field(min_length=1, max_length=500)
    scene_tags: list[str] = Field(default_factory=list, max_length=30)
    need_tags: list[str] = Field(default_factory=list, max_length=10)
    other_traits: list[str] = Field(default_factory=list, max_length=20)
    tag_groups: list[BehaviorTagGroup] = Field(default_factory=list, max_length=30)
    tag_distribution: dict[str, float] = Field(default_factory=dict)
    scene_cluster_id: str | None = Field(default=None, max_length=80)
    action: str = Field(min_length=1, max_length=500)
    expected_outcome: str = Field(min_length=1, max_length=500)
    pattern_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor_type: BehaviorActorType = BehaviorActorType.UNKNOWN
    learning_type: BehaviorLearningType = BehaviorLearningType.OBSERVED
    status: LearnedItemStatus = LearnedItemStatus.ACTIVE
    confidence: float = Field(default=0.6, ge=0, le=1)
    occurrence_count: int = Field(default=1, ge=1)
    activation_count: int = Field(default=0, ge=0)
    success_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    score: float = Field(default=0.0, ge=-5, le=5)
    evidence_message_ids: list[str] = Field(default_factory=list, max_length=100)
    last_reinforced_at: datetime = Field(default_factory=utc_now)
    last_selected_at: datetime | None = None
    last_feedback_at: datetime | None = None
    decay_count: int = Field(default=0, ge=0)
    last_maintained_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BehaviorSelection(BaseModel):
    """一次回复前的行为候选选择，以及回复后的评价归属。"""

    id: str = Field(default_factory=lambda: new_id("behavior_selection"))
    session_id: str
    turn_id: str = Field(min_length=1, max_length=100)
    behavior_id: str = Field(min_length=1, max_length=80)
    scene_summary: str = Field(min_length=1, max_length=500)
    scene_tags: list[str] = Field(default_factory=list, max_length=30)
    status: BehaviorSelectionStatus = BehaviorSelectionStatus.PENDING
    assistant_message_ids: list[str] = Field(default_factory=list, max_length=20)
    feedback_message_ids: list[str] = Field(default_factory=list, max_length=20)
    evaluation_attempts: int = Field(default=0, ge=0, le=10)
    adopted: bool | None = None
    feedback_status: str | None = Field(default=None, max_length=30)
    score_delta: float | None = Field(default=None, ge=-1, le=1)
    outcome: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=500)
    selected_at: datetime = Field(default_factory=utc_now)
    evaluated_at: datetime | None = None


class SocialLearningRun(BaseModel):
    """一次可恢复、幂等的三类社交学习批次。"""

    id: str = Field(default_factory=lambda: new_id("learning_run"))
    session_id: str
    source_run_id: str | None = Field(default=None, max_length=100)
    source_message_ids: list[str] = Field(min_length=1, max_length=200)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_epoch: int = Field(default=1, ge=1)
    status: ExtractionStatus = ExtractionStatus.PENDING
    produced_jargon_ids: list[str] = Field(default_factory=list, max_length=100)
    produced_expression_ids: list[str] = Field(default_factory=list, max_length=100)
    produced_behavior_ids: list[str] = Field(default_factory=list, max_length=100)
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=500)
    attempt_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ScheduleTask(BaseModel):
    """一个 Session 内可恢复、可审计的单次提醒或周期任务。"""

    id: str = Field(default_factory=lambda: new_id("schedule"))
    session_id: str
    created_by: str
    title: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, max_length=4000)
    source_text: str = Field(min_length=1, max_length=4000)
    timezone: str = Field(min_length=1, max_length=100)
    dtstart: datetime
    rrule: str = Field(min_length=1, max_length=1000)
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    revision: int = Field(default=1, ge=1)
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ScheduleRun(BaseModel):
    """调度任务单次计划时刻的执行记录。"""

    id: str = Field(default_factory=lambda: new_id("schedule_run"))
    schedule_id: str
    session_id: str
    scheduled_for: datetime
    status: ScheduleRunStatus = ScheduleRunStatus.PENDING
    missed_occurrences: int = Field(default=0, ge=0)
    outbound_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ToolExecution(BaseModel):
    """模型工具调用的权威审计记录。"""

    id: str = Field(default_factory=lambda: new_id("tool_exec"))
    session_id: str
    tool_call_id: str
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    status: ToolExecutionStatus = ToolExecutionStatus.RUNNING
    turn_id: str | None = None
    schedule_run_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class ModelAttempt(BaseModel):
    """一次实际 Provider 尝试的隐私安全观测，不包含请求或响应正文。"""

    id: str = Field(default_factory=lambda: new_id("model_attempt"))
    invocation_id: str = Field(min_length=1, max_length=80)
    attempt_number: int = Field(default=1, ge=1)
    task: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=100)
    profile: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=300)
    session_id: str | None = Field(default=None, max_length=80)
    turn_id: str | None = Field(default=None, max_length=100)
    run_id: str | None = Field(default=None, max_length=100)
    streamed: bool = False
    tool_call_count: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    usage_source: str = Field(pattern=r"^(provider|unknown)$")
    latency_ms: int = Field(ge=0)
    success: bool
    error_type: str | None = Field(default=None, max_length=100)
    error_code: str | None = Field(default=None, max_length=100)
    cost_microusd: int | None = Field(default=None, ge=0)
    started_at: datetime
    completed_at: datetime


class EngagementPolicy(BaseModel):
    """一个 Session 的主动触达与 Drift 硬门控。"""

    session_id: str
    proactive_enabled: bool
    drift_enabled: bool = True
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)
    quiet_start: str = Field(default="22:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    quiet_end: str = Field(default="08:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    minimum_interval_minutes: int = Field(default=240, ge=5, le=10080)
    updated_at: datetime = Field(default_factory=utc_now)


class FeedSource(BaseModel):
    """当前私聊独占的公开 RSS/Atom 来源。"""

    id: str = Field(default_factory=lambda: new_id("feed"))
    session_id: str
    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=200)
    enabled: bool = True
    poll_interval_minutes: int = Field(default=30, ge=15, le=1440)
    etag: str | None = Field(default=None, max_length=500)
    last_modified: str | None = Field(default=None, max_length=500)
    consecutive_failures: int = Field(default=0, ge=0)
    next_poll_at: datetime | None = None
    last_polled_at: datetime | None = None
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProactiveCandidate(BaseModel):
    """尚未发送的真实来源或 Drift 候选。"""

    id: str = Field(default_factory=lambda: new_id("candidate"))
    session_id: str
    source_kind: CandidateSourceKind
    source_id: str | None = None
    source_key: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=4000)
    url: str = Field(default="", max_length=2000)
    published_at: datetime | None = None
    source_refs: list[str] = Field(default_factory=list, max_length=50)
    parent_candidate_ids: list[str] = Field(default_factory=list, max_length=20)
    aggregation_key: str | None = Field(default=None, max_length=128)
    status: ProactiveCandidateStatus = ProactiveCandidateStatus.PENDING
    decision_reason: str | None = Field(default=None, max_length=500)
    attempt_count: int = Field(default=0, ge=0)
    available_at: datetime = Field(default_factory=utc_now)
    next_attempt_at: datetime | None = None
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProactiveRun(BaseModel):
    """一次候选门控、判断、生成和投递的审计记录。"""

    id: str = Field(default_factory=lambda: new_id("proactive_run"))
    session_id: str
    status: ProactiveRunStatus
    stage: ProactiveStage = ProactiveStage.GATING
    gate_reason: str = Field(default="", max_length=100)
    decision_code: str = Field(default="", max_length=100)
    decision_reason: str = Field(default="", max_length=500)
    score: float | None = Field(default=None, ge=0, le=1)
    candidate_ids: list[str] = Field(default_factory=list, max_length=50)
    candidate_id: str | None = None
    snapshot_at: datetime
    snapshot_message_id: str | None = None
    manual_triggered: bool = False
    outbound_id: str | None = None
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DriftRun(BaseModel):
    """一次无直接出站权限的空闲活动。"""

    id: str = Field(default_factory=lambda: new_id("drift_run"))
    session_id: str
    status: DriftRunStatus = DriftRunStatus.RUNNING
    stage: DriftStage = DriftStage.SELECTING
    activity: str = Field(default="", max_length=100)
    decision_reason: str = Field(default="", max_length=500)
    resume_payload: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    produced_candidate_id: str | None = None
    snapshot_at: datetime = Field(default_factory=utc_now)
    snapshot_message_id: str | None = None
    resumed_from_run_id: str | None = None
    auto_resume_count: int = Field(default=0, ge=0, le=3)
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BlacklistSource(StrEnum):
    """黑名单条目的写入来源，用于区分 Agent 自动拉黑与人工手动拉黑。"""

    AGENT = "agent"
    MANUAL = "manual"


class BlacklistEntry(BaseModel):
    """本地黑名单中不再受理其请求的用户记录。

    维度与 ``IdentityRow`` 一致：同一机器人账号下某平台的某个外部用户。
    拉黑后该用户在任何会话（私聊或群聊）的入站消息都不再受理、不调度 Turn。
    """

    id: str = Field(default_factory=lambda: new_id("block"))
    platform: str = Field(min_length=1, max_length=50)
    account_id: str = Field(min_length=1, max_length=200)
    external_user_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)
    source: BlacklistSource = BlacklistSource.AGENT
    session_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class SessionResolver:
    """会话身份的唯一生成入口，业务模块不得自行拼接会话 ID。"""

    @staticmethod
    def resolve(platform: str, account_id: str, external_chat_id: str, chat_type: ChatType) -> str:
        fields = [platform.strip().lower(), account_id.strip(), chat_type.value, external_chat_id.strip()]
        if not all(fields):
            raise ValueError("会话路由字段不能为空")
        digest = hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()
        return f"session_{digest[:32]}"


def scope_key_for(session: SessionView) -> str:
    """生成严格隔离的画像可见域。"""

    return f"{session.chat_type.value}:{session.id}"
