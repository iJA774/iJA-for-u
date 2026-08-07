"""QQ 群管理 Skill 所需的无策略宿主桥接。"""

from __future__ import annotations

import re
from typing import Any

from domain.errors import InputValidationError, NotFoundError
from domain.models import ChatType, SessionView
from plugins._host import ChannelRouter
from ports import ChatRepository


class QQGroupManagementGateway:
    """只解析权威宿主对象并转发 OneBot 动作，不拥有 Skill 授权规则。"""

    def __init__(self, store: ChatRepository, channel: ChannelRouter) -> None:
        self.store = store
        self.channel = channel

    async def is_available(self) -> bool:
        """仅报告宿主是否存在 OneBot 群管理路由。"""

        return self.channel.has_group_management_route("onebot")

    async def get_current_group(self, *, session_id: str) -> dict[str, str]:
        """返回当前 Session 的最小群身份，不推断任何管理权限。"""

        session = await self._group_session(session_id)
        return {
            "session_id": session.id,
            "platform": session.platform,
            "group_id": session.external_chat_id,
            "bot_id": session.account_id,
        }

    async def inspect_member(
        self,
        *,
        session_id: str,
        user_id: str,
    ) -> dict[str, str]:
        """读取指定成员的实时平台信息，不决定调用方是否有权操作。"""

        session = await self._group_session(session_id)
        normalized_id = self._validate_qq_id(user_id, "群成员")
        result = await self.channel.manage_group(
            session,
            "inspect_member",
            {"user_id": normalized_id},
        )
        return {
            "user_id": str(result.get("user_id") or ""),
            "display_name": str(result.get("display_name") or ""),
            "role": str(result.get("role") or ""),
        }

    async def resolve_recallable_message(
        self,
        *,
        session_id: str,
        message_id: str,
    ) -> dict[str, str] | None:
        """把当前 Session 内部消息解析为执行撤回所需的最小信息。"""

        await self._group_session(session_id)
        message = await self.store.get_recallable_message(session_id, message_id)
        if message is None:
            return None
        return {
            "message_id": message.id,
            "sender_id": message.sender_id,
            "external_message_id": message.external_message_id or "",
        }

    async def recall_message(
        self,
        *,
        session_id: str,
        external_message_id: str,
    ) -> dict[str, Any]:
        """转发撤回动作；权限判断由 Skill 在调用本方法前完成。"""

        session = await self._group_session(session_id)
        return await self.channel.manage_group(
            session,
            "recall_message",
            {"external_message_id": external_message_id},
        )

    async def set_member_mute(
        self,
        *,
        session_id: str,
        target_user_id: str,
        duration_seconds: int,
    ) -> dict[str, Any]:
        """转发成员禁言动作；不包含角色层级规则。"""

        session = await self._group_session(session_id)
        return await self.channel.manage_group(
            session,
            "set_member_mute",
            {"user_id": target_user_id, "duration": duration_seconds},
        )

    async def set_whole_mute(
        self,
        *,
        session_id: str,
        enable: bool,
    ) -> dict[str, Any]:
        """转发全员禁言动作；不包含授权规则。"""

        session = await self._group_session(session_id)
        return await self.channel.manage_group(
            session,
            "set_whole_mute",
            {"enable": enable},
        )

    async def kick_member(
        self,
        *,
        session_id: str,
        target_user_id: str,
        reject_add_request: bool,
    ) -> dict[str, Any]:
        """转发移出成员动作；不包含角色层级规则。"""

        session = await self._group_session(session_id)
        return await self.channel.manage_group(
            session,
            "kick_member",
            {
                "user_id": target_user_id,
                "reject_add_request": reject_add_request,
            },
        )

    async def _group_session(self, session_id: str) -> SessionView:
        """解析当前 OneBot 群会话；这是数据边界校验而非权限决策。"""

        session = await self.store.get_session(session_id)
        if session is None:
            raise NotFoundError("当前会话不存在")
        if session.platform != "onebot" or session.chat_type != ChatType.GROUP:
            raise InputValidationError("QQ 群管理只能用于当前 OneBot 群会话")
        return session

    @staticmethod
    def _validate_qq_id(value: str, label: str) -> str:
        normalized = str(value).strip()
        if re.fullmatch(r"[1-9][0-9]{4,19}", normalized) is None:
            raise InputValidationError(f"{label} QQ 号无效")
        return normalized
