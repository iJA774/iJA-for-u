"""QQ 开放平台机器人 Channel 插件。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform as system_platform
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator
from websockets.exceptions import WebSocketException

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
from plugins._host import (
    HttpCallbackRequest,
    HttpCallbackResponse,
    InboundEnvelope,
    PluginContext,
    PluginUnavailableError,
)

logger = logging.getLogger(__name__)

_GROUP_AND_C2C_EVENT = 1 << 25
_SUPPORTED_EVENTS = {
    "C2C_MESSAGE_CREATE",
    "GROUP_AT_MESSAGE_CREATE",
    "GROUP_MESSAGE_CREATE",
}


class QQSettings(BaseModel):
    """QQ 插件配置；凭据必须来自本机配置或环境变量。"""

    app_id: str = Field(min_length=1, max_length=200)
    app_secret: str = Field(min_length=1, max_length=500)
    api_base_url: str = "https://api.bot.qq.com"
    access_token_url: str = "https://api.bot.qq.com/app/getAppAccessToken"
    intents: int = Field(default=_GROUP_AND_C2C_EVENT, gt=0)
    request_timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    gateway_open_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    max_reconnect_attempts: int = Field(default=8, ge=0, le=20)

    @model_validator(mode="after")
    def validate_official_endpoints(self) -> QQSettings:
        allowed_api_hosts = {
            "api.bot.qq.com",
            "sandbox.api.sgroup.qq.com",
        }
        for field_name, value in (
            ("api_base_url", self.api_base_url),
            ("access_token_url", self.access_token_url),
        ):
            parsed = httpx.URL(value)
            if (
                parsed.scheme != "https"
                or parsed.host not in allowed_api_hosts
                or bool(parsed.userinfo)
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f"{field_name} 必须是腾讯 QQ 机器人官方 HTTPS 地址")
        return self

    @classmethod
    def from_context(cls, context: PluginContext) -> QQSettings:
        values = dict(context.options)
        env_values = {
            "app_id": os.getenv("IJA_QQ_APP_ID", ""),
            "app_secret": os.getenv("IJA_QQ_APP_SECRET", ""),
        }
        for key, value in env_values.items():
            if value:
                values[key] = value
        return cls.model_validate(values)


class QQAccessToken:
    """串行刷新并仅在内存中缓存 QQ Access Token。"""

    def __init__(self, settings: QQSettings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            response = await self.client.post(
                self.settings.access_token_url,
                json={
                    "appId": self.settings.app_id,
                    "clientSecret": self.settings.app_secret,
                },
            )
            response.raise_for_status()
            payload = response.json()
            token = payload.get("access_token")
            try:
                expires_in = int(payload.get("expires_in", 0))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("QQ Access Token 有效期无效") from exc
            if not isinstance(token, str) or not token or expires_in <= 60:
                raise RuntimeError("QQ Access Token 响应缺少有效凭据")
            self._token = token
            self._expires_at = time.monotonic() + expires_in - 60
            return token

    def invalidate(self) -> None:
        self._token = ""
        self._expires_at = 0.0


class QQPlugin:
    """QQ WebSocket 入站与 OpenAPI 出站适配器。"""

    plugin_id = "qq"
    platform = "qq"

    def __init__(
        self,
        settings: QQSettings,
        context: PluginContext,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.context = context
        self.account_id = settings.app_id
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
        )
        self._tokens = QQAccessToken(settings, self._client)
        self._gateway_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._gateway_url = ""
        self._gateway_session_id: str | None = None
        self._gateway_seq: int | None = None

    async def prepare(self) -> None:
        """纯校验阶段；配置已由 Pydantic 完成，此处不得访问 QQ 网络。"""

    async def activate(self) -> None:
        """获取 Gateway 并启动连接任务；正式事件仍受宿主 generation 准入控制。"""

        if self._gateway_task is not None and not self._gateway_task.done():
            return
        if self._client.is_closed:
            if not self._owns_client:
                raise RuntimeError("QQ 插件注入的 HTTP Client 已关闭，不能重新激活")
            self._client = httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                follow_redirects=False,
            )
            self._tokens = QQAccessToken(self.settings, self._client)
        token = await self._tokens.get()
        response = await self._client.get(
            f"{self.settings.api_base_url.rstrip('/')}/gateway",
            headers={"Authorization": f"QQBot {token}"},
        )
        response.raise_for_status()
        payload = response.json()
        gateway_url = payload.get("url")
        if not isinstance(gateway_url, str) or not gateway_url.startswith("wss://"):
            raise RuntimeError("QQ Gateway 响应缺少安全 WSS 地址")
        if httpx.URL(gateway_url).host not in {
            "api.bot.qq.com",
            "api.sgroup.qq.com",
            "sandbox.api.sgroup.qq.com",
        }:
            raise RuntimeError("QQ Gateway 返回了非官方域名")
        self._gateway_url = gateway_url
        self._stop_event.clear()
        self._ready_event.clear()
        self._gateway_task = asyncio.create_task(self._run_gateway(), name="qq-channel-gateway")
        self._gateway_task.add_done_callback(self._report_gateway_exit)

    async def ready(self) -> None:
        """等待 QQ Gateway 返回 READY/RESUMED，不能把重连任务误报为已就绪。"""

        task = self._gateway_task
        if task is None:
            raise RuntimeError("QQ Channel 尚未 activate")
        waiter = asyncio.create_task(self._ready_event.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, task},
                timeout=self.settings.gateway_open_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                if task.cancelled():
                    raise RuntimeError("QQ Gateway 在 ready 前被取消")
                error = task.exception()
                if error is not None:
                    raise RuntimeError("QQ Gateway 在 ready 前退出") from error
                raise RuntimeError("QQ Gateway 在 ready 前结束")
            if waiter in done and self._ready_event.is_set():
                return
            raise RuntimeError("等待 QQ Gateway READY 超时")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def deactivate(self) -> None:
        """停止 Gateway 并释放当前激活周期的 HTTP 资源。"""

        self._stop_event.set()
        self._ready_event.clear()
        if self._gateway_task is not None:
            self._gateway_task.cancel()
            await asyncio.gather(self._gateway_task, return_exceptions=True)
            self._gateway_task = None
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
            "QQ 插件使用官方 WebSocket Gateway，不接受 HTTP 回调",
            status_code=405,
        )

    async def send(self, session: SessionView, message: OutboundMessage) -> DeliveryReceipt:
        content, unsupported = _render_text(message.components)
        if unsupported:
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code="qq_component_unsupported",
                error_message=f"QQ 首版尚不支持出站组件: {', '.join(unsupported)}",
            )
        if not content:
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code="qq_empty_message",
                error_message="QQ 文本消息不能为空",
            )
        if session.chat_type == ChatType.PRIVATE:
            path = f"/v2/users/{quote(session.external_chat_id, safe='')}/messages"
        else:
            path = f"/v2/groups/{quote(session.external_chat_id, safe='')}/messages"
        payload: dict[str, Any] = {"content": content, "msg_type": 0}
        if message.reply_to_message_id:
            encoded_reply_id = await self.context.resolve_external_message_id(message.reply_to_message_id)
            reply_id = decode_inbound_message_id(encoded_reply_id)
            if reply_id:
                payload["msg_id"] = reply_id
                payload["msg_seq"] = 1
        token = await self._tokens.get()
        response = await self._client.post(
            f"{self.settings.api_base_url.rstrip('/')}{path}",
            headers={"Authorization": f"QQBot {token}"},
            json=payload,
        )
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError("QQ 发消息响应不是有效 JSON，投递结果未知") from exc
        if response.status_code >= 400 or int(result.get("err_code", 0) or 0) != 0:
            error_code = str(result.get("err_code") or response.status_code)
            if response.status_code == 401:
                self._tokens.invalidate()
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code=f"qq_{error_code}",
                error_message=str(result.get("message") or "QQ OpenAPI 请求失败")[:500],
            )
        external_id = result.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise RuntimeError("QQ 发消息成功响应缺少消息 ID，投递结果未知")
        return DeliveryReceipt(
            outbound_id=message.id,
            status=DeliveryStatus.SENT,
            external_message_id=external_id,
            delivered_at=utc_now(),
        )

    async def _run_gateway(self) -> None:
        from websockets.asyncio.client import connect

        attempts = 0
        while not self._stop_event.is_set():
            try:
                async with connect(
                    self._gateway_url,
                    open_timeout=self.settings.gateway_open_timeout_seconds,
                    max_size=2 * 1024 * 1024,
                    ping_interval=None,
                ) as websocket:
                    await self._gateway_session(websocket)
                attempts = 0
            except asyncio.CancelledError:
                raise
            except (
                OSError,
                TimeoutError,
                httpx.HTTPError,
                json.JSONDecodeError,
                WebSocketException,
                RuntimeError,
            ):
                attempts += 1
                if attempts > self.settings.max_reconnect_attempts:
                    raise RuntimeError("QQ Gateway 连续重连失败，已停止 Channel") from None
                logger.exception(
                    "QQ Gateway 连接中断",
                    extra={
                        "session_id": "-",
                        "turn_id": "-",
                        "attempt": attempts,
                    },
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=min(2**attempts, 30),
                    )
                except TimeoutError:
                    pass

    async def _gateway_session(self, websocket: Any) -> None:
        hello = json.loads(
            await asyncio.wait_for(
                websocket.recv(),
                timeout=self.settings.gateway_open_timeout_seconds,
            )
        )
        if hello.get("op") != 10:
            raise RuntimeError("QQ Gateway 未返回 Hello")
        interval_ms = hello.get("d", {}).get("heartbeat_interval")
        if not isinstance(interval_ms, (int, float)) or interval_ms <= 0:
            raise RuntimeError("QQ Gateway 心跳周期无效")
        token = await self._tokens.get()
        auth_token = f"QQBot {token}"
        if self._gateway_session_id and self._gateway_seq is not None:
            await websocket.send(
                json.dumps(
                    {
                        "op": 6,
                        "d": {
                            "token": auth_token,
                            "session_id": self._gateway_session_id,
                            "seq": self._gateway_seq,
                        },
                    }
                )
            )
        else:
            await websocket.send(
                json.dumps(
                    {
                        "op": 2,
                        "d": {
                            "token": auth_token,
                            "intents": self.settings.intents,
                            "shard": [0, 1],
                            "properties": {
                                "$os": system_platform.system().lower(),
                                "$browser": "ija-for-u",
                                "$device": "ija-for-u",
                            },
                        },
                    }
                )
            )
        heartbeat = asyncio.create_task(self._heartbeat(websocket, interval_ms / 1000))
        try:
            async for raw in websocket:
                payload = json.loads(raw)
                sequence = payload.get("s")
                if isinstance(sequence, int):
                    self._gateway_seq = sequence
                opcode = payload.get("op")
                if opcode == 0:
                    event_type = payload.get("t")
                    if event_type in {"READY", "RESUMED"}:
                        event_data = payload.get("d")
                        session_id = (
                            event_data.get("session_id")
                            if isinstance(event_data, dict)
                            else None
                        )
                        if isinstance(session_id, str) and session_id:
                            self._gateway_session_id = session_id
                        self._ready_event.set()
                    elif event_type in _SUPPORTED_EVENTS:
                        async with self.context.admit_event() as admitted:
                            if not admitted:
                                continue
                            envelope = normalize_event(
                                self.account_id,
                                str(event_type),
                                payload.get("d"),
                            )
                            if envelope is not None:
                                await self.context.ingest(envelope)
                elif opcode == 7:
                    return
                elif opcode == 9:
                    self._gateway_session_id = None
                    self._gateway_seq = None
                    return
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, websocket: Any, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            await websocket.send(json.dumps({"op": 1, "d": self._gateway_seq}))

    @staticmethod
    def _report_gateway_exit(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "QQ Channel 已离线",
                exc_info=(type(error), error, error.__traceback__),
                extra={"session_id": "-", "turn_id": "-"},
            )


def normalize_event(account_id: str, event_type: str, data: Any) -> InboundEnvelope | None:
    """把 QQ 单聊/群聊消息事件规范化为核心协议。"""

    if event_type not in _SUPPORTED_EVENTS or not isinstance(data, dict):
        return None
    author = data.get("author")
    if not isinstance(author, dict) or author.get("bot"):
        return None
    if event_type == "C2C_MESSAGE_CREATE":
        chat_type = ChatType.PRIVATE
        sender_id = author.get("user_openid") or author.get("id")
        external_chat_id = sender_id
        role = ParticipantRole.OWNER
    else:
        chat_type = ChatType.GROUP
        sender_id = author.get("member_openid") or author.get("id")
        external_chat_id = data.get("group_openid")
        role = _participant_role(author.get("member_role"))
    message_id = data.get("id")
    if (
        not isinstance(sender_id, str)
        or not sender_id
        or not isinstance(external_chat_id, str)
        or not external_chat_id
        or not isinstance(message_id, str)
        or not message_id
    ):
        return None
    components: list[MessageComponent] = []
    if event_type == "GROUP_AT_MESSAGE_CREATE":
        # QQ 开放平台只把直接 @ 机器人的群消息投递到该事件。事件载荷中的
        # mentions 仍可能只列出其他成员，因此在协议边界显式补成核心主体。
        components.append(
            MessageComponent(
                type=ComponentType.MENTION,
                target_id="agent",
                target_name="iJA",
            )
        )
    content = data.get("content")
    if isinstance(content, str) and content.strip():
        components.append(MessageComponent.text_component(content.strip()))
    mentions = data.get("mentions")
    if isinstance(mentions, list):
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            target_id = mention.get("member_openid") or mention.get("user_openid") or mention.get("id")
            if isinstance(target_id, str) and target_id:
                components.append(
                    MessageComponent(
                        type=ComponentType.MENTION,
                        target_id=target_id,
                        target_name=mention.get("username") or target_id[-6:],
                    )
                )
    attachments = data.get("attachments")
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            kind = str(attachment.get("content_type") or "附件")
            filename = str(attachment.get("filename") or "")
            label = f"[QQ {kind}{f'：{filename}' if filename else ''}]"
            components.append(MessageComponent.text_component(label))
    if not components:
        return None
    sender_name = str(author.get("username") or f"QQ用户-{sender_id[-6:]}")
    timestamp = _parse_timestamp(data.get("timestamp"))
    message_scene = data.get("message_scene")
    ext = message_scene.get("ext") if isinstance(message_scene, dict) else None
    msg_idx = _find_ext_value(ext, "msg_idx")
    return InboundEnvelope(
        message=InboundMessage(
            platform="qq",
            account_id=account_id,
            external_message_id=encode_inbound_message_id(message_id, msg_idx),
            external_chat_id=external_chat_id,
            sender_id=sender_id,
            sender_name=sender_name[:100],
            chat_type=chat_type,
            components=components[:30],
            received_at=timestamp,
        ),
        session_display_name=(
            sender_name[:100] if chat_type == ChatType.PRIVATE else f"QQ群 {external_chat_id[-8:]}"
        ),
        participant_role=role,
    )


def encode_inbound_message_id(message_id: str, msg_idx: str | None) -> str:
    """同时保存 QQ 回复所需 msg_id 与去重所需 msg_idx。"""

    encoded_id = base64.urlsafe_b64encode(message_id.encode()).decode().rstrip("=")
    encoded_idx = base64.urlsafe_b64encode(msg_idx.encode()).decode().rstrip("=") if msg_idx else ""
    return f"qq-reply:{encoded_id}:{encoded_idx}"


def decode_inbound_message_id(encoded: str | None) -> str | None:
    if not encoded or not encoded.startswith("qq-reply:"):
        return None
    encoded_id = encoded.split(":", maxsplit=2)[1]
    try:
        padding = "=" * (-len(encoded_id) % 4)
        return base64.urlsafe_b64decode(encoded_id + padding).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def _render_text(
    components: list[MessageComponent],
) -> tuple[str, list[str]]:
    parts: list[str] = []
    unsupported: list[str] = []
    for component in components:
        if component.type == ComponentType.TEXT:
            parts.append(component.text or "")
        elif component.type == ComponentType.MENTION:
            parts.append(f"@{component.target_name or component.target_id}")
        elif component.type == ComponentType.QUOTE:
            continue
        else:
            unsupported.append(component.type.value)
    return "\n".join(part.strip() for part in parts if part.strip()), unsupported


def _participant_role(value: Any) -> ParticipantRole:
    mapping = {
        "owner": ParticipantRole.OWNER,
        "admin": ParticipantRole.ADMIN,
        "member": ParticipantRole.MEMBER,
    }
    return mapping.get(str(value), ParticipantRole.MEMBER)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        return utc_now()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return utc_now()


def _find_ext_value(values: Any, key: str) -> str | None:
    if not isinstance(values, list):
        return None
    prefix = f"{key}="
    for value in values:
        if isinstance(value, str) and value.startswith(prefix):
            return value[len(prefix) :]
    return None


def create_plugin(context: PluginContext) -> QQPlugin:
    """插件清单入口。"""

    try:
        settings = QQSettings.from_context(context)
    except ValidationError as exc:
        raise PluginUnavailableError("QQ 插件配置不完整或无效") from exc
    return QQPlugin(settings, context)
