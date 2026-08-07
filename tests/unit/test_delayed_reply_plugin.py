import asyncio
from pathlib import Path

import pytest

from application.service import IngressResult
from domain.models import ChatType, InboundMessage, MessageComponent
from plugins._host import (
    InboundEnvelope,
    IngressPluginContext,
    TypingEvent,
)
from plugins.delayed_reply.runtime import (
    DelayedReplyPlugin,
    DelayedReplySettings,
)


def _context() -> IngressPluginContext:
    return IngressPluginContext(
        plugin_id="delayed_reply",
        plugin_root=Path("plugins") / "delayed_reply",
        options={},
    )


def _envelope(
    text: str,
    external_message_id: str,
    *,
    chat_type: ChatType = ChatType.PRIVATE,
) -> InboundEnvelope:
    external_chat_id = "111" if chat_type == ChatType.PRIVATE else "222"
    return InboundEnvelope(
        message=InboundMessage(
            platform="onebot",
            account_id="999",
            external_message_id=external_message_id,
            external_chat_id=external_chat_id,
            sender_id="111",
            sender_name="小明",
            chat_type=chat_type,
            components=[MessageComponent.text_component(text)],
        ),
        session_display_name="小明",
    )


def _typing() -> TypingEvent:
    return TypingEvent(
        platform="onebot",
        account_id="999",
        external_chat_id="111",
        chat_type=ChatType.PRIVATE,
        sender_id="111",
        event_type=2,
    )


@pytest.mark.asyncio
async def test_private_message_waits_for_five_second_equivalent_window() -> None:
    delivered: list[str] = []

    async def downstream(envelope: InboundEnvelope) -> IngressResult:
        delivered.append(envelope.message.plain_text)
        return IngressResult(accepted=True)

    plugin = DelayedReplyPlugin(
        DelayedReplySettings(message_delay_seconds=0.04, typing_delay_seconds=0.08),
        _context(),
    )
    await plugin.start()
    await plugin.ingest(_envelope("第一句", "m1"), downstream)
    await asyncio.sleep(0.02)
    assert delivered == []
    await asyncio.sleep(0.04)
    assert delivered == ["第一句"]
    await plugin.stop()


@pytest.mark.asyncio
async def test_typing_extends_once_and_repeated_typing_does_not_slide_window() -> None:
    delivered: list[str] = []

    async def downstream(envelope: InboundEnvelope) -> IngressResult:
        delivered.append(envelope.message.plain_text)
        return IngressResult(accepted=True)

    async def typing_downstream(_: TypingEvent) -> None:
        return None

    plugin = DelayedReplyPlugin(
        DelayedReplySettings(message_delay_seconds=0.05, typing_delay_seconds=0.10),
        _context(),
    )
    await plugin.start()
    await plugin.ingest(_envelope("等等", "m1"), downstream)
    await asyncio.sleep(0.02)
    await plugin.typing(_typing(), typing_downstream)
    await asyncio.sleep(0.05)
    await plugin.typing(_typing(), typing_downstream)
    await asyncio.sleep(0.065)

    assert delivered == ["等等"]
    await plugin.stop()


@pytest.mark.asyncio
async def test_message_during_ten_second_window_returns_to_five_and_flushes_all() -> None:
    delivered: list[str] = []
    scheduling: list[tuple[bool, bool, float | None]] = []

    async def downstream(envelope: InboundEnvelope) -> IngressResult:
        delivered.append(envelope.message.plain_text)
        scheduling.append(
            (
                envelope.schedule_turn,
                envelope.schedule_duplicate,
                envelope.debounce_seconds,
            )
        )
        return IngressResult(accepted=True)

    async def typing_downstream(_: TypingEvent) -> None:
        return None

    plugin = DelayedReplyPlugin(
        DelayedReplySettings(message_delay_seconds=0.04, typing_delay_seconds=0.10),
        _context(),
    )
    await plugin.start()
    await plugin.ingest(_envelope("第一句", "m1"), downstream)
    await asyncio.sleep(0.015)
    await plugin.typing(_typing(), typing_downstream)
    await asyncio.sleep(0.025)
    await plugin.ingest(_envelope("第二句", "m2"), downstream)
    await asyncio.sleep(0.025)
    assert delivered == []
    await asyncio.sleep(0.035)

    assert delivered == ["第一句", "第二句"]
    assert scheduling == [(False, False, None), (True, True, 0.0)]
    await plugin.stop()


@pytest.mark.asyncio
async def test_group_message_bypasses_delay_and_stop_flushes_private_batch() -> None:
    delivered: list[str] = []

    async def downstream(envelope: InboundEnvelope) -> IngressResult:
        delivered.append(envelope.message.plain_text)
        return IngressResult(accepted=True)

    plugin = DelayedReplyPlugin(
        DelayedReplySettings(message_delay_seconds=1, typing_delay_seconds=2),
        _context(),
    )
    await plugin.start()
    await plugin.ingest(_envelope("群聊", "g1", chat_type=ChatType.GROUP), downstream)
    assert delivered == ["群聊"]

    await plugin.ingest(_envelope("关机前私聊", "m1"), downstream)
    assert delivered == ["群聊"]
    await plugin.stop()
    assert delivered == ["群聊", "关机前私聊"]


@pytest.mark.asyncio
async def test_stop_waits_for_batch_that_is_already_flushing() -> None:
    delivered: list[str] = []
    first_started = asyncio.Event()
    allow_flush = asyncio.Event()

    async def downstream(envelope: InboundEnvelope) -> IngressResult:
        delivered.append(envelope.message.plain_text)
        if len(delivered) == 1:
            first_started.set()
            await allow_flush.wait()
        return IngressResult(accepted=True)

    plugin = DelayedReplyPlugin(
        DelayedReplySettings(message_delay_seconds=0.01, typing_delay_seconds=0.02),
        _context(),
    )
    await plugin.start()
    await plugin.ingest(_envelope("第一句", "m1"), downstream)
    await plugin.ingest(_envelope("第二句", "m2"), downstream)
    await first_started.wait()

    stopping = asyncio.create_task(plugin.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    allow_flush.set()
    await stopping

    assert delivered == ["第一句", "第二句"]
