"""持久化纯序列化与 FTS 查询构造规则。"""

import pytest
from pydantic import ValidationError

from adapters.persistence.search import (
    fts_phrase,
    memory_fts_query,
    message_search_text,
    normalize_search_text,
)
from adapters.persistence.serialization import (
    components_json,
    parse_components,
    parse_finite_vector,
)
from domain.models import ComponentType, MessageComponent


def test_component_serialization_round_trip_and_search_projection_do_not_leak_paths() -> None:
    """JSON 往返保真，全文索引只包含用户可见描述。"""

    components = [
        MessageComponent.text_component("  真不错\n继续！ "),
        MessageComponent(
            type=ComponentType.IMAGE_REF,
            attachment_id="attachment-private-id",
            filename="reaction.png",
            mime_type="image/png",
            size=42,
            sha256="a" * 64,
            storage_path="C:/private/uploads/secret.png",
            description="庆祝表情",
            is_expression=True,
        ),
    ]

    raw = components_json(components)
    assert parse_components(raw) == components
    assert "庆祝表情" in raw

    search_text = message_search_text(components)
    assert search_text == "真不错 继续！ 庆祝表情"
    assert "attachment-private-id" not in search_text
    assert "secret.png" not in search_text
    assert "a" * 64 not in search_text


def test_component_deserialization_revalidates_persisted_schema() -> None:
    """损坏的持久化 JSON 必须在信任边界失败，不返回半合法组件。"""

    with pytest.raises(ValidationError, match="文本组件不能为空"):
        parse_components('[{"type":"text","text":"  "}]')


def test_search_helpers_share_normalization_and_safe_fts_rules() -> None:
    assert normalize_search_text("  AbC\n 数据库  ") == "abc 数据库"
    assert fts_phrase("ab") is None
    assert fts_phrase(' Ab"C ') == '"ab""c"'
    assert memory_fts_query("偏好") is None
    assert memory_fts_query("数据库连接异常") == ('"数据库" OR "据库连" OR "库连接" OR "连接异" OR "接异常"')


def test_vector_deserialization_rejects_dimension_and_non_finite_values() -> None:
    assert parse_finite_vector(
        "[0.25, -1.5]",
        expected_dimension=2,
        context="测试向量",
    ) == [0.25, -1.5]
    with pytest.raises(ValueError, match="维度错误"):
        parse_finite_vector(
            "[0.25]",
            expected_dimension=2,
            context="测试向量",
        )
    with pytest.raises(ValueError, match="非有限数"):
        parse_finite_vector(
            "[0.25, NaN]",
            expected_dimension=2,
            context="测试向量",
        )
