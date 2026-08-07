"""长期记忆工具的参数契约与应用服务适配。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.errors import InputValidationError
from domain.models import MemoryKind, MemorySourceChain, utc_now
from tools.registry import RegisteredTool, ToolContext

if TYPE_CHECKING:
    from application.memory import MemoryService


class _StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecallMemoryArguments(_StrictArguments):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=10)


class RememberArguments(_StrictArguments):
    content: str = Field(min_length=1, max_length=4000)
    kind: Literal[
        "profile",
        "preference",
        "event",
        "episode",
        "commitment",
        "relationship",
        "procedure",
    ] = "event"
    subject_id: str | None = Field(default=None, max_length=200)


class ForgetMemoryArguments(_StrictArguments):
    memory_id: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=500)


class CorrectMemoryArguments(_StrictArguments):
    memory_id: str = Field(min_length=1, max_length=80)
    corrected_content: str = Field(min_length=1, max_length=4000)
    reason: str = Field(min_length=1, max_length=500)


def build_memory_tools(memory: MemoryService) -> list[RegisteredTool]:
    """构造受当前会话可见域约束的长期记忆工具。"""

    async def recall(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        assert isinstance(arguments, RecallMemoryArguments)
        session = await memory.session_for_tool(context.session_id)
        records = await memory.retrieve(session=session, query=arguments.query, limit=arguments.limit)
        return {
            "memories": [
                {
                    "memory_id": item.id,
                    "kind": item.kind.value,
                    "content": item.content,
                    "confidence": item.confidence,
                    "happened_at": item.happened_at.isoformat() if item.happened_at else None,
                }
                for item in records
            ]
        }

    async def remember(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        assert isinstance(arguments, RememberArguments)
        session = await memory.session_for_tool(context.session_id)
        allowed_subjects = {item.external_user_id for item in session.participants} | {"agent"}
        if arguments.subject_id is not None and arguments.subject_id not in allowed_subjects:
            raise InputValidationError("记忆主体不属于当前会话")
        record = await memory.remember(
            session=session,
            content=arguments.content,
            kind=MemoryKind(arguments.kind),
            subject_id=arguments.subject_id or context.actor_id,
            source_chain=MemorySourceChain.MANUAL,
            source_run_id=context.turn_id,
            source_refs=[f"user_request:{context.turn_id or 'unknown'}"],
            happened_at=utc_now(),
        )
        return {"memory_id": record.id, "status": record.status.value}

    async def forget(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        assert isinstance(arguments, ForgetMemoryArguments)
        session = await memory.session_for_tool(context.session_id)
        record = await memory.forget(session=session, memory_id=arguments.memory_id, reason=arguments.reason)
        return {"memory_id": record.id, "status": record.status.value}

    async def correct(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        assert isinstance(arguments, CorrectMemoryArguments)
        session = await memory.session_for_tool(context.session_id)
        record = await memory.correct(
            session=session,
            memory_id=arguments.memory_id,
            corrected_content=arguments.corrected_content,
            reason=arguments.reason,
        )
        return {
            "memory_id": record.id,
            "status": record.status.value,
            "supersedes_id": record.supersedes_id,
        }

    return [
        RegisteredTool(
            "recall_memory", "按当前会话可见域检索长期记忆；不跨私聊或群聊。", RecallMemoryArguments, recall
        ),
        RegisteredTool(
            "remember",
            "仅当用户明确要求记住某件事时，将其保存到当前会话长期记忆。",
            RememberArguments,
            remember,
        ),
        RegisteredTool(
            "forget_memory",
            "仅当用户明确要求忘记一条已召回记忆时，按 memory_id 可审计撤回。",
            ForgetMemoryArguments,
            forget,
        ),
        RegisteredTool(
            "correct_memory",
            "仅当用户明确纠正一条已召回记忆时，保留旧版本并写入替代版本。",
            CorrectMemoryArguments,
            correct,
        ),
    ]
