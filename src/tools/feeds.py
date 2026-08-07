"""聊天内公开资讯订阅的工具契约。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domain.errors import InputValidationError, NotFoundError
from ports import OperationsRepository
from tools.registry import RegisteredTool, ToolContext

if TYPE_CHECKING:
    from proactive.service import EngagementService


class _StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeedSubscribeArguments(_StrictArguments):
    """支持用户提供公开 Feed，也支持只提供自然语言资讯主题。"""

    topic: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    title: str = Field(default="", max_length=200)
    poll_interval_minutes: int = Field(default=30, ge=15, le=1440)

    @model_validator(mode="after")
    def require_exactly_one_source(self) -> FeedSubscribeArguments:
        if bool((self.topic or "").strip()) == bool((self.url or "").strip()):
            raise ValueError("topic 和 url 必须且只能填写一个")
        return self


class FeedListArguments(_StrictArguments):
    pass


class FeedUnsubscribeArguments(_StrictArguments):
    feed_id: str = Field(min_length=1, max_length=80)


def _topic_feed_url(topic: str) -> str:
    """把主题映射到应用控制的公开 Google News RSS 查询，不信任模型生成 URL。"""

    query = urlencode(
        {
            "q": topic.strip(),
            "hl": "zh-CN",
            "gl": "CN",
            "ceid": "CN:zh-Hans",
        }
    )
    return f"https://news.google.com/rss/search?{query}"


def build_feed_tools(
    engagement: EngagementService,
    store: OperationsRepository,
    wake_scheduler: Callable[[], None],
) -> list[RegisteredTool]:
    """构造当前私聊内的订阅管理工具。"""

    async def subscribe(
        arguments: BaseModel, context: ToolContext
    ) -> dict[str, object]:
        assert isinstance(arguments, FeedSubscribeArguments)
        topic = (arguments.topic or "").strip()
        url = _topic_feed_url(topic) if topic else (arguments.url or "").strip()
        title = arguments.title.strip() or (f"{topic}资讯" if topic else "")
        source = await engagement.create_feed(
            context.session_id,
            url=url,
            title=title,
            poll_interval_minutes=arguments.poll_interval_minutes,
        )
        policy = await engagement.get_policy(context.session_id)
        if not policy.proactive_enabled:
            await engagement.update_policy(
                context.session_id,
                proactive_enabled=True,
                drift_enabled=policy.drift_enabled,
                timezone=policy.timezone,
                quiet_start=policy.quiet_start,
                quiet_end=policy.quiet_end,
                minimum_interval_minutes=policy.minimum_interval_minutes,
            )
        wake_scheduler()
        return {
            "feed": source.model_dump(mode="json"),
            "summary": f"已订阅：{source.title or source.url}",
        }

    async def list_(
        arguments: BaseModel, context: ToolContext
    ) -> dict[str, object]:
        assert isinstance(arguments, FeedListArguments)
        session = await store.get_session(context.session_id)
        if session is None:
            raise NotFoundError("会话不存在")
        if session.chat_type.value != "private":
            raise InputValidationError("资讯订阅只支持私聊")
        items = await store.list_feed_sources(context.session_id)
        return {
            "feeds": [item.model_dump(mode="json") for item in items],
            "summary": f"当前私聊有 {len(items)} 个订阅",
        }

    async def unsubscribe(
        arguments: BaseModel, context: ToolContext
    ) -> dict[str, object]:
        assert isinstance(arguments, FeedUnsubscribeArguments)
        source = await store.get_feed_source(arguments.feed_id)
        if source is None or source.session_id != context.session_id:
            raise NotFoundError("订阅源不存在或不属于当前会话")
        deleted = await engagement.delete_feed(source.id)
        return {
            "feed": deleted.model_dump(mode="json"),
            "summary": f"已取消订阅：{deleted.title or deleted.url}",
        }

    return [
        RegisteredTool(
            "feed_subscribe",
            (
                "当私聊用户明确要求订阅、持续关注或以后推送某类公开资讯时调用。"
                "用户只给主题（如“金融新闻”）时填写 topic，不要自行编造 URL；"
                "用户明确给出 RSS/Atom 地址时才填写 url。"
            ),
            FeedSubscribeArguments,
            subscribe,
        ),
        RegisteredTool(
            "feed_list",
            "列出当前私聊已经创建的公开资讯订阅及 feed_id。",
            FeedListArguments,
            list_,
        ),
        RegisteredTool(
            "feed_unsubscribe",
            "取消当前私聊的资讯订阅；应先调用 feed_list 获取准确 feed_id。",
            FeedUnsubscribeArguments,
            unsubscribe,
        ),
    ]
