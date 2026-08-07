"""API 边界请求模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from config import (
    DetectionModelSettings,
    EmbeddingSettings,
    ImageModelSettings,
    ModelSettings,
    VisionModelSettings,
)
from domain.models import (
    CandidateSourceKind,
    ChatType,
    GroupParticipationMode,
    MemoryKind,
    MessageComponent,
    Participant,
)


class CreateSessionRequest(BaseModel):
    chat_type: ChatType
    display_name: str = Field(min_length=1, max_length=200)
    external_chat_id: str = Field(min_length=1, max_length=300)
    participants: list[Participant] = Field(min_length=1, max_length=100)


class SendMessageRequest(BaseModel):
    sender_id: str = Field(min_length=1, max_length=200)
    sender_name: str = Field(min_length=1, max_length=100)
    components: list[MessageComponent] = Field(min_length=1, max_length=30)
    external_message_id: str | None = Field(default=None, max_length=300)


class SessionMembersUpdateRequest(BaseModel):
    participants: list[Participant] = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)


class ScheduleUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, max_length=4000)
    source_text: str = Field(min_length=1, max_length=4000)
    timezone: str = Field(min_length=1, max_length=100)
    dtstart: datetime
    rrule: str = Field(min_length=1, max_length=1000)
    expected_revision: int = Field(ge=1)


class RevisionRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class DeleteAllLocalUserDataRequest(BaseModel):
    """高风险全局删除必须提交完整中文确认词。"""

    confirmation: str = Field(min_length=1, max_length=100)


class EngagementPolicyUpdateRequest(BaseModel):
    proactive_enabled: bool
    drift_enabled: bool
    timezone: str = Field(min_length=1, max_length=100)
    quiet_start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    quiet_end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    minimum_interval_minutes: int = Field(ge=5, le=10080)


class GroupParticipationPolicyUpdateRequest(BaseModel):
    """群聊普通发言策略更新；运行态字段不允许由 UI 写入。"""

    mode: GroupParticipationMode
    trigger_count: int = Field(ge=1, le=999999)
    frequency_factor: float = Field(ge=0, le=1)
    cooldown_seconds: int = Field(ge=0, le=3600)
    expected_revision: int = Field(ge=1)


class OneBotGroupAccessConfig(BaseModel):
    """WebUI 可编辑的单个 OneBot 群准入规则。"""

    group_id: str = Field(min_length=1, max_length=50)
    require_at: bool = True
    allow_from: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("group_id")
    @classmethod
    def normalize_group_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("群号不能为空")
        return normalized

    @field_validator("allow_from")
    @classmethod
    def normalize_allow_from(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        return list(dict.fromkeys(normalized))


class OneBotGroupAccessUpdateRequest(BaseModel):
    """OneBot 群准入白名单整体替换请求。"""

    groups: list[OneBotGroupAccessConfig] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_unique_groups(self) -> OneBotGroupAccessUpdateRequest:
        group_ids = [group.group_id for group in self.groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("OneBot 群配置不得包含重复群号")
        return self


class MemoryCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    kind: MemoryKind
    subject_id: str | None = Field(default=None, max_length=200)
    importance: float = Field(default=0.8, ge=0, le=1)


class MemoryForgetRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class MemoryCorrectRequest(MemoryForgetRequest):
    corrected_content: str = Field(min_length=1, max_length=4000)


class FeedCreateRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=200)
    poll_interval_minutes: int = Field(default=30, ge=15, le=1440)


class FeedUpdateRequest(FeedCreateRequest):
    enabled: bool = True


class ProactiveCandidateCreateRequest(BaseModel):
    """由受信任本地控制面注入的类型化主动来源事件。"""

    source_kind: CandidateSourceKind
    source_key: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=4000)
    url: str = Field(default="", max_length=2000)
    source_ref: str = Field(min_length=1, max_length=500)
    published_at: datetime | None = None


class ExpressionRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class BlacklistCreateRequest(BaseModel):
    """控制台手动拉黑某用户的请求；source 固定为 manual。"""

    platform: str = Field(min_length=1, max_length=50)
    account_id: str = Field(min_length=1, max_length=200)
    external_user_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)
    session_id: str | None = Field(default=None, max_length=80)


class PersonaUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    persona_prompt: str = Field(min_length=1, max_length=20_000)
    expected_revision: int = Field(ge=1)


class PersonaAssignmentsUpdateRequest(BaseModel):
    """WebUI 对私聊、群聊人格的完整指派。"""

    private: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    group: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class ModelConfigUpdateRequest(BaseModel):
    """控制台可修改的模型字段；密钥留空时保留已保存的本机密钥。"""

    mode: str = Field(pattern="^(fake|openai)$")
    protocol: str = Field(
        default="openai_chat",
        pattern="^(openai_chat|openai_responses|anthropic_messages)$",
    )
    base_url: str = Field(min_length=1, max_length=2000)
    name: str = Field(max_length=300)
    profile_protocol: str | None = Field(
        default=None,
        pattern="^(openai_chat|openai_responses|anthropic_messages)$",
    )
    profile_base_url: str = Field(default="", max_length=2000)
    profile_name: str = Field(default="", max_length=300)
    api_key: str | None = Field(default=None, min_length=1, max_length=10000)
    profile_api_key: str | None = Field(default=None, min_length=1, max_length=10000)
    clear_api_key: bool = False
    clear_profile_api_key: bool = False
    supports_json_object: bool = True
    supports_tools: bool = True
    supports_vision: bool = False
    supports_streaming: bool = False

    def merged_model_settings(self, current: ModelSettings) -> ModelSettings:
        """只用显式提交的密钥覆盖本地值，防止环境变量密钥被复制到文件。"""

        values = current.model_dump()
        values.update(
            self.model_dump(
                exclude={
                    "api_key",
                    "profile_api_key",
                    "clear_api_key",
                    "clear_profile_api_key",
                }
            )
        )
        if self.clear_api_key:
            values["api_key"] = ""
        elif self.api_key is not None:
            values["api_key"] = self.api_key
        if self.clear_profile_api_key:
            values["profile_api_key"] = ""
        elif self.profile_api_key is not None:
            values["profile_api_key"] = self.profile_api_key
        return ModelSettings.model_validate(values)


class ImageModelConfigUpdateRequest(BaseModel):
    """可选图片模型配置；密钥留空时保留已保存值。"""

    enabled: bool = False
    base_url: str = Field(min_length=1, max_length=2000)
    name: str = Field(max_length=300)
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    api_key: str | None = Field(default=None, min_length=1, max_length=10000)
    clear_api_key: bool = False

    def merged_image_model_settings(
        self, current: ImageModelSettings
    ) -> ImageModelSettings:
        """只用显式提交的密钥覆盖本地图片模型凭据。"""

        values = current.model_dump()
        values.update(self.model_dump(exclude={"api_key", "clear_api_key"}))
        if self.clear_api_key:
            values["api_key"] = ""
        elif self.api_key is not None:
            values["api_key"] = self.api_key
        return ImageModelSettings.model_validate(values)


class VisionModelConfigUpdateRequest(BaseModel):
    """视觉理解模型配置；main 模式不保存或要求独立凭据。"""

    mode: str = Field(default="main", pattern="^(main|external)$")
    protocol: str = Field(
        default="openai_chat",
        pattern="^(openai_chat|openai_responses|anthropic_messages)$",
    )
    base_url: str = Field(min_length=1, max_length=2000)
    name: str = Field(default="", max_length=300)
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    wait_seconds: float = Field(default=5.0, ge=0, le=30)
    api_key: str | None = Field(default=None, min_length=1, max_length=10000)
    clear_api_key: bool = False

    def merged_vision_model_settings(
        self, current: VisionModelSettings
    ) -> VisionModelSettings:
        values = current.model_dump()
        values.update(self.model_dump(exclude={"api_key", "clear_api_key"}))
        if self.clear_api_key:
            values["api_key"] = ""
        elif self.api_key is not None:
            values["api_key"] = self.api_key
        return VisionModelSettings.model_validate(values)


class DetectionModelConfigUpdateRequest(BaseModel):
    """可选检测模型配置；密钥留空时保留已保存值。"""

    enabled: bool = False
    base_url: str = Field(min_length=1, max_length=2000)
    name: str = Field(max_length=300)
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    api_key: str | None = Field(default=None, min_length=1, max_length=10000)
    clear_api_key: bool = False

    def merged_detection_model_settings(
        self, current: DetectionModelSettings
    ) -> DetectionModelSettings:
        """只用显式提交的密钥覆盖本地检测模型凭据。"""

        values = current.model_dump()
        values.update(self.model_dump(exclude={"api_key", "clear_api_key"}))
        if self.clear_api_key:
            values["api_key"] = ""
        elif self.api_key is not None:
            values["api_key"] = self.api_key
        return DetectionModelSettings.model_validate(values)


class EmbeddingConfigUpdateRequest(BaseModel):
    """统一 embedding 配置；凭据和模型名齐全后才允许发送文本。"""

    enabled: bool = True
    base_url: str = Field(min_length=1, max_length=2000)
    name: str = Field(max_length=300)
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    api_key: str | None = Field(default=None, min_length=1, max_length=10000)
    clear_api_key: bool = False

    def merged_embedding_settings(self, current: EmbeddingSettings) -> EmbeddingSettings:
        values = current.model_dump()
        values.update(self.model_dump(exclude={"api_key", "clear_api_key"}))
        if self.clear_api_key:
            values["api_key"] = ""
        elif self.api_key is not None:
            values["api_key"] = self.api_key
        return EmbeddingSettings.model_validate(values)
