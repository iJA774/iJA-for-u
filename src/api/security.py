"""本地控制面的 Host、Origin 与 Token 访问边界。"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from hmac import new as new_hmac
from urllib.parse import urlsplit

from config import ServerSettings


@dataclass(frozen=True, slots=True)
class ControlAccessFailure:
    """不携带敏感输入的控制面拒绝结果。"""

    status_code: int
    websocket_code: int
    code: str
    message: str


class ControlPlaneSecurity:
    """对 HTTP 与 WebSocket 使用同一套精确来源和令牌规则。"""

    SESSION_COOKIE_NAME = "ija_control_session"

    def __init__(self, settings: ServerSettings) -> None:
        self._token = settings.control_token.get_secret_value()
        persisted_credential = settings.session_credential.get_secret_value()
        if self._token:
            # 优先复用已持久化的控制会话凭据，保证后端重启后旧 Cookie 仍可校验；
            # 缺失时（如测试直接构造带 token 的配置）退化为进程内随机凭据，
            # 仅保证单进程内 Cookie 一致，重启后失效。
            self._session_credential = persisted_credential or new_hmac(
                self._token.encode("ascii"),
                secrets.token_bytes(32),
                sha256,
            ).hexdigest()
        else:
            self._session_credential = ""
        self._trusted_hosts = frozenset(settings.trusted_hosts)
        self._trusted_origins = frozenset(settings.trusted_origins)

    @property
    def authentication_enabled(self) -> bool:
        """仅暴露认证是否启用，不暴露令牌正文。"""

        return bool(self._token)

    def validate_request_boundary(
        self,
        headers: Mapping[str, str],
        *,
        scheme: str,
    ) -> ControlAccessFailure | None:
        """校验 Host 和可选 Origin；缺 Origin 仅表示非浏览器客户端。"""

        authority = self._parse_authority(headers.get("host"))
        if authority is None or authority[0] not in self._trusted_hosts:
            return ControlAccessFailure(
                status_code=400,
                websocket_code=4403,
                code="untrusted_host",
                message="控制面拒绝了未受信任的 Host",
            )

        raw_origin = headers.get("origin")
        if raw_origin is None:
            return None
        origin = self._normalize_origin(raw_origin)
        request_scheme = "https" if scheme in {"https", "wss"} else "http"
        same_origin = f"{request_scheme}://{authority[1]}"
        if origin is None or (origin != same_origin and origin not in self._trusted_origins):
            return ControlAccessFailure(
                status_code=403,
                websocket_code=4403,
                code="untrusted_origin",
                message="控制面拒绝了未受信任的 Origin",
            )
        return None

    def validate_http_authentication(
        self,
        headers: Mapping[str, str],
        cookies: Mapping[str, str],
    ) -> ControlAccessFailure | None:
        """校验 Bearer 或由 Bearer 换取的 HttpOnly 同站会话 Cookie。"""

        if not self._token:
            return None
        candidate = self._bearer_token(headers.get("authorization"))
        bearer_valid = candidate is not None and secrets.compare_digest(
            candidate,
            self._token,
        )
        cookie = cookies.get(self.SESSION_COOKIE_NAME, "")
        cookie_valid = bool(cookie) and secrets.compare_digest(
            cookie,
            self._session_credential,
        )
        return None if bearer_valid or cookie_valid else self.authentication_failure()

    def bearer_authenticated(self, headers: Mapping[str, str]) -> bool:
        """判断请求是否以原始 Bearer 认证，供无 Origin 的 CLI 调用分流。"""

        if not self._token:
            return False
        candidate = self._bearer_token(headers.get("authorization"))
        return candidate is not None and secrets.compare_digest(candidate, self._token)

    def websocket_authenticated(
        self,
        headers: Mapping[str, str],
        cookies: Mapping[str, str],
    ) -> bool:
        """WebSocket 复用控制会话 Cookie，非浏览器也可直接发 Bearer。"""

        if not self._token:
            return True
        candidate = self._bearer_token(headers.get("authorization"))
        bearer_valid = candidate is not None and secrets.compare_digest(
            candidate,
            self._token,
        )
        cookie = cookies.get(self.SESSION_COOKIE_NAME, "")
        cookie_valid = bool(cookie) and secrets.compare_digest(
            cookie,
            self._session_credential,
        )
        return bearer_valid or cookie_valid

    @property
    def session_credential(self) -> str:
        """返回仅在本进程有效的派生会话凭据。"""

        return self._session_credential

    @staticmethod
    def authentication_failure() -> ControlAccessFailure:
        return ControlAccessFailure(
            status_code=401,
            websocket_code=4401,
            code="control_authentication_required",
            message="控制面认证失败",
        )

    @staticmethod
    def origin_required_failure() -> ControlAccessFailure:
        return ControlAccessFailure(
            status_code=403,
            websocket_code=4403,
            code="origin_required",
            message="使用控制会话 Cookie 的写请求必须提供可信 Origin",
        )

    @staticmethod
    def _bearer_token(raw_authorization: str | None) -> str | None:
        if raw_authorization is None:
            return None
        scheme, separator, candidate = raw_authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not candidate or candidate != candidate.strip():
            return None
        return candidate

    @staticmethod
    def _parse_authority(raw_host: str | None) -> tuple[str, str] | None:
        if raw_host is None or not raw_host or any(character.isspace() for character in raw_host):
            return None
        parsed = urlsplit(f"//{raw_host}")
        if (
            parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.hostname is None
        ):
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        host = parsed.hostname.lower()
        rendered_host = f"[{host}]" if ":" in host else host
        authority = f"{rendered_host}:{port}" if port is not None else rendered_host
        return host, authority

    @classmethod
    def _normalize_origin(cls, raw_origin: str) -> str | None:
        if not raw_origin or raw_origin == "null":
            return None
        parsed = urlsplit(raw_origin)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.hostname is None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        host = parsed.hostname.lower()
        rendered_host = f"[{host}]" if ":" in host else host
        authority = f"{rendered_host}:{port}" if port is not None else rendered_host
        return f"{parsed.scheme.lower()}://{authority}"
