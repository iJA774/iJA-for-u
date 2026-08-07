"""微信服务号消息回调与客服消息 Channel 插件。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import struct
import time
from datetime import UTC, datetime
from typing import Literal

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from defusedxml import ElementTree
from pydantic import BaseModel, Field, ValidationError, model_validator

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


class WeChatSettings(BaseModel):
    """微信服务号配置。"""

    app_id: str = Field(min_length=1, max_length=200)
    app_secret: str = Field(min_length=1, max_length=500)
    token: str = Field(min_length=1, max_length=200)
    message_mode: Literal["plaintext", "safe"] = "safe"
    encoding_aes_key: str = ""
    api_base_url: str = "https://api.weixin.qq.com"
    request_timeout_seconds: float = Field(default=20.0, gt=0, le=60)

    @model_validator(mode="after")
    def validate_security(self) -> WeChatSettings:
        parsed = httpx.URL(self.api_base_url)
        if (
            parsed.scheme != "https"
            or parsed.host != "api.weixin.qq.com"
            or bool(parsed.userinfo)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("api_base_url 必须是微信官方 HTTPS 地址")
        if self.message_mode == "safe":
            if len(self.encoding_aes_key) != 43:
                raise ValueError("安全模式要求 43 字符 EncodingAESKey")
            try:
                decoded = base64.b64decode(self.encoding_aes_key + "=")
            except ValueError as exc:
                raise ValueError("EncodingAESKey 不是有效 Base64") from exc
            if len(decoded) != 32:
                raise ValueError("EncodingAESKey 解码后必须为 32 字节")
        return self

    @classmethod
    def from_context(cls, context: PluginContext) -> WeChatSettings:
        values = dict(context.options)
        env_values = {
            "app_id": os.getenv("IJA_WECHAT_APP_ID", ""),
            "app_secret": os.getenv("IJA_WECHAT_APP_SECRET", ""),
            "token": os.getenv("IJA_WECHAT_TOKEN", ""),
            "encoding_aes_key": os.getenv("IJA_WECHAT_ENCODING_AES_KEY", ""),
        }
        for key, value in env_values.items():
            if value:
                values[key] = value
        return cls.model_validate(values)


class WeChatCryptor:
    """实现微信服务号安全模式规定的 AES-CBC 消息体格式。"""

    def __init__(self, app_id: str, encoding_aes_key: str) -> None:
        self.app_id = app_id
        self.key = base64.b64decode(encoding_aes_key + "=")
        self.iv = self.key[:16]

    def decrypt(self, ciphertext: str) -> bytes:
        try:
            encrypted = base64.b64decode(ciphertext, validate=True)
        except ValueError as exc:
            raise ValueError("微信密文不是有效 Base64") from exc
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        unpadder = padding.PKCS7(256).unpadder()
        try:
            plaintext = unpadder.update(padded) + unpadder.finalize()
        except ValueError as exc:
            raise ValueError("微信密文填充无效") from exc
        if len(plaintext) < 20:
            raise ValueError("微信密文正文过短")
        message_length = struct.unpack("!I", plaintext[16:20])[0]
        end = 20 + message_length
        if end > len(plaintext):
            raise ValueError("微信密文正文长度无效")
        message = plaintext[20:end]
        received_app_id = plaintext[end:].decode("utf-8")
        if not hmac.compare_digest(received_app_id, self.app_id):
            raise ValueError("微信密文 AppID 不匹配")
        return message

    def encrypt(self, message: bytes, *, random_bytes: bytes | None = None) -> str:
        """生成安全模式密文；主要用于协议测试和后续被动回复扩展。"""

        prefix = random_bytes or os.urandom(16)
        if len(prefix) != 16:
            raise ValueError("微信加密随机前缀必须为 16 字节")
        plaintext = prefix + struct.pack("!I", len(message)) + message + self.app_id.encode("utf-8")
        padder = padding.PKCS7(256).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(encrypted).decode("ascii")


class WeChatAccessToken:
    """串行刷新并仅在内存中保存服务号 access_token。"""

    def __init__(self, settings: WeChatSettings, client: httpx.AsyncClient) -> None:
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
            response = await self.client.get(
                f"{self.settings.api_base_url.rstrip('/')}/cgi-bin/token",
                params={
                    "grant_type": "client_credential",
                    "appid": self.settings.app_id,
                    "secret": self.settings.app_secret,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if int(payload.get("errcode", 0) or 0) != 0:
                raise RuntimeError(f"微信 access_token 获取失败: {payload.get('errcode')}")
            token = payload.get("access_token")
            try:
                expires_in = int(payload.get("expires_in", 0))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("微信 access_token 有效期无效") from exc
            if not isinstance(token, str) or not token or expires_in <= 60:
                raise RuntimeError("微信 access_token 响应缺少有效凭据")
            self._token = token
            self._expires_at = time.monotonic() + expires_in - 60
            return token

    def invalidate(self) -> None:
        self._token = ""
        self._expires_at = 0.0


class WeChatPlugin:
    """微信服务号入站验签/解密与客服消息出站。"""

    plugin_id = "wechat"
    platform = "wechat-service-account"

    def __init__(
        self,
        settings: WeChatSettings,
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
        self._tokens = WeChatAccessToken(settings, self._client)
        self._cryptor = (
            WeChatCryptor(settings.app_id, settings.encoding_aes_key)
            if settings.message_mode == "safe"
            else None
        )

    async def start(self) -> None:
        # 不主动获取 access_token；服务号允许先完成回调验证再发送消息。
        return None

    async def stop(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def handle_http(self, request: HttpCallbackRequest) -> HttpCallbackResponse:
        if request.method == "GET":
            return self._verify_endpoint(request)
        if request.method != "POST":
            return HttpCallbackResponse.text("method not allowed", status_code=405)
        try:
            plaintext = self._authenticate_and_decode(request)
            envelope = normalize_message(self.account_id, plaintext)
        except PermissionError:
            return HttpCallbackResponse.text("forbidden", status_code=403)
        except ValueError:
            return HttpCallbackResponse.text("invalid callback", status_code=400)
        if envelope is not None:
            await self.context.ingest(envelope)
        # Agent 生成异步进行；官方要求 5 秒内返回 success 以停止重试。
        return HttpCallbackResponse.text("success")

    async def send(self, session: SessionView, message: OutboundMessage) -> DeliveryReceipt:
        content, unsupported = _render_text(message.components)
        if unsupported:
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code="wechat_component_unsupported",
                error_message=("微信服务号首版尚不支持出站组件: " + ", ".join(unsupported)),
            )
        if not content:
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code="wechat_empty_message",
                error_message="微信客服文本消息不能为空",
            )
        token = await self._tokens.get()
        response = await self._client.post(
            f"{self.settings.api_base_url.rstrip('/')}/cgi-bin/message/custom/send",
            params={"access_token": token},
            json={
                "touser": session.external_chat_id,
                "msgtype": "text",
                "text": {"content": content},
            },
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("微信客服消息响应不是有效 JSON，投递结果未知") from exc
        error_code = int(payload.get("errcode", 0) or 0)
        if response.status_code >= 400 or error_code != 0:
            if error_code in {40014, 42001}:
                self._tokens.invalidate()
            return DeliveryReceipt(
                outbound_id=message.id,
                status=DeliveryStatus.FAILED,
                error_code=f"wechat_{error_code or response.status_code}",
                error_message=str(payload.get("errmsg") or "微信客服消息接口请求失败")[:500],
            )
        return DeliveryReceipt(
            outbound_id=message.id,
            status=DeliveryStatus.SENT,
            # 客服消息接口成功时不返回消息 ID，使用本地幂等 ID 标识该回执。
            external_message_id=f"wechat:{message.id}",
            delivered_at=utc_now(),
        )

    def _verify_endpoint(self, request: HttpCallbackRequest) -> HttpCallbackResponse:
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")
        echo = request.query.get("echostr", "")
        if not timestamp or not nonce or not echo:
            return HttpCallbackResponse.text("missing parameters", status_code=400)
        if self.settings.message_mode == "safe":
            signature = request.query.get("msg_signature", "")
            if not _verify_signature(self.settings.token, signature, timestamp, nonce, echo):
                return HttpCallbackResponse.text("forbidden", status_code=403)
            assert self._cryptor is not None
            try:
                decoded_echo = self._cryptor.decrypt(echo).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return HttpCallbackResponse.text("invalid echostr", status_code=400)
            return HttpCallbackResponse.text(decoded_echo)
        signature = request.query.get("signature", "")
        if not _verify_signature(self.settings.token, signature, timestamp, nonce):
            return HttpCallbackResponse.text("forbidden", status_code=403)
        return HttpCallbackResponse.text(echo)

    def _authenticate_and_decode(self, request: HttpCallbackRequest) -> bytes:
        timestamp = request.query.get("timestamp", "")
        nonce = request.query.get("nonce", "")
        if not timestamp or not nonce:
            raise ValueError("微信回调缺少签名参数")
        if self.settings.message_mode == "safe":
            outer = _parse_xml(request.body)
            encrypted = outer.get("Encrypt", "")
            signature = request.query.get("msg_signature", "")
            if not encrypted or not _verify_signature(
                self.settings.token, signature, timestamp, nonce, encrypted
            ):
                raise PermissionError("微信安全模式签名无效")
            assert self._cryptor is not None
            return self._cryptor.decrypt(encrypted)
        signature = request.query.get("signature", "")
        if not _verify_signature(self.settings.token, signature, timestamp, nonce):
            raise PermissionError("微信明文模式签名无效")
        return request.body


def normalize_message(account_id: str, xml_payload: bytes) -> InboundEnvelope | None:
    """把服务号用户消息转换成单聊核心消息。"""

    fields = _parse_xml(xml_payload)
    sender_id = fields.get("FromUserName", "").strip()
    message_type = fields.get("MsgType", "").strip().lower()
    if not sender_id or not message_type or message_type == "event":
        return None
    components: list[MessageComponent] = []
    if message_type == "text":
        content = fields.get("Content", "").strip()
        if content:
            components.append(MessageComponent.text_component(content))
    elif message_type == "voice":
        recognition = fields.get("Recognition", "").strip()
        components.append(MessageComponent.text_component(recognition if recognition else "[微信语音消息]"))
    elif message_type == "image":
        components.append(MessageComponent.text_component("[微信图片消息]"))
    elif message_type == "location":
        label = fields.get("Label", "").strip()
        components.append(MessageComponent.text_component(f"[微信位置消息]{f' {label}' if label else ''}"))
    elif message_type == "link":
        title = fields.get("Title", "").strip()
        url = fields.get("Url", "").strip()
        components.append(
            MessageComponent.text_component(" ".join(part for part in ("[微信链接消息]", title, url) if part))
        )
    else:
        return None
    if not components:
        return None
    external_message_id = fields.get("MsgId", "").strip()
    if not external_message_id:
        fingerprint = "\x1f".join(
            [
                sender_id,
                fields.get("CreateTime", ""),
                message_type,
                fields.get("Content", ""),
            ]
        )
        external_message_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    received_at = _parse_wechat_time(fields.get("CreateTime"))
    display_name = f"微信用户-{sender_id[-6:]}"
    return InboundEnvelope(
        message=InboundMessage(
            platform="wechat-service-account",
            account_id=account_id,
            external_message_id=f"wechat:{external_message_id}",
            external_chat_id=sender_id,
            sender_id=sender_id,
            sender_name=display_name,
            chat_type=ChatType.PRIVATE,
            components=components,
            received_at=received_at,
        ),
        session_display_name=display_name,
        participant_role=ParticipantRole.OWNER,
    )


def _parse_xml(payload: bytes) -> dict[str, str]:
    if not payload:
        raise ValueError("微信 XML 为空")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ValueError("微信 XML 无效") from exc
    if root.tag != "xml":
        raise ValueError("微信 XML 根节点无效")
    return {child.tag: child.text or "" for child in root if isinstance(child.tag, str)}


def _verify_signature(token: str, signature: str, *parts: str) -> bool:
    if not signature:
        return False
    expected = hashlib.sha1("".join(sorted((token, *parts))).encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, signature)


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


def _parse_wechat_time(value: str | None) -> datetime:
    try:
        return datetime.fromtimestamp(int(value or ""), tz=UTC)
    except (TypeError, ValueError, OSError):
        return utc_now()


def create_plugin(context: PluginContext) -> WeChatPlugin:
    """插件清单入口。"""

    try:
        settings = WeChatSettings.from_context(context)
    except ValidationError as exc:
        raise PluginUnavailableError("微信插件配置不完整或无效") from exc
    return WeChatPlugin(settings, context)
