"""SQLite JSON/向量字段的纯序列化边界。"""

from __future__ import annotations

import json
import math

from domain.models import MessageComponent


def components_json(components: list[MessageComponent]) -> str:
    """把规范消息组件序列化为不转义中文的稳定 JSON。"""

    return json.dumps(
        [item.model_dump(mode="json") for item in components],
        ensure_ascii=False,
    )


def parse_components(raw: str) -> list[MessageComponent]:
    """在持久化读取边界重新执行组件 schema 校验。"""

    return [MessageComponent.model_validate(item) for item in json.loads(raw)]


def parse_finite_vector(
    raw: str,
    *,
    expected_dimension: int,
    context: str,
) -> list[float]:
    """严格读取派生向量，损坏时带索引上下文失败。"""

    parsed = json.loads(raw)
    if not isinstance(parsed, list) or len(parsed) != expected_dimension:
        raise ValueError(
            f"{context} 维度错误: expected={expected_dimension}, "
            f"actual={len(parsed) if isinstance(parsed, list) else 'non-list'}"
        )
    vector = [float(value) for value in parsed]
    if not vector or any(not math.isfinite(value) for value in vector):
        raise ValueError(f"{context} 包含空向量或非有限数")
    return vector
