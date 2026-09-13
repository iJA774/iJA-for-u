"""插件自包含规则测试，不读取任何用户数据。"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from plugins.circuit_breaker.runtime import (
    CircuitBreakerGuard,
    CircuitBreakerSettings,
    create_plugin,
)
from ports.tool_guard import ToolGuardPluginContext, ToolObservation


def observation(name="lookup", arguments="{}", result='{"value":1}', succeeded=True):
    """构造不含审计时间或 call_id 的只读观测。"""

    return ToolObservation(name, arguments, result, succeeded)


@pytest.mark.parametrize("length", [1, 2, 4, 16])
def test_cycle_trips_exactly_at_threshold_and_latches(length):
    guard = CircuitBreakerGuard(CircuitBreakerSettings(max_cycle_length=length))
    for index in range(length * 3 - 1):
        assert guard.observe(observation(name=f"tool_{index % length}")) is None
    violation = guard.observe(observation(name=f"tool_{length - 1}"))
    assert violation is not None
    assert violation.reason == ("repeated_result" if length == 1 else "repeated_cycle")
    assert guard.observe(observation(result="new progress")) == violation


def test_json_order_and_whitespace_do_not_hide_repetition():
    guard = CircuitBreakerGuard(CircuitBreakerSettings())
    for args in ('{"a":1,"b":2}', '{ "b":2, "a":1 }'):
        assert guard.observe(observation(arguments=args)) is None
    assert guard.observe(observation(arguments='{"b":2,"a":1}')) is not None


def test_changing_results_and_changing_arguments_are_progress():
    for change in ("result", "arguments"):
        guard = CircuitBreakerGuard(CircuitBreakerSettings())
        for index in range(100):
            assert guard.observe(observation(**{change: json.dumps({"page": index})})) is None
        assert len(guard._history) == 12
        assert all(isinstance(item, bytes) and len(item) == 32 for item in guard._history)


def test_failures_across_different_tools_trip_and_success_resets():
    guard = CircuitBreakerGuard(CircuitBreakerSettings())
    for index in range(3):
        assert guard.observe(observation(name=str(index), succeeded=False)) is None
    assert guard.observe(observation()) is None
    for index in range(3):
        assert guard.observe(observation(name=str(index), succeeded=False)) is None
    violation = guard.observe(observation(name="fourth", succeeded=False))
    assert violation is not None
    assert violation.reason == "consecutive_failures"


@pytest.mark.parametrize("value", ["{", "[]", "null"])
def test_malformed_arguments_still_count(value):
    guard = CircuitBreakerGuard(CircuitBreakerSettings())
    assert guard.observe(observation(arguments=value, succeeded=False)) is None
    assert guard.observe(observation(arguments=value, succeeded=False)) is None
    assert guard.observe(observation(arguments=value, succeeded=False)) is not None


def test_each_attempt_has_fresh_state(tmp_path: Path):
    plugin = create_plugin(ToolGuardPluginContext("circuit_breaker", tmp_path, {}))
    first, second = plugin.create_guard(), plugin.create_guard()
    for _ in range(3):
        first.observe(observation())
    assert second.observe(observation()) is None
    assert plugin.create_guard().observe(observation()) is None


@pytest.mark.parametrize("options", [
    {"repetitions": 1}, {"repetitions": True}, {"repetitions": "3"},
    {"max_cycle_length": 0}, {"consecutive_failures": 1},
    {"timeout_seconds": 0}, {"timeout_seconds": float("inf")},
    {"timeout_seconds": float("nan")}, {"unknown": 1},
])
def test_invalid_configuration_fails_loudly(options, tmp_path: Path):
    with pytest.raises(ValidationError):
        create_plugin(ToolGuardPluginContext("circuit_breaker", tmp_path, options))
