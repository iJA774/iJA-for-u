"""按稳定平台路由选择唯一出站 Channel 插件。"""

from __future__ import annotations

from typing import Any, cast

from domain.errors import NotFoundError
from domain.models import (
    DeliveryReceipt,
    DeliveryStatus,
    OutboundMessage,
    SessionView,
)
from ports import ChannelAdapter, ChannelRuntimeContext

from .contracts import GroupManagementChannel, RuntimeContextChannel


class ChannelRouter:
    """统一出站适配器；未知路由明确失败，不退回模拟通道。"""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], ChannelAdapter] = {}

    def register(self, platform: str, account_id: str, adapter: ChannelAdapter) -> None:
        key = self._key(platform, account_id)
        if key in self._routes:
            raise ValueError(f"Channel 路由重复注册: {platform}/{account_id}")
        self._routes[key] = adapter

    def replace_owned(
        self,
        old_keys: set[tuple[str, str]],
        routes: dict[tuple[str, str], ChannelAdapter],
    ) -> None:
        """原子替换一个插件 generation 拥有的路由集合。"""

        normalized = {
            self._key(platform, account_id): adapter
            for (platform, account_id), adapter in routes.items()
        }
        conflicts = set(normalized).intersection(self._routes).difference(old_keys)
        if conflicts:
            platform, account_id = sorted(conflicts)[0]
            raise ValueError(f"Channel 路由与非插件路由冲突: {platform}/{account_id}")
        updated = dict(self._routes)
        for key in old_keys:
            updated.pop(key, None)
        updated.update(normalized)
        self._routes = updated

    async def send(self, session: SessionView, message: OutboundMessage) -> DeliveryReceipt:
        adapter = self._routes.get(self._key(session.platform, session.account_id))
        if adapter is None:
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code="channel_route_not_found",
                error_message=(f"未找到 Channel 路由: {session.platform}/{session.account_id}"),
            )
        return await adapter.send(session, message)

    async def runtime_context(self, session: SessionView) -> ChannelRuntimeContext:
        """读取当前路由声明的临时会话状态；不支持时返回空状态。"""

        adapter = self._routes.get(self._key(session.platform, session.account_id))
        if adapter is None:
            return ChannelRuntimeContext()
        handler = getattr(adapter, "runtime_context", None)
        if not callable(handler):
            return ChannelRuntimeContext()
        result = await cast(RuntimeContextChannel, adapter).runtime_context(session)
        if not isinstance(result, ChannelRuntimeContext):
            raise TypeError("Channel runtime_context 必须返回 ChannelRuntimeContext")
        return result

    def has_group_management_route(self, platform: str) -> bool:
        """判断指定平台是否至少存在一个声明群管理能力的路由。"""

        normalized = platform.strip().lower()
        return any(
            route_platform == normalized and callable(getattr(adapter, "manage_group", None))
            for (route_platform, _), adapter in self._routes.items()
        )

    async def manage_group(
        self,
        session: SessionView,
        action: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """把群管理动作路由到当前 Channel generation。"""

        adapter = self._routes.get(self._key(session.platform, session.account_id))
        if adapter is None:
            raise NotFoundError(f"未找到 Channel 路由: {session.platform}/{session.account_id}")
        handler = getattr(adapter, "manage_group", None)
        if not callable(handler):
            raise NotFoundError("当前 Channel 不支持群管理")
        result = await cast(GroupManagementChannel, adapter).manage_group(
            session,
            action,
            parameters,
        )
        if not isinstance(result, dict):
            raise TypeError("Channel 群管理结果必须为 dict")
        return result

    @staticmethod
    def _key(platform: str, account_id: str) -> tuple[str, str]:
        return platform.strip().lower(), account_id.strip()
