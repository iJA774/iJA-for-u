"""基于消息与 typing 空闲窗口的私聊入站缓冲。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace

from pydantic import BaseModel, Field, ValidationError, model_validator

from application.service import IngressResult
from domain.models import ChatType
from plugins._host import (
    InboundEnvelope,
    IngressHandler,
    IngressPluginContext,
    PluginUnavailableError,
    TypingEvent,
    TypingHandler,
)

logger = logging.getLogger(__name__)


class DelayedReplySettings(BaseModel):
    """私聊消息和输入状态对应的固定空闲窗口。"""

    message_delay_seconds: float = Field(default=5.0, gt=0, le=60)
    typing_delay_seconds: float = Field(default=10.0, gt=0, le=60)

    @model_validator(mode="after")
    def validate_delays(self) -> DelayedReplySettings:
        if self.typing_delay_seconds < self.message_delay_seconds:
            raise ValueError("typing_delay_seconds 不得短于 message_delay_seconds")
        return self


@dataclass(slots=True)
class _PendingBatch:
    """单个私聊路由尚未提交给聊天核心的消息批次。"""

    envelopes: list[InboundEnvelope]
    call_next: IngressHandler
    external_message_ids: set[str] = field(default_factory=set)
    typing_extended: bool = False
    generation: int = 0
    timer: asyncio.Task[None] | None = None


class DelayedReplyPlugin:
    """只在私聊中等待用户完成分段输入，再按序提交整批消息。"""

    plugin_id = "delayed_reply"

    def __init__(
        self,
        settings: DelayedReplySettings,
        context: IngressPluginContext,
    ) -> None:
        self.settings = settings
        self.context = context
        self._batches: dict[str, _PendingBatch] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False

    async def stop(self) -> None:
        """取消计时并提交剩余批次，确保正常关机不丢消息。"""

        async with self._lock:
            self._stopping = True
            batches = list(self._batches.values())
            self._batches.clear()
            tasks = list(self._tasks)
            # 只取消仍在等待的当前计时器；已经越过超时边界、正在向核心
            # 提交的任务必须自然完成，否则可能只提交半个批次。
            for batch in batches:
                if batch.timer is not None and not batch.timer.done():
                    batch.timer.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for batch in batches:
            await self._flush(batch)

    async def ingest(
        self,
        envelope: InboundEnvelope,
        call_next: IngressHandler,
    ) -> IngressResult:
        """群聊直接放行；私聊消息进入对应用户的唯一待提交批次。"""

        if envelope.message.chat_type != ChatType.PRIVATE:
            return await call_next(envelope)

        key = self._route_key(
            envelope.message.platform,
            envelope.message.account_id,
            envelope.message.external_chat_id,
            envelope.message.sender_id,
        )
        async with self._lock:
            if self._stopping:
                return await call_next(envelope)
            batch = self._batches.get(key)
            if batch is None:
                batch = _PendingBatch(envelopes=[], call_next=call_next)
                self._batches[key] = batch
            external_id = envelope.message.external_message_id
            if external_id and external_id in batch.external_message_ids:
                return IngressResult(accepted=True, duplicate=True)
            batch.envelopes.append(envelope)
            if external_id:
                batch.external_message_ids.add(external_id)
            # 任何新消息都开启一个全新的 5 秒窗口，并允许下一次 typing
            # 恰好延长一次；这对应“十秒内回复则重回步骤 1”。
            batch.typing_extended = False
            self._arm_timer(key, batch, self.settings.message_delay_seconds)
            logger.info(
                "延迟回复等待消息结束 delay=%.3fs batch_size=%d",
                self.settings.message_delay_seconds,
                len(batch.envelopes),
                extra={"session_id": "-", "turn_id": "-"},
            )
        return IngressResult(accepted=True)

    async def typing(
        self,
        event: TypingEvent,
        call_next: TypingHandler,
    ) -> None:
        """同一私聊批次首次 typing 把当前窗口改为固定 5 秒。"""

        if event.chat_type == ChatType.PRIVATE:
            key = self._route_key(
                event.platform,
                event.account_id,
                event.external_chat_id,
                event.sender_id,
            )
            async with self._lock:
                batch = self._batches.get(key)
                if batch is not None and not batch.typing_extended:
                    batch.typing_extended = True
                    self._arm_timer(key, batch, self.settings.typing_delay_seconds)
                    logger.info(
                        "延迟回复检测到 typing delay=%.3fs batch_size=%d",
                        self.settings.typing_delay_seconds,
                        len(batch.envelopes),
                        extra={"session_id": "-", "turn_id": "-"},
                    )
        await call_next(event)

    def _arm_timer(self, key: str, batch: _PendingBatch, delay: float) -> None:
        """替换当前计时器；generation 防止取消边界上的旧任务提交批次。"""

        if batch.timer is not None and not batch.timer.done():
            batch.timer.cancel()
        batch.generation += 1
        task = asyncio.create_task(
            self._wait_and_flush(key, batch.generation, delay),
            name=f"delayed-reply:{key}",
        )
        batch.timer = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _wait_and_flush(
        self,
        key: str,
        generation: int,
        delay: float,
    ) -> None:
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                batch = self._batches.get(key)
                if batch is None or batch.generation != generation:
                    return
                self._batches.pop(key, None)
            await self._flush(batch)
            logger.info(
                "延迟回复批次已提交 batch_size=%d",
                len(batch.envelopes),
                extra={"session_id": "-", "turn_id": "-"},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "延迟回复批次提交失败",
                extra={
                    "session_id": "-",
                    "turn_id": "-",
                    "route_key": key,
                },
            )

    @staticmethod
    async def _flush(batch: _PendingBatch) -> None:
        """按接收顺序提交整批消息，让核心在同一 Turn 快照中读取它们。"""

        last_index = len(batch.envelopes) - 1
        for index, envelope in enumerate(batch.envelopes):
            # 前面的消息只提交历史，最后一条才以零额外防抖触发 Turn。
            # asyncio task 会在当前协程交还控制权后运行，因此最后一条返回时
            # 整批消息都已进入核心的权威存储。
            await batch.call_next(
                replace(
                    envelope,
                    schedule_turn=index == last_index,
                    schedule_duplicate=index == last_index,
                    debounce_seconds=0.0 if index == last_index else None,
                )
            )

    @staticmethod
    def _route_key(
        platform: str,
        account_id: str,
        external_chat_id: str,
        sender_id: str,
    ) -> str:
        return "\x1f".join((platform, account_id, external_chat_id, sender_id))


def create_plugin(context: IngressPluginContext) -> DelayedReplyPlugin:
    """由宿主同步创建私聊延迟回复插件。"""

    try:
        settings = DelayedReplySettings.model_validate(dict(context.options))
    except ValidationError as exc:
        raise PluginUnavailableError("延迟回复插件配置不完整或无效") from exc
    return DelayedReplyPlugin(settings, context)
