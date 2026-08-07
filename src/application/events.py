"""带有有界回放与显式重同步语义的进程内控制事件流。"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

RuntimeEvent = dict[str, Any]


@dataclass(frozen=True, slots=True)
class EventReplay:
    """从一个不透明游标开始的有界事件回放结果。"""

    events: tuple[RuntimeEvent, ...]
    current_cursor: str
    resync_required: bool = False
    reason: str | None = None


@dataclass(slots=True)
class _SubscriberState:
    loop: asyncio.AbstractEventLoop | None
    overflowed: bool = False


class EventHub:
    """为控制台提供单调事件序号、进程内回放和慢消费者失败语义。

    history 不是权威持久化；游标跨进程重启或超出窗口时，调用方必须补拉 REST
    快照。队列溢出也不会继续静默丢事件，而会发送一次 ``resync_required``。
    """

    def __init__(
        self,
        *,
        history_capacity: int = 1000,
        subscriber_queue_capacity: int = 100,
    ) -> None:
        if history_capacity < 1:
            raise ValueError("事件回放容量必须大于零")
        if subscriber_queue_capacity < 1:
            raise ValueError("事件订阅队列容量必须大于零")
        self._history: deque[RuntimeEvent] = deque(maxlen=history_capacity)
        self._subscriber_queue_capacity = subscriber_queue_capacity
        self._subscribers: dict[
            asyncio.Queue[RuntimeEvent],
            _SubscriberState,
        ] = {}
        self._stream_id = uuid4().hex
        self._event_seq = 0
        self._state_lock = threading.Lock()

    @property
    def stream_id(self) -> str:
        """返回本进程事件流 ID；重启后必然变化。"""

        return self._stream_id

    @property
    def current_sequence(self) -> int:
        """返回当前已分配的最大事件序号。"""

        with self._state_lock:
            return self._event_seq

    @property
    def current_cursor(self) -> str:
        """返回指向当前事件尾部的不透明游标。"""

        with self._state_lock:
            return self._encode_cursor(self._event_seq)

    def subscribe(self) -> asyncio.Queue[RuntimeEvent]:
        """注册实时订阅；调用方必须在结束时显式 unsubscribe。"""

        queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue(maxsize=self._subscriber_queue_capacity)
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._state_lock:
            self._subscribers[queue] = _SubscriberState(loop=loop)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[RuntimeEvent]) -> None:
        with self._state_lock:
            self._subscribers.pop(queue, None)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        self._dispatch(event_type, payload)

    def publish_nowait(self, event_type: str, payload: dict[str, Any]) -> None:
        """供同步上下文（如 logging Handler）广播事件。"""

        self._dispatch(event_type, payload)

    def replay(self, cursor: str | None) -> EventReplay:
        """返回游标后的 retained events；失效游标要求调用方重建快照。"""

        with self._state_lock:
            current_cursor = self._encode_cursor(self._event_seq)
            if cursor is None:
                return EventReplay(events=(), current_cursor=current_cursor)
            parsed = self._decode_cursor(cursor)
            if parsed is None:
                return EventReplay(
                    events=(),
                    current_cursor=current_cursor,
                    resync_required=True,
                    reason="invalid_cursor",
                )
            stream_id, sequence = parsed
            if stream_id != self._stream_id:
                return EventReplay(
                    events=(),
                    current_cursor=current_cursor,
                    resync_required=True,
                    reason="stream_restarted",
                )
            if sequence > self._event_seq:
                return EventReplay(
                    events=(),
                    current_cursor=current_cursor,
                    resync_required=True,
                    reason="cursor_ahead",
                )
            if self._history and sequence < int(self._history[0]["event_seq"]) - 1:
                return EventReplay(
                    events=(),
                    current_cursor=current_cursor,
                    resync_required=True,
                    reason="history_expired",
                )
            events = tuple(dict(event) for event in self._history if int(event["event_seq"]) > sequence)
            return EventReplay(events=events, current_cursor=current_cursor)

    def resync_event(self, reason: str) -> RuntimeEvent:
        """构造不进入全局序列的终止控制帧。"""

        with self._state_lock:
            return self._resync_event_locked(reason)

    def _dispatch(self, event_type: str, payload: dict[str, Any]) -> None:
        if not event_type or not isinstance(event_type, str):
            raise ValueError("事件类型必须是非空字符串")
        try:
            current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        with self._state_lock:
            self._event_seq += 1
            event: RuntimeEvent = {
                "type": event_type,
                "payload": dict(payload),
                "event_seq": self._event_seq,
                "cursor": self._encode_cursor(self._event_seq),
                "stream_id": self._stream_id,
            }
            self._history.append(event)
            for queue, state in list(self._subscribers.items()):
                if state.loop is not None and state.loop is not current_loop:
                    try:
                        state.loop.call_soon_threadsafe(
                            self._enqueue_from_subscriber_loop,
                            queue,
                            event,
                        )
                    except RuntimeError:
                        self._subscribers.pop(queue, None)
                    continue
                self._enqueue_locked(queue, state, event)

    def _enqueue_from_subscriber_loop(
        self,
        queue: asyncio.Queue[RuntimeEvent],
        event: RuntimeEvent,
    ) -> None:
        with self._state_lock:
            state = self._subscribers.get(queue)
            if state is not None:
                self._enqueue_locked(queue, state, event)

    def _enqueue_locked(
        self,
        queue: asyncio.Queue[RuntimeEvent],
        state: _SubscriberState,
        event: RuntimeEvent,
    ) -> None:
        if state.overflowed:
            return
        if queue.full():
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            queue.put_nowait(self._resync_event_locked("slow_consumer"))
            state.overflowed = True
            return
        queue.put_nowait(event)

    def _resync_event_locked(self, reason: str) -> RuntimeEvent:
        cursor = self._encode_cursor(self._event_seq)
        return {
            "type": "control.resync_required",
            "payload": {
                "reason": reason,
                "snapshot_required": True,
                "current_cursor": cursor,
            },
            "event_seq": self._event_seq,
            "cursor": cursor,
            "stream_id": self._stream_id,
        }

    def _encode_cursor(self, sequence: int) -> str:
        return f"{self._stream_id}.{sequence}"

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[str, int] | None:
        if len(cursor) > 100:
            return None
        stream_id, separator, raw_sequence = cursor.partition(".")
        if (
            separator != "."
            or len(stream_id) != 32
            or not raw_sequence.isascii()
            or not raw_sequence.isdecimal()
        ):
            return None
        sequence = int(raw_sequence)
        if sequence < 0:
            return None
        return stream_id, sequence
