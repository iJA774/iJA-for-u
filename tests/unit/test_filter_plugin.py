"""输出过滤器 egress 插件单元测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.filter.runtime import _TEMPLATE, FilterPlugin, _normalize
from ports import ModelMessage, ModelResult
from ports.egress import EgressEnvelope, EgressPluginContext

_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "filter"


def _context(detection_model=None) -> EgressPluginContext:
    return EgressPluginContext(
        plugin_id="filter",
        plugin_root=_PLUGIN_ROOT,
        options={},
        detection_model_resolver=lambda: detection_model,
    )


def _envelope(
    text: str,
    *,
    model=None,
    prompt_messages: list[ModelMessage] | None = None,
    detection_model_resolver=None,
) -> EgressEnvelope:
    return EgressEnvelope(
        session_id="test-session",
        turn_id="test-turn",
        text=text,
        prompt_messages=prompt_messages or [ModelMessage(role="user", content="你好")],
        model=model or MagicMock(),
        model_name="test-model",
        temperature=0.8,
        max_tokens=500,
    )


async def _passthrough(envelope: EgressEnvelope) -> str:
    return envelope.text


# ─── 文本标准化 ───


def test_normalize_removes_punctuation_and_lowercases() -> None:
    assert _normalize("Hello, 世界！\n") == "hello世界"
    assert _normalize("Fuck!!!") == "fuck"
    assert _normalize("插 B") == "插b"
    assert _normalize("你好，世界。") == "你好世界"


def test_normalize_empty_string() -> None:
    assert _normalize("") == ""


def test_normalize_only_punctuation() -> None:
    assert _normalize("，。！？\n\t ") == ""


# ─── 词汇表加载 ───


@pytest.mark.asyncio
async def test_plugin_loads_vocabulary_on_start() -> None:
    plugin = FilterPlugin(_context())
    await plugin.start()
    assert "中国" in plugin._vocabulary
    assert "fuck" in plugin._vocabulary
    assert len(plugin._vocabulary) > 0
    await plugin.stop()


# ─── 直通透传 ───


@pytest.mark.asyncio
async def test_pass_through_when_no_match() -> None:
    plugin = FilterPlugin(_context())
    await plugin.start()
    envelope = _envelope("今天天气真好，我们去公园玩吧。")
    result = await plugin.filter(envelope, _passthrough)
    assert result == "今天天气真好，我们去公园玩吧。"
    await plugin.stop()


# ─── 原文命中 → 重新生成 ───


@pytest.mark.asyncio
async def test_raw_match_regenerates_successfully() -> None:
    model = MagicMock()
    # 第一次调用：返回干净文本
    model.complete = AsyncMock(
        return_value=ModelResult(content="好的，我们去公园吧。")
    )
    plugin = FilterPlugin(_context())
    await plugin.start()
    envelope = _envelope("我们去中国共产党吧", model=model)
    result = await plugin.filter(envelope, _passthrough)
    assert result == "好的，我们去公园吧。"
    assert model.complete.call_count == 1
    await plugin.stop()


@pytest.mark.asyncio
async def test_raw_match_three_failures_uses_template() -> None:
    model = MagicMock()
    # 每次重新生成都仍然包含敏感词
    model.complete = AsyncMock(
        return_value=ModelResult(content="中国共产党万岁")
    )
    plugin = FilterPlugin(_context())
    await plugin.start()
    envelope = _envelope("中国共产党", model=model)
    result = await plugin.filter(envelope, _passthrough)
    assert result == _TEMPLATE
    assert model.complete.call_count == 3
    await plugin.stop()


@pytest.mark.asyncio
async def test_raw_match_second_attempt_succeeds() -> None:
    model = MagicMock()
    # 第一次仍命中，第二次干净
    responses = [
        ModelResult(content="毛主席说得好"),
        ModelResult(content="他说得很好"),
    ]
    model.complete = AsyncMock(side_effect=responses)
    plugin = FilterPlugin(_context())
    await plugin.start()
    envelope = _envelope("毛泽东", model=model)
    result = await plugin.filter(envelope, _passthrough)
    assert result == "他说得很好"
    assert model.complete.call_count == 2
    await plugin.stop()


# ─── 标准化命中 → 模型确认 ───


@pytest.mark.asyncio
async def test_normalized_match_detection_approved() -> None:
    detection_model = MagicMock()
    detection_model.complete = AsyncMock(
        return_value=ModelResult(content='{"approved": true}')
    )
    plugin = FilterPlugin(_context(detection_model=detection_model))
    await plugin.start()
    # "Fuck!!!" 原文不在词汇表（词汇表是小写 "fuck"），但标准化后命中
    envelope = _envelope("Fuck!!!")
    result = await plugin.filter(envelope, _passthrough)
    assert result == "Fuck!!!"
    await plugin.stop()


@pytest.mark.asyncio
async def test_normalized_match_detection_rejected_uses_template() -> None:
    detection_model = MagicMock()
    detection_model.complete = AsyncMock(
        return_value=ModelResult(content='{"approved": false}')
    )
    plugin = FilterPlugin(_context(detection_model=detection_model))
    await plugin.start()
    envelope = _envelope("Fuck!!!")
    result = await plugin.filter(envelope, _passthrough)
    assert result == _TEMPLATE
    await plugin.stop()


@pytest.mark.asyncio
async def test_normalized_match_no_detection_model_uses_template() -> None:
    plugin = FilterPlugin(_context(detection_model=None))
    await plugin.start()
    envelope = _envelope("Fuck!!!")
    result = await plugin.filter(envelope, _passthrough)
    assert result == _TEMPLATE
    await plugin.stop()


@pytest.mark.asyncio
async def test_normalized_match_detection_model_error_blocks() -> None:
    detection_model = MagicMock()
    detection_model.complete = AsyncMock(side_effect=RuntimeError("网络错误"))
    plugin = FilterPlugin(_context(detection_model=detection_model))
    await plugin.start()
    envelope = _envelope("Fuck!!!")
    result = await plugin.filter(envelope, _passthrough)
    assert result == _TEMPLATE
    await plugin.stop()


# ─── 检测结果解析 ───


def test_parse_detection_result_valid_json_true() -> None:
    assert FilterPlugin._parse_detection_result('{"approved": true}') is True


def test_parse_detection_result_valid_json_false() -> None:
    assert FilterPlugin._parse_detection_result('{"approved": false}') is False


@pytest.mark.parametrize(
    "content",
    [
        '{"approved": "false"}',
        '{"approved": 1}',
        '{"approved": null}',
        "[]",
    ],
)
def test_parse_detection_result_rejects_invalid_json_schema(content: str) -> None:
    """合法 JSON 也必须严格包含布尔 approved，不能真值转换或抛错。"""

    assert FilterPlugin._parse_detection_result(content) is False


def test_parse_detection_result_fallback_regex() -> None:
    assert FilterPlugin._parse_detection_result(
        '检测通过，结果为 "approved": true'
    ) is True


def test_parse_detection_result_invalid_returns_false() -> None:
    assert FilterPlugin._parse_detection_result("无法判断") is False


# ─── 原文命中优先于标准化命中 ───


@pytest.mark.asyncio
async def test_raw_match_takes_priority_over_normalized() -> None:
    """原文命中时直接进入重新生成，不走检测模型。"""

    detection_model = MagicMock()
    detection_model.complete = AsyncMock(
        return_value=ModelResult(content='{"approved": true}')
    )
    model = MagicMock()
    model.complete = AsyncMock(
        return_value=ModelResult(content="好的，换个话题。")
    )
    plugin = FilterPlugin(_context(detection_model=detection_model))
    await plugin.start()
    # "fuck" 在词汇表中且原文也命中
    envelope = _envelope("fuck you", model=model)
    result = await plugin.filter(envelope, _passthrough)
    assert result == "好的，换个话题。"
    # 检测模型不应被调用（原文命中走重新生成）
    detection_model.complete.assert_not_called()
    await plugin.stop()


# ─── 重新生成后的文本也需要过标准化检查 ───


@pytest.mark.asyncio
async def test_regenerated_text_still_checked_normalized() -> None:
    """重新生成后原文不再命中，但标准化命中 → 进入检测模型确认。"""

    detection_model = MagicMock()
    detection_model.complete = AsyncMock(
        return_value=ModelResult(content='{"approved": false}')
    )
    model = MagicMock()
    # 重新生成返回大小写变体：原文不再直接匹配 "fuck"，但标准化后仍匹配
    model.complete = AsyncMock(
        return_value=ModelResult(content="Fuck off")
    )
    plugin = FilterPlugin(_context(detection_model=detection_model))
    await plugin.start()
    envelope = _envelope("fuck", model=model)
    result = await plugin.filter(envelope, _passthrough)
    assert result == _TEMPLATE
    await plugin.stop()
