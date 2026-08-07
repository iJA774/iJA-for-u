"""当前会话人物画像查询工具。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from domain.models import FactStatus, scope_key_for
from ports import ProfileMemoryRepository
from tools.registry import RegisteredTool, ToolContext


class QueryProfileArguments(BaseModel):
    """查询当前可见域内指定成员的已归档画像。"""

    model_config = ConfigDict(extra="forbid")

    subject_id: str | None = Field(default=None, min_length=1, max_length=200)
    limit: int = Field(default=8, ge=1, le=20)


def build_profile_tools(store: ProfileMemoryRepository) -> list[RegisteredTool]:
    """构造不会跨会话域读取画像的查询工具。"""

    async def query_profile(arguments: BaseModel, context: ToolContext) -> dict[str, object]:
        assert isinstance(arguments, QueryProfileArguments)
        session = await store.get_session(context.session_id)
        if session is None:
            # 工具调用上下文只能由应用层创建，会话缺失属于不可恢复的契约错误。
            raise RuntimeError("工具调用对应会话不存在")
        subject_id = arguments.subject_id or context.actor_id
        if not subject_id:
            return {"facts": [], "summary": "当前请求没有可查询的成员身份"}
        facts = [
            item
            for item in await store.list_facts(scope_key_for(session))
            if item.subject_id == subject_id and item.status == FactStatus.ACTIVE
        ][: arguments.limit]
        return {
            "subject_id": subject_id,
            "facts": [
                {
                    "fact_id": item.id,
                    "category": item.category,
                    "content": item.content,
                    "confidence": item.confidence,
                    "source_message_ids": item.source_message_ids,
                }
                for item in facts
            ],
            "summary": f"当前会话中找到 {len(facts)} 条画像事实",
        }

    return [
        RegisteredTool(
            name="query_profile",
            description="查询当前会话可见域内某位成员的已归档画像；不跨私聊或群聊。",
            arguments_model=QueryProfileArguments,
            handler=query_profile,
        )
    ]
