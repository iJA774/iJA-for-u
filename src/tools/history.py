"""当前会话历史分页、全文搜索与精确回源工具。"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from domain.errors import InputValidationError, NotFoundError
from domain.models import StoredMessage
from ports import ApplicationRepository
from tools.registry import RegisteredTool, ToolContext


class HistoryPageArguments(BaseModel):
    """限制单页大小并允许继续读取更早结果。"""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=1000)


class SearchMessagesArguments(HistoryPageArguments):
    """在当前会话全部可召回原始消息中搜索文本。"""

    query: str = Field(min_length=1, max_length=500)


class FetchSourceArguments(BaseModel):
    """精确读取消息或主动候选来源。"""

    model_config = ConfigDict(extra="forbid")

    source_ref: str = Field(min_length=1, max_length=500)


def _encode_cursor(message: StoredMessage) -> str:
    payload = json.dumps(
        {"created_at": message.created_at.isoformat(), "message_id": message.id},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    if not cursor:
        return None, None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        message_id = str(payload["message_id"]).strip()
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputValidationError("历史游标无效") from exc
    if not message_id:
        raise InputValidationError("历史游标无效")
    return created_at, message_id


def _message_payload(item: StoredMessage) -> dict[str, object]:
    return {
        "source_ref": f"message:{item.id}",
        "message_id": item.id,
        "role": item.role.value,
        "sender_id": item.sender_id,
        "sender_name": item.sender_name,
        "content": item.plain_text,
        "created_at": item.created_at.isoformat(),
    }


def build_history_tools(store: ApplicationRepository) -> list[RegisteredTool]:
    """构造只能读取当前会话可召回窗口的历史工具。"""

    async def _page(
        arguments: HistoryPageArguments,
        context: ToolContext,
        *,
        query_text: str | None = None,
    ) -> dict[str, Any]:
        cursor_at, cursor_id = _decode_cursor(arguments.cursor)
        rows = await store.page_recallable_messages(
            context.session_id,
            limit=arguments.limit + 1,
            cursor_created_at=cursor_at,
            cursor_message_id=cursor_id,
            query_text=query_text,
        )
        has_more = len(rows) > arguments.limit
        page = rows[: arguments.limit]
        return {
            "messages": [_message_payload(item) for item in page],
            "next_cursor": _encode_cursor(page[-1]) if has_more and page else None,
            "has_more": has_more,
        }

    async def fetch_history(
        arguments: BaseModel, context: ToolContext
    ) -> dict[str, object]:
        assert isinstance(arguments, HistoryPageArguments)
        result = await _page(arguments, context)
        result["summary"] = f"已返回当前会话 {len(result['messages'])} 条原始消息"
        return result

    async def search_messages(
        arguments: BaseModel, context: ToolContext
    ) -> dict[str, object]:
        assert isinstance(arguments, SearchMessagesArguments)
        normalized = arguments.query.strip()
        if not normalized:
            raise InputValidationError("搜索文本不能为空")
        result = await _page(arguments, context, query_text=normalized)
        result["query"] = normalized
        result["summary"] = f"当前会话本页命中 {len(result['messages'])} 条原始消息"
        return result

    async def fetch_source(
        arguments: BaseModel, context: ToolContext
    ) -> dict[str, object]:
        assert isinstance(arguments, FetchSourceArguments)
        source_ref = arguments.source_ref.strip()
        message_id = source_ref.removeprefix("message:")
        message = await store.get_recallable_message(context.session_id, message_id)
        if message is not None:
            return {"kind": "message", "source": _message_payload(message)}
        candidate = await store.get_proactive_candidate(source_ref)
        if candidate is not None and candidate.session_id == context.session_id:
            return {
                "kind": "proactive_candidate",
                "source": {
                    "source_ref": candidate.id,
                    "title": candidate.title,
                    "summary": candidate.summary,
                    "url": candidate.url,
                    "source_refs": candidate.source_refs,
                    "published_at": (
                        candidate.published_at.isoformat()
                        if candidate.published_at is not None
                        else None
                    ),
                },
            }
        raise NotFoundError("当前会话中不存在该可读取来源")

    return [
        RegisteredTool(
            name="fetch_history",
            description="按稳定游标分页读取当前会话原始消息；不能访问其他会话。",
            arguments_model=HistoryPageArguments,
            handler=fetch_history,
        ),
        RegisteredTool(
            name="search_messages",
            description=(
                "全文搜索当前会话全部可召回原始消息；使用 next_cursor 继续读取，"
                "未命中不代表清除窗口之前从未发生。"
            ),
            arguments_model=SearchMessagesArguments,
            handler=search_messages,
        ),
        RegisteredTool(
            name="fetch_source",
            description=(
                "按 search_messages 返回的 message:ID 或主动候选 source_ref 精确回源；"
                "只允许当前会话。"
            ),
            arguments_model=FetchSourceArguments,
            handler=fetch_source,
        ),
    ]
