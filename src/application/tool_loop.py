"""结构化模型工具循环；执行权始终留在应用层。"""

from __future__ import annotations

import json
import logging
from time import perf_counter

from application.events import EventHub
from application.replies import draft_from_model_text
from config import AppSettings
from domain.errors import InvalidModelResponseError, ToolLimitError
from domain.models import ReplyDraft, ToolExecution, ToolExecutionStatus, utc_now
from observability import model_observation_scope, sensitive_log_scope
from ports import ModelMessage, ModelProvider, ModelRequest, OperationsRepository
from tools import ToolContext, ToolRegistry

logger = logging.getLogger(__name__)


def _public_execution_payload(execution: ToolExecution) -> dict[str, object]:
    """构造实时事件的安全投影；完整参数和结果只进入受控审计表。"""

    return {
        "schema_version": 1,
        "execution_id": execution.id,
        "session_id": execution.session_id,
        "turn_id": execution.turn_id,
        "schedule_run_id": execution.schedule_run_id,
        "tool_call_id": execution.tool_call_id,
        "tool_name": execution.tool_name,
        "status": execution.status.value,
        "error_code": execution.error_code,
        "started_at": execution.started_at.isoformat(),
        "completed_at": (
            execution.completed_at.isoformat()
            if execution.completed_at is not None
            else None
        ),
    }


class ToolLoop:
    """在有限轮次内执行原生 tool_calls，并返回唯一最终回复草稿。"""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: OperationsRepository,
        events: EventHub,
        registry: ToolRegistry,
    ) -> None:
        self.settings = settings
        self.store = store
        self.events = events
        self.registry = registry

    async def run(
        self,
        *,
        model: ModelProvider,
        messages: list[ModelMessage],
        context: ToolContext,
        allowed_tools: set[str],
    ) -> ReplyDraft:
        call_count = 0
        transcript = list(messages)
        for _ in range(self.settings.tools.max_rounds):
            # load_skill 会在同一轮上下文中更新 loaded_skills；每轮重新投影定义，
            # 使模型先读取 Skill 指令，再看到该 Skill 的原子工具。
            definitions = self.registry.definitions(
                allowed_tools,
                authorization_scopes=context.authorization_scopes,
                loaded_skills=context.loaded_skills,
            )
            exposed_tool_names = {item.name for item in definitions}
            with model_observation_scope(
                task=(
                    "schedule.reply"
                    if context.schedule_run_id is not None
                    else "chat.reply"
                ),
                session_id=context.session_id,
                turn_id=context.turn_id,
                run_id=context.schedule_run_id or context.turn_id,
            ):
                result = await model.complete(
                    ModelRequest(
                        messages=transcript,
                        model=self.settings.model.name,
                        temperature=self.settings.model.temperature,
                        max_tokens=self.settings.model.max_tokens,
                        tools=definitions or None,
                        tool_choice="auto" if definitions else None,
                    )
                )
            if not result.tool_calls:
                if not result.content:
                    raise InvalidModelResponseError("工具循环结束时模型没有返回最终文本")
                draft = draft_from_model_text(
                    result.content,
                    split_short_lines="send_messages" in allowed_tools,
                )
                if draft.follow_up_components:
                    logger.info(
                        "模型未调用多消息终态工具，已将连续聊天短行规范化为独立消息",
                        extra={
                            "session_id": context.session_id,
                            "turn_id": context.turn_id or "-",
                            "message_count": 1 + len(draft.follow_up_components),
                        },
                    )
                return draft
            hidden_calls = sorted(
                {
                    item.name
                    for item in result.tool_calls
                    if item.name not in exposed_tool_names
                }
            )
            if hidden_calls:
                raise InvalidModelResponseError(
                    "模型调用了本轮未授权或未下发的工具: "
                    + ", ".join(hidden_calls)
                )
            terminal_calls = [
                item for item in result.tool_calls if self.registry.is_terminal(item.name)
            ]
            if terminal_calls and len(result.tool_calls) != 1:
                raise InvalidModelResponseError("终态回复工具必须是本轮唯一的工具调用")
            transcript.append(
                ModelMessage(role="assistant", content=result.content, tool_calls=result.tool_calls)
            )
            for tool_call in result.tool_calls:
                call_count += 1
                if call_count > self.settings.tools.max_calls:
                    raise ToolLimitError("本轮工具调用次数超过上限")
                execution = ToolExecution(
                    session_id=context.session_id,
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    arguments={},
                    turn_id=context.turn_id,
                    schedule_run_id=context.schedule_run_id,
                )
                try:
                    arguments = json.loads(tool_call.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments 必须是 JSON object")
                    execution.arguments = arguments
                except (json.JSONDecodeError, ValueError) as exc:
                    execution.status = ToolExecutionStatus.FAILED
                    execution.error_code = "invalid_tool_arguments"
                    execution.error_message = str(exc)[:500]
                    execution.completed_at = utc_now()
                    await self.store.save_tool_execution(execution)
                    await self.events.publish(
                        "tool.failed", _public_execution_payload(execution)
                    )
                    tool_result = {
                        "ok": False,
                        "error_code": execution.error_code,
                        "message": "工具参数不是合法 JSON object",
                    }
                else:
                    await self.store.save_tool_execution(execution)
                    await self.events.publish(
                        "tool.started", _public_execution_payload(execution)
                    )
                    logger.info(
                        "工具调用开始",
                        extra={
                            "session_id": context.session_id,
                            "turn_id": context.turn_id or "-",
                            "tool_name": tool_call.name,
                            "tool_call_id": tool_call.id,
                            "schema_version": 1,
                            "status": execution.status.value,
                        },
                    )
                    started_monotonic = perf_counter()
                    try:
                        with sensitive_log_scope(arguments):
                            outcome = await self.registry.execute(
                                tool_call.name,
                                arguments,
                                context,
                            )
                    except Exception as exc:
                        execution.status = ToolExecutionStatus.FAILED
                        execution.error_code = getattr(exc, "code", "tool_execution_failed")
                        execution.error_message = str(exc)[:500]
                        execution.completed_at = utc_now()
                        await self.store.save_tool_execution(execution)
                        await self.events.publish(
                            "tool.failed", _public_execution_payload(execution)
                        )
                        logger.warning(
                            "工具调用失败",
                            extra={
                                "session_id": context.session_id,
                                "turn_id": context.turn_id or "-",
                                "tool_name": tool_call.name,
                                "error_code": execution.error_code,
                                "tool_call_id": tool_call.id,
                                "schema_version": 1,
                                "status": execution.status.value,
                                "duration_ms": round(
                                    (perf_counter() - started_monotonic) * 1000,
                                    3,
                                ),
                            },
                        )
                        tool_result = {
                            "ok": False,
                            "error_code": execution.error_code,
                            "message": execution.error_message,
                        }
                    else:
                        execution.status = ToolExecutionStatus.COMPLETED
                        execution.result = outcome.value
                        execution.completed_at = utc_now()
                        await self.store.save_tool_execution(execution)
                        await self.events.publish(
                            "tool.completed", _public_execution_payload(execution)
                        )
                        logger.info(
                            "工具调用完成",
                            extra={
                                "session_id": context.session_id,
                                "turn_id": context.turn_id or "-",
                                "tool_name": tool_call.name,
                                "tool_call_id": tool_call.id,
                                "schema_version": 1,
                                "status": execution.status.value,
                                "duration_ms": round(
                                    (perf_counter() - started_monotonic) * 1000,
                                    3,
                                ),
                            },
                        )
                        tool_result = {"ok": True, **outcome.value}
                        if outcome.reply_draft is not None:
                            return outcome.reply_draft
                encoded = json.dumps(tool_result, ensure_ascii=False)
                if len(encoded) > 32_000:
                    encoded = json.dumps(
                        {
                            "ok": True,
                            "result_truncated": True,
                            "tool_call_id": tool_call.id,
                            "message": (
                                "工具已成功执行，但详细结果过大，"
                                "请使用对应查询工具按需获取。"
                            ),
                        },
                        ensure_ascii=False,
                    )
                transcript.append(ModelMessage(role="tool", content=encoded, tool_call_id=tool_call.id))
        raise ToolLimitError("模型工具循环超过最大轮次")
