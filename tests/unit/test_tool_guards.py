"""宿主执行守卫契约测试：停止副作用、取消、隔离和授权边界。"""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, ConfigDict

from application.events import EventHub
from application.replies import draft_from_model_text
from application.tool_loop import ToolLoop
from domain.errors import InvalidModelResponseError, ToolLimitError, ToolLoopAbortedError
from domain.models import ToolExecutionStatus
from plugins.circuit_breaker.runtime import CircuitBreakerGuard, CircuitBreakerSettings
from ports import ModelResult, ModelToolCall
from tools import RegisteredTool, ToolContext, ToolOutcome, ToolRegistry


class Arguments(BaseModel):
    """限制工具参数为对象。"""

    model_config = ConfigDict(extra="forbid")
    value: str = "测试正文"


def make_loop(settings, *, handler=None, options=None, required_scopes=frozenset()):
    """只使用内存替身；预算与业务配置来自隔离 fixture。"""

    settings.tools.max_rounds = 8
    settings.tools.max_calls = 32
    handler = handler or AsyncMock(return_value={"value": "私密结果"})
    store = AsyncMock()
    events = EventHub()
    config = CircuitBreakerSettings.model_validate(options or {})

    @asynccontextmanager
    async def lease():
        yield (("circuit_breaker", CircuitBreakerGuard(config)),)

    loop = ToolLoop(
        settings=settings, store=store, events=events,
        registry=ToolRegistry([RegisteredTool(
            "lookup", "测试工具", Arguments, handler, required_scopes=required_scopes,
        )]),
        guard_lease=lease,
    )
    return loop, handler, store, events


def response(*, count=1, arguments="{}"):
    """构造模型提出的单次或批量工具调用。"""

    return ModelResult(tool_calls=[
        ModelToolCall(id=f"call-{index}", name="lookup", arguments=arguments)
        for index in range(count)
    ])


async def run(loop, model, *, turn="turn", schedule=None):
    """私聊和周期任务使用同一个宿主循环。"""

    return await loop.run(
        model=model, messages=[], allowed_tools={"lookup"},
        context=ToolContext(session_id="session", actor_id="user", turn_id=turn, schedule_run_id=schedule),
    )


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("schedule", [None, "scheduled-run"])
async def test_trip_stops_before_next_tool_and_model_call(settings, batch, schedule, caplog):
    loop, handler, store, events = make_loop(settings)
    queue = events.subscribe()
    model = AsyncMock()
    model.complete.return_value = response(count=8 if batch else 1)
    with pytest.raises(ToolLoopAbortedError) as captured:
        await run(loop, model, schedule=schedule)
    assert captured.value.details is not None
    assert captured.value.details["reason"] == "repeated_result"
    assert handler.await_count == 3
    assert model.complete.await_count == (1 if batch else 3)
    assert store.save_tool_execution.await_count == 6
    emitted = []
    while not queue.empty():
        emitted.append(queue.get_nowait())
    trip = [event for event in emitted if event["type"] == "tool_loop.tripped"]
    assert len(trip) == 1
    assert trip[0]["payload"]["schedule_run_id"] == schedule
    public = json.dumps(emitted, ensure_ascii=False) + caplog.text
    assert "私密结果" not in public
    assert "测试正文" not in public


@pytest.mark.parametrize("arguments", ["{", "[]", '{"unknown":1}'])
async def test_bad_arguments_do_not_bypass_guard(settings, arguments):
    loop, handler, store, _ = make_loop(settings)
    model = AsyncMock()
    model.complete.return_value = response(arguments=arguments)
    with pytest.raises(ToolLoopAbortedError):
        await run(loop, model)
    handler.assert_not_awaited()
    assert model.complete.await_count == 3
    assert store.save_tool_execution.call_args.args[0].status == ToolExecutionStatus.FAILED


async def test_fresh_state_for_concurrent_runs_and_retry(settings):
    loop, handler, _, _ = make_loop(settings)

    async def attempt():
        model = AsyncMock()
        model.complete.side_effect = [response(), response(), ModelResult(content="完成")]
        return await run(loop, model)

    results = await asyncio.gather(attempt(), attempt())
    assert all(result.components[0].text == "完成" for result in results)
    assert (await attempt()).components[0].text == "完成"
    assert handler.await_count == 6


@pytest.mark.parametrize("stage", ["model", "tool"])
async def test_task_timeout_cancels_inflight_work_and_audits(settings, stage):
    cancelled = asyncio.Event()

    async def hang(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    loop, _, store, _ = make_loop(
        settings, handler=hang if stage == "tool" else None,
        options={"timeout_seconds": 0.1},
    )
    model = AsyncMock()
    model.complete.side_effect = hang if stage == "model" else None
    model.complete.return_value = response()
    with pytest.raises(ToolLoopAbortedError) as captured:
        await run(loop, model)
    assert captured.value.details is not None
    assert captured.value.details["reason"] == "task_timeout"
    assert cancelled.is_set()
    if stage == "tool":
        audit = store.save_tool_execution.call_args.args[0]
        assert audit.status == ToolExecutionStatus.FAILED
        assert audit.error_code == "tool_execution_cancelled"


async def test_external_cancellation_is_not_mislabeled_as_trip(settings):
    entered = asyncio.Event()

    async def hang(*args):
        entered.set()
        await asyncio.Event().wait()

    loop, _, store, events = make_loop(settings, handler=hang)
    queue = events.subscribe()
    model = AsyncMock()
    model.complete.return_value = response()
    task = asyncio.create_task(run(loop, model))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.save_tool_execution.call_args.args[0].error_code == "tool_execution_cancelled"
    while not queue.empty():
        assert queue.get_nowait()["type"] != "tool_loop.tripped"


async def test_provider_timeout_retains_original_error(settings):
    loop, _, _, _ = make_loop(settings)
    model = AsyncMock()
    model.complete.side_effect = TimeoutError("上游超时")
    with pytest.raises(TimeoutError, match="上游超时"):
        await run(loop, model)


@pytest.mark.parametrize("stage", ["model", "tool"])
async def test_late_result_after_suppressed_cancellation_cannot_continue(settings, stage):
    """依赖吞掉一次取消后返回时，宿主仍不得继续执行或接受迟到草稿。"""

    async def late(*args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return ModelResult(content="迟到草稿") if stage == "model" else {"done": True}

    handler = AsyncMock(side_effect=late) if stage == "tool" else None
    loop, handler, _, _ = make_loop(settings, handler=handler, options={"timeout_seconds": 0.1})
    model = AsyncMock()
    model.complete.side_effect = late if stage == "model" else [response(), ModelResult(content="完成")]
    with pytest.raises(ToolLoopAbortedError) as captured:
        await run(loop, model)
    assert captured.value.details is not None
    assert captured.value.details["reason"] == "task_timeout"
    assert model.complete.await_count == 1
    assert handler.await_count == (1 if stage == "tool" else 0)


async def test_guard_cannot_grant_hidden_tool_permission(settings):
    loop, handler, store, _ = make_loop(settings, required_scopes=frozenset({"test:write"}))
    model = AsyncMock()
    model.complete.return_value = response()
    with pytest.raises(InvalidModelResponseError, match="未授权"):
        await run(loop, model)
    handler.assert_not_awaited()
    store.save_tool_execution.assert_not_awaited()


async def test_removing_guard_keeps_host_hard_limits(settings):
    loop, handler, _, _ = make_loop(settings)
    loop.guard_lease = None
    settings.tools.max_calls = 2
    model = AsyncMock()
    model.complete.return_value = response(count=8)
    with pytest.raises(ToolLimitError) as captured:
        await run(loop, model)
    assert type(captured.value) is ToolLimitError
    assert handler.await_count == 2


async def test_terminal_reply_returns_without_requesting_another_model_round(settings):
    handler = AsyncMock(return_value=ToolOutcome(
        value={"prepared": True}, reply_draft=draft_from_model_text("已完成", split_short_lines=False),
    ))
    loop, _, _, _ = make_loop(settings, handler=handler)
    loop.registry = ToolRegistry([RegisteredTool("lookup", "终态测试", Arguments, handler, terminal=True)])
    model = AsyncMock()
    model.complete.return_value = response()
    assert (await run(loop, model)).components[0].text == "已完成"
    assert model.complete.await_count == 1


async def test_changing_large_results_use_untruncated_observation(settings):
    handler = AsyncMock(side_effect=[{"large": "x" * 40000, "page": page} for page in range(4)])
    loop, _, _, _ = make_loop(settings, handler=handler)
    model = AsyncMock()
    model.complete.side_effect = [response()] * 4 + [ModelResult(content="完成")]
    assert (await run(loop, model)).components[0].text == "完成"
    assert handler.await_count == 4
