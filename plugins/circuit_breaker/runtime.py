"""检测重复结果、短周期循环和连续失败；所有状态只属于单次执行。"""

from __future__ import annotations

import hashlib
import json
from collections import deque

from pydantic import BaseModel, ConfigDict, Field

from ports.tool_guard import GuardViolation, ToolGuardPluginContext, ToolObservation


class CircuitBreakerSettings(BaseModel):
    """由管理员配置的有界阈值，不接受模型控制。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    repetitions: int = Field(default=3, ge=2, le=10)
    max_cycle_length: int = Field(default=4, ge=1, le=16)
    consecutive_failures: int = Field(default=4, ge=2, le=20)
    timeout_seconds: float = Field(default=120.0, ge=0.1, le=3600, allow_inf_nan=False)


def _canonical(value: str) -> str:
    """忽略 JSON 排版和键顺序；非法参数保留原串参与失败检测。"""

    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    return json.dumps(decoded, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class CircuitBreakerGuard:
    """只保存有界摘要；结果变化视为进展，成功会清空连续失败计数。"""

    def __init__(self, settings: CircuitBreakerSettings) -> None:
        self.settings = settings
        self.timeout_seconds = settings.timeout_seconds
        self._history: deque[bytes] = deque(
            maxlen=settings.repetitions * settings.max_cycle_length
        )
        self._failures = 0
        self._violation: GuardViolation | None = None

    def observe(self, observation: ToolObservation) -> GuardViolation | None:
        """达到阈值后锁存熔断，避免同一次执行被后续成功重新开启。"""

        if self._violation is not None:
            return self._violation
        self._failures = 0 if observation.succeeded else self._failures + 1
        if self._failures >= self.settings.consecutive_failures:
            self._violation = GuardViolation(
                "consecutive_failures", "工具连续失败达到阈值，已停止本次任务"
            )
            return self._violation
        # call_id、耗时和审计时间戳不参与摘要，模型换 ID 不能绕过检测。
        encoded = json.dumps(
            [observation.name, _canonical(observation.arguments),
             _canonical(observation.result), observation.succeeded],
            ensure_ascii=False,
        ).encode("utf-8")
        self._history.append(hashlib.sha256(encoded).digest())
        history = list(self._history)
        for length in range(1, self.settings.max_cycle_length + 1):
            count = length * self.settings.repetitions
            if len(history) >= count and history[-count:] == history[-length:] * self.settings.repetitions:
                self._violation = GuardViolation(
                    "repeated_result" if length == 1 else "repeated_cycle",
                    "工具调用及结果重复且无进展，已停止本次任务",
                )
                return self._violation
        return None


class CircuitBreakerPlugin:
    """自动装载的被动插件，不持有任务历史或外部资源。"""

    plugin_id = "circuit_breaker"

    def __init__(self, context: ToolGuardPluginContext) -> None:
        self.settings = CircuitBreakerSettings.model_validate(dict(context.options))

    async def start(self) -> None:
        """纯内存插件，无需启动后台任务。"""

    async def stop(self) -> None:
        """任务状态随宿主租约释放，无全局资源需要清理。"""

    def create_guard(self) -> CircuitBreakerGuard:
        """新任务及显式重试都获得独立预算。"""

        return CircuitBreakerGuard(self.settings)


def create_plugin(context: ToolGuardPluginContext) -> CircuitBreakerPlugin:
    """校验配置并构造插件；非法配置明确失败，不静默撤掉保护。"""

    return CircuitBreakerPlugin(context)
