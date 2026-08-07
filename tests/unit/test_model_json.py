import json

import pytest

from application.model_json import parse_model_json


def test_parse_model_json_accepts_plain_json() -> None:
    assert parse_model_json('{"ok": true}') == {"ok": True}


def test_parse_model_json_accepts_markdown_fence() -> None:
    assert parse_model_json('```json\n{"ok": true}\n```') == {"ok": True}


def test_parse_model_json_accepts_surrounding_text() -> None:
    assert parse_model_json('结果如下：\n{"ok": true}\n以上。') == {"ok": True}


def test_parse_model_json_skips_malformed_braces_before_valid_json() -> None:
    assert parse_model_json('说明 {不是 JSON} 后面是 {"ok": true}') == {"ok": True}


def test_parse_model_json_rejects_text_without_json() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_model_json('没有结构化结果')
