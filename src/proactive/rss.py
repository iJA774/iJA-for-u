"""公开 RSS/Atom 的安全抓取、解析与稳定候选键。"""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from defusedxml import ElementTree

from domain.errors import InputValidationError
from domain.models import FeedSource

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_Resolver = Callable[[str, int], Awaitable[list[tuple]]]


@dataclass(frozen=True, slots=True)
class FeedItem:
    """解析后可安全持久化的最小 Feed 条目。"""

    source_key: str
    title: str
    summary: str
    url: str
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class FeedFetchResult:
    """一次条件抓取结果。"""

    title: str
    items: list[FeedItem]
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


class CandidateSource(Protocol):
    """可插拔主动候选来源的最小协议。"""

    async def fetch(self, source: FeedSource) -> FeedFetchResult: ...

    async def close(self) -> None: ...


async def _default_resolver(host: str, port: int) -> list[tuple]:
    return await asyncio.to_thread(
        socket.getaddrinfo,
        host,
        port,
        type=socket.SOCK_STREAM,
    )


def normalize_public_url(raw: str) -> str:
    """规范化公开 HTTP(S) URL，不接受内嵌凭据或片段。"""

    text = raw.strip()
    parts = urlsplit(text)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise InputValidationError("RSS/Atom 地址必须是公开 HTTP(S) URL")
    if parts.username or parts.password:
        raise InputValidationError("RSS/Atom 地址不得包含用户名或密码")
    port = parts.port
    host = parts.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if port is not None and not (
        (parts.scheme.lower() == "http" and port == 80)
        or (parts.scheme.lower() == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    return urlunsplit(
        (
            parts.scheme.lower(),
            netloc,
            parts.path or "/",
            parts.query,
            "",
        )
    )


async def require_public_destination(
    raw: str, resolver: _Resolver = _default_resolver
) -> str:
    """解析目标全部地址，任一非公网地址都拒绝。"""

    normalized = normalize_public_url(raw)
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            rows = await resolver(host, port)
        except OSError as exc:
            raise InputValidationError("RSS/Atom 域名无法解析") from exc
        addresses = []
        for row in rows:
            address = row[4][0]
            try:
                addresses.append(ipaddress.ip_address(address))
            except ValueError:
                continue
    if not addresses:
        raise InputValidationError("RSS/Atom 域名没有可用地址")
    if any(not address.is_global for address in addresses):
        raise InputValidationError("RSS/Atom 地址不得指向内网、回环或保留网络")
    return normalized


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _children(element, name: str):
    return [child for child in list(element) if _local_name(child.tag) == name]


def _first_text(element, *names: str) -> str:
    for name in names:
        for child in _children(element, name):
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def _clean_text(raw: str, limit: int) -> str:
    value = html.unescape(_TAG_RE.sub(" ", raw))
    return _SPACE_RE.sub(" ", value).strip()[:limit]


def _entry_link(element: object, base_url: str) -> str:
    for child in list(element):  # type: ignore[arg-type]
        if _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        rel = str(child.attrib.get("rel") or "alternate").strip()
        if href and rel in {"alternate", ""}:
            return normalize_public_url(urljoin(base_url, href))
        text = (child.text or "").strip()
        if text:
            return normalize_public_url(urljoin(base_url, text))
    return ""


def _parse_datetime(raw: str) -> datetime | None:
    text = raw.strip()
    if not text:
        return None
    try:
        value = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_feed(content: bytes, base_url: str) -> tuple[str, list[FeedItem]]:
    """解析 RSS 2.x 或 Atom，拒绝损坏 XML 并裁剪不可信正文。"""

    try:
        root = ElementTree.fromstring(content)
    except Exception as exc:
        raise InputValidationError("RSS/Atom XML 无法安全解析") from exc
    root_name = _local_name(root.tag)
    if root_name == "rss":
        channels = _children(root, "channel")
        if not channels:
            raise InputValidationError("RSS 缺少 channel")
        container = channels[0]
        entries = _children(container, "item")
    elif root_name == "feed":
        container = root
        entries = _children(container, "entry")
    else:
        raise InputValidationError("只支持 RSS 或 Atom")
    feed_title = _clean_text(_first_text(container, "title"), 200)
    parsed: list[FeedItem] = []
    for entry in entries[:200]:
        title = _clean_text(_first_text(entry, "title"), 500)
        summary = _clean_text(
            _first_text(entry, "summary", "description", "content"), 4000
        )
        if not title:
            continue
        try:
            link = _entry_link(entry, base_url)
        except (InputValidationError, ValueError):
            link = ""
        guid = _clean_text(_first_text(entry, "guid", "id"), 500)
        published = _parse_datetime(
            _first_text(entry, "published", "pubdate", "updated")
        )
        key_material = guid or link or f"{title}\x1f{summary}"
        source_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        parsed.append(
            FeedItem(
                source_key=source_key,
                title=title,
                summary=summary,
                url=link,
                published_at=published,
            )
        )
    return feed_title, parsed


class RssFeedClient:
    """手动处理重定向和响应大小的公开 Feed 客户端。"""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        max_redirects: int,
        client: httpx.AsyncClient | None = None,
        resolver: _Resolver = _default_resolver,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds, follow_redirects=False
        )
        self._owns_client = client is None
        self._resolver = resolver

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, source: FeedSource) -> FeedFetchResult:
        url = source.url
        headers = {"Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml"}
        if source.etag:
            headers["If-None-Match"] = source.etag
        if source.last_modified:
            headers["If-Modified-Since"] = source.last_modified
        for redirect_count in range(self.max_redirects + 1):
            url = await require_public_destination(url, self._resolver)
            async with self._client.stream("GET", url, headers=headers) as response:
                if response.status_code == 304:
                    return FeedFetchResult(
                        title=source.title,
                        items=[],
                        etag=source.etag,
                        last_modified=source.last_modified,
                        not_modified=True,
                    )
                if response.is_redirect:
                    if redirect_count >= self.max_redirects:
                        raise InputValidationError("RSS/Atom 重定向次数超过上限")
                    location = response.headers.get("location", "")
                    if not location:
                        raise InputValidationError("RSS/Atom 重定向缺少 Location")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise InputValidationError("RSS/Atom 响应超过 1 MiB 上限")
                    chunks.append(chunk)
                title, items = parse_feed(b"".join(chunks), url)
                return FeedFetchResult(
                    title=title or source.title,
                    items=items,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                )
        raise RuntimeError("RSS/Atom 重定向循环未正常结束")
