"""入站、策略、模型、出站和画像提取的唯一应用编排层。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from adapters.persistence import PersonaStore
from application.conversation import (
    ConversationUnderstanding,
    needs_conversation_understanding,
    understand_conversation,
)
from application.events import EventHub
from application.expressions import ExpressionService
from application.memory import MemoryService
from application.model_json import parse_model_json
from application.outbound import OutboundCoordinator
from application.planning import ReplyPlanner
from application.replies import draft_from_model_text, validate_reply_draft
from application.social_learning import SocialLearningService
from application.tool_loop import ToolLoop
from application.vision import VisionUnderstandingService
from config import AppSettings
from domain.errors import (
    ConflictError,
    InputValidationError,
    InvalidModelResponseError,
    NotFoundError,
)
from domain.models import (
    ChatType,
    ComponentType,
    DecisionAction,
    DeliveryReceipt,
    DeliveryStatus,
    ExtractionStatus,
    GroupParticipationMode,
    GroupParticipationPolicy,
    InboundMessage,
    MemoryConsolidationRun,
    MemorySourceChain,
    MessageComponent,
    MessageOrigin,
    OutboundMessage,
    Participant,
    ParticipantRole,
    ProfileExtractionRun,
    ProfileFact,
    ReplyDraft,
    ScheduleRun,
    ScheduleRunStatus,
    ScheduleStatus,
    ScheduleTask,
    SessionView,
    StoredMessage,
    TurnDecision,
    scope_key_for,
    utc_now,
)
from observability import model_observation_scope
from ports import (
    ApplicationRepository,
    AttachmentValidator,
    ChannelAdapter,
    ChannelCapabilityProvider,
    ChannelRuntimeContext,
    ChannelRuntimeContextProvider,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    StreamingModelProvider,
)
from ports.egress import EgressEnvelope, EgressHandler
from prompting import PromptAssembler
from prompting.budget import estimate_messages_tokens
from scheduling import ScheduleService
from skill_runtime import SkillCatalog, SkillPluginManager
from strategies import GroupChatStrategy, PrivateChatStrategy
from tools import ToolContext, ToolRegistry

logger = logging.getLogger(__name__)
DELETE_ALL_LOCAL_USER_DATA_CONFIRMATION = "删除全部本地用户数据"


async def _default_egress(envelope: EgressEnvelope) -> str:
    """无 egress 插件时的直通透传。"""

    return envelope.text


class IngressResult(BaseModel):
    """入站 API 的明确接收结果。"""

    accepted: bool
    duplicate: bool = False
    control: bool = False
    message: StoredMessage | None = None


class ExtractedFact(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    source_message_ids: list[str] = Field(min_length=1)


class ExtractionPayload(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=20)


class ChatService:
    """拥有 Session Turn 排队和画像后台任务生命周期。"""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: ApplicationRepository,
        model: ModelProvider,
        profile_model: ModelProvider | None = None,
        channel: ChannelAdapter,
        attachment_validator: AttachmentValidator,
        events: EventHub,
        tool_registry: ToolRegistry,
        schedule_service: ScheduleService,
        personas: PersonaStore,
        prompting: PromptAssembler,
        expressions: ExpressionService,
        vision: VisionUnderstandingService | None = None,
        memory: MemoryService | None = None,
        social_learning: SocialLearningService | None = None,
        outbound: OutboundCoordinator | None = None,
        skills: SkillCatalog,
        skill_plugins: SkillPluginManager | None = None,
        channel_capabilities: ChannelCapabilityProvider,
    ) -> None:
        self.settings = settings
        self.store = store
        self.model = model
        self.profile_model = profile_model or model
        self.channel = channel
        self.attachment_validator = attachment_validator
        self.events = events
        self.tool_registry = tool_registry
        self.schedule_service = schedule_service
        self.personas = personas
        self.expressions = expressions
        self.channel_capabilities = channel_capabilities
        self.vision = vision
        self._owns_memory = memory is None
        self.memory = memory or MemoryService(
            settings=settings,
            store=store,
            model=self.profile_model,
            prompting=prompting,
            events=events,
        )
        self._owns_social_learning = social_learning is None
        self.social_learning = social_learning or SocialLearningService(
            settings=settings,
            store=store,
            model=self.profile_model,
            prompting=prompting,
            events=events,
        )
        self.outbound = outbound or OutboundCoordinator(
            store=store,
            channel=channel,
            personas=personas,
            events=events,
        )
        self.skills = skills
        self.skill_plugins = skill_plugins or SkillPluginManager(
            skills,
            capabilities={"expression-library": expressions},
        )
        self.tool_loop = ToolLoop(settings=settings, store=store, events=events, registry=tool_registry)
        self.prompting = prompting
        self._egress_filter: EgressHandler = _default_egress
        self.private_strategy = PrivateChatStrategy()
        self.group_strategy = GroupChatStrategy(
            threshold=settings.chat.group_reply_threshold,
            trigger_count=settings.chat.group_trigger_count,
            frequency_factor=settings.chat.group_frequency_factor,
        )
        self.reply_planner = ReplyPlanner()
        self._session_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._debounce_tasks: dict[str, asyncio.Task[None]] = {}
        self._turn_driver_tasks: set[asyncio.Task[None]] = set()
        self._turn_tasks: set[asyncio.Task[None]] = set()
        self._active_turn_tasks: dict[str, asyncio.Task[None]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._background_tasks_by_session: defaultdict[
            str, set[asyncio.Task[Any]]
        ] = defaultdict(set)
        self._data_lifecycle_lock = asyncio.Lock()
        self._stopping = False

    def session_lock(self, session_id: str) -> asyncio.Lock:
        """返回统一 Session FIFO 锁，供主动发送前做短临界区复核。"""

        return self._session_locks[session_id]

    @property
    def channel(self) -> ChannelAdapter:
        return self._channel

    @channel.setter
    def channel(self, channel: ChannelAdapter) -> None:
        """测试或运行时替换 Channel 时同步统一出站 owner。"""

        self._channel = channel
        outbound = getattr(self, "outbound", None)
        if outbound is not None:
            outbound.channel = channel

    def set_egress_filter(self, filter: EgressHandler) -> None:
        """热切换输出过滤管线；无 egress 插件时为直通透传。"""

        self._egress_filter = filter

    async def _apply_egress_filter(
        self,
        draft: ReplyDraft,
        *,
        prompt_messages: list[ModelMessage],
        session_id: str,
        turn_id: str,
        split_short_lines: bool,
    ) -> ReplyDraft:
        """对模型最终可见文本运行输出过滤；未变更时保持原始草稿结构。"""

        text = "\n".join(
            component.text
            for group in (draft.components, *draft.follow_up_components)
            for component in group
            if component.text
        )
        envelope = EgressEnvelope(
            session_id=session_id,
            turn_id=turn_id,
            text=text,
            prompt_messages=prompt_messages,
            model=self.model,
            model_name=self.settings.model.name,
            temperature=self.settings.model.temperature,
            max_tokens=self.settings.model.max_tokens,
        )
        filtered = await self._egress_filter(envelope)
        if filtered == text:
            return draft
        return draft_from_model_text(filtered, split_short_lines=split_short_lines)

    async def start(self) -> None:
        """恢复响应式出站、未处理消息及后台任务。"""

        self._stopping = False
        if self._owns_memory:
            await self.memory.start()
        if self._owns_social_learning:
            await self.social_learning.start()
        for run in await self.store.list_recoverable_runs():
            run.status = ExtractionStatus.PENDING
            run.updated_at = utc_now()
            await self.store.save_extraction_run(run)
            self._track_task(
                asyncio.create_task(self._execute_extraction(run)),
                session_id=run.session_id,
            )
        await self._recover_reactive_outbound_batches()
        for session in await self.store.list_sessions():
            if await self.store.list_pending_messages(session.id):
                self._schedule_turn(session, delay_seconds=0)

    async def stop(self) -> None:
        """停止接收新 Turn，取消在途响应式工作并等待后台任务收尾。"""

        self._stopping = True
        for task in self._debounce_tasks.values():
            task.cancel()
        await asyncio.gather(*self._debounce_tasks.values(), return_exceptions=True)
        for task in tuple(self._turn_tasks):
            task.cancel()
        if self._turn_tasks:
            await asyncio.gather(*tuple(self._turn_tasks), return_exceptions=True)
        if self._turn_driver_tasks:
            await asyncio.gather(
                *tuple(self._turn_driver_tasks), return_exceptions=True
            )
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        if self._owns_memory:
            await self.memory.stop()
        if self._owns_social_learning:
            await self.social_learning.stop()

    async def create_session(
        self,
        *,
        chat_type: ChatType,
        display_name: str,
        external_chat_id: str,
        participants: list[Participant],
        platform: str = "web-simulator",
        account_id: str = "ija-local",
    ) -> SessionView:
        """在全局数据生命周期锁内创建 Session，避免与全局删除交错。"""

        async with self._data_lifecycle_lock:
            return await self._create_session_unlocked(
                chat_type=chat_type,
                display_name=display_name,
                external_chat_id=external_chat_id,
                participants=participants,
                platform=platform,
                account_id=account_id,
            )

    async def _create_session_unlocked(
        self,
        *,
        chat_type: ChatType,
        display_name: str,
        external_chat_id: str,
        participants: list[Participant],
        platform: str,
        account_id: str,
    ) -> SessionView:
        participants = self._normalize_participants(chat_type, participants)
        if chat_type == ChatType.PRIVATE and len(participants) != 1:
            raise InputValidationError("私聊模拟会话必须且只能有一个对端用户")
        if chat_type == ChatType.GROUP and not participants:
            raise InputValidationError("群聊模拟会话至少需要一个成员")
        session = await self.store.create_session(
            platform=platform,
            account_id=account_id,
            external_chat_id=external_chat_id,
            chat_type=chat_type,
            display_name=display_name,
            participants=participants,
            group_trigger_count=self.settings.chat.group_trigger_count,
            group_frequency_factor=self.settings.chat.group_frequency_factor,
        )
        await self.events.publish("session.updated", session.model_dump(mode="json"))
        logger.info(
            "会话已创建",
            extra={
                "session_id": session.id,
                "turn_id": "-",
                "chat_type": session.chat_type.value,
                "display_name": session.display_name,
            },
        )
        return session

    @staticmethod
    def _normalize_participants(chat_type: ChatType, participants: list[Participant]) -> list[Participant]:
        """兼容未显式传角色的模拟请求，并强制角色不变量。"""

        if not participants:
            return participants
        normalized = [item.model_copy(deep=True) for item in participants]
        if chat_type == ChatType.PRIVATE:
            normalized[0].role = ParticipantRole.OWNER
            return normalized
        owners = [item for item in normalized if item.role == ParticipantRole.OWNER]
        if not owners:
            normalized[0].role = ParticipantRole.OWNER
            owners = [normalized[0]]
        if len(owners) != 1:
            raise InputValidationError("群聊必须且只能有一个 owner")
        return normalized

    async def get_group_participation_policy(
        self, session_id: str
    ) -> GroupParticipationPolicy:
        """读取群聊普通发言参与策略。"""

        return await self.store.get_group_participation_policy(session_id)

    async def update_group_participation_policy(
        self,
        session_id: str,
        *,
        mode: GroupParticipationMode,
        trigger_count: int,
        frequency_factor: float,
        cooldown_seconds: int,
        expected_revision: int,
    ) -> GroupParticipationPolicy:
        """串行写入群参与配置，并通过 revision 拒绝并发覆盖。"""

        async with self._session_locks[session_id]:
            policy = await self.store.update_group_participation_policy(
                session_id,
                mode=mode,
                trigger_count=trigger_count,
                frequency_factor=frequency_factor,
                cooldown_seconds=cooldown_seconds,
                expected_revision=expected_revision,
            )
        await self.events.publish(
            "session.group_participation.updated",
            policy.model_dump(mode="json"),
        )
        return policy

    async def update_session_members(
        self, session_id: str, participants: list[Participant], expected_revision: int
    ) -> SessionView:
        """串行化成员更新与隐私删除，避免删除后恢复身份行。"""

        async with self._data_lifecycle_lock:
            return await self._update_session_members_unlocked(
                session_id,
                participants,
                expected_revision,
            )

    async def _update_session_members_unlocked(
        self,
        session_id: str,
        participants: list[Participant],
        expected_revision: int,
    ) -> SessionView:
        session = await self.store.get_session(session_id)
        if session is None:
            raise NotFoundError("会话不存在")
        normalized = self._normalize_participants(session.chat_type, participants)
        if session.chat_type == ChatType.PRIVATE and len(normalized) != 1:
            raise InputValidationError("私聊必须且只能有一个对端用户")
        if session.chat_type == ChatType.GROUP and not normalized:
            raise InputValidationError("群聊至少需要一个成员")
        updated = await self.store.update_session_members(session_id, normalized, expected_revision)
        await self.events.publish("session.updated", updated.model_dump(mode="json"))
        return updated

    async def delete_session(self, session_id: str) -> dict[str, object]:
        """删除整个会话及其全部关联数据，并取消在途防抖任务。

        数据库删除是物理的、不可逆的；调用前应由 API 层完成用户确认。
        删除前先取该 Session 的 FIFO 锁，等待在途 Turn 自然完成，避免删除
        期间仍有写入产生指向已删除会话的悬空数据；删除后清理锁与防抖任务。
        """

        async with self._data_lifecycle_lock:
            session = await self.store.get_session(session_id)
            if session is None:
                raise NotFoundError("会话不存在")
            self._cancel_debounce(session_id)
            async with self._session_locks[session_id]:
                session = await self.store.get_session(session_id)
                if session is None:
                    raise NotFoundError("会话不存在")
                await self.store.advance_session_data_epochs([session_id])
                await self._cancel_session_background_tasks([session_id])
                async with self.social_learning.hold_writes([session_id]):
                    candidate_paths = (
                        await self.store.list_referenced_attachment_paths(
                            session_id
                        )
                    )
                    result = await self.store.delete_session(session_id)
                    referenced_paths = (
                        await self.store.list_referenced_attachment_paths()
                    )
                    result.update(
                        self.attachment_validator.garbage_collect_history_files(
                            candidate_paths,
                            referenced_paths,
                        )
                    )
        self._session_locks.pop(session_id, None)
        await self.events.publish(
            "session.deleted",
            {"session_id": session_id},
        )
        logger.info(
            "会话已删除",
            extra={
                "session_id": session_id,
                "turn_id": "-",
            },
        )
        return result

    async def clear_memory(self, session_id: str | None = None) -> dict[str, object]:
        """只清空长期记忆与派生学习，聊天正文和自含审计继续保留。"""

        async with self._data_lifecycle_lock:
            sessions = (
                [await self.memory.session_for_tool(session_id)]
                if session_id is not None
                else await self.store.list_sessions()
            )
            session_ids = sorted(item.id for item in sessions)
            async with AsyncExitStack() as stack:
                for target_id in session_ids:
                    self._cancel_debounce(target_id)
                    await stack.enter_async_context(
                        self._session_locks[target_id]
                    )
                await self.store.advance_session_data_epochs(session_ids)
                await self._cancel_session_background_tasks(session_ids)
                await stack.enter_async_context(
                    self.social_learning.hold_writes(session_ids)
                )
                return await self.memory.clear(session_id=session_id)

    async def clear_chat(
        self, session_id: str | None = None
    ) -> dict[str, object]:
        """清空聊天正文与正文型审计，保留 Session、配置和长期知识。"""

        async with self._data_lifecycle_lock:
            sessions = (
                [await self.memory.session_for_tool(session_id)]
                if session_id is not None
                else await self.store.list_sessions()
            )
            session_ids = sorted(item.id for item in sessions)
            async with AsyncExitStack() as stack:
                for target_id in session_ids:
                    self._cancel_debounce(target_id)
                    await stack.enter_async_context(
                        self._session_locks[target_id]
                    )
                await self.store.advance_session_data_epochs(session_ids)
                await self._cancel_session_background_tasks(session_ids)
                await stack.enter_async_context(
                    self.social_learning.hold_writes(session_ids)
                )
                candidate_paths = (
                    await self.store.list_referenced_attachment_paths(session_id)
                )
                result = await self.store.clear_chat_content(session_id)
                referenced_paths = (
                    await self.store.list_referenced_attachment_paths()
                )
                result.update(
                    self.attachment_validator.garbage_collect_history_files(
                        candidate_paths,
                        referenced_paths,
                    )
                )
        await self.events.publish("chat.cleared", result)
        return result

    async def delete_all_local_user_data(
        self,
        confirmation: str,
    ) -> dict[str, object]:
        """经精确确认后删除全部数据库用户数据与本地用户媒体。"""

        if confirmation != DELETE_ALL_LOCAL_USER_DATA_CONFIRMATION:
            raise InputValidationError(
                "确认词不匹配；必须完整输入“删除全部本地用户数据”"
            )
        async with self._data_lifecycle_lock:
            sessions = await self.store.list_sessions()
            session_ids = sorted(item.id for item in sessions)
            async with AsyncExitStack() as stack:
                for target_id in session_ids:
                    self._cancel_debounce(target_id)
                    await stack.enter_async_context(
                        self._session_locks[target_id]
                    )
                await self.store.advance_session_data_epochs(session_ids)
                await self._cancel_session_background_tasks(session_ids)
                await stack.enter_async_context(
                    self.social_learning.hold_writes(session_ids)
                )
                await stack.enter_async_context(
                    self.expressions.persona_lock
                )
                result = await self.store.delete_all_local_user_data()
                result.update(self.personas.clear_all_portraits())
                result.update(
                    {
                        f"user_{key}": value
                        for key, value in (
                            self.attachment_validator.purge_all_user_media()
                        ).items()
                    }
                )
            for target_id in session_ids:
                self._session_locks.pop(target_id, None)
        await self.events.publish(
            "local_data.deleted",
            {"session_count": len(session_ids)},
        )
        logger.info(
            "全部本地用户数据已删除",
            extra={
                "session_id": "-",
                "turn_id": "-",
                "session_count": len(session_ids),
            },
        )
        return result

    async def _cancel_session_background_tasks(
        self,
        session_ids: list[str],
    ) -> None:
        """在 clear/delete 提交前排空所有已知的 Session 派生写任务。"""

        profile_tasks = {
            task
            for target_id in session_ids
            for task in self._background_tasks_by_session.get(target_id, set())
            if not task.done()
        }
        for task in profile_tasks:
            task.cancel()
        await asyncio.gather(
            self.memory.cancel_session_tasks(session_ids),
            self.social_learning.cancel_session_tasks(session_ids),
            return_exceptions=False,
        )
        if profile_tasks:
            await asyncio.gather(*profile_tasks, return_exceptions=True)

    def _cancel_debounce(self, session_id: str) -> None:
        """取消尚未开始的防抖任务，等待其响应取消信号。"""

        task = self._debounce_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def ingest(
        self,
        inbound: InboundMessage,
        *,
        schedule_turn: bool = True,
        schedule_duplicate: bool = False,
        debounce_seconds: float | None = None,
    ) -> IngressResult:
        """串行化入站提交与数据删除，避免清空后迟到追加正文。"""

        async with self._data_lifecycle_lock:
            return await self._ingest_unlocked(
                inbound,
                schedule_turn=schedule_turn,
                schedule_duplicate=schedule_duplicate,
                debounce_seconds=debounce_seconds,
            )

    async def _ingest_unlocked(
        self,
        inbound: InboundMessage,
        *,
        schedule_turn: bool = True,
        schedule_duplicate: bool = False,
        debounce_seconds: float | None = None,
    ) -> IngressResult:
        """校验边界、追加消息并触发对应 Session 的防抖处理。"""

        if debounce_seconds is not None and debounce_seconds < 0:
            raise InputValidationError("入站防抖时间不能为负数")
        if inbound.sender_id == "agent":
            raise InputValidationError("模拟入站消息不能冒充 Agent")
        # 黑名单拦截：被拉黑用户的消息不再受理、不存储、不调度 Turn。
        if await self.store.is_blacklisted(
            inbound.platform, inbound.account_id, inbound.sender_id
        ):
            return IngressResult(accepted=False, control=True)
        session = await self.store.get_session_by_route(
            inbound.platform, inbound.account_id, inbound.external_chat_id, inbound.chat_type
        )
        if session is None:
            raise NotFoundError("目标会话不存在")
        if inbound.sender_id not in {item.external_user_id for item in session.participants}:
            raise InputValidationError("发送者不是当前模拟会话成员")
        if inbound.plain_text.strip() == "/stop":
            self.request_interrupt(session.id, sender_id=inbound.sender_id)
            return IngressResult(accepted=True, control=True)
        for component in inbound.components:
            if component.type in {
                ComponentType.IMAGE_REF,
                ComponentType.AUDIO_REF,
                ComponentType.FILE_REF,
            }:
                self.attachment_validator.validate_attachment_ref(component)
        await self._validate_references(session.id, inbound.components)
        message, created = await self.store.append_inbound(inbound)
        if not created:
            if schedule_turn and schedule_duplicate:
                self._schedule_turn(session, delay_seconds=debounce_seconds)
            return IngressResult(accepted=True, duplicate=True, message=message)
        if (
            self.vision is not None
            and self.channel_capabilities.supports_processing(
                session.platform,
                "expression_asset",
            )
        ):
            persona = self.personas.get_for_chat_type(session.chat_type)
            await asyncio.gather(
                *(
                    self.vision.collect_inbound_expression(
                        component,
                        character_id=persona.character_id,
                    )
                    for component in message.components
                    if component.type == ComponentType.IMAGE_REF
                    and (
                        component.is_expression
                        or (component.description or "").startswith("QQ表情包")
                    )
                )
            )
        await self.events.publish("message.committed", message.model_dump(mode="json"))
        if schedule_turn:
            self._schedule_turn(session, delay_seconds=debounce_seconds)
        return IngressResult(accepted=True, message=message)

    async def _validate_references(self, session_id: str, components: list[MessageComponent]) -> None:
        for component in components:
            if component.type != ComponentType.QUOTE:
                continue
            quoted = await self.store.get_message(component.message_id or "")
            if quoted is None or quoted.session_id != session_id:
                raise InputValidationError("引用消息不存在或不属于当前会话")
            component.target_id = quoted.sender_id
            component.target_name = quoted.sender_name

    def _schedule_turn(
        self,
        session: SessionView,
        *,
        delay_seconds: float | None = None,
    ) -> None:
        if self._stopping:
            raise RuntimeError("聊天服务正在停止，拒绝调度新 Turn")
        existing = self._debounce_tasks.get(session.id)
        if existing is not None and not existing.done():
            existing.cancel()
        if delay_seconds is None:
            delay_ms = (
                self.settings.chat.private_debounce_ms
                if session.chat_type == ChatType.PRIVATE
                else self.settings.chat.group_debounce_ms
            )
            delay_seconds = delay_ms / 1000
        task = asyncio.create_task(self._debounced_process(session.id, delay_seconds))
        self._debounce_tasks[session.id] = task

    async def _debounced_process(self, session_id: str, delay: float) -> None:
        driver: asyncio.Task[None] | None = None
        try:
            if delay:
                await asyncio.sleep(delay)
            # 睡眠结束即退出“可取消防抖”阶段；生成期间的新消息只能排入下一 Turn。
            current = asyncio.current_task()
            if self._debounce_tasks.get(session_id) is current:
                self._debounce_tasks.pop(session_id, None)
            if current is not None:
                driver = current
                self._turn_driver_tasks.add(current)
            await self.process_session(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("处理聊天 Turn 失败", extra={"session_id": session_id})
            await self.events.publish(
                "turn.failed", {"session_id": session_id, "error": "聊天处理失败，请查看服务日志"}
            )
        finally:
            if driver is not None:
                self._turn_driver_tasks.discard(driver)

    def request_interrupt(self, session_id: str, *, sender_id: str) -> bool:
        """取消当前 Session 正在执行的响应式 Turn；控制帧不进入聊天历史。"""

        task = self._active_turn_tasks.get(session_id)
        if task is None or task.done():
            logger.info(
                "收到中断命令，但当前 Session 没有活动 Turn",
                extra={
                    "session_id": session_id,
                    "turn_id": "-",
                    "sender_id": sender_id,
                },
            )
            return False
        task.cancel()
        logger.info(
            "响应式 Turn 已请求中断",
            extra={
                "session_id": session_id,
                "turn_id": "-",
                "sender_id": sender_id,
            },
        )
        return True

    async def process_session(
        self, session_id: str, *, retry_turn_id: str | None = None
    ) -> None:
        """创建由服务持有的响应式 Turn task，并等待其明确终态。"""

        if self._stopping:
            raise RuntimeError("聊天服务正在停止，拒绝启动新 Turn")
        task = asyncio.create_task(
            self._run_session_turn(session_id, retry_turn_id=retry_turn_id),
            name=f"reactive-turn:{session_id}",
        )
        self._turn_tasks.add(task)
        try:
            await task
        finally:
            self._turn_tasks.discard(task)

    async def _run_session_turn(
        self, session_id: str, *, retry_turn_id: str | None = None
    ) -> None:
        """在 Session FIFO 锁内处理新消息，或按冻结批次人工恢复失败回复。"""

        lock = self._session_locks[session_id]
        await lock.acquire()
        current = asyncio.current_task()
        if current is None:
            lock.release()
            raise RuntimeError("响应式 Turn 缺少 asyncio task owner")
        self._active_turn_tasks[session_id] = current
        try:
            snapshot_time = utc_now()
            session = await self.store.get_session(session_id)
            if session is None:
                raise NotFoundError("会话不存在")
            history = await self.store.list_recallable_messages(
                session_id,
                self.settings.chat.recent_context_messages,
                received_before=snapshot_time,
            )
            memory_run: MemoryConsolidationRun | None = None
            if retry_turn_id is not None:
                decision = await self.store.get_decision(retry_turn_id)
                if decision is None or decision.session_id != session_id:
                    raise NotFoundError("待重试的回复 Turn 不存在或不属于当前会话")
                if decision.action != DecisionAction.REPLY:
                    raise InputValidationError("只有原本应回复的 Turn 可以人工重试")
                existing_batch = await self.store.list_reactive_outbound_batch(
                    session_id, decision.id
                )
                if existing_batch:
                    statuses: list[DeliveryStatus] = []
                    for message in existing_batch:
                        receipt = await self.store.get_delivery(message.id)
                        if receipt is None:
                            raise RuntimeError(
                                "响应式出站正文缺少对应投递状态"
                            )
                        statuses.append(receipt.status)
                    if statuses and all(
                        status == DeliveryStatus.SENT for status in statuses
                    ):
                        raise InputValidationError(
                            "该 Turn 已有真实送达的完整回复，禁止重复发送"
                        )
                    if any(
                        status in {DeliveryStatus.UNKNOWN, DeliveryStatus.DROPPED}
                        for status in statuses
                    ):
                        raise InputValidationError(
                            "该 Turn 存在无法确认或已撤销的投递，禁止自动重发"
                        )
                    await self._deliver_reactive_batch(
                        session,
                        existing_batch,
                        decision.id,
                        retry_failed=True,
                    )
                    return
                if await self.store.has_committed_reactive_reply(session_id, decision.id):
                    raise InputValidationError("该 Turn 已有真实送达的回复，禁止重复发送")
                pending = await self.store.list_turn_source_messages(session_id, decision.id)
                if not pending:
                    raise InputValidationError("待重试 Turn 缺少原始用户消息")
                # 重试必须重放原 Turn 的历史边界，不能吸收之后才到达的新用户消息。
                history = await self.store.list_recallable_messages(
                    session_id,
                    self.settings.chat.recent_context_messages,
                    received_before=decision.created_at,
                )
            else:
                group_policy: GroupParticipationPolicy | None = None
                pending = await self.store.list_pending_messages(
                    session_id, received_before=snapshot_time
                )
                if not pending:
                    return
                if session.chat_type == ChatType.PRIVATE:
                    decision = self.private_strategy.decide(session_id, pending)
                else:
                    group_policy = (
                        await self.store.get_group_participation_policy(session_id)
                    )
                    decision = self.group_strategy.decide(
                        session_id,
                        pending,
                        history,
                        policy=group_policy,
                        participants=session.participants,
                        now=snapshot_time,
                    )
                hard_gate = (
                    not decision.score_detail.get("forced")
                    and (
                        decision.score_detail.get("participation_mode") == "silent"
                        or decision.score_detail.get("reply_cooldown_remaining_seconds", 0) > 0
                        or decision.score_detail.get("idle_backoff_remaining_seconds", 0) > 0
                    )
                )
                if not hard_gate and needs_conversation_understanding(session, pending, history):
                    understanding = await understand_conversation(
                        settings=self.settings, model=self.model, prompting=self.prompting,
                        session=session, pending=pending, history=history, decision=decision,
                    )
                    decision.score_detail["conversation_understanding"] = understanding.model_dump(
                        mode="json"
                    )
                    if session.chat_type == ChatType.GROUP:
                        decision.score_detail["conversation_message_ids"] = understanding.relevant_message_ids
                        decision.trigger_message_id = understanding.target_message_id
                        if not decision.score_detail.get("forced"):
                            decision.score = max(0, round(
                                understanding.utility
                                * float(decision.score_detail["effective_frequency_factor"])
                            ) - int(decision.score_detail.get("presence_penalty", 0)))
                            decision.action = (
                                DecisionAction.REPLY if understanding.target_message_id is not None
                                and decision.score >= decision.threshold else DecisionAction.SILENCE
                            )
                            decision.reason = f"对话参与价值 {decision.score}，阈值 {decision.threshold}"
                            decision.score_detail["increment_idle_streak"] = (
                                decision.action == DecisionAction.SILENCE
                            )
                if decision.action == DecisionAction.REPLY:
                    reply_plan = self.reply_planner.plan(
                        session=session,
                        decision=decision,
                        pending=pending,
                    )
                    decision.score_detail["reply_plan"] = reply_plan.model_dump(mode="json")
                pending_ids = [item.id for item in pending]
                memory_run = self.memory.build_consolidation_run(
                    session=session,
                    source_messages=pending,
                    source_chain=MemorySourceChain.REACTIVE,
                    source_run_id=decision.id,
                )
                memory_run_created = False
                if group_policy is not None:
                    memory_run, memory_run_created = (
                        await self.store.save_group_reactive_turn(
                            decision,
                            pending_ids,
                            expected_state_version=group_policy.state_version,
                            external_at=max(item.created_at for item in pending),
                            observed_at=snapshot_time,
                            run=memory_run,
                        )
                    )
                elif memory_run is None:
                    await self.store.save_decision(decision, pending_ids)
                else:
                    memory_run, memory_run_created = (
                        await self.store.save_reactive_turn_with_memory_run(
                            decision, pending_ids, memory_run
                        )
                    )
                if memory_run is not None:
                    await self.memory.publish_enqueued_consolidation(
                        memory_run, created=memory_run_created
                    )
                await self.events.publish("turn.decision", decision.model_dump(mode="json"))
                # 用户消息进入权威历史后就应独立归档；合法沉默、模型失败和投递失败
                # 都不能让长期记忆链路漏掉这批证据。
                await self._schedule_profile_extractions(session, pending)
                self.social_learning.schedule_feedback(session)
                await self.social_learning.maybe_enqueue(
                    session=session,
                    source_run_id=decision.id,
                )
            logger.info(
                "Turn 决策",
                extra={
                    "session_id": session_id,
                    "turn_id": decision.id,
                    "action": decision.action.value,
                    "pending_count": len(pending),
                },
            )
            if decision.action == DecisionAction.SILENCE:
                await self._start_reactive_memory(memory_run)
                return

            raw_plan = decision.score_detail.get("reply_plan")
            reply_plan = self.reply_planner.restore(
                payload=raw_plan,
                session=session,
                decision=decision,
                pending=pending,
            )
            pending_by_id = {item.id: item for item in pending}
            relevant_messages = [
                pending_by_id[message_id]
                for message_id in reply_plan.relevant_message_ids
            ]
            raw_understanding = decision.score_detail.get("conversation_understanding")
            understanding = (
                ConversationUnderstanding.model_validate(raw_understanding)
                if raw_understanding is not None else None
            )
            contextual_messages = [
                item for item in history
                if understanding is not None and item.id in understanding.history_message_ids
            ]
            conversation_messages = contextual_messages + relevant_messages

            persona = self.personas.get_for_chat_type(session.chat_type)
            facts = await self.store.list_facts(scope_key_for(session))
            memories = await self.memory.retrieve(
                session=session,
                query=(understanding.retrieval_query if understanding and understanding.retrieval_query else
                       self.prompting.project_messages_text(
                    session,
                    conversation_messages,
                )),
                context_budget=True,
            )
            summaries = await self.memory.latest_conversation_summaries(session=session)
            messages_for_prompt = history[-self.settings.chat.recent_context_messages :]
            required_messages = conversation_messages if session.chat_type == ChatType.GROUP else (
                contextual_messages + pending
            )
            required_ids = {item.id for item in required_messages}
            # 选中的对话可能早于最近历史窗口，不能只保住问题却丢掉其引用证据。
            remaining = max(0, self.settings.chat.recent_context_messages - len(required_messages))
            retained = [item for item in messages_for_prompt if item.id not in required_ids]
            messages_for_prompt = sorted(
                required_messages + (retained[-remaining:] if remaining else []),
                key=lambda item: (item.created_at, item.id),
            )
            learning_context = await self.social_learning.prepare_reply_context(
                session=session,
                messages=conversation_messages,
                turn_id=decision.id,
            )
            channel_runtime = await self._channel_runtime_context(session)
            if (
                self.vision is not None
                and "image_description"
                in channel_runtime.supported_processing
            ):
                with model_observation_scope(
                    task="vision.analyze",
                    profile="vision",
                    session_id=session_id,
                    turn_id=decision.id,
                    run_id=decision.id,
                ):
                    messages_for_prompt = await self.vision.enrich_messages(
                        messages_for_prompt
                    )
            candidate_tools = self._allowed_reactive_tools(
                session,
                reply_plan.address_sender_id,
            )
            authorization_scopes = self.settings.authorization.scopes_for(
                platform=session.platform,
                account_id=session.account_id,
                actor_id=reply_plan.address_sender_id,
            )
            tool_context = ToolContext(
                session_id=session_id,
                actor_id=reply_plan.address_sender_id,
                turn_id=decision.id,
                source_text=self.prompting.project_messages_text(
                    session,
                    # 对话证据可以跨成员，工具授权文本仍只属于本轮行动发起人。
                    [item for item in relevant_messages
                     if item.sender_id == reply_plan.address_sender_id],
                ),
                character_id=persona.character_id,
                authorization_scopes=authorization_scopes,
                supported_egress_components=(
                    self.channel_capabilities.supported_egress_components(
                        session.platform
                    )
                ),
            )
            available_skills = await self._available_skills(
                authorization_scopes,
                context=tool_context,
            )
            tool_context.available_skills.update(available_skills)
            if available_skills:
                candidate_tools.add("load_skill")
                candidate_tools.update(
                    self.skill_plugins.tool_names(
                        available_skills,
                        authorization_scopes,
                    )
                )
            allowed_tools = (
                {
                    item.name
                    for item in self.tool_registry.definitions(
                        candidate_tools,
                        authorization_scopes=authorization_scopes,
                    )
                }
                if self.settings.model.supports_tools
                else set()
            )
            prompt_tools = (
                {
                    item.name
                    for item in self.tool_registry.definitions(
                        allowed_tools,
                        authorization_scopes=authorization_scopes,
                        loaded_skills=set(),
                    )
                }
                if self.settings.model.supports_tools
                else set()
            )
            default_zone = ZoneInfo(self.settings.schedule.default_timezone)
            local_now = utc_now().astimezone(default_zone)
            prompt_messages = self.prompting.build_chat(
                session=session,
                persona=persona,
                messages=messages_for_prompt,
                facts=facts,
                available_tools=prompt_tools,
                skills_summary=self.skills.summary(
                    available_skills,
                    authorization_scopes,
                ),
                request_time=local_now.isoformat(),
                timezone=self.settings.schedule.default_timezone,
                channel_runtime=channel_runtime,
                memories=memories,
                summaries=summaries,
                reply_plan=reply_plan,
                learning_context=learning_context,
                conversation_context=understanding.model_dump(mode="json") if understanding else None,
                include_images=(
                    self.settings.model.supports_vision
                    and "image_description"
                    in channel_runtime.supported_prompt_projections
                    and not (
                        self.vision is not None
                        and self.vision.uses_external_model
                    )
                ),
                required_message_ids=required_ids,
                input_token_budget=(
                    self.settings.model.context_window_tokens
                    - self.settings.model.max_tokens
                ),
            )
            await self.events.publish("model.started", {"session_id": session_id, "turn_id": decision.id})
            system_prompt = prompt_messages[0].content or ""
            prompt_sha256 = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
            logger.info(
                (
                    "模型生成开始 character_id=%s persona_revision=%s "
                    "persona_prompt_sha256=%s prompt_sha256=%s prompt_chars=%s "
                    "history_messages=%s/%s scene=%s"
                ),
                persona.character_id,
                persona.revision,
                persona.prompt_sha256,
                prompt_sha256,
                len(system_prompt),
                len(messages_for_prompt),
                self.settings.chat.recent_context_messages,
                session.chat_type.value,
                extra={
                    "session_id": session_id,
                    "turn_id": decision.id,
                    "supports_tools": self.settings.model.supports_tools,
                    "character_id": persona.character_id,
                    "persona_revision": persona.revision,
                    "persona_prompt_sha256": persona.prompt_sha256,
                    "prompt_sha256": prompt_sha256,
                    "prompt_chars": len(system_prompt),
                    "history_message_count": len(messages_for_prompt),
                    "history_message_limit": self.settings.chat.recent_context_messages,
                    "scene": session.chat_type.value,
                    "prompt_input_tokens_estimate": estimate_messages_tokens(
                        prompt_messages
                    ),
                    "prompt_input_token_budget": (
                        self.settings.model.context_window_tokens
                        - self.settings.model.max_tokens
                    ),
                },
            )
            try:
                if self.settings.model.supports_tools:
                    draft = await self.tool_loop.run(
                        model=self.model,
                        messages=prompt_messages,
                        context=tool_context,
                        allowed_tools=allowed_tools,
                    )
                else:
                    request = ModelRequest(
                        messages=prompt_messages,
                        model=self.settings.model.name,
                        temperature=self.settings.model.temperature,
                        max_tokens=self.settings.model.max_tokens,
                    )
                    if self.settings.model.supports_streaming:
                        chunks: list[str] = []
                        streaming_model = cast(StreamingModelProvider, self.model)
                        if not hasattr(streaming_model, "stream"):
                            raise InputValidationError("当前模型 Provider 不支持流式文本输出")
                        with model_observation_scope(
                            task="chat.reply",
                            session_id=session_id,
                            turn_id=decision.id,
                            run_id=decision.id,
                        ):
                            async for event in streaming_model.stream(request):
                                if event.delta:
                                    chunks.append(event.delta)
                                    await self.events.publish(
                                        "model.delta",
                                        {
                                            "session_id": session_id,
                                            "turn_id": decision.id,
                                            "delta": event.delta,
                                        },
                                    )
                        content = "".join(chunks)
                    else:
                        with model_observation_scope(
                            task="chat.reply",
                            session_id=session_id,
                            turn_id=decision.id,
                            run_id=decision.id,
                        ):
                            result = await self.model.complete(request)
                        content = result.content or ""
                    if not content:
                        raise InvalidModelResponseError("模型没有返回聊天文本")
                    draft = draft_from_model_text(
                        content,
                        # 无原生工具能力时，应用层仍需兑现人格要求的聊天气泡节奏。
                        split_short_lines=True,
                    )
                draft = validate_reply_draft(draft, reply_plan)
            except Exception as exc:
                failed_outbound = OutboundMessage(
                    session_id=session_id,
                    components=[MessageComponent.text_component("[模型生成失败，未发送]")],
                    reply_to_message_id=reply_plan.target_message_id,
                )
                receipt = DeliveryReceipt(
                    outbound_id=failed_outbound.id,
                    status=DeliveryStatus.FAILED,
                    error_code=getattr(exc, "code", "provider_error"),
                    error_message=str(exc)[:500],
                )
                await self.store.save_delivery(failed_outbound, receipt)
                await self.events.publish(
                    "model.failed",
                    {"session_id": session_id, "turn_id": decision.id, "error_code": receipt.error_code},
                )
                logger.warning(
                    "模型生成失败",
                    extra={
                        "session_id": session_id,
                        "turn_id": decision.id,
                        "error_code": receipt.error_code,
                    },
                )
                # 模型或工具失败时仍归档用户证据；此时不存在成功的显式记忆操作，
                # 后台归档可以按自己的候选协议处理明确纠正/忘记。
                await self._start_reactive_memory(memory_run)
                raise

            await self.events.publish(
                "model.finished", {"session_id": session_id, "turn_id": decision.id}
            )

            try:
                filtered_draft = await self._apply_egress_filter(
                    draft,
                    prompt_messages=prompt_messages,
                    session_id=session_id,
                    turn_id=decision.id,
                    split_short_lines=True,
                )
                draft = validate_reply_draft(filtered_draft, reply_plan)
            except Exception as exc:
                logger.exception(
                    "输出过滤异常，已阻止发送原始草稿",
                    extra={"session_id": session_id, "turn_id": decision.id},
                )
                failed_outbound = OutboundMessage(
                    session_id=session_id,
                    components=[
                        MessageComponent.text_component("[输出过滤失败，未发送]")
                    ],
                    reply_to_message_id=reply_plan.target_message_id,
                )
                receipt = DeliveryReceipt(
                    outbound_id=failed_outbound.id,
                    status=DeliveryStatus.FAILED,
                    error_code="egress_filter_failed",
                    error_message=str(exc)[:500],
                )
                await self.store.save_delivery(failed_outbound, receipt)
                await self.events.publish(
                    "reply.filter_failed",
                    {
                        "session_id": session_id,
                        "turn_id": decision.id,
                        "error_code": receipt.error_code,
                    },
                )
                await self._start_reactive_memory(memory_run)
                raise

            try:
                message_groups = [draft.components, *draft.follow_up_components]
                batch_created_at = utc_now()
                outbounds = [
                    OutboundMessage(
                        id=self._reactive_outbound_id(decision.id, index),
                        session_id=session_id,
                        components=components,
                        # 多气泡回复只在第一条绑定原消息，避免平台重复展示引用。
                        reply_to_message_id=(
                            reply_plan.target_message_id if index == 0 else None
                        ),
                        origin=MessageOrigin.REACTIVE,
                        origin_run_id=decision.id,
                        source_refs=[item.id for item in conversation_messages],
                        created_at=batch_created_at
                        + timedelta(microseconds=index),
                    )
                    for index, components in enumerate(message_groups)
                ]
                await self.outbound.prepare_batch(outbounds)
                await self._deliver_reactive_batch(
                    session,
                    outbounds,
                    decision.id,
                    retry_failed=False,
                )
            finally:
                # 显式 remember/correct/forget 工具先完成，再启动后台归档。这样归档读取
                # 到的是工具提交后的活跃版本，不会与同一 Turn 的用户授权修改竞态。
                await self._start_reactive_memory(memory_run)
        finally:
            if self._active_turn_tasks.get(session_id) is current:
                self._active_turn_tasks.pop(session_id, None)
            lock.release()

    @staticmethod
    def _reactive_outbound_id(turn_id: str, index: int) -> str:
        """为回复气泡生成可跨重启复用、同时保留顺序的稳定出站 ID。"""

        digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()[:24]
        return f"out_reactive_{digest}_{index:02d}"

    async def _deliver_reactive_batch(
        self,
        session: SessionView,
        messages: list[OutboundMessage],
        turn_id: str,
        *,
        retry_failed: bool,
    ) -> bool:
        """严格按序推进响应式批次；失败、未知或阻塞时不越过当前气泡。"""

        prior_sent = True
        for message in messages:
            receipt = await self.store.get_delivery(message.id)
            if receipt is None:
                raise RuntimeError("响应式出站批次缺少持久化回执")
            if receipt.status == DeliveryStatus.BLOCKED:
                if not prior_sent:
                    return False
                receipt = await self.outbound.promote_blocked(message)
            elif receipt.status == DeliveryStatus.FAILED and retry_failed:
                receipt = await self.outbound.retry_failed(message)
            elif receipt.status in {
                DeliveryStatus.FAILED,
                DeliveryStatus.UNKNOWN,
                DeliveryStatus.DROPPED,
            }:
                await self._publish_reactive_delivery_failure(
                    session.id, turn_id, receipt
                )
                return False

            receipt, committed = await self.outbound.deliver(session, message)
            if receipt.status != DeliveryStatus.SENT or committed is None:
                prior_sent = False
                await self._publish_reactive_delivery_failure(
                    session.id, turn_id, receipt
                )
                return False
            prior_sent = True
            await self.social_learning.record_reply_delivery(
                turn_id=turn_id,
                message_id=committed.id,
            )
        return True

    async def _publish_reactive_delivery_failure(
        self,
        session_id: str,
        turn_id: str,
        receipt: DeliveryReceipt,
    ) -> None:
        """公开响应式投递失败，但不把未送达正文写入聊天历史。"""

        await self.events.publish(
            "reply.delivery_failed",
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "delivery_status": receipt.status.value,
                "error_code": receipt.error_code,
            },
        )

    async def _recover_reactive_outbound_batches(self) -> None:
        """在 Session FIFO 锁内恢复响应式批次，不自动重试明确失败。"""

        for messages in await self.store.list_recoverable_reactive_outbound_batches():
            if not messages:
                continue
            session = await self.store.get_session(messages[0].session_id)
            if session is None:
                logger.error(
                    "响应式恢复批次对应会话不存在",
                    extra={
                        "session_id": messages[0].session_id,
                        "turn_id": messages[0].origin_run_id or "-",
                    },
                )
                continue
            turn_id = messages[0].origin_run_id
            if turn_id is None:
                logger.error(
                    "响应式恢复批次缺少 Turn ID",
                    extra={"session_id": session.id, "turn_id": "-"},
                )
                continue
            async with self._session_locks[session.id]:
                await self._deliver_reactive_batch(
                    session,
                    messages,
                    turn_id,
                    retry_failed=False,
                )

    async def retry_reactive_reply(
        self, session_id: str, turn_id: str
    ) -> dict[str, str]:
        """人工重试一个未送达的回复，不重复归档用户证据。"""

        await self.process_session(session_id, retry_turn_id=turn_id)
        return {"session_id": session_id, "turn_id": turn_id, "status": "retried"}

    async def reactive_reply_retryable(
        self,
        session_id: str,
        decision: TurnDecision,
    ) -> bool:
        """按权威出站状态判断控制台是否应展示人工重试。"""

        if (
            decision.session_id != session_id
            or decision.action != DecisionAction.REPLY
        ):
            return False
        batch = await self.store.list_reactive_outbound_batch(
            session_id,
            decision.id,
        )
        if not batch:
            return not await self.store.has_committed_reactive_reply(
                session_id,
                decision.id,
            )
        statuses: list[DeliveryStatus] = []
        for message in batch:
            receipt = await self.store.get_delivery(message.id)
            if receipt is None:
                raise RuntimeError("响应式出站正文缺少对应投递状态")
            statuses.append(receipt.status)
        if all(status == DeliveryStatus.SENT for status in statuses):
            return False
        return not any(
            status
            in {
                DeliveryStatus.DISPATCHING,
                DeliveryStatus.UNKNOWN,
                DeliveryStatus.DROPPED,
            }
            for status in statuses
        )

    async def _start_reactive_memory(
        self,
        run: MemoryConsolidationRun | None,
    ) -> None:
        """在显式工具状态稳定后启动已持久化的本轮归档任务。"""

        if run is not None:
            await self.memory.start_consolidation(run.id)

    def _allowed_reactive_tools(self, session: SessionView, actor_id: str) -> set[str]:
        allowed = {
            "get_current_time",
            "get_weather",
            "fetch_history",
            "search_messages",
            "fetch_source",
            "query_profile",
            "send_messages",
        }
        if self.settings.memory.enabled:
            allowed.update(
                {"recall_memory", "remember", "forget_memory", "correct_memory"}
            )
        member = next((item for item in session.participants if item.external_user_id == actor_id), None)
        if session.chat_type == ChatType.PRIVATE or (
            member is not None and member.role in {ParticipantRole.OWNER, ParticipantRole.ADMIN}
        ):
            allowed.update(
                {
                    "schedule_create",
                    "schedule_list",
                    "schedule_update",
                    "schedule_pause",
                    "schedule_resume",
                    "schedule_delete",
                }
            )
        if session.chat_type == ChatType.PRIVATE:
            allowed.update(
                {"feed_subscribe", "feed_list", "feed_unsubscribe"}
            )
        supported_egress = (
            self.channel_capabilities.supported_egress_components(
                session.platform
            )
        )
        if supported_egress & {
            ComponentType.AUDIO_REF.value,
            ComponentType.FILE_REF.value,
        }:
            allowed.add("send_attachment")
        return allowed

    async def _available_skills(
        self,
        authorization_scopes: frozenset[str] = frozenset(),
        *,
        context: ToolContext | None = None,
    ) -> set[str]:
        """只开放当前会话、授权、形象和运行态真实支持的 Skill。"""

        if not self.settings.model.supports_tools:
            return set()
        return await self.skill_plugins.available_skills(
            authorization_scopes,
            context=context,
        )

    async def _channel_runtime_context(
        self,
        session: SessionView,
    ) -> ChannelRuntimeContext:
        """读取可选 Channel 临时状态；无扩展的 Channel 返回空上下文。"""

        handler = getattr(self.channel, "runtime_context", None)
        result = (
            await cast(
                ChannelRuntimeContextProvider,
                self.channel,
            ).runtime_context(session)
            if callable(handler)
            else ChannelRuntimeContext()
        )
        if not isinstance(result, ChannelRuntimeContext):
            raise TypeError("Channel runtime_context 必须返回 ChannelRuntimeContext")
        return replace(
            result,
            supported_processing=frozenset(
                capability
                for capability in (
                    "image_description",
                    "expression_asset",
                    "audio_transcript",
                    "ocr_text",
                    "forward_expansion",
                )
                if self.channel_capabilities.supports_processing(
                    session.platform,
                    capability,
                )
            ),
            supported_prompt_projections=(
                self.channel_capabilities.supported_prompt_projections(
                    session.platform
                )
            ),
            supported_egress_components=(
                self.channel_capabilities.supported_egress_components(
                    session.platform
                )
            ),
            capability_summary=self.channel_capabilities.prompt_summary(
                session.platform
            ),
        )

    async def _mark_expression_used(self, outbound: OutboundMessage) -> None:
        """从受信任图片组件恢复表达 ID，并仅在 sent 后累计使用。"""

        expression_ids = {
            component.attachment_id
            for component in outbound.components
            if component.type == ComponentType.IMAGE_REF
            and (component.attachment_id or "").startswith("expression_")
        }
        await self.store.record_expression_usage(
            outbound.id,
            {expression_id for expression_id in expression_ids if expression_id is not None},
        )

    async def execute_scheduled(self, schedule: ScheduleTask, run: ScheduleRun) -> None:
        """在原 Session FIFO 中执行一次后台任务并提交可追踪结果。"""

        async with self._session_locks[schedule.session_id]:
            session = await self.store.get_session(schedule.session_id)
            if session is None:
                raise NotFoundError("周期任务对应会话不存在")
            run.status = ScheduleRunStatus.RUNNING
            run.started_at = utc_now()
            run.updated_at = run.started_at
            run.outbound_id = "out_schedule_" + hashlib.sha256(run.id.encode()).hexdigest()[:32]
            await self.store.save_schedule_run(run)
            await self.events.publish("schedule.run.updated", run.model_dump(mode="json"))
            try:
                persona = self.personas.get_for_chat_type(session.chat_type)
                existing_receipt = await self.store.get_delivery(run.outbound_id)
                if existing_receipt is not None and existing_receipt.status in {
                    DeliveryStatus.PREPARED,
                    DeliveryStatus.DISPATCHING,
                    DeliveryStatus.SENT,
                }:
                    recovered = await self.store.get_outbound_message(run.outbound_id)
                    if recovered is None:
                        raise RuntimeError("在途回执缺少可恢复的出站正文")
                    recovered_receipt, committed = await self.outbound.deliver(
                        session, recovered
                    )
                    if (
                        recovered_receipt.status != DeliveryStatus.SENT
                        or committed is None
                    ):
                        raise RuntimeError(
                            "恢复的周期任务投递未确认成功: "
                            f"{recovered_receipt.status.value}"
                        )
                    run.status = ScheduleRunStatus.COMPLETED
                    schedule.consecutive_failures = 0
                    schedule.last_run_at = run.scheduled_for
                    await self._record_scheduled_memory(
                        session, schedule, run, committed
                    )
                    return
                prior_executions = await self.store.list_tool_executions(
                    schedule_run_id=run.id
                )
                recovered_draft = await self._recover_scheduled_skill_reply(
                    run, prior_executions
                )
                if recovered_draft is not None:
                    recovered_outbound = OutboundMessage(
                        id=run.outbound_id,
                        session_id=session.id,
                        components=recovered_draft.components,
                        origin=MessageOrigin.SCHEDULED,
                        origin_run_id=run.id,
                    )
                    recovered_receipt, committed = await self.outbound.deliver(
                        session, recovered_outbound
                    )
                    if (
                        recovered_receipt.status != DeliveryStatus.SENT
                        or committed is None
                    ):
                        raise RuntimeError("恢复的周期表情消息投递失败")
                    run.status = ScheduleRunStatus.COMPLETED
                    schedule.consecutive_failures = 0
                    schedule.last_run_at = run.scheduled_for
                    await self._record_scheduled_memory(
                        session, schedule, run, committed
                    )
                    return
                facts = await self.store.list_facts(scope_key_for(session))
                memories = await self.memory.retrieve(
                    session=session,
                    query=schedule.instruction,
                    context_budget=True,
                )
                candidate_tools = {"get_current_time", "get_weather"}
                authorization_scopes: frozenset[str] = frozenset()
                tool_context = ToolContext(
                    session_id=session.id,
                    actor_id=None,
                    schedule_run_id=run.id,
                    source_text=schedule.instruction,
                    character_id=persona.character_id,
                    authorization_scopes=authorization_scopes,
                    supported_egress_components=(
                        self.channel_capabilities.supported_egress_components(
                            session.platform
                        )
                    ),
                )
                available_skills = await self._available_skills(
                    authorization_scopes,
                    context=tool_context,
                )
                tool_context.available_skills.update(available_skills)
                if available_skills:
                    candidate_tools.add("load_skill")
                    candidate_tools.update(
                        self.skill_plugins.tool_names(
                            available_skills,
                            authorization_scopes,
                        )
                    )
                allowed_tools = (
                    {
                        item.name
                        for item in self.tool_registry.definitions(
                            candidate_tools,
                            authorization_scopes=authorization_scopes,
                        )
                    }
                    if self.settings.model.supports_tools
                    else set()
                )
                prompt_tools = (
                    {
                        item.name
                        for item in self.tool_registry.definitions(
                            allowed_tools,
                            authorization_scopes=authorization_scopes,
                            loaded_skills=set(),
                        )
                    }
                    if self.settings.model.supports_tools
                    else set()
                )
                default_zone = ZoneInfo(self.settings.schedule.default_timezone)
                local_now = utc_now().astimezone(default_zone)
                messages = self.prompting.build_scheduled(
                    session=session,
                    persona=persona,
                    instruction=schedule.instruction,
                    facts=facts,
                    available_tools=prompt_tools,
                    skills_summary=self.skills.summary(
                        available_skills,
                        authorization_scopes,
                    ),
                    request_time=local_now.isoformat(),
                    timezone=self.settings.schedule.default_timezone,
                    channel_runtime=await self._channel_runtime_context(session),
                    memories=memories,
                )
                if self.settings.model.supports_tools:
                    draft = await self.tool_loop.run(
                        model=self.model,
                        messages=messages,
                        context=tool_context,
                        allowed_tools=allowed_tools,
                    )
                else:
                    with model_observation_scope(
                        task="schedule.reply",
                        session_id=session.id,
                        turn_id=run.id,
                        run_id=run.id,
                    ):
                        result = await self.model.complete(
                            ModelRequest(
                                messages=messages,
                                model=self.settings.model.name,
                                temperature=self.settings.model.temperature,
                                max_tokens=self.settings.model.max_tokens,
                            )
                        )
                    if not result.content:
                        raise InvalidModelResponseError("模型没有返回周期任务文本")
                    draft = ReplyDraft(
                        components=[MessageComponent.text_component(result.content)]
                    )
                draft = await self._apply_egress_filter(
                    draft,
                    prompt_messages=messages,
                    session_id=session.id,
                    turn_id=run.id,
                    split_short_lines=False,
                )
                outbound = OutboundMessage(
                    id=run.outbound_id,
                    session_id=session.id,
                    components=draft.components,
                    origin=MessageOrigin.SCHEDULED,
                    origin_run_id=run.id,
                )
                receipt, committed = await self.outbound.deliver(session, outbound)
                if receipt.status != DeliveryStatus.SENT or committed is None:
                    raise RuntimeError(f"周期任务消息投递失败: {receipt.error_code or receipt.status.value}")
                await self._record_scheduled_memory(
                    session, schedule, run, committed
                )
            except Exception as exc:
                run.status = ScheduleRunStatus.FAILED
                run.error_code = getattr(exc, "code", "schedule_run_failed")
                run.error_message = str(exc)[:500]
                schedule.consecutive_failures += 1
                if schedule.consecutive_failures >= 3:
                    schedule.status = ScheduleStatus.PAUSED
                    schedule.next_run_at = None
                    await self._send_schedule_paused_alert(session, schedule)
                logger.exception(
                    "周期任务执行失败",
                    extra={"session_id": session.id, "schedule_run_id": run.id},
                )
            else:
                run.status = ScheduleRunStatus.COMPLETED
                run.error_code = None
                run.error_message = None
                schedule.consecutive_failures = 0
                schedule.last_run_at = run.scheduled_for
            finally:
                now = utc_now()
                run.completed_at = now
                run.updated_at = now
                schedule.updated_at = now
                # 先持久化任务累计状态，再公开 Run 终态，避免紧随其后的 run-now
                # 看到 FAILED 后仍读到旧的连续失败次数。
                await self.store.save_schedule_runtime(schedule)
                await self.store.save_schedule_run(run)
                await self.events.publish("schedule.run.updated", run.model_dump(mode="json"))
                await self.events.publish("schedule.updated", schedule.model_dump(mode="json"))

    async def _recover_scheduled_skill_reply(
        self, run: ScheduleRun, executions: list[Any]
    ) -> ReplyDraft | None:
        """通过通用插件钩子恢复已落库但尚未 PREPARED 的 Skill 草稿。"""

        recovered = await self.skill_plugins.recover_schedule(run.id, executions)
        if recovered is not None and not isinstance(recovered, ReplyDraft):
            raise RuntimeError("Skill 周期恢复钩子返回了无效草稿")
        return recovered

    async def _record_scheduled_memory(
        self,
        session: SessionView,
        schedule: ScheduleTask,
        run: ScheduleRun,
        committed: StoredMessage,
    ) -> None:
        """只在真实送达后记录周期结果，记忆故障不改写投递终态。"""

        await self.memory.try_record_chain_outcome(
            session=session,
            source_chain=MemorySourceChain.SCHEDULED,
            source_run_id=run.id,
            content=f"周期任务「{schedule.title}」已完成：{committed.plain_text}",
            source_message_ids=[committed.id],
            source_refs=[schedule.id, run.id],
        )

    async def _send_schedule_paused_alert(self, session: SessionView, schedule: ScheduleTask) -> None:
        outbound = OutboundMessage(
            session_id=session.id,
            components=[
                MessageComponent.text_component(
                    f"周期任务“{schedule.title}”连续失败 3 次，已自动暂停。"
                )
            ],
            origin=MessageOrigin.SCHEDULED,
            origin_run_id=schedule.id,
        )
        await self.outbound.deliver(session, outbound)

    async def _schedule_profile_extractions(
        self, session: SessionView, source_messages: list[StoredMessage]
    ) -> None:
        by_subject: defaultdict[str, list[StoredMessage]] = defaultdict(list)
        for message in source_messages:
            by_subject[message.sender_id].append(message)
        for subject_id, messages in by_subject.items():
            run = ProfileExtractionRun(
                session_id=session.id,
                subject_id=subject_id,
                scope_key=scope_key_for(session),
                source_message_ids=[message.id for message in messages],
                data_epoch=session.data_epoch,
            )
            await self.store.save_extraction_run(run)
            await self.events.publish("profile.pending", run.model_dump(mode="json"))
            self._track_task(
                asyncio.create_task(self._execute_extraction(run)),
                session_id=run.session_id,
            )

    async def retry_extraction(self, run_id: str) -> ProfileExtractionRun:
        run = await self.store.get_extraction_run(run_id)
        if run is None:
            raise NotFoundError("画像提取任务不存在")
        if run.status not in {ExtractionStatus.FAILED, ExtractionStatus.PENDING}:
            raise InputValidationError("只有失败或待处理任务可以重试")
        run.status = ExtractionStatus.PENDING
        run.error_code = None
        run.error_message = None
        run.updated_at = utc_now()
        await self.store.save_extraction_run(run)
        self._track_task(
            asyncio.create_task(self._execute_extraction(run)),
            session_id=run.session_id,
        )
        return run

    async def _execute_extraction(self, run: ProfileExtractionRun) -> None:
        run.status = ExtractionStatus.RUNNING
        run.attempt_count += 1
        run.updated_at = utc_now()
        await self.store.save_extraction_run(run)
        try:
            session = await self.store.get_session(run.session_id)
            if session is None:
                raise NotFoundError("画像任务对应会话不存在")
            if not await self.store.messages_are_recallable(
                run.session_id, run.source_message_ids
            ):
                run.status = ExtractionStatus.COMPLETED
                run.error_code = None
                run.error_message = None
                return
            messages = [
                message
                for message_id in run.source_message_ids
                if (message := await self.store.get_message(message_id)) is not None
            ]
            with model_observation_scope(
                task="profile.extract",
                profile="profile",
                session_id=run.session_id,
                run_id=run.id,
            ):
                result = await self.profile_model.complete(
                    ModelRequest(
                        messages=self.prompting.build_profile_extraction(
                            session=session,
                            subject_id=run.subject_id,
                            messages=messages,
                        ),
                        model=self.settings.model.profile_name,
                        temperature=0,
                        max_tokens=min(800, self.settings.model.max_tokens),
                        json_mode=True,
                    )
                )
            raw = result.content
            if not raw:
                raise InvalidModelResponseError("画像提取模型没有返回文本")
            try:
                payload = ExtractionPayload.model_validate(parse_model_json(raw))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise InvalidModelResponseError(f"画像提取结果不是合法 schema: {exc}") from exc
            allowed_sources = set(run.source_message_ids)
            facts: list[ProfileFact] = []
            for candidate in payload.facts:
                if not set(candidate.source_message_ids).issubset(allowed_sources):
                    raise InvalidModelResponseError("画像事实引用了本次任务范围外的消息")
                facts.append(
                    ProfileFact(
                        subject_id=run.subject_id,
                        scope_key=run.scope_key,
                        category=candidate.category,
                        content=candidate.content,
                        confidence=candidate.confidence,
                        source_message_ids=candidate.source_message_ids,
                    )
                )
            _, changed_memories = await self.store.commit_profile_extraction(
                run=run,
                facts=facts,
            )
            for memory in changed_memories:
                await self.events.publish(
                    "memory.updated", memory.model_dump(mode="json")
                )
            run.status = ExtractionStatus.COMPLETED
            run.error_code = None
            run.error_message = None
        except asyncio.CancelledError:
            run.status = ExtractionStatus.CANCELLED
            run.error_code = "session_lifecycle_cancelled"
            run.error_message = "画像任务因会话清空或删除被取消"
            raise
        except ConflictError as exc:
            run.status = ExtractionStatus.STALE
            run.error_code = "session_data_epoch_stale"
            run.error_message = str(exc)[:500]
        except Exception as exc:
            run.status = ExtractionStatus.FAILED
            run.error_code = getattr(exc, "code", "profile_extraction_failed")
            run.error_message = str(exc)[:500]
            logger.exception(
                "画像提取失败",
                extra={"session_id": run.session_id, "extraction_run_id": run.id},
            )
        finally:
            run.updated_at = utc_now()
            await self.store.save_extraction_run(run)
            await self.events.publish("profile.updated", run.model_dump(mode="json"))

    def _track_task(
        self,
        task: asyncio.Task[Any],
        *,
        session_id: str | None = None,
    ) -> None:
        """登记后台任务归属，使清空/删除能先停止旧 epoch 的写 owner。"""

        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        if session_id is None:
            return
        owned = self._background_tasks_by_session[session_id]
        owned.add(task)

        def discard(completed: asyncio.Task[Any]) -> None:
            current = self._background_tasks_by_session.get(session_id)
            if current is None:
                return
            current.discard(completed)
            if not current:
                self._background_tasks_by_session.pop(session_id, None)

        task.add_done_callback(discard)
