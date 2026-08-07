"""本地黑名单应用服务：Agent 拉黑与 WebUI 管理的统一入口。"""

from __future__ import annotations

import logging
from typing import Any

from application.events import EventHub
from domain.errors import InputValidationError, NotFoundError
from domain.models import (
    BlacklistEntry,
    BlacklistSource,
    MessageComponent,
    ReplyDraft,
    SessionView,
)
from ports import OperationsRepository

logger = logging.getLogger(__name__)


class BlacklistService:
    """向 WebUI 和 local-blacklist Skill 提供黑名单写入、查询与拦截判定。

    拉黑维度与 ``IdentityRow`` 一致：同一机器人账号下某平台的某个外部用户。
    拉黑后该用户在任何会话（私聊或群聊）的入站消息都不再受理、不调度 Turn。
    """

    DEFAULT_AGENT_CAPTION = "由于你的发言屡次违反社区准则，我已停止为你服务。"

    def __init__(self, *, store: OperationsRepository, events: EventHub) -> None:
        self.store = store
        self.events = events

    async def list(self) -> list[BlacklistEntry]:
        return await self.store.list_blacklist()

    async def is_blocked(
        self, platform: str, account_id: str, external_user_id: str
    ) -> bool:
        return await self.store.is_blacklisted(platform, account_id, external_user_id)

    async def block(
        self,
        *,
        platform: str,
        account_id: str,
        external_user_id: str,
        display_name: str,
        reason: str,
        source: BlacklistSource = BlacklistSource.AGENT,
        session_id: str | None = None,
    ) -> BlacklistEntry:
        """写入黑名单；同一用户重复拉黑时更新原因与展示名。"""

        entry = BlacklistEntry(
            platform=platform,
            account_id=account_id,
            external_user_id=external_user_id,
            display_name=display_name,
            reason=reason,
            source=source,
            session_id=session_id,
        )
        saved = await self.store.add_blacklist(entry)
        await self.events.publish("blacklist.blocked", saved.model_dump(mode="json"))
        logger.info(
            "用户已被拉黑",
            extra={
                "session_id": session_id or "-",
                "turn_id": "-",
                "platform": platform,
                "account_id": account_id,
                "external_user_id": external_user_id,
                "source": source.value,
            },
        )
        return saved

    async def unblock(self, entry_id: str) -> BlacklistEntry:
        removed = await self.store.remove_blacklist(entry_id)
        await self.events.publish("blacklist.unblocked", removed.model_dump(mode="json"))
        logger.info(
            "用户已从黑名单移除",
            extra={
                "session_id": "-",
                "turn_id": "-",
                "entry_id": entry_id,
                "external_user_id": removed.external_user_id,
            },
        )
        return removed

    async def get(self, entry_id: str) -> BlacklistEntry | None:
        return await self.store.get_blacklist(entry_id)

    # ---- 以下为 local-blacklist Skill 的 capability 接口 ----

    async def is_available(self) -> bool:
        """Skill 始终可用；拉黑是宿主本地副作用，不依赖外部 Provider。"""

        return True

    async def block_current_user(
        self,
        *,
        context: Any,
        reason: str,
        caption: str | None,
    ) -> dict[str, Any]:
        """按当前 Turn 上下文拉黑发送者，并返回终态回复草稿。

        context 必须提供 ``session_id`` 与 ``actor_id``（当前消息发送者）。
        返回 ``{"value": ..., "reply_draft": ReplyDraft}``，由 Skill 透传给宿主。
        """

        session_id = getattr(context, "session_id", None)
        actor_id = getattr(context, "actor_id", None)
        if not session_id or not actor_id:
            raise InputValidationError("缺少会话或发送者上下文，无法拉黑")
        session = await self.store.get_session(session_id)
        if session is None:
            raise NotFoundError("当前会话不存在，无法拉黑")
        display_name = self._resolve_display_name(session, actor_id)
        entry = await self.block(
            platform=session.platform,
            account_id=session.account_id,
            external_user_id=actor_id,
            display_name=display_name,
            reason=reason,
            source=BlacklistSource.AGENT,
            session_id=session_id,
        )
        visible_caption = (caption or "").strip() or self.DEFAULT_AGENT_CAPTION
        draft = ReplyDraft(
            components=[MessageComponent.text_component(visible_caption)]
        )
        return {
            "value": {
                "blocked": True,
                "entry_id": entry.id,
                "external_user_id": entry.external_user_id,
                "display_name": entry.display_name,
                "platform": entry.platform,
                "account_id": entry.account_id,
            },
            "reply_draft": draft,
        }

    @staticmethod
    def _resolve_display_name(session: SessionView, external_user_id: str) -> str:
        member = next(
            (
                item
                for item in session.participants
                if item.external_user_id == external_user_id
            ),
            None,
        )
        return member.display_name if member is not None else external_user_id
