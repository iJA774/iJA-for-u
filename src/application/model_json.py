"""模型文本中结构化 JSON 的严格提取辅助。"""

from __future__ import annotations

import json
from typing import Any


def parse_model_json(text: str) -> Any:
    """返回模型文本中的首个完整 JSON 值；不修补或猜测非法 JSON。"""

    stripped = text.strip()
    if not stripped:
        raise json.JSONDecodeError("模型没有返回 JSON", text, 0)

    decoder = json.JSONDecoder()
    try:
        return decoder.decode(stripped)
    except json.JSONDecodeError as direct_error:
        for index, char in enumerate(text):
            if char not in "{[":
                continue
            try:
                value, _ = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                continue
            return value
        raise direct_error
