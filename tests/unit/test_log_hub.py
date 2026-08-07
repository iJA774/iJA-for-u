"""LogHub 内存快照与实时广播的单元测试。"""

from __future__ import annotations

import logging

from application.events import EventHub
from observability import LogHub, LogHubHandler


def _attach_logger(hub: LogHub, name: str) -> logging.Logger:
    """构造一个只接入 LogHubHandler 的独立 logger，避免污染全局配置。"""

    test_logger = logging.getLogger(name)
    test_logger.handlers = [LogHubHandler(hub)]
    test_logger.setLevel(logging.DEBUG)
    test_logger.propagate = False
    return test_logger


def test_log_hub_stores_and_broadcasts() -> None:
    events = EventHub()
    hub = LogHub(events)
    queue = events.subscribe()
    test_logger = _attach_logger(hub, "test.log_hub.broadcast")

    test_logger.info("hello", extra={"session_id": "s1", "turn_id": "t1"})

    snapshot = hub.snapshot()
    assert len(snapshot) == 1
    entry = snapshot[0]
    assert entry["message"] == "hello"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "test.log_hub.broadcast"
    assert entry["extra"]["session_id"] == "s1"
    assert entry["extra"]["turn_id"] == "t1"

    event = queue.get_nowait()
    assert event["type"] == "log.appended"
    assert event["payload"]["message"] == "hello"


def test_log_hub_snapshot_filters_by_level() -> None:
    events = EventHub()
    hub = LogHub(events)
    test_logger = _attach_logger(hub, "test.log_hub.filter")

    test_logger.info("info-event")
    test_logger.warning("warn-event")
    test_logger.error("error-event")

    all_entries = hub.snapshot()
    assert len(all_entries) == 3
    warnings = hub.snapshot(level="WARNING")
    assert len(warnings) == 1
    assert warnings[0]["message"] == "warn-event"
    errors = hub.snapshot(level="ERROR")
    assert len(errors) == 1
    assert errors[0]["level"] == "ERROR"


def test_log_hub_snapshot_limit() -> None:
    events = EventHub()
    hub = LogHub(events)
    test_logger = _attach_logger(hub, "test.log_hub.limit")

    for index in range(10):
        test_logger.info("event-%d", index)

    limited = hub.snapshot(limit=3)
    assert len(limited) == 3
    assert limited[0]["message"] == "event-7"
    assert limited[-1]["message"] == "event-9"


def test_log_hub_capacity_bounds_buffer() -> None:
    events = EventHub()
    hub = LogHub(events, capacity=3)
    test_logger = _attach_logger(hub, "test.log_hub.capacity")

    for index in range(5):
        test_logger.info("event-%d", index)

    snapshot = hub.snapshot()
    assert len(snapshot) == 3
    assert snapshot[0]["message"] == "event-2"
    assert snapshot[-1]["message"] == "event-4"


def test_log_hub_recursion_guard() -> None:
    """发布事件期间触发的日志不应再次入队，避免无限递归。"""

    events = EventHub()
    hub = LogHub(events)
    inner_logger = _attach_logger(hub, "test.log_hub.recursion.inner")

    # 模拟事件分发期间再次产生日志：直接置位 _emitting 后记录。
    hub._emitting = True
    inner_logger.info("should-be-skipped")
    hub._emitting = False

    assert hub.snapshot() == []


def test_log_hub_logger_name_filter() -> None:
    events = EventHub()
    hub = LogHub(events)
    _attach_logger(hub, "ija.application.service")
    _attach_logger(hub, "ija.api.app")
    logging.getLogger("ija.application.service").info("app-event")
    logging.getLogger("ija.api.app").info("api-event")

    matched = hub.snapshot(logger_name="application")
    assert len(matched) == 1
    assert matched[0]["message"] == "app-event"


def test_log_hub_exception_attached() -> None:
    events = EventHub()
    hub = LogHub(events)
    test_logger = _attach_logger(hub, "test.log_hub.exc")

    try:
        raise ValueError("boom")
    except ValueError:
        test_logger.exception("失败", extra={"session_id": "s2", "turn_id": "-"})

    entry = hub.snapshot()[0]
    assert entry["message"] == "失败"
    assert entry["exception"] is not None
    assert "ValueError" in entry["exception"]
    assert "boom" in entry["exception"]


def test_log_hub_drops_unregistered_sensitive_extra_fields() -> None:
    """调用方不能把工具参数、URL 或文件名旁路进实时日志。"""

    events = EventHub()
    hub = LogHub(events)
    test_logger = _attach_logger(hub, "test.log_hub.sensitive-extra")

    test_logger.info(
        "工具调用开始",
        extra={
            "session_id": "s-safe",
            "tool_name": "remember_memory",
            "arguments": '{"content":"绝密正文"}',
            "url": "https://example.invalid/private?token=secret",
            "file_name": "隐私文件.txt",
            "confirmation_phrase": "确认删除全部",
        },
    )

    entry = hub.snapshot()[0]
    encoded = str(entry)
    assert entry["extra"] == {
        "session_id": "s-safe",
        "tool_name": "remember_memory",
    }
    for secret in ("绝密正文", "token=secret", "隐私文件.txt", "确认删除全部"):
        assert secret not in encoded


def test_event_hub_publish_nowait_dispatches_to_subscriber() -> None:
    """同步广播路径与 async publish 行为一致。"""

    events = EventHub()
    queue = events.subscribe()
    events.publish_nowait("custom.event", {"k": 1})
    event = queue.get_nowait()
    assert event["type"] == "custom.event"
    assert event["payload"] == {"k": 1}


def test_event_hub_assigns_monotonic_sequence_and_replays_after_cursor() -> None:
    """断线游标之后的事件必须按严格递增序号完整回放。"""

    events = EventHub(history_capacity=4)
    events.publish_nowait("event.one", {"value": 1})
    first_cursor = events.current_cursor
    events.publish_nowait("event.two", {"value": 2})
    events.publish_nowait("event.three", {"value": 3})

    replay = events.replay(first_cursor)

    assert replay.resync_required is False
    assert [event["type"] for event in replay.events] == [
        "event.two",
        "event.three",
    ]
    assert [event["event_seq"] for event in replay.events] == [2, 3]
    assert len({event["stream_id"] for event in replay.events}) == 1
    assert replay.events[-1]["cursor"] == replay.current_cursor


def test_event_hub_requires_resync_for_expired_or_foreign_cursor() -> None:
    """历史窗口过期或进程重启后，不得假装已经连续回放。"""

    events = EventHub(history_capacity=2)
    initial_cursor = events.current_cursor
    for index in range(3):
        events.publish_nowait("event.test", {"index": index})

    expired = events.replay(initial_cursor)
    foreign = events.replay(f"{'0' * 32}.1")

    assert expired.resync_required is True
    assert expired.reason == "history_expired"
    assert foreign.resync_required is True
    assert foreign.reason == "stream_restarted"


def test_event_hub_slow_consumer_receives_one_terminal_resync_frame() -> None:
    """订阅队列溢出应显式失败，不能静默丢头部事件后继续伪装在线。"""

    events = EventHub(subscriber_queue_capacity=1)
    queue = events.subscribe()

    events.publish_nowait("event.one", {})
    events.publish_nowait("event.two", {})
    events.publish_nowait("event.three", {})

    resync = queue.get_nowait()
    assert resync["type"] == "control.resync_required"
    assert resync["payload"]["reason"] == "slow_consumer"
    assert resync["payload"]["snapshot_required"] is True
    assert queue.empty()


def test_event_hub_builds_resync_frame_without_advancing_sequence() -> None:
    events = EventHub()
    frame = events.resync_event("invalid_cursor")

    assert frame["event_seq"] == 0
    assert frame["cursor"] == events.current_cursor
    assert events.current_sequence == 0


def test_log_hub_handler_emit_forwards_record() -> None:
    events = EventHub()
    hub = LogHub(events)
    handler = LogHubHandler(hub)
    record = logging.LogRecord(
        name="manual",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="manual-record",
        args=None,
        exc_info=None,
    )
    handler.emit(record)
    assert hub.snapshot()[0]["level"] == "WARNING"
