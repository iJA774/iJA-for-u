"""OneBot 11 协议 QQ Channel 插件。

通过正向 WebSocket 连接 OneBot 实现（如 NapCat、Lagrange.OneBot）接收事件，
通过 HTTP API 发送消息。不依赖任何第三方 QQ SDK，也不登录真实账号；
账号登录与协议实现由外部 OneBot 服务承担，本插件只负责协议适配与富消息映射。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from domain.errors import InputValidationError
from domain.models import (
    ChatType,
    ComponentType,
    DeliveryReceipt,
    DeliveryStatus,
    InboundMessage,
    MessageComponent,
    OutboundMessage,
    ParticipantRole,
    SessionView,
    utc_now,
)
from imaging import inspect_dimensions
from plugins._host import (
    HttpCallbackRequest,
    HttpCallbackResponse,
    InboundEnvelope,
    MessageReference,
    PluginContext,
    PluginUnavailableError,
    TypingEvent,
)
from ports import ChannelRuntimeContext

logger = logging.getLogger(__name__)

# 群成员角色 -> 核心参与者角色
_ROLE_MAP = {
    "owner": ParticipantRole.OWNER,
    "admin": ParticipantRole.ADMIN,
    "member": ParticipantRole.MEMBER,
}

# CQ 码通用匹配：[CQ:type,key=value,key=value]
_CQ_RE = re.compile(r"\[CQ:([A-Za-z0-9_-]+)((?:,[A-Za-z0-9_.-]+=[^,\]]*)*)\]")


class OneBotGroupConfig(BaseModel):
    """单个群聊的接入策略。"""

    group_id: str = Field(min_length=1, max_length=50)
    require_at: bool = True
    allow_from: list[str] = Field(default_factory=list)


class OneBotSettings(BaseModel):
    """OneBot 插件配置；凭据与 OneBot 服务地址由部署方提供。"""

    ws_url: str = Field(min_length=1, max_length=500)
    http_base_url: str = Field(min_length=1, max_length=500)
    access_token: str = ""
    bot_uin: str = Field(min_length=1, max_length=50)
    allow_from: list[str] = Field(default_factory=list)
    groups: list[OneBotGroupConfig] = Field(default_factory=list)
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    ws_open_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    max_reconnect_attempts: int = Field(default=8, ge=0, le=20)
    max_inbound_images: int = Field(default=4, ge=1, le=10)
    max_image_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    max_image_redirects: int = Field(default=3, ge=0, le=5)
    image_allowed_host_suffixes: list[str] = Field(
        default_factory=lambda: [".qpic.cn", ".qq.com", ".qq.com.cn", ".gtimg.cn"],
        max_length=20,
    )

    @model_validator(mode="after")
    def validate_endpoints(self) -> OneBotSettings:
        for field_name, value in (("ws_url", self.ws_url), ("http_base_url", self.http_base_url)):
            parsed = httpx.URL(value)
            if parsed.scheme not in {"ws", "wss", "http", "https"}:
                raise ValueError(f"{field_name} 必须是 ws/wss/http/https 地址")
            if not parsed.host:
                raise ValueError(f"{field_name} 缺少主机名")
            if bool(parsed.userinfo):
                raise ValueError(f"{field_name} 不得携带用户名密码")
        return self

    @field_validator("groups")
    @classmethod
    def validate_unique_groups(cls, groups: list[OneBotGroupConfig]) -> list[OneBotGroupConfig]:
        seen: set[str] = set()
        for group in groups:
            if group.group_id in seen:
                raise ValueError(f"重复的群配置: {group.group_id}")
            seen.add(group.group_id)
        return groups

    @field_validator("image_allowed_host_suffixes")
    @classmethod
    def validate_image_hosts(cls, values: list[str]) -> list[str]:
        """只接受显式主机名或点前缀域名，避免配置被解释成任意 URL。"""

        normalized: list[str] = []
        for value in values:
            host = value.strip().casefold().rstrip(".")
            if not host or "/" in host or ":" in host or "@" in host:
                raise ValueError(f"无效的图片主机规则: {value}")
            normalized.append(host)
        return list(dict.fromkeys(normalized))

    @classmethod
    def from_context(cls, context: PluginContext) -> OneBotSettings:
        values = dict(context.options)
        env_values = {
            "ws_url": os.getenv("IJA_ONEBOT_WS_URL", ""),
            "http_base_url": os.getenv("IJA_ONEBOT_HTTP_URL", ""),
            "access_token": os.getenv("IJA_ONEBOT_ACCESS_TOKEN", ""),
            "bot_uin": os.getenv("IJA_ONEBOT_BOT_UIN", ""),
        }
        for key, value in env_values.items():
            if value:
                values[key] = value
        return cls.model_validate(values)


class OneBotPlugin:
    """OneBot 正向 WebSocket 入站与 HTTP API 出站适配器。"""

    plugin_id = "onebot"
    platform = "onebot"

    def __init__(
        self,
        settings: OneBotSettings,
        context: PluginContext,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.context = context
        self.account_id = settings.bot_uin
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
        )
        self._ws_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._groups: dict[str, OneBotGroupConfig] = {g.group_id: g for g in settings.groups}
        self._private_allow: set[str] = set(settings.allow_from)

    async def prepare(self) -> None:
        """纯校验阶段；不得连接 OneBot 或访问正式附件目录。"""

    async def activate(self) -> None:
        """启动 OneBot 连接；平台事件在发布前会被 generation admission 丢弃。"""

        if self._ws_task is not None and not self._ws_task.done():
            return
        if self._client.is_closed:
            if not self._owns_client:
                raise RuntimeError("OneBot 插件注入的 HTTP Client 已关闭，不能重新激活")
            self._client = httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                follow_redirects=False,
            )
        self._stop_event.clear()
        self._ready_event.clear()
        self._ws_task = asyncio.create_task(self._run_loop(), name="onebot-ws-client")
        self._ws_task.add_done_callback(self._report_exit)

    async def ready(self) -> None:
        """等待正向 WebSocket 实际建连，避免仅凭后台任务存在就发布。"""

        task = self._ws_task
        if task is None:
            raise RuntimeError("OneBot Channel 尚未 activate")
        waiter = asyncio.create_task(self._ready_event.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, task},
                timeout=self.settings.ws_open_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                if task.cancelled():
                    raise RuntimeError("OneBot WebSocket 在 ready 前被取消")
                error = task.exception()
                if error is not None:
                    raise RuntimeError("OneBot WebSocket 在 ready 前退出") from error
                raise RuntimeError("OneBot WebSocket 在 ready 前结束")
            if waiter in done and self._ready_event.is_set():
                return
            raise RuntimeError("等待 OneBot WebSocket ready 超时")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def deactivate(self) -> None:
        """停止连接并释放当前激活周期的 HTTP 资源。"""

        self._stop_event.set()
        self._ready_event.clear()
        if self._ws_task is not None:
            self._ws_task.cancel()
            await asyncio.gather(self._ws_task, return_exceptions=True)
            self._ws_task = None
        if self._owns_client:
            await self._client.aclose()

    async def start(self) -> None:
        """兼容旧宿主与独立测试；generation 宿主会调用 activate/ready。"""

        await self.activate()

    async def stop(self) -> None:
        """兼容旧宿主与独立测试。"""

        await self.deactivate()

    async def handle_http(self, request: HttpCallbackRequest) -> HttpCallbackResponse:
        return HttpCallbackResponse.text(
            "OneBot 插件使用正向 WebSocket，不接受 HTTP 回调",
            status_code=405,
        )

    async def send(self, session: SessionView, message: OutboundMessage) -> DeliveryReceipt:
        file_components = [
            component
            for component in message.components
            if component.type == ComponentType.FILE_REF
        ]
        if file_components:
            if len(file_components) != 1 or len(message.components) != 1:
                return DeliveryReceipt(
                    outbound_id=message.id,
                    status=DeliveryStatus.FAILED,
                    error_code="onebot_file_must_be_standalone",
                    error_message="OneBot 文件必须作为唯一组件独立上传",
                )
            return await self._upload_file(session, message, file_components[0])

        segments, unsupported = _render_segments(message.components)
        if unsupported:
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code="onebot_component_unsupported",
                error_message=f"OneBot 出站尚不支持组件: {', '.join(unsupported)}",
            )
        # 私聊是严格的一对一会话，引用段既没有额外的指向价值，也会造成多余的
        # 平台样式；仅在群聊中保留被动回复的引用关联。
        if session.chat_type == ChatType.GROUP and message.reply_to_message_id:
            external_id = await self.context.resolve_external_message_id(message.reply_to_message_id)
            reply_id = decode_reply_id(external_id)
            if reply_id:
                segments.insert(0, {"type": "reply", "data": {"id": reply_id}})
        if not segments:
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code="onebot_empty_message",
                error_message="OneBot 出站消息不能为空",
            )
        # 图片和语音段需读取本地受控文件并转 base64；失败则整体投递失败。
        try:
            await self._materialize_media_segments(segments)
        except (OSError, ValueError) as exc:
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code="onebot_media_read_failed",
                error_message=str(exc)[:500],
            )
        endpoint, params = self._send_target(session)
        if endpoint is None:
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code="onebot_invalid_chat_id",
                error_message=f"无法从会话 external_chat_id 解析目标: {session.external_chat_id}",
            )
        try:
            response = await self._client.post(
                f"{self.settings.http_base_url.rstrip('/')}{endpoint}",
                headers=self._auth_headers(),
                json={"message": segments, "auto_escape": False, **params},
            )
        except httpx.HTTPError as exc:
            raise RuntimeError("OneBot HTTP 请求失败，投递结果未知") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("OneBot 发消息响应不是有效 JSON，投递结果未知") from exc
        try:
            retcode = int(payload.get("retcode", 0) or 0)
        except (TypeError, ValueError):
            retcode = -1
        if response.status_code >= 400 or retcode != 0:
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code=f"onebot_{retcode or response.status_code}",
                error_message=str(payload.get("msg") or "OneBot API 请求失败")[:500],
            )
        data = payload.get("data")
        external_id = data.get("message_id") if isinstance(data, dict) else None
        return DeliveryReceipt(
            outbound_id=message.id,
            status=DeliveryStatus.SENT,
            external_message_id=str(external_id) if external_id is not None else None,
            delivered_at=utc_now(),
        )

    async def manage_group(
        self,
        session: SessionView,
        action: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """执行经过宿主授权的 OneBot 群管理动作。"""

        if session.chat_type != ChatType.GROUP:
            raise ValueError("OneBot 群管理只能用于群聊会话")
        group_id = _parse_uin(session.external_chat_id)
        if group_id is None or group_id <= 0:
            raise ValueError("当前 OneBot 群号无效")

        if action == "inspect_member":
            user_id = _parse_uin(str(parameters.get("user_id") or ""))
            if user_id is None or user_id <= 0:
                raise ValueError("群成员 QQ 号无效")
            data = await self._request_group_action(
                "/get_group_member_info",
                {"group_id": group_id, "user_id": user_id, "no_cache": True},
                mutating=False,
            )
            role = str(data.get("role") or "").lower()
            if role not in {"owner", "admin", "member"}:
                raise RuntimeError("NapCat 返回了无效的群成员角色")
            returned_user_id = _parse_uin(str(data.get("user_id") or ""))
            if returned_user_id != user_id:
                raise RuntimeError("NapCat 群成员响应与请求的 QQ 号不一致")
            returned_group_id = data.get("group_id")
            if returned_group_id is not None and _parse_uin(str(returned_group_id)) != group_id:
                raise RuntimeError("NapCat 群成员响应与当前群不一致")
            return {
                "user_id": str(returned_user_id),
                "display_name": str(
                    data.get("card")
                    or data.get("nickname")
                    or f"用户{str(user_id)[-6:]}"
                )[:100],
                "role": role,
            }

        if action == "recall_message":
            external_id = str(parameters.get("external_message_id") or "")
            message_id = decode_reply_id(external_id) or external_id
            parsed_message_id = _parse_uin(message_id)
            if parsed_message_id is None or parsed_message_id <= 0:
                raise ValueError("OneBot 外部消息 ID 无效")
            await self._request_group_action(
                "/delete_msg",
                {"message_id": parsed_message_id},
                mutating=True,
            )
            return {"success": True, "action": action}

        if action == "set_member_mute":
            user_id = _parse_uin(str(parameters.get("user_id") or ""))
            duration = parameters.get("duration")
            if (
                user_id is None
                or user_id <= 0
                or isinstance(duration, bool)
                or not isinstance(duration, int)
                or duration < 0
                or duration > 30 * 24 * 60 * 60
            ):
                raise ValueError("OneBot 群禁言参数无效")
            await self._request_group_action(
                "/set_group_ban",
                {
                    "group_id": group_id,
                    "user_id": user_id,
                    "duration": duration,
                },
                mutating=True,
            )
            return {"success": True, "action": action}

        if action == "set_whole_mute":
            enable = parameters.get("enable")
            if not isinstance(enable, bool):
                raise ValueError("OneBot 全员禁言开关无效")
            await self._request_group_action(
                "/set_group_whole_ban",
                {"group_id": group_id, "enable": enable},
                mutating=True,
            )
            return {"success": True, "action": action}

        if action == "kick_member":
            user_id = _parse_uin(str(parameters.get("user_id") or ""))
            reject_add_request = parameters.get("reject_add_request", False)
            if (
                user_id is None
                or user_id <= 0
                or not isinstance(reject_add_request, bool)
            ):
                raise ValueError("OneBot 踢人参数无效")
            await self._request_group_action(
                "/set_group_kick",
                {
                    "group_id": group_id,
                    "user_id": user_id,
                    "reject_add_request": reject_add_request,
                },
                mutating=True,
            )
            return {"success": True, "action": action}

        raise ValueError(f"未知 OneBot 群管理动作: {action}")

    async def runtime_context(self, session: SessionView) -> ChannelRuntimeContext:
        """实时投影 iJA 在当前群中的平台角色，供本轮 Prompt 明确身份。"""

        if session.chat_type != ChatType.GROUP:
            return ChannelRuntimeContext()
        member = await self.manage_group(
            session,
            "inspect_member",
            {"user_id": self.settings.bot_uin},
        )
        return ChannelRuntimeContext(
            agent_group_role=_ROLE_MAP[str(member["role"])],
        )

    async def _request_group_action(
        self,
        endpoint: str,
        payload_body: dict[str, Any],
        *,
        mutating: bool,
    ) -> dict[str, Any]:
        """调用白名单群管理 API；写请求传输失败时明确标记结果未知。"""

        try:
            response = await self._client.post(
                f"{self.settings.http_base_url.rstrip('/')}{endpoint}",
                headers=self._auth_headers(),
                json=payload_body,
            )
        except httpx.HTTPError as exc:
            message = (
                "OneBot 群管理请求失败，操作结果未知，请到 QQ 群核对"
                if mutating
                else "OneBot 群成员权限查询失败"
            )
            raise RuntimeError(message) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            message = (
                "OneBot 群管理响应无效，操作结果未知，请到 QQ 群核对"
                if mutating
                else "OneBot 群成员权限响应不是有效 JSON"
            )
            raise RuntimeError(message) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("OneBot 群管理响应必须是 JSON object")
        if "retcode" not in payload:
            message = (
                "OneBot 群管理响应缺少 retcode，操作结果未知，请到 QQ 群核对"
                if mutating
                else "OneBot 群成员权限响应缺少 retcode"
            )
            raise RuntimeError(message)
        try:
            retcode = int(payload["retcode"])
        except (TypeError, ValueError) as exc:
            message = (
                "OneBot 群管理 retcode 无效，操作结果未知，请到 QQ 群核对"
                if mutating
                else "OneBot 群成员权限 retcode 无效"
            )
            raise RuntimeError(message) from exc
        status = payload.get("status")
        if (
            response.status_code >= 400
            or retcode != 0
            or (status is not None and status != "ok")
        ):
            detail = str(
                payload.get("wording")
                or payload.get("message")
                or payload.get("msg")
                or "OneBot 群管理失败"
            )[:300]
            raise RuntimeError(f"OneBot 群管理失败（{retcode or response.status_code}）：{detail}")
        data = payload.get("data")
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise RuntimeError("OneBot 群管理 data 必须是 JSON object")
        return data

    def _send_target(self, session: SessionView) -> tuple[str | None, dict[str, Any]]:
        chat_id = session.external_chat_id
        if session.chat_type == ChatType.PRIVATE:
            user_id = _parse_uin(chat_id)
            if user_id is None:
                return None, {}
            return "/send_private_msg", {"user_id": user_id}
        group_id = _parse_uin(chat_id)
        if group_id is None:
            return None, {}
        return "/send_group_msg", {"group_id": group_id}

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.access_token:
            headers["Authorization"] = f"Bearer {self.settings.access_token}"
        return headers

    async def _upload_file(
        self,
        session: SessionView,
        message: OutboundMessage,
        component: MessageComponent,
    ) -> DeliveryReceipt:
        """把受控文件转换为 OneBot 私聊/群聊文件上传动作。"""

        endpoint, params = self._file_upload_target(session)
        if endpoint is None:
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code="onebot_invalid_chat_id",
                error_message=f"无法从会话 external_chat_id 解析目标: {session.external_chat_id}",
            )
        try:
            encoded = await asyncio.to_thread(
                _load_media_base64,
                component.storage_path or "",
                component.size,
                component.sha256,
            )
        except (OSError, ValueError) as exc:
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code="onebot_media_read_failed",
                error_message=str(exc)[:500],
            )
        return await self._post_action(
            message.id,
            endpoint,
            {
                **params,
                "file": f"base64://{encoded}",
                "name": component.filename or "attachment",
            },
        )

    async def _post_action(
        self, outbound_id: str, endpoint: str, payload_body: dict[str, Any]
    ) -> DeliveryReceipt:
        """执行单个 OneBot HTTP 动作并统一解释协议成功与失败。"""

        try:
            response = await self._client.post(
                f"{self.settings.http_base_url.rstrip('/')}{endpoint}",
                headers=self._auth_headers(),
                json=payload_body,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError("OneBot HTTP 请求失败，投递结果未知") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("OneBot 响应不是有效 JSON，投递结果未知") from exc
        try:
            retcode = int(payload.get("retcode", 0) or 0)
        except (TypeError, ValueError):
            retcode = -1
        if response.status_code >= 400 or retcode != 0:
            return DeliveryReceipt(
                outbound_id=outbound_id,
                status=DeliveryStatus.FAILED,
                error_code=f"onebot_{retcode or response.status_code}",
                error_message=str(payload.get("msg") or "OneBot API 请求失败")[:500],
            )
        data = payload.get("data")
        external_id = data.get("message_id") if isinstance(data, dict) else None
        return DeliveryReceipt(
            outbound_id=outbound_id,
            status=DeliveryStatus.SENT,
            external_message_id=str(external_id) if external_id is not None else None,
            delivered_at=utc_now(),
        )

    def _file_upload_target(self, session: SessionView) -> tuple[str | None, dict[str, Any]]:
        target_id = _parse_uin(session.external_chat_id)
        if target_id is None:
            return None, {}
        if session.chat_type == ChatType.PRIVATE:
            return "/upload_private_file", {"user_id": target_id}
        return "/upload_group_file", {"group_id": target_id}

    async def _materialize_media_segments(self, segments: list[dict[str, Any]]) -> None:
        """把出站图片/语音段中的受控路径解析为 OneBot base64 URI。"""

        for segment in segments:
            if segment.get("type") not in {"image", "record"}:
                continue
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            file_value = data.get("file")
            if not isinstance(file_value, str) or not file_value.startswith("storage:"):
                continue
            storage_path = file_value[len("storage:") :]
            expected_size = data.pop("_size", None)
            expected_sha256 = data.pop("_sha256", None)
            data["file"] = "base64://" + await asyncio.to_thread(
                _load_media_base64,
                storage_path,
                expected_size,
                expected_sha256,
            )

    async def _run_loop(self) -> None:
        attempts = 0
        while not self._stop_event.is_set():
            try:
                ws_headers: dict[str, str] = {}
                if self.settings.access_token:
                    ws_headers["Authorization"] = f"Bearer {self.settings.access_token}"
                async with connect(
                    self.settings.ws_url,
                    additional_headers=ws_headers,
                    open_timeout=self.settings.ws_open_timeout_seconds,
                    max_size=16 * 1024 * 1024,
                    ping_interval=None,
                ) as websocket:
                    self._ready_event.set()
                    await self._serve(websocket)
                attempts = 0
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, WebSocketException, json.JSONDecodeError, RuntimeError):
                attempts += 1
                if attempts > self.settings.max_reconnect_attempts:
                    raise RuntimeError("OneBot WebSocket 连续重连失败，已停止 Channel") from None
                logger.exception(
                    "OneBot WebSocket 连接中断",
                    extra={"session_id": "-", "turn_id": "-", "attempt": attempts},
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=min(2**attempts, 30),
                    )
                except TimeoutError:
                    pass

    async def _serve(self, websocket: Any) -> None:
        async for raw in websocket:
            async with self.context.admit_event() as admitted:
                if not admitted:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(
                        "OneBot 事件不是有效 JSON，已丢弃",
                        extra={"session_id": "-", "turn_id": "-"},
                    )
                    continue
                if not isinstance(event, dict):
                    continue
                self_id = str(event.get("self_id", ""))
                if self_id and self.settings.bot_uin and self_id != self.settings.bot_uin:
                    # 多机器人共用同一 OneBot 服务时只处理自身事件。
                    continue
                typing_event = normalize_typing_event(
                    self.account_id,
                    event,
                    self._private_allow,
                    self._groups,
                    self.settings.bot_uin,
                )
                if typing_event is not None:
                    await self.context.typing(typing_event)
                    continue
                envelope = normalize_event(
                    self.account_id,
                    event,
                    self._private_allow,
                    self._groups,
                    self.settings.bot_uin,
                    defer_reply_resolution=True,
                )
                if envelope is not None:
                    raw_segments = _coerce_segments(event.get("message"))
                    resolved_images = await self._resolve_inbound_images(
                        raw_segments,
                        external_message_id=str(event.get("message_id") or "-"),
                    )
                    _, reply_id, _ = _segments_to_components(
                        raw_segments,
                        self.settings.bot_uin,
                        max_segments=30,
                    )
                    resolved_reply = await self._resolve_reply_reference(reply_id)
                    hydrated = normalize_event(
                        self.account_id,
                        event,
                        self._private_allow,
                        self._groups,
                        self.settings.bot_uin,
                        resolved_images=resolved_images,
                        resolved_reply=resolved_reply,
                    )
                    if hydrated is not None:
                        await self.context.ingest(hydrated)

    async def _resolve_reply_reference(
        self, external_reply_id: str
    ) -> MessageReference | None:
        """把 OneBot 原始引用 ID 映射为核心消息身份。

        入站用户消息使用带 reply 元数据的稳定编码，Agent 出站消息保存平台原始
        ID，因此按两种形式查找。只拿回身份字段，不把被引用正文交给插件。
        """

        if not external_reply_id:
            return None
        for candidate in (
            external_reply_id,
            encode_reply_id(external_reply_id, None),
        ):
            reference = await self.context.resolve_message_reference(
                self.platform,
                self.account_id,
                candidate,
            )
            if reference is not None:
                return reference
        return None

    async def _resolve_inbound_images(
        self,
        segments: list[dict[str, Any]],
        *,
        external_message_id: str,
    ) -> dict[int, MessageComponent]:
        """即时取回 NapCat 图片资源；失败时让规范化层保留明确占位。"""

        if self.context.store_image is None:
            return {}
        resolved: dict[int, MessageComponent] = {}
        attempted = 0
        for index, segment in enumerate(segments):
            seg_type = str(segment.get("type") or "").casefold()
            if seg_type not in {"image", "mface"}:
                continue
            if attempted >= self.settings.max_inbound_images:
                break
            attempted += 1
            data_any = segment.get("data")
            data: dict[str, Any] = data_any if isinstance(data_any, dict) else {}
            try:
                component = await self._fetch_image_component(seg_type, data)
            except (
                httpx.HTTPError,
                InputValidationError,
                OSError,
                RuntimeError,
                ValueError,
            ) as exc:
                logger.warning(
                    "OneBot 入站图片读取失败，已保留不可用占位",
                    extra={
                        "session_id": "-",
                        "turn_id": "-",
                        "external_message_id": external_message_id,
                        "component_index": index,
                        "error_type": type(exc).__name__,
                    },
                )
                continue
            resolved[index] = component
        return resolved

    async def _fetch_image_component(
        self,
        seg_type: str,
        data: dict[str, Any],
    ) -> MessageComponent:
        """从事件 URL 或 ``get_image`` 解析资源并交给宿主附件仓库存储。"""

        candidates = _inline_or_http_candidates(data)
        file_id = str(data.get("file") or "").strip()
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                return await self._store_image_candidate(seg_type, data, candidate)
            except (httpx.HTTPError, InputValidationError, OSError, RuntimeError, ValueError) as exc:
                last_error = exc

        if file_id:
            try:
                napcat_data = await self._get_image_info(file_id)
                fallback_candidates = _inline_or_http_candidates(napcat_data)
            except RuntimeError as exc:
                last_error = exc
            else:
                for candidate in fallback_candidates:
                    if candidate in candidates:
                        continue
                    try:
                        return await self._store_image_candidate(seg_type, data, candidate)
                    except (
                        httpx.HTTPError,
                        InputValidationError,
                        OSError,
                        RuntimeError,
                        ValueError,
                    ) as exc:
                        last_error = exc
        if last_error is None:
            raise RuntimeError("NapCat 未返回可读取的图片资源")
        raise RuntimeError("所有 NapCat 图片资源均读取失败") from last_error

    async def _store_image_candidate(
        self,
        seg_type: str,
        data: dict[str, Any],
        candidate: str,
    ) -> MessageComponent:
        """下载单个候选资源并通过宿主能力生成受控图片组件。"""

        content = await self._read_image_candidate(candidate)
        mime_type = _detect_image_mime(content)
        await asyncio.to_thread(inspect_dimensions, content)
        filename = _safe_image_filename(data, candidate, mime_type)
        store_image = self.context.store_image
        if store_image is None:
            raise RuntimeError("宿主未授予图片存储能力")
        component = await asyncio.to_thread(
            store_image,
            filename,
            mime_type,
            content,
        )
        is_expression = (
            seg_type == "mface"
            or str(data.get("file") or "").strip().casefold() == "marketface"
            or any(
                str(data.get(key) or "").strip() not in {"", "0"}
                for key in ("emoji_id", "emoji_package_id", "key")
            )
        )
        summary = str(data.get("summary") or "").strip()
        description = "QQ表情包" if is_expression else "QQ图片"
        if summary:
            description = f"{description}：{summary[:100]}"
        return component.model_copy(
            update={
                "description": description,
                "is_expression": is_expression,
            }
        )

    async def _get_image_info(self, file_id: str) -> dict[str, Any]:
        """调用 OneBot ``get_image``，兼容事件只给临时 file 标识的实现。"""

        try:
            response = await self._client.post(
                f"{self.settings.http_base_url.rstrip('/')}/get_image",
                headers=self._auth_headers(),
                json={"file": file_id},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("OneBot get_image 请求失败") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("OneBot get_image 响应不是对象")
        try:
            retcode = int(payload.get("retcode", 0) or 0)
        except (TypeError, ValueError):
            retcode = -1
        data = payload.get("data")
        if retcode != 0 or not isinstance(data, dict):
            raise RuntimeError("OneBot get_image 未返回图片数据")
        return data

    async def _read_image_candidate(self, candidate: str) -> bytes:
        """读取受限的内联数据或经主机规则校验的 HTTP(S) 图片。"""

        if candidate.startswith("base64://"):
            return _decode_inline_image(candidate[len("base64://") :], self.settings.max_image_bytes)
        if candidate.startswith("data:"):
            marker = ";base64,"
            if marker not in candidate:
                raise ValueError("图片 data URL 不是 base64 编码")
            return _decode_inline_image(
                candidate.split(marker, maxsplit=1)[1],
                self.settings.max_image_bytes,
            )
        return await self._download_image(candidate)

    async def _download_image(self, initial_url: str) -> bytes:
        """逐跳校验图片地址并限制响应体，防止 QQ 消息触发内网探测。"""

        current = initial_url
        for redirect_count in range(self.settings.max_image_redirects + 1):
            self._validate_image_url(current)
            headers = self._auth_headers() if self._is_napcat_origin(current) else {}
            headers.pop("Content-Type", None)
            async with self._client.stream("GET", current, headers=headers) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location or redirect_count >= self.settings.max_image_redirects:
                        raise RuntimeError("图片下载重定向无效或超过上限")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                declared_length = response.headers.get("content-length")
                if declared_length:
                    try:
                        if int(declared_length) > self.settings.max_image_bytes:
                            raise InputValidationError("OneBot 入站图片超过大小上限")
                    except ValueError as exc:
                        raise RuntimeError("图片响应 Content-Length 无效") from exc
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.settings.max_image_bytes:
                        raise InputValidationError("OneBot 入站图片超过大小上限")
                    chunks.append(chunk)
                if not chunks:
                    raise InputValidationError("OneBot 入站图片为空")
                return b"".join(chunks)
        raise RuntimeError("图片下载重定向超过上限")

    def _validate_image_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise InputValidationError("OneBot 图片 URL 无效")
        if self._is_napcat_origin(url):
            return
        host = parsed.hostname.casefold().rstrip(".")
        if not any(
            host == rule.lstrip(".") or (rule.startswith(".") and host.endswith(rule))
            for rule in self.settings.image_allowed_host_suffixes
        ):
            raise InputValidationError("OneBot 图片 URL 主机不在允许范围")

    def _is_napcat_origin(self, url: str) -> bool:
        candidate = urlsplit(url)
        configured = urlsplit(self.settings.http_base_url)
        return (
            candidate.scheme == configured.scheme
            and candidate.hostname == configured.hostname
            and _effective_port(candidate) == _effective_port(configured)
        )

    @staticmethod
    def _report_exit(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "OneBot Channel 已离线",
                exc_info=(type(error), error, error.__traceback__),
                extra={"session_id": "-", "turn_id": "-"},
            )


@dataclass(frozen=True, slots=True)
class _GroupPolicy:
    allow: bool
    require_at: bool
    allow_from: set[str]


def normalize_typing_event(
    account_id: str,
    event: dict[str, Any],
    private_allow: set[str],
    groups: dict[str, OneBotGroupConfig],
    bot_uin: str,
) -> TypingEvent | None:
    """规范化 NapCat 的 ``notice.notify.input_status`` 扩展事件。"""

    if (
        event.get("post_type") != "notice"
        or event.get("notice_type") != "notify"
        or event.get("sub_type") != "input_status"
    ):
        return None
    user_id = str(event.get("user_id") or "")
    if not user_id or user_id == bot_uin:
        return None
    group_id = str(event.get("group_id") or "")
    if group_id and group_id != "0":
        config = groups.get(group_id)
        if config is None:
            return None
        if config.allow_from and user_id not in config.allow_from:
            return None
        chat_type = ChatType.GROUP
        external_chat_id = group_id
    else:
        if private_allow and user_id not in private_allow:
            return None
        chat_type = ChatType.PRIVATE
        external_chat_id = user_id
    raw_event_type = event.get("event_type")
    try:
        event_type = int(raw_event_type) if raw_event_type is not None else None
    except (TypeError, ValueError):
        event_type = None
    return TypingEvent(
        platform="onebot",
        account_id=account_id,
        external_chat_id=external_chat_id,
        chat_type=chat_type,
        sender_id=user_id,
        status_text=str(event.get("status_text") or "")[:200],
        event_type=event_type,
    )


def normalize_event(
    account_id: str,
    event: dict[str, Any],
    private_allow: set[str],
    groups: dict[str, OneBotGroupConfig],
    bot_uin: str,
    *,
    resolved_images: dict[int, MessageComponent] | None = None,
    resolved_reply: MessageReference | None = None,
    defer_reply_resolution: bool = False,
) -> InboundEnvelope | None:
    """把 OneBot message 事件规范化为核心入站信封。"""

    if event.get("post_type") != "message":
        return None
    message_type = event.get("message_type")
    user_id = str(event.get("user_id") or "")
    if not user_id:
        return None
    # 过滤机器人自身发出的消息，避免回环。
    if user_id == bot_uin:
        return None
    sender_any = event.get("sender")
    sender: dict[str, Any] = sender_any if isinstance(sender_any, dict) else {}

    if message_type == "private":
        chat_type = ChatType.PRIVATE
        external_chat_id = user_id
        if private_allow and user_id not in private_allow:
            logger.info(
                "OneBot 拒绝未授权私聊 user_id=%s",
                user_id,
                extra={"session_id": "-", "turn_id": "-"},
            )
            return None
        role = ParticipantRole.OWNER
        group_policy: _GroupPolicy | None = None
    elif message_type == "group":
        group_id = str(event.get("group_id") or "")
        if not group_id:
            return None
        chat_type = ChatType.GROUP
        external_chat_id = group_id
        config = groups.get(group_id)
        if config is None:
            return None
        role = _ROLE_MAP.get(str(sender.get("role", "")).lower(), ParticipantRole.MEMBER)
        group_policy = _GroupPolicy(
            allow=True,
            require_at=config.require_at,
            allow_from=set(config.allow_from),
        )
        if group_policy.allow_from and user_id not in group_policy.allow_from:
            logger.info(
                "OneBot 拒绝非白名单群成员 group_id=%s user_id=%s",
                group_id,
                user_id,
                extra={"session_id": "-", "turn_id": "-"},
            )
            return None
    else:
        return None

    raw_segments = _coerce_segments(event.get("message"))
    if not raw_segments:
        return None

    message_id = str(event.get("message_id") or "")
    if not message_id:
        return None
    sender_name = str(sender.get("card") or sender.get("nickname") or f"用户{user_id[-6:]}")[:100]

    components, reply_id, had_at_bot = _segments_to_components(
        raw_segments,
        bot_uin,
        max_segments=30,
        resolved_images=resolved_images,
    )
    if resolved_reply is not None:
        components.insert(
            0,
            MessageComponent(
                type=ComponentType.QUOTE,
                message_id=resolved_reply.message_id,
                target_id=resolved_reply.sender_id,
                target_name=resolved_reply.sender_name,
            ),
        )
    if group_policy is not None and group_policy.require_at and not had_at_bot:
        if defer_reply_resolution and reply_id:
            pass
        elif resolved_reply is None or resolved_reply.sender_id != "agent":
            return None
    if not components:
        return None

    encoded_id = encode_reply_id(message_id, reply_id)
    timestamp = _parse_timestamp(event.get("time"))
    display_name = sender_name if chat_type == ChatType.PRIVATE else f"QQ群 {external_chat_id[-8:]}"
    return InboundEnvelope(
        message=InboundMessage(
            platform="onebot",
            account_id=account_id,
            external_message_id=encoded_id,
            external_chat_id=external_chat_id,
            sender_id=user_id,
            sender_name=sender_name,
            chat_type=chat_type,
            components=components[:30],
            received_at=timestamp,
        ),
        session_display_name=display_name,
        participant_role=role,
    )


def _coerce_segments(message: Any) -> list[dict[str, Any]]:
    """OneBot message 字段可能是消息段数组或 CQ 码字符串，统一成段列表。"""

    if isinstance(message, list):
        return [item for item in message if isinstance(item, dict) and "type" in item]
    if isinstance(message, str) and message:
        return _parse_cq_string(message)
    return []


def _parse_cq_string(raw: str) -> list[dict[str, Any]]:
    """把 CQ 码字符串解析为消息段数组；纯文本保留为 text 段。"""

    segments: list[dict[str, Any]] = []
    last_end = 0
    for match in _CQ_RE.finditer(raw):
        if match.start() > last_end:
            text = raw[last_end:match.start()]
            if text:
                segments.append({"type": "text", "data": {"text": _decode_cq_entities(text)}})
        seg_type = match.group(1).lower()
        data: dict[str, str] = {}
        raw_params = match.group(2)
        if raw_params:
            for pair in raw_params.split(","):
                if not pair or "=" not in pair:
                    continue
                key, value = pair.split("=", maxsplit=1)
                data[key] = _decode_cq_entities(value)
        segments.append({"type": seg_type, "data": data})
        last_end = match.end()
    if last_end < len(raw):
        tail = raw[last_end:]
        if tail:
            segments.append({"type": "text", "data": {"text": _decode_cq_entities(tail)}})
    return segments


_CQ_ENTITY_MAP = {"&amp;": "&", "&#44;": ",", "&#91;": "[", "&#93;": "]", "&amp;#44;": ","}


def _decode_cq_entities(value: str) -> str:
    for entity, char in _CQ_ENTITY_MAP.items():
        value = value.replace(entity, char)
    return value


def _segments_to_components(
    segments: list[dict[str, Any]],
    bot_uin: str,
    *,
    max_segments: int,
    resolved_images: dict[int, MessageComponent] | None = None,
) -> tuple[list[MessageComponent], str, bool]:
    """把 OneBot 消息段转换为核心组件，并提取回复 id 与是否 @ 了机器人。"""

    components: list[MessageComponent] = []
    reply_id = ""
    had_at_bot = False
    for segment_index, segment in enumerate(segments):
        if len(components) >= max_segments:
            break
        seg_type = str(segment.get("type", "")).lower()
        data_any = segment.get("data")
        data: dict[str, Any] = data_any if isinstance(data_any, dict) else {}
        if seg_type == "text":
            text = str(data.get("text", "")).strip()
            if text:
                components.append(MessageComponent.text_component(text))
        elif seg_type == "at":
            qq = str(data.get("qq", "")).strip()
            if not qq:
                continue
            if qq == bot_uin or qq == "all":
                had_at_bot = True
            target_name = str(data.get("name") or data.get("qq") or "")
            components.append(
                MessageComponent(
                    type=ComponentType.MENTION,
                    # 核心策略只认识规范主体 ``agent``；平台机器人 UIN
                    # 不能泄漏为另一个“群成员”，否则直接 @ 会失去硬触发语义。
                    target_id="agent" if qq == bot_uin else qq,
                    target_name=target_name[:100] or None,
                )
            )
        elif seg_type == "reply":
            # 被引用消息可能尚未入库，无法构造可通过校验的 QUOTE 组件；
            # 仅记录外部 id 供出站回复使用，避免触发核心不存在的引用校验失败。
            rid = str(data.get("id", "")).strip()
            if rid and not reply_id:
                reply_id = rid
        elif seg_type == "image":
            image = (resolved_images or {}).get(segment_index)
            components.append(
                image or MessageComponent.text_component(_image_placeholder(data))
            )
        elif seg_type == "mface":
            image = (resolved_images or {}).get(segment_index)
            components.append(
                image or MessageComponent.text_component(_face_placeholder(seg_type, data))
            )
        elif seg_type in {"face", "bface"}:
            components.append(MessageComponent.text_component(_face_placeholder(seg_type, data)))
        elif seg_type == "record":
            components.append(MessageComponent.text_component("[语音消息]"))
        elif seg_type == "file":
            components.append(MessageComponent.text_component(_file_placeholder(data)))
        elif seg_type in {"forward", "xml", "json"}:
            components.append(MessageComponent.text_component(f"[{seg_type}消息]"))
        # 其余段（poke、gift 等）忽略，避免噪声。
    return components, reply_id, had_at_bot


def _image_placeholder(data: dict[str, Any]) -> str:
    del data
    return "[图片读取失败]"


def _inline_or_http_candidates(data: dict[str, Any]) -> list[str]:
    """从 OneBot 图片数据中提取可跨进程读取的资源引用。"""

    candidates: list[str] = []
    for key in ("url", "file"):
        value = str(data.get(key) or "").strip()
        if value.startswith(("http://", "https://", "base64://", "data:")):
            candidates.append(value)
    return candidates


def _decode_inline_image(encoded: str, max_bytes: int) -> bytes:
    """严格解码 OneBot 内联图片，并在分配大块内存前检查估算大小。"""

    compact = "".join(encoded.split())
    if not compact or len(compact) > ((max_bytes + 2) // 3) * 4 + 8:
        raise InputValidationError("OneBot 内联图片为空或超过大小上限")
    try:
        content = base64.b64decode(compact, validate=True)
    except binascii.Error as exc:
        raise InputValidationError("OneBot 内联图片不是有效 base64") from exc
    if not content or len(content) > max_bytes:
        raise InputValidationError("OneBot 内联图片为空或超过大小上限")
    return content


def _detect_image_mime(content: bytes) -> str:
    """仅依据魔数识别核心允许的图片格式，不信任远端 Content-Type。"""

    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    raise InputValidationError("OneBot 下载内容不是受支持的图片")


def _safe_image_filename(
    data: dict[str, Any],
    candidate: str,
    mime_type: str,
) -> str:
    """从不可信段数据构造只用于显示的安全文件名。"""

    raw_name = str(data.get("name") or data.get("file") or "").strip()
    if raw_name.startswith(("http://", "https://", "base64://", "data:")):
        raw_name = ""
    if not raw_name and candidate.startswith(("http://", "https://")):
        raw_name = unquote(Path(urlsplit(candidate).path).name)
    cleaned = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", Path(raw_name).name)[:180]
    suffix = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }[mime_type]
    if not cleaned:
        return f"qq-image{suffix}"
    if Path(cleaned).suffix.casefold() not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        cleaned += suffix
    return cleaned


def _effective_port(parsed: Any) -> int | None:
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None


def _face_placeholder(seg_type: str, data: dict[str, Any]) -> str:
    face_id = str(data.get("id") or "").strip()
    summary = str(data.get("summary") or "").strip()
    if summary:
        return f"[表情:{summary}]"
    if face_id:
        return f"[表情:{face_id}]"
    return f"[{seg_type}表情]"


def _file_placeholder(data: dict[str, Any]) -> str:
    name = str(data.get("file") or data.get("name") or "").strip()
    size = data.get("size")
    if name and size:
        return f"[文件:{name}({size})]"
    if name:
        return f"[文件:{name}]"
    return "[文件]"


def _render_segments(
    components: list[MessageComponent],
) -> tuple[list[dict[str, Any]], list[str]]:
    """把核心出站组件渲染成 OneBot 消息段数组。"""

    segments: list[dict[str, Any]] = []
    unsupported: list[str] = []
    for component in components:
        if component.type == ComponentType.TEXT:
            text = (component.text or "").strip()
            if text:
                segments.append({"type": "text", "data": {"text": component.text or ""}})
        elif component.type == ComponentType.MENTION:
            target_id = (component.target_id or "").strip()
            if target_id:
                segments.append({"type": "at", "data": {"qq": target_id}})
        elif component.type == ComponentType.QUOTE:
            # 引用通过 OutboundMessage.reply_to_message_id 统一处理，组件本身不渲染。
            continue
        elif component.type == ComponentType.IMAGE_REF:
            storage_path = (component.storage_path or "").strip()
            if not storage_path:
                unsupported.append(component.type.value)
                continue
            segments.append(
                {
                    "type": "image",
                    "data": {
                        "file": f"storage:{storage_path}",
                        "_size": component.size,
                        "_sha256": component.sha256,
                    },
                }
            )
        elif component.type == ComponentType.AUDIO_REF:
            storage_path = (component.storage_path or "").strip()
            if not storage_path:
                unsupported.append(component.type.value)
                continue
            segments.append(
                {
                    "type": "record",
                    "data": {
                        "file": f"storage:{storage_path}",
                        "_size": component.size,
                        "_sha256": component.sha256,
                    },
                }
            )
        else:
            unsupported.append(component.type.value)
    return segments, unsupported


def encode_reply_id(message_id: str, reply_id: str | None) -> str:
    """同时保存入站 message_id（出站回复用）与被引用 reply_id（幂等去重用）。"""

    encoded_msg = _b64(message_id)
    encoded_reply = _b64(reply_id) if reply_id else ""
    return f"onebot:{encoded_msg}:{encoded_reply}"


def decode_reply_id(encoded: str | None) -> str | None:
    """出站时取回入站 message_id，作为 OneBot reply 段的引用目标。"""

    if not encoded or not encoded.startswith("onebot:"):
        return None
    parts = encoded.split(":", maxsplit=2)
    if len(parts) < 2:
        return None
    return _unb64(parts[1])


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _unb64(value: str) -> str | None:
    try:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding).decode()
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def _parse_uin(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_media_base64(
    storage_path: str, expected_size: int | None, expected_sha256: str | None
) -> str:
    """复核受控媒体摘要并编码为 OneBot base64 URI；需在线程中调用。"""

    target = Path(storage_path).resolve()
    if not target.is_file():
        raise ValueError(f"出站媒体文件不存在: {storage_path}")
    content = target.read_bytes()
    if expected_size is None or len(content) != expected_size:
        raise ValueError("出站媒体大小与组件元数据不一致")
    if expected_sha256 is None or hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("出站媒体摘要与组件元数据不一致")
    return base64.b64encode(content).decode("ascii")


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OSError, ValueError, OverflowError):
            return utc_now()
    return utc_now()


def create_plugin(context: PluginContext) -> OneBotPlugin:
    """插件清单入口。"""

    try:
        settings = OneBotSettings.from_context(context)
    except ValidationError as exc:
        raise PluginUnavailableError("OneBot 插件配置不完整或无效") from exc
    return OneBotPlugin(settings, context)
