"""进程内日志中心：收集内存日志快照并通过 EventHub 实时广播给前端。

不承担权威持久化。进程重启后缓冲清空，完整历史仍在 ``logs/ija.log`` 轮转文件中。
LogHub 只负责“实时可观测窗口”，便于网页端按需浏览最近发生的运行事件。
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from application.events import EventHub

# logging.LogRecord 的内置属性，用于隔离调用方传入的 extra 上下文。
_RECORD_BUILTIN_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
})

# 实时日志是普通控制台可见面，默认拒绝任意调用方字段。完整工具参数、
# 消息正文、Prompt、URL 与文件名只能进入各自受控的权威/审计存储。
_SAFE_EXTRA_FIELDS = frozenset(
    {
        "attempt_count",
        "boot_id",
        "chat_type",
        "component_type",
        "duration_ms",
        "error_code",
        "error_type",
        "event_seq",
        "generation",
        "learning_run_id",
        "message_count",
        "model_mode",
        "outbound_id",
        "plugin_id",
        "request_id",
        "run_id",
        "schedule_run_id",
        "schema_version",
        "session_id",
        "source_chain",
        "status",
        "supports_tools",
        "task_key",
        "tool_call_id",
        "tool_name",
        "turn_id",
    }
)

_sensitive_log_values: ContextVar[tuple[str, ...]] = ContextVar(
    "sensitive_log_values",
    default=(),
)


def _collect_sensitive_values(value: object) -> set[str]:
    """递归提取工具参数中的字符串值，不把字段名误当作秘密。"""

    if isinstance(value, str):
        if not value:
            return set()
        escaped = json.dumps(value, ensure_ascii=False)[1:-1]
        return {value, escaped}
    if isinstance(value, Mapping):
        result: set[str] = set()
        for item in value.values():
            result.update(_collect_sensitive_values(item))
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        result = set()
        for item in value:
            result.update(_collect_sensitive_values(item))
        return result
    return set()


@contextmanager
def sensitive_log_scope(arguments: Mapping[str, object]) -> Iterator[None]:
    """在工具执行动态域内遮盖参数值，阻止消息或异常旁路进入日志。"""

    inherited = set(_sensitive_log_values.get())
    inherited.update(_collect_sensitive_values(arguments))
    token = _sensitive_log_values.set(
        tuple(sorted(inherited, key=len, reverse=True))
    )
    try:
        yield
    finally:
        _sensitive_log_values.reset(token)


def redact_sensitive_log_text(value: str) -> str:
    """遮盖当前工具调用参数值；域外日志保持原始可观测性。"""

    redacted = value
    for secret in _sensitive_log_values.get():
        redacted = redacted.replace(secret, "[已遮盖工具参数]")
    return redacted


class LogHub:
    """维护最近 capacity 条日志的内存快照，并实时广播给前端订阅者。"""

    def __init__(self, events: EventHub, *, capacity: int = 1000) -> None:
        self._events = events
        self._buffer: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = Lock()
        # 广播日志事件时 EventHub 内部可能再次触发日志，用该标志打断递归。
        self._emitting = False

    def handle(self, record: logging.LogRecord) -> None:
        """把一条标准日志记录转换成前端可消费的 dict 并广播。"""

        if self._emitting:
            return
        entry = self._format(record)
        with self._lock:
            self._buffer.append(entry)
        self._emitting = True
        try:
            self._events.publish_nowait("log.appended", entry)
        finally:
            self._emitting = False

    def snapshot(
        self,
        *,
        level: str | None = None,
        logger_name: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """返回最近日志的过滤快照，按时间正序，最多 limit 条。"""

        with self._lock:
            items = list(self._buffer)
        if level:
            wanted = level.upper()
            items = [item for item in items if item["level"] == wanted]
        if logger_name:
            items = [item for item in items if logger_name in item["logger"]]
        return items[-limit:] if limit > 0 else items

    @staticmethod
    def _format(record: logging.LogRecord) -> dict[str, Any]:
        """把 LogRecord 投影成安全的、可序列化的日志条目。"""

        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RECORD_BUILTIN_ATTRS
            and not key.startswith("_")
            and key in _SAFE_EXTRA_FIELDS
            and isinstance(value, (str, int, float, bool, type(None)))
        }
        entry: dict[str, Any] = {
            "id": f"log_{record.created:.6f}_{record.thread}",
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_sensitive_log_text(record.getMessage()),
            "extra": extra,
        }
        if record.exc_info:
            entry["exception"] = redact_sensitive_log_text(
                logging.Formatter().formatException(record.exc_info)
            )
        return entry


class LogHubHandler(logging.Handler):
    """把标准 logging 记录桥接到 LogHub，使现有 logger.* 调用自动进入实时流。"""

    def __init__(self, log_hub: LogHub) -> None:
        super().__init__()
        self._log_hub = log_hub

    def emit(self, record: logging.LogRecord) -> None:
        self._log_hub.handle(record)
