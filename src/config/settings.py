"""集中加载并校验项目配置。"""

from __future__ import annotations

import os
import re
import secrets as stdlib_secrets
import tomllib
from hashlib import sha256
from hmac import new as new_hmac
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, model_validator

from .secrets import (
    LocalSecretStore,
    persist_secret_settings,
    resolve_secret_references,
)

_CONTROL_TOKEN_REFERENCE = "server.control_token"
_SESSION_CREDENTIAL_REFERENCE = "server.session_credential"


class ServerSettings(BaseModel):
    """本地控制面监听、认证和事件回放边界。"""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    control_token: SecretStr = Field(default_factory=lambda: SecretStr(""))
    # 控制会话凭据由 token 派生但独立持久化，使后端重启后已换发的 Cookie 仍可校验。
    session_credential: SecretStr = Field(default_factory=lambda: SecretStr(""))
    trusted_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost", "::1"],
        min_length=1,
        max_length=32,
    )
    trusted_origins: list[str] = Field(default_factory=list, max_length=32)
    event_history_capacity: int = Field(default=1000, ge=1, le=100_000)
    event_subscriber_queue_capacity: int = Field(default=100, ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_control_plane(self) -> ServerSettings:
        """拒绝公网监听、弱令牌和模糊的 Host/Origin 匹配规则。"""

        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("第一阶段服务只能绑定本机回环地址")

        token = self.control_token.get_secret_value()
        if token and (
            len(token) < 16
            or len(token) > 4096
            or not token.isascii()
            or not token.isprintable()
            or any(character.isspace() for character in token)
        ):
            raise ValueError("server.control_token 必须为空或至少 16 位的无空白可打印 ASCII 字符")

        normalized_hosts: list[str] = []
        for raw_host in self.trusted_hosts:
            host = raw_host.strip().lower()
            if host.startswith("[") and host.endswith("]"):
                host = host[1:-1]
            if (
                not host
                or host == "*"
                or "/" in host
                or "@" in host
                or (":" in host and host != "::1")
                or (host != "::1" and re.fullmatch(r"[a-z0-9.-]+", host) is None)
            ):
                raise ValueError("server.trusted_hosts 只能包含不带端口的精确主机名或回环 IP")
            normalized_hosts.append(host)
        if len(normalized_hosts) != len(set(normalized_hosts)):
            raise ValueError("server.trusted_hosts 不能包含重复项")
        self.trusted_hosts = normalized_hosts

        normalized_origins: list[str] = []
        for raw_origin in self.trusted_origins:
            origin = raw_origin.strip()
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or origin.endswith("/")
            ):
                raise ValueError("server.trusted_origins 必须是无路径、凭据、查询和片段的精确 HTTP(S) Origin")
            normalized_origins.append(f"{parsed.scheme.lower()}://{parsed.netloc.lower()}")
        if len(normalized_origins) != len(set(normalized_origins)):
            raise ValueError("server.trusted_origins 不能包含重复项")
        self.trusted_origins = normalized_origins
        return self

    @property
    def control_authentication_enabled(self) -> bool:
        """返回控制面是否要求 Bearer Token，不暴露令牌正文。"""

        return bool(self.control_token.get_secret_value())


class ModelTaskProfile(BaseModel):
    """一个任务的确定性模型路由；空值从调用请求继承。

    只允许固定主模型或有序 fallback，刻意不提供随机选择，避免画像、
    记忆和社交学习等状态变更任务因采样路由而更换模型。
    """

    models: list[str] = Field(default_factory=list, max_length=8)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    hard_timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    selection_policy: Literal["primary", "ordered_fallback"] = "primary"

    @model_validator(mode="after")
    def validate_models(self) -> ModelTaskProfile:
        normalized = [item.strip() for item in self.models]
        if any(not item or len(item) > 300 for item in normalized):
            raise ValueError("task profile 的模型名必须为 1 到 300 个字符")
        if len(normalized) != len(set(normalized)):
            raise ValueError("task profile 的模型列表不能重复")
        self.models = normalized
        return self


class ModelSettings(BaseModel):
    """聊天与画像模型的协议、端点和能力配置。"""

    mode: Literal["fake", "openai"] = "fake"
    protocol: Literal["openai_chat", "openai_responses", "anthropic_messages"] = "openai_chat"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_ref: str = ""
    name: str = ""
    profile_protocol: Literal["openai_chat", "openai_responses", "anthropic_messages"] | None = None
    profile_base_url: str = ""
    profile_api_key: str = ""
    profile_api_key_ref: str = ""
    profile_name: str = ""
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    max_tokens: int = Field(default=500, ge=32, le=8192)
    context_window_tokens: int = Field(default=16384, ge=1024, le=1_000_000)
    max_concurrency: int = Field(default=4, ge=1, le=32)
    min_request_interval_seconds: float = Field(default=0.0, ge=0, le=60)
    temperature: float = Field(default=0.8, ge=0, le=2)
    supports_json_object: bool = True
    supports_tools: bool = True
    supports_vision: bool = False
    supports_streaming: bool = False
    use_max_completion_tokens: bool = False
    input_price_usd_per_million_tokens: float | None = Field(
        default=None, ge=0
    )
    output_price_usd_per_million_tokens: float | None = Field(
        default=None, ge=0
    )
    profile_input_price_usd_per_million_tokens: float | None = Field(
        default=None, ge=0
    )
    profile_output_price_usd_per_million_tokens: float | None = Field(
        default=None, ge=0
    )
    task_profiles: dict[str, ModelTaskProfile] = Field(
        default_factory=dict,
        max_length=100,
    )

    @model_validator(mode="after")
    def validate_real_provider(self) -> ModelSettings:
        if any(
            re.fullmatch(r"(?:default|[a-z][a-z0-9_.-]{0,99})", task)
            is None
            for task in self.task_profiles
        ):
            raise ValueError(
                "model.task_profiles 的任务名必须是 default 或安全的小写标识"
            )
        if self.max_tokens >= self.context_window_tokens:
            raise ValueError("model.max_tokens 必须小于 model.context_window_tokens")
        oversized_tasks = [
            task
            for task, profile in self.task_profiles.items()
            if (
                profile.max_tokens is not None
                and profile.max_tokens >= self.context_window_tokens
            )
        ]
        if oversized_tasks:
            raise ValueError(
                "model.task_profiles 的 max_tokens 必须小于 "
                f"model.context_window_tokens：{', '.join(oversized_tasks)}"
            )
        if self.mode == "openai":
            if not self.api_key.strip():
                raise ValueError("IJA_MODEL_MODE=openai 时必须设置 IJA_MODEL_API_KEY")
            if not self.name.strip():
                raise ValueError("IJA_MODEL_MODE=openai 时必须设置 IJA_MODEL_NAME")
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("IJA_MODEL_BASE_URL 必须是有效的 HTTP(S) 地址")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("IJA_MODEL_BASE_URL 不得包含凭据、查询参数或片段")
            profile_url = self.profile_base_url or self.base_url
            profile_parsed = urlsplit(profile_url)
            if (
                profile_parsed.scheme not in {"http", "https"}
                or not profile_parsed.hostname
                or profile_parsed.username
                or profile_parsed.password
                or profile_parsed.query
                or profile_parsed.fragment
            ):
                raise ValueError("画像模型 Base URL 必须是无凭据和查询参数的 HTTP(S) 地址")
        if self.protocol != "openai_chat" and self.supports_streaming:
            raise ValueError("当前仅 openai_chat 协议支持流式输出")
        if not self.profile_name:
            self.profile_name = self.name
        return self

    def for_profile(self) -> ModelSettings:
        """生成画像/记忆归档专用 Provider 配置，空字段显式继承聊天端点。"""

        return self.model_copy(
            update={
                "protocol": self.profile_protocol or self.protocol,
                "base_url": self.profile_base_url or self.base_url,
                "api_key": self.profile_api_key or self.api_key,
                "name": self.profile_name or self.name,
                "supports_tools": False,
                "supports_vision": False,
                "supports_streaming": False,
                "input_price_usd_per_million_tokens": (
                    self.profile_input_price_usd_per_million_tokens
                ),
                "output_price_usd_per_million_tokens": (
                    self.profile_output_price_usd_per_million_tokens
                ),
            }
        )


class ImageModelSettings(BaseModel):
    """独立的 OpenAI-compatible Images Edits 配置。"""

    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_ref: str = ""
    name: str = ""
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    price_usd_per_image: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_enabled_provider(self) -> ImageModelSettings:
        if not self.enabled:
            return self
        if not self.api_key.strip():
            raise ValueError("启用图片模型时必须设置 API Key")
        if not self.name.strip():
            raise ValueError("启用图片模型时必须设置模型名")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("图片模型 Base URL 必须是有效的 HTTP(S) 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("图片模型 Base URL 不得包含凭据、查询参数或片段")
        return self


class VisionModelSettings(BaseModel):
    """图片理解模型配置；默认由主聊天模型直接处理视觉输入。"""

    mode: Literal["main", "external"] = "main"
    protocol: Literal["openai_chat", "openai_responses", "anthropic_messages"] = "openai_chat"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_ref: str = ""
    name: str = ""
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    wait_seconds: float = Field(default=5.0, ge=0, le=30)
    input_price_usd_per_million_tokens: float | None = Field(
        default=None, ge=0
    )
    output_price_usd_per_million_tokens: float | None = Field(
        default=None, ge=0
    )

    @model_validator(mode="after")
    def validate_external_provider(self) -> VisionModelSettings:
        if self.mode == "main":
            return self
        if not self.api_key.strip():
            raise ValueError("使用独立视觉模型时必须设置 API Key")
        if not self.name.strip():
            raise ValueError("使用独立视觉模型时必须设置模型名")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("视觉模型 Base URL 必须是有效的 HTTP(S) 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("视觉模型 Base URL 不得包含凭据、查询参数或片段")
        return self

    def as_model_settings(
        self,
        *,
        task_profiles: dict[str, ModelTaskProfile] | None = None,
    ) -> ModelSettings:
        """生成仅用于识图、不获得工具权限且保留任务路由的模型配置。"""

        if self.mode != "external":
            raise ValueError("主模型模式不应创建独立视觉 Provider")
        return ModelSettings(
            mode="openai",
            protocol=self.protocol,
            base_url=self.base_url,
            api_key=self.api_key,
            name=self.name,
            timeout_seconds=self.timeout_seconds,
            max_tokens=500,
            temperature=0.1,
            supports_json_object=True,
            supports_tools=False,
            supports_vision=True,
            supports_streaming=False,
            task_profiles=dict(task_profiles or {}),
            input_price_usd_per_million_tokens=(
                self.input_price_usd_per_million_tokens
            ),
            output_price_usd_per_million_tokens=(
                self.output_price_usd_per_million_tokens
            ),
        )


class ExpressionSelectionSettings(BaseModel):
    """表情混合选择策略；本地预筛优先，必要时才调用视觉模型。"""

    candidate_count: int = Field(default=8, ge=2, le=12)
    direct_score_threshold: float = Field(default=0.55, ge=0, le=1)
    direct_margin_threshold: float = Field(default=0.10, ge=0, le=1)
    minimum_fallback_score: float = Field(default=0.18, ge=0, le=1)
    visual_rerank_enabled: bool = True

    @model_validator(mode="after")
    def validate_thresholds(self) -> ExpressionSelectionSettings:
        if self.minimum_fallback_score > self.direct_score_threshold:
            raise ValueError("表情最低回退分数不得高于直接发送分数")
        return self


class DetectionModelSettings(BaseModel):
    """输出过滤检测模型的独立配置；未启用时过滤插件无法进行模型确认。"""

    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_ref: str = ""
    name: str = ""
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    input_price_usd_per_million_tokens: float | None = Field(
        default=None, ge=0
    )
    output_price_usd_per_million_tokens: float | None = Field(
        default=None, ge=0
    )

    @model_validator(mode="after")
    def validate_enabled_provider(self) -> DetectionModelSettings:
        if not self.enabled:
            return self
        if not self.api_key.strip():
            raise ValueError("启用检测模型时必须设置 API Key")
        if not self.name.strip():
            raise ValueError("启用检测模型时必须设置模型名")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("检测模型 Base URL 必须是有效的 HTTP(S) 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("检测模型 Base URL 不得包含凭据、查询参数或片段")
        return self


class EmbeddingSettings(BaseModel):
    """记忆与群体表达共用的 Embeddings 配置。"""

    enabled: bool = True
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_ref: str = ""
    name: str = ""
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    input_price_usd_per_million_tokens: float | None = Field(
        default=None, ge=0
    )

    @model_validator(mode="after")
    def validate_enabled_provider(self) -> EmbeddingSettings:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Embedding Base URL 必须是有效的 HTTP(S) 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Embedding Base URL 不得包含凭据、查询参数或片段")
        return self

    @property
    def available(self) -> bool:
        """只有启用且模型名、密钥齐全时才允许创建远端 Provider。"""

        return bool(self.enabled and self.api_key.strip() and self.name.strip())


class ChatSettings(BaseModel):
    """私聊与群聊策略配置。"""

    private_debounce_ms: int = Field(default=800, ge=0, le=10000)
    group_debounce_ms: int = Field(default=1200, ge=0, le=10000)
    group_reply_threshold: int = Field(default=80, ge=1, le=100)
    group_trigger_count: int = Field(default=3, ge=1, le=999999)
    group_frequency_factor: float = Field(default=0.9, ge=0, le=1)
    recent_context_messages: int = Field(default=60, ge=5, le=500)
    max_attachment_bytes: int = Field(default=10 * 1024 * 1024, ge=1)


class MemorySettings(BaseModel):
    """长期记忆归档、检索与 Prompt 投影的硬边界。"""

    enabled: bool = True
    retrieval_limit: int = Field(default=8, ge=1, le=30)
    max_context_chars: int = Field(default=4000, ge=500, le=20000)
    max_context_tokens: int = Field(default=1200, ge=128, le=8000)
    consolidation_context_messages: int = Field(default=24, ge=4, le=100)
    minimum_confidence: float = Field(default=0.55, ge=0, le=1)
    automatic_max_items_per_run: int = Field(default=12, ge=1, le=50)
    retrieval_min_score: float = Field(default=0.12, ge=0, le=1)
    retrieval_per_kind_limit: int = Field(default=3, ge=1, le=10)
    vector_score_threshold: float = Field(default=0.45, ge=-1, le=1)
    vector_score_thresholds: dict[str, float] = Field(default_factory=dict)
    rrf_k: int = Field(default=60, ge=1, le=500)
    keyword_rrf_weight: float = Field(default=0.5, ge=0, le=2)
    inject_procedure_preference_limit: int = Field(default=4, ge=1, le=10)
    inject_event_profile_limit: int = Field(default=2, ge=0, le=10)
    inject_episode_relationship_limit: int = Field(default=3, ge=0, le=10)


class SocialLearningSettings(BaseModel):
    """黑话、群体表达和行为经验三条独立学习链的运行边界。"""

    enabled: bool = True
    context_messages: int = Field(default=60, ge=8, le=200)
    minimum_new_user_messages: int = Field(default=4, ge=2, le=50)
    maximum_items_per_kind: int = Field(default=12, ge=1, le=30)
    model_max_tokens: int = Field(default=1400, ge=256, le=4096)
    jargon_inference_thresholds: list[int] = Field(
        default_factory=lambda: [2, 4, 8, 25, 100],
        min_length=1,
        max_length=10,
    )
    jargon_injection_limit: int = Field(default=8, ge=1, le=20)
    jargon_reuse_limit: int = Field(default=3, ge=0, le=10)
    jargon_reuse_min_occurrences: int = Field(default=4, ge=2, le=1000)
    jargon_reuse_min_confidence: float = Field(default=0.75, ge=0, le=1)
    expression_injection_limit: int = Field(default=3, ge=1, le=10)
    behavior_injection_limit: int = Field(default=3, ge=1, le=10)
    minimum_confidence: float = Field(default=0.55, ge=0, le=1)
    expression_match_threshold: float = Field(default=0.16, ge=0, le=1)
    behavior_match_threshold: float = Field(default=0.16, ge=0, le=1)
    expression_vector_enabled: bool = True
    expression_vector_batch_size: int = Field(default=64, ge=1, le=256)
    expression_vector_cluster_max: int = Field(default=32, ge=1, le=100)
    expression_vector_cluster_pool_size: int = Field(default=4, ge=1, le=20)
    expression_vector_candidate_pool_size: int = Field(default=50, ge=1, le=200)
    expression_vector_item_weight: float = Field(default=0.7, ge=0, le=2)
    expression_vector_cluster_weight: float = Field(default=0.1, ge=0, le=2)
    expression_vector_lexical_weight: float = Field(default=0.2, ge=0, le=2)
    expression_vector_diversity_lambda: float = Field(default=0.85, ge=0, le=1)
    behavior_scene_analysis_enabled: bool = True
    behavior_graph_enabled: bool = True
    behavior_graph_spread_depth: int = Field(default=1, ge=0, le=2)
    behavior_scene_cluster_reuse_threshold: float = Field(default=0.72, ge=0, le=1)
    behavior_graph_direct_lock_threshold: float = Field(default=0.6, ge=0, le=2)
    feedback_max_attempts: int = Field(default=3, ge=1, le=10)
    maintenance_interval_hours: int = Field(default=24, ge=1, le=24 * 30)
    jargon_decay_after_days: int = Field(default=90, ge=1, le=3650)
    jargon_decay_period_days: int = Field(default=30, ge=1, le=3650)
    jargon_decay_step: float = Field(default=0.05, gt=0, le=0.5)
    jargon_disable_after_days: int = Field(default=365, ge=1, le=3650)
    expression_decay_after_days: int = Field(default=30, ge=1, le=3650)
    expression_decay_period_days: int = Field(default=30, ge=1, le=3650)
    expression_decay_step: float = Field(default=0.08, gt=0, le=0.5)
    expression_disable_after_days: int = Field(default=180, ge=1, le=3650)
    behavior_unused_decay_after_days: int = Field(default=14, ge=1, le=3650)
    behavior_unanswered_decay_after_days: int = Field(default=21, ge=1, le=3650)
    behavior_positive_stale_decay_after_days: int = Field(default=60, ge=1, le=3650)
    behavior_unused_disable_after_days: int = Field(default=60, ge=1, le=3650)

    @model_validator(mode="after")
    def validate_learning_lifecycle(self) -> SocialLearningSettings:
        """确保推断节点和衰减窗口单调且具备实际意义。"""

        thresholds = self.jargon_inference_thresholds
        if any(value < 2 for value in thresholds):
            raise ValueError("jargon_inference_thresholds 的每个节点必须至少为 2")
        if thresholds != sorted(set(thresholds)):
            raise ValueError("jargon_inference_thresholds 必须严格递增且不能重复")
        if self.jargon_disable_after_days <= self.jargon_decay_after_days:
            raise ValueError("黑话停用时间必须晚于开始衰减时间")
        if self.expression_disable_after_days <= self.expression_decay_after_days:
            raise ValueError("表达停用时间必须晚于开始衰减时间")
        if self.behavior_unused_disable_after_days <= self.behavior_unused_decay_after_days:
            raise ValueError("行为停用时间必须晚于一次性经验开始衰减时间")
        vector_weight = (
            self.expression_vector_item_weight
            + self.expression_vector_cluster_weight
            + self.expression_vector_lexical_weight
        )
        if vector_weight <= 0:
            raise ValueError("表达向量选择器至少需要一个正权重")
        if self.expression_vector_cluster_pool_size > self.expression_vector_candidate_pool_size:
            raise ValueError("表达向量簇候选数不能大于表达候选池")
        return self


class PersonaSettings(BaseModel):
    """当前启用角色；角色正文由 PersonaStore 从独立文件加载。"""

    active_character_id: str = Field(default="default", pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class StorageSettings(BaseModel):
    """本地持久化配置。"""

    data_dir: Path = Path("./data")
    database_name: str = "ija.sqlite3"

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_name


class ToolSettings(BaseModel):
    """工具循环和外部只读请求的硬限制。"""

    max_rounds: int = Field(default=4, ge=1, le=8)
    max_calls: int = Field(default=8, ge=1, le=32)
    weather_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    weather_max_response_bytes: int = Field(default=256 * 1024, ge=1024, le=2 * 1024 * 1024)


class ScheduleSettings(BaseModel):
    """周期任务的本地安全边界。"""

    default_timezone: str = "Asia/Shanghai"
    minimum_interval_minutes: int = Field(default=5, ge=1, le=1440)
    max_active_per_session: int = Field(default=20, ge=1, le=200)
    max_concurrency: int = Field(default=4, ge=1, le=32)

    @model_validator(mode="after")
    def validate_timezone(self) -> ScheduleSettings:
        try:
            ZoneInfo(self.default_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("schedule.default_timezone 必须是有效的 IANA 时区") from exc
        return self


class ProactiveSettings(BaseModel):
    """主动来源抓取和模型判断的进程级安全边界。"""

    scheduler_interval_seconds: int = Field(default=60, ge=10, le=3600)
    default_poll_interval_minutes: int = Field(default=30, ge=15, le=1440)
    max_candidates_per_tick: int = Field(default=10, ge=1, le=50)
    max_concurrency: int = Field(default=4, ge=1, le=32)
    judge_threshold: float = Field(default=0.70, ge=0, le=1)
    candidate_retention_days: int = Field(default=30, ge=1, le=365)
    feed_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    feed_max_response_bytes: int = Field(default=1024 * 1024, ge=4096, le=10 * 1024 * 1024)
    feed_max_redirects: int = Field(default=3, ge=0, le=5)


class DriftSettings(BaseModel):
    """Drift 调度与自动续接上限。"""

    scheduler_interval_seconds: int = Field(default=300, ge=30, le=3600)
    idle_hours: int = Field(default=12, ge=1, le=720)
    minimum_interval_hours: int = Field(default=24, ge=1, le=720)
    max_auto_resumes: int = Field(default=3, ge=0, le=3)


class PlatformPluginSettings(BaseModel):
    """运行时插件的显式开关及其非敏感配置。"""

    enabled: list[str] = Field(default_factory=list, max_length=20)
    disabled: list[str] = Field(default_factory=list, max_length=20)
    options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    callback_max_body_bytes: int = Field(default=256 * 1024, ge=1024, le=2 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_plugin_ids(self) -> PlatformPluginSettings:
        for field, plugin_ids in (
            ("enabled", self.enabled),
            ("disabled", self.disabled),
        ):
            if len(plugin_ids) != len(set(plugin_ids)):
                raise ValueError(f"platform_plugins.{field} 不得包含重复插件")
        overlap = set(self.enabled) & set(self.disabled)
        if overlap:
            raise ValueError(f"插件不能同时启用和禁用: {sorted(overlap)}")
        for plugin_id in (*self.enabled, *self.disabled, *self.options):
            if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", plugin_id) is None:
                raise ValueError(f"无效的插件 ID: {plugin_id}")
        return self


class WorkspaceAdministratorPrincipal(BaseModel):
    """可获得工作区管理 scope 的精确外部身份。"""

    platform: str = Field(min_length=1, max_length=100)
    account_id: str = Field(min_length=1, max_length=200)
    actor_id: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_exact_principal(self) -> WorkspaceAdministratorPrincipal:
        values = (self.platform, self.account_id, self.actor_id)
        if any("*" in value or value != value.strip() for value in values):
            raise ValueError("workspace 管理员 principal 必须精确匹配且禁止 wildcard")
        if len(self.scopes) != len(set(self.scopes)):
            raise ValueError("workspace 管理员 scopes 不得重复")
        for scope in self.scopes:
            if (
                "*" in scope
                or re.fullmatch(
                    r"[a-z][a-z0-9-]*(?::[a-z][a-z0-9-]*)+",
                    scope,
                )
                is None
            ):
                raise ValueError(f"无效的授权 scope: {scope}")
        return self


class AuthorizationSettings(BaseModel):
    """工作区高权限能力的显式 principal allowlist。"""

    workspace_administrators: list[WorkspaceAdministratorPrincipal] = Field(
        default_factory=list,
        max_length=100,
    )

    @model_validator(mode="after")
    def reject_duplicate_principals(self) -> AuthorizationSettings:
        principals = [
            (item.platform, item.account_id, item.actor_id) for item in self.workspace_administrators
        ]
        if len(principals) != len(set(principals)):
            raise ValueError("workspace 管理员 principal 不得重复")
        return self

    def scopes_for(
        self,
        *,
        platform: str,
        account_id: str,
        actor_id: str | None,
    ) -> frozenset[str]:
        """按平台、机器人账号和用户三元组精确计算授权 scope。"""

        if actor_id is None:
            return frozenset()
        for principal in self.workspace_administrators:
            if (
                principal.platform == platform
                and principal.account_id == account_id
                and principal.actor_id == actor_id
            ):
                return frozenset(principal.scopes)
        return frozenset()


class AppSettings(BaseModel):
    """应用完整配置。"""

    server: ServerSettings = Field(default_factory=ServerSettings)
    model: ModelSettings = Field(default_factory=ModelSettings)
    vision_model: VisionModelSettings = Field(default_factory=VisionModelSettings)
    expression_selection: ExpressionSelectionSettings = Field(default_factory=ExpressionSelectionSettings)
    image_model: ImageModelSettings = Field(default_factory=ImageModelSettings)
    detection_model: DetectionModelSettings = Field(default_factory=DetectionModelSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    chat: ChatSettings = Field(default_factory=ChatSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    social_learning: SocialLearningSettings = Field(default_factory=SocialLearningSettings)
    persona: PersonaSettings = Field(default_factory=PersonaSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    proactive: ProactiveSettings = Field(default_factory=ProactiveSettings)
    drift: DriftSettings = Field(default_factory=DriftSettings)
    platform_plugins: PlatformPluginSettings = Field(default_factory=PlatformPluginSettings)
    authorization: AuthorizationSettings = Field(default_factory=AuthorizationSettings)
    project_root: Path


def _merge_dict(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as file:
        return tomllib.load(file)


def _model_local_path(project_root: Path) -> Path:
    """返回仅供本机控制台保存模型凭据的配置文件路径。"""

    return project_root / "config" / "model.local.toml"


def _image_model_local_path(project_root: Path) -> Path:
    """返回仅供本机保存图片模型凭据的配置文件。"""

    return project_root / "config" / "image-model.local.toml"


def _vision_model_local_path(project_root: Path) -> Path:
    """返回仅供本机保存独立视觉模型凭据的配置文件。"""

    return project_root / "config" / "vision-model.local.toml"


def _detection_model_local_path(project_root: Path) -> Path:
    """返回仅供本机保存检测模型凭据的配置文件。"""

    return project_root / "config" / "detection-model.local.toml"


def _embedding_local_path(project_root: Path) -> Path:
    return project_root / "config" / "embedding.local.toml"


def _platform_plugins_local_path(project_root: Path) -> Path:
    """返回 WebUI 独占的平台插件非敏感覆盖配置文件。"""

    return project_root / "config" / "platform-plugins.local.toml"


def save_onebot_group_configs(project_root: Path, groups: list[dict[str, Any]]) -> None:
    """原子保存 WebUI 管理的 OneBot 群准入规则，不重写用户的 local.toml。"""

    from tomli_w import dumps

    path = _platform_plugins_local_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".toml.tmp")
    temporary.write_text(
        dumps({"platform_plugins": {"options": {"onebot": {"groups": groups}}}}),
        encoding="utf-8",
    )
    temporary.replace(path)


def _resolve_persisted_secrets(
    values: dict[str, Any],
    *,
    project_root: Path,
    data_dir: Path | None,
) -> dict[str, Any]:
    """解析文件中的 secret reference，不读取模型 API Key 环境变量。"""

    selected_data_dir = data_dir
    if selected_data_dir is None:
        storage = values.get("storage", {})
        raw = storage.get("data_dir", "./data") if isinstance(storage, dict) else "./data"
        selected_data_dir = Path(raw)
        if not selected_data_dir.is_absolute():
            selected_data_dir = (project_root / selected_data_dir).resolve()
    resolve_secret_references(values, data_dir=selected_data_dir)
    return values


def load_persisted_embedding_settings(
    project_root: Path,
    *,
    data_dir: Path | None = None,
) -> EmbeddingSettings:
    values = _read_toml(project_root / "config" / "default.toml")
    values = _merge_dict(values, _read_toml(project_root / "config" / "local.toml"))
    values = _merge_dict(values, _read_toml(_embedding_local_path(project_root)))
    _resolve_persisted_secrets(
        values,
        project_root=project_root,
        data_dir=data_dir,
    )
    return EmbeddingSettings.model_validate(values.get("embedding", {}))


def save_embedding_settings(
    project_root: Path,
    embedding: EmbeddingSettings,
    *,
    data_dir: Path,
) -> None:
    persist_secret_settings(
        path=_embedding_local_path(project_root),
        section_name="embedding",
        values=embedding.model_dump(),
        data_dir=data_dir,
        key_bindings=(("api_key", "api_key_ref", "embedding.api_key"),),
    )


def load_persisted_model_settings(
    project_root: Path,
    *,
    data_dir: Path | None = None,
) -> ModelSettings:
    """读取文件配置，不读取环境变量，避免将环境中的密钥意外落盘。"""

    values = _read_toml(project_root / "config" / "default.toml")
    values = _merge_dict(values, _read_toml(project_root / "config" / "local.toml"))
    values = _merge_dict(values, _read_toml(_model_local_path(project_root)))
    _resolve_persisted_secrets(
        values,
        project_root=project_root,
        data_dir=data_dir,
    )
    return ModelSettings.model_validate(values.get("model", {}))


def save_model_settings(
    project_root: Path,
    model: ModelSettings,
    *,
    data_dir: Path,
) -> None:
    """原子保存 WebUI 的本机模型配置；调用方绝不能记录其中的密钥。"""

    persist_secret_settings(
        path=_model_local_path(project_root),
        section_name="model",
        values=model.model_dump(),
        data_dir=data_dir,
        key_bindings=(
            ("api_key", "api_key_ref", "model.api_key"),
            (
                "profile_api_key",
                "profile_api_key_ref",
                "model.profile_api_key",
            ),
        ),
    )


def load_persisted_image_model_settings(
    project_root: Path,
    *,
    data_dir: Path | None = None,
) -> ImageModelSettings:
    """读取图片模型文件配置，不读取环境变量。"""

    values = _read_toml(project_root / "config" / "default.toml")
    values = _merge_dict(values, _read_toml(project_root / "config" / "local.toml"))
    values = _merge_dict(values, _read_toml(_image_model_local_path(project_root)))
    _resolve_persisted_secrets(
        values,
        project_root=project_root,
        data_dir=data_dir,
    )
    return ImageModelSettings.model_validate(values.get("image_model", {}))


def save_image_model_settings(
    project_root: Path,
    image_model: ImageModelSettings,
    *,
    data_dir: Path,
) -> None:
    """原子保存图片模型本机配置；调用方不得记录其中的密钥。"""

    persist_secret_settings(
        path=_image_model_local_path(project_root),
        section_name="image_model",
        values=image_model.model_dump(),
        data_dir=data_dir,
        key_bindings=(("api_key", "api_key_ref", "image_model.api_key"),),
    )


def load_persisted_vision_model_settings(
    project_root: Path,
    *,
    data_dir: Path | None = None,
) -> VisionModelSettings:
    """读取视觉模型文件配置，不读取环境变量。"""

    values = _read_toml(project_root / "config" / "default.toml")
    values = _merge_dict(values, _read_toml(project_root / "config" / "local.toml"))
    values = _merge_dict(values, _read_toml(_vision_model_local_path(project_root)))
    _resolve_persisted_secrets(
        values,
        project_root=project_root,
        data_dir=data_dir,
    )
    return VisionModelSettings.model_validate(values.get("vision_model", {}))


def save_vision_model_settings(
    project_root: Path,
    vision_model: VisionModelSettings,
    *,
    data_dir: Path,
) -> None:
    """原子保存视觉模型本机配置；调用方不得记录其中的密钥。"""

    persist_secret_settings(
        path=_vision_model_local_path(project_root),
        section_name="vision_model",
        values=vision_model.model_dump(),
        data_dir=data_dir,
        key_bindings=(("api_key", "api_key_ref", "vision_model.api_key"),),
    )


def load_persisted_detection_model_settings(
    project_root: Path,
    *,
    data_dir: Path | None = None,
) -> DetectionModelSettings:
    """读取检测模型文件配置，不读取环境变量。"""

    values = _read_toml(project_root / "config" / "default.toml")
    values = _merge_dict(values, _read_toml(project_root / "config" / "local.toml"))
    values = _merge_dict(values, _read_toml(_detection_model_local_path(project_root)))
    _resolve_persisted_secrets(
        values,
        project_root=project_root,
        data_dir=data_dir,
    )
    return DetectionModelSettings.model_validate(values.get("detection_model", {}))


def save_detection_model_settings(
    project_root: Path,
    detection_model: DetectionModelSettings,
    *,
    data_dir: Path,
) -> None:
    """原子保存检测模型本机配置；调用方不得记录其中的密钥。"""

    persist_secret_settings(
        path=_detection_model_local_path(project_root),
        section_name="detection_model",
        values=detection_model.model_dump(),
        data_dir=data_dir,
        key_bindings=(("api_key", "api_key_ref", "detection_model.api_key"),),
    )


def migrate_legacy_secret_files(
    project_root: Path,
    *,
    data_dir: Path,
) -> list[str]:
    """显式把现有本机 TOML 明文凭据迁移成安全引用。"""

    migrated: list[str] = []
    model_path = _model_local_path(project_root)
    if model_path.is_file():
        save_model_settings(
            project_root,
            load_persisted_model_settings(project_root, data_dir=data_dir),
            data_dir=data_dir,
        )
        migrated.append(model_path.name)
    image_path = _image_model_local_path(project_root)
    if image_path.is_file():
        save_image_model_settings(
            project_root,
            load_persisted_image_model_settings(
                project_root,
                data_dir=data_dir,
            ),
            data_dir=data_dir,
        )
        migrated.append(image_path.name)
    vision_path = _vision_model_local_path(project_root)
    if vision_path.is_file():
        save_vision_model_settings(
            project_root,
            load_persisted_vision_model_settings(
                project_root,
                data_dir=data_dir,
            ),
            data_dir=data_dir,
        )
        migrated.append(vision_path.name)
    detection_path = _detection_model_local_path(project_root)
    if detection_path.is_file():
        save_detection_model_settings(
            project_root,
            load_persisted_detection_model_settings(
                project_root,
                data_dir=data_dir,
            ),
            data_dir=data_dir,
        )
        migrated.append(detection_path.name)
    embedding_path = _embedding_local_path(project_root)
    if embedding_path.is_file():
        save_embedding_settings(
            project_root,
            load_persisted_embedding_settings(
                project_root,
                data_dir=data_dir,
            ),
            data_dir=data_dir,
        )
        migrated.append(embedding_path.name)
    return migrated


def ensure_control_token(settings: AppSettings) -> bool:
    """为正常启动生成并密封 control token 与控制会话凭据；返回本次是否首次生成 token。"""

    store = LocalSecretStore(settings.storage.data_dir)
    if settings.server.control_authentication_enabled:
        token_generated = False
    else:
        token, token_generated = store.get_or_create(
            _CONTROL_TOKEN_REFERENCE,
            lambda: stdlib_secrets.token_urlsafe(32),
        )
        settings.server.control_token = SecretStr(token)
    # 控制会话凭据独立持久化：后端重启后已换发的 HttpOnly Cookie 仍然有效，
    # 避免每次重启都要求浏览器重新走登录链接换取新 Cookie。
    if (
        settings.server.control_authentication_enabled
        and not settings.server.session_credential.get_secret_value()
    ):
        token = settings.server.control_token.get_secret_value()
        credential, _ = store.get_or_create(
            _SESSION_CREDENTIAL_REFERENCE,
            lambda: new_hmac(
                token.encode("ascii"),
                stdlib_secrets.token_bytes(32),
                sha256,
            ).hexdigest(),
        )
        settings.server.session_credential = SecretStr(credential)
    return token_generated


def control_login_url(settings: AppSettings) -> str:
    """显式读取本机控制凭据并构造只存在于 URL fragment 的登录链接。"""

    token = settings.server.control_token.get_secret_value()
    if not token:
        token = LocalSecretStore(settings.storage.data_dir).get_optional(
            _CONTROL_TOKEN_REFERENCE
        ) or ""
    if not token:
        raise ValueError("尚未生成本机 control token；请先启动一次 iJA")
    host = "127.0.0.1" if settings.server.host in {"localhost", "::1"} else settings.server.host
    return f"http://{host}:{settings.server.port}/#control_token={token}"


def load_settings(project_root: Path | None = None) -> AppSettings:
    """加载默认配置、本地覆盖和环境变量，并在启动前一次性校验。"""

    root = (project_root or Path.cwd()).resolve()
    values = _read_toml(root / "config" / "default.toml")
    values = _merge_dict(values, _read_toml(root / "config" / "local.toml"))
    values = _merge_dict(values, _read_toml(_platform_plugins_local_path(root)))
    values = _merge_dict(values, _read_toml(_model_local_path(root)))
    values = _merge_dict(values, _read_toml(_vision_model_local_path(root)))
    values = _merge_dict(values, _read_toml(_image_model_local_path(root)))
    values = _merge_dict(values, _read_toml(_detection_model_local_path(root)))
    values = _merge_dict(values, _read_toml(_embedding_local_path(root)))

    model_values = values.setdefault("model", {})
    server_values = values.setdefault("server", {})
    vision_model_values = values.setdefault("vision_model", {})
    expression_selection_values = values.setdefault("expression_selection", {})
    image_model_values = values.setdefault("image_model", {})
    values.setdefault("detection_model", {})
    embedding_values = values.setdefault("embedding", {})
    storage_values = values.setdefault("storage", {})
    platform_plugin_values = values.setdefault("platform_plugins", {})
    if "IJA_CONTROL_TOKEN" in os.environ:
        server_values["control_token"] = os.environ["IJA_CONTROL_TOKEN"]
    if "IJA_TRUSTED_HOSTS" in os.environ:
        server_values["trusted_hosts"] = [
            item.strip() for item in os.environ["IJA_TRUSTED_HOSTS"].split(",") if item.strip()
        ]
    if "IJA_TRUSTED_ORIGINS" in os.environ:
        server_values["trusted_origins"] = [
            item.strip() for item in os.environ["IJA_TRUSTED_ORIGINS"].split(",") if item.strip()
        ]
    env_map = {
        "mode": "IJA_MODEL_MODE",
        "protocol": "IJA_MODEL_PROTOCOL",
        "base_url": "IJA_MODEL_BASE_URL",
        "api_key": "IJA_MODEL_API_KEY",
        "name": "IJA_MODEL_NAME",
        "profile_protocol": "IJA_PROFILE_MODEL_PROTOCOL",
        "profile_base_url": "IJA_PROFILE_MODEL_BASE_URL",
        "profile_api_key": "IJA_PROFILE_MODEL_API_KEY",
        "profile_name": "IJA_PROFILE_MODEL_NAME",
        "supports_tools": "IJA_MODEL_SUPPORTS_TOOLS",
        "supports_vision": "IJA_MODEL_SUPPORTS_VISION",
        "supports_streaming": "IJA_MODEL_SUPPORTS_STREAMING",
    }
    for key, env_name in env_map.items():
        if env_name in os.environ:
            model_values[key] = os.environ[env_name]
    image_env_map = {
        "enabled": "IJA_IMAGE_MODEL_ENABLED",
        "base_url": "IJA_IMAGE_MODEL_BASE_URL",
        "api_key": "IJA_IMAGE_MODEL_API_KEY",
        "name": "IJA_IMAGE_MODEL_NAME",
        "timeout_seconds": "IJA_IMAGE_MODEL_TIMEOUT_SECONDS",
    }
    for key, env_name in image_env_map.items():
        if env_name in os.environ:
            image_model_values[key] = os.environ[env_name]
    vision_env_map = {
        "mode": "IJA_VISION_MODEL_MODE",
        "protocol": "IJA_VISION_MODEL_PROTOCOL",
        "base_url": "IJA_VISION_MODEL_BASE_URL",
        "api_key": "IJA_VISION_MODEL_API_KEY",
        "name": "IJA_VISION_MODEL_NAME",
        "timeout_seconds": "IJA_VISION_MODEL_TIMEOUT_SECONDS",
        "wait_seconds": "IJA_VISION_WAIT_SECONDS",
    }
    for key, env_name in vision_env_map.items():
        if env_name in os.environ:
            vision_model_values[key] = os.environ[env_name]
    expression_selection_env_map = {
        "candidate_count": "IJA_EXPRESSION_SELECTION_CANDIDATE_COUNT",
        "direct_score_threshold": "IJA_EXPRESSION_SELECTION_DIRECT_SCORE_THRESHOLD",
        "direct_margin_threshold": "IJA_EXPRESSION_SELECTION_DIRECT_MARGIN_THRESHOLD",
        "minimum_fallback_score": "IJA_EXPRESSION_SELECTION_MINIMUM_FALLBACK_SCORE",
        "visual_rerank_enabled": "IJA_EXPRESSION_SELECTION_VISUAL_RERANK_ENABLED",
    }
    for key, env_name in expression_selection_env_map.items():
        if env_name in os.environ:
            expression_selection_values[key] = os.environ[env_name]
    embedding_env_map = {
        "enabled": "IJA_EMBEDDING_ENABLED",
        "base_url": "IJA_EMBEDDING_BASE_URL",
        "api_key": "IJA_EMBEDDING_API_KEY",
        "name": "IJA_EMBEDDING_MODEL_NAME",
    }
    for key, env_name in embedding_env_map.items():
        if env_name in os.environ:
            embedding_values[key] = os.environ[env_name]
    if "IJA_DATA_DIR" in os.environ:
        storage_values["data_dir"] = os.environ["IJA_DATA_DIR"]
    if "IJA_PLATFORM_PLUGINS" in os.environ:
        platform_plugin_values["enabled"] = [
            item.strip() for item in os.environ["IJA_PLATFORM_PLUGINS"].split(",") if item.strip()
        ]
    if "IJA_PLATFORM_PLUGINS_DISABLED" in os.environ:
        platform_plugin_values["disabled"] = [
            item.strip() for item in os.environ["IJA_PLATFORM_PLUGINS_DISABLED"].split(",") if item.strip()
        ]

    data_dir = Path(storage_values.get("data_dir", "./data"))
    if not data_dir.is_absolute():
        data_dir = (root / data_dir).resolve()
    else:
        data_dir = data_dir.resolve()
    storage_values["data_dir"] = data_dir
    resolve_secret_references(values, data_dir=data_dir)
    if not str(server_values.get("control_token") or ""):
        stored_control_token = LocalSecretStore(data_dir).get_optional(
            _CONTROL_TOKEN_REFERENCE
        )
        if stored_control_token is not None:
            server_values["control_token"] = stored_control_token
    # 控制会话凭据同样从本机密封存储恢复，保证重启后旧 Cookie 仍可校验。
    if (
        str(server_values.get("control_token") or "")
        and not str(server_values.get("session_credential") or "")
    ):
        stored_session_credential = LocalSecretStore(data_dir).get_optional(
            _SESSION_CREDENTIAL_REFERENCE
        )
        if stored_session_credential is not None:
            server_values["session_credential"] = stored_session_credential
    return AppSettings.model_validate({**values, "project_root": root})
