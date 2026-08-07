"""周期任务工具的参数契约与应用服务适配。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from domain.models import ScheduleStatus
from tools.registry import RegisteredTool, ToolContext

if TYPE_CHECKING:
    from scheduling.service import ScheduleService


class _StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScheduleCreateArguments(_StrictArguments):
    title: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, max_length=4000)
    timezone: str = Field(min_length=1, max_length=100)
    dtstart: datetime
    rrule: str = Field(min_length=1, max_length=1000)


class ScheduleListArguments(_StrictArguments):
    pass


class ScheduleUpdateArguments(ScheduleCreateArguments):
    schedule_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=1)


class ScheduleMutationArguments(_StrictArguments):
    schedule_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=1)


def build_schedule_tools(schedules: ScheduleService) -> list[RegisteredTool]:
    """构造周期任务的会话受权控制工具。"""

    async def create(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        assert isinstance(arguments, ScheduleCreateArguments)
        schedule = await schedules.create(
            session_id=context.session_id,
            actor_id=context.actor_id or "",
            title=arguments.title,
            instruction=arguments.instruction,
            source_text=context.source_text or arguments.instruction,
            timezone=arguments.timezone,
            dtstart=arguments.dtstart,
            rrule=arguments.rrule,
        )
        return {"schedule": schedule.model_dump(mode="json"), "summary": f"已创建：{schedule.title}"}

    async def list_(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        assert isinstance(arguments, ScheduleListArguments)
        items = await schedules.list_for_tool(context.session_id, context.actor_id)
        return {
            "schedules": [item.model_dump(mode="json") for item in items],
            "summary": f"当前会话有 {len(items)} 个周期任务",
        }

    async def update(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        assert isinstance(arguments, ScheduleUpdateArguments)
        schedule = await schedules.update(
            arguments.schedule_id,
            actor_id=context.actor_id,
            expected_revision=arguments.expected_revision,
            title=arguments.title,
            instruction=arguments.instruction,
            source_text=context.source_text or arguments.instruction,
            timezone=arguments.timezone,
            dtstart=arguments.dtstart,
            rrule=arguments.rrule,
        )
        return {"schedule": schedule.model_dump(mode="json"), "summary": f"已更新：{schedule.title}"}

    def status_handler(status: ScheduleStatus):
        async def handle(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
            assert isinstance(arguments, ScheduleMutationArguments)
            schedule = await schedules.set_status(
                arguments.schedule_id,
                status,
                actor_id=context.actor_id,
                expected_revision=arguments.expected_revision,
            )
            return {
                "schedule": schedule.model_dump(mode="json"),
                "summary": f"任务状态：{schedule.status.value}",
            }

        return handle

    return [
        RegisteredTool(
            "schedule_create",
            (
                "当用户明确要求建立单次提醒或周期任务时调用。单次提醒使用 COUNT=1；"
                "把自然语言归一化为 IANA 时区、dtstart 和单条 RFC 5545 RRULE。"
            ),
            ScheduleCreateArguments,
            create,
        ),
        RegisteredTool(
            "schedule_list",
            "列出当前会话的提醒与周期任务及 revision。",
            ScheduleListArguments,
            list_,
        ),
        RegisteredTool(
            "schedule_update",
            "更新当前会话中的周期任务；先 list 获取 ID 和 revision。",
            ScheduleUpdateArguments,
            update,
        ),
        RegisteredTool(
            "schedule_pause",
            "暂停任务；先 list 获取 ID 和 revision。",
            ScheduleMutationArguments,
            status_handler(ScheduleStatus.PAUSED),
        ),
        RegisteredTool(
            "schedule_resume",
            "恢复任务；先 list 获取 ID 和 revision。",
            ScheduleMutationArguments,
            status_handler(ScheduleStatus.ACTIVE),
        ),
        RegisteredTool(
            "schedule_delete",
            "删除任务；先 list 获取 ID 和 revision。",
            ScheduleMutationArguments,
            status_handler(ScheduleStatus.DELETED),
        ),
    ]
