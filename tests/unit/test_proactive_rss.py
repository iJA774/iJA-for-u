import socket

import pytest

from domain.errors import InputValidationError
from proactive.rss import (
    normalize_public_url,
    parse_feed,
    require_public_destination,
)

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example Feed</title>
<item><guid>entry-1</guid><title> First &amp; New </title>
<description><![CDATA[<p>Hello <b>world</b></p>]]></description>
<link>https://example.com/posts/1#fragment</link>
<pubDate>Wed, 23 Jul 2026 10:00:00 GMT</pubDate></item>
</channel></rss>
"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Feed</title>
<entry><id>tag:example.com,2026:2</id><title>Atom item</title>
<summary>Summary</summary><link href="/posts/2"/><updated>2026-07-23T11:00:00Z</updated></entry>
</feed>
"""


def test_parse_rss_and_atom_with_stable_safe_fields() -> None:
    title, rss_items = parse_feed(RSS, "https://example.com/feed.xml")
    atom_title, atom_items = parse_feed(ATOM, "https://example.com/feed.xml")

    assert title == "Example Feed"
    assert rss_items[0].title == "First & New"
    assert rss_items[0].summary == "Hello world"
    assert rss_items[0].url == "https://example.com/posts/1"
    assert atom_title == "Atom Feed"
    assert atom_items[0].url == "https://example.com/posts/2"
    assert rss_items[0].source_key != atom_items[0].source_key


def test_parse_feed_rejects_entity_expansion() -> None:
    malicious = b"""<!DOCTYPE rss [<!ENTITY x "secret">]>
    <rss><channel><title>&x;</title></channel></rss>"""
    with pytest.raises(InputValidationError, match="安全解析"):
        parse_feed(malicious, "https://example.com/feed")


@pytest.mark.asyncio
async def test_public_url_guard_rejects_private_dns_answer() -> None:
    async def resolver(host: str, port: int) -> list[tuple]:
        del host, port
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

    with pytest.raises(InputValidationError, match="内网"):
        await require_public_destination(
            "https://feed.example.com/rss", resolver=resolver
        )


def test_url_normalization_rejects_credentials_and_removes_fragment() -> None:
    assert (
        normalize_public_url("HTTPS://Example.COM:443/feed?q=1#x")
        == "https://example.com/feed?q=1"
    )
    with pytest.raises(InputValidationError, match="用户名"):
        normalize_public_url("https://user:pass@example.com/feed")
