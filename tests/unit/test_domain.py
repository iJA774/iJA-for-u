import pytest
from pydantic import ValidationError

from domain.models import ChatType, ComponentType, MessageComponent, SessionResolver


def test_session_resolver_isolates_chat_type_and_account() -> None:
    private = SessionResolver.resolve("qq", "bot-a", "42", ChatType.PRIVATE)
    group = SessionResolver.resolve("qq", "bot-a", "42", ChatType.GROUP)
    other_account = SessionResolver.resolve("qq", "bot-b", "42", ChatType.PRIVATE)
    assert len({private, group, other_account}) == 3
    assert private == SessionResolver.resolve("QQ", "bot-a", "42", ChatType.PRIVATE)


def test_message_component_rejects_incomplete_image() -> None:
    with pytest.raises(ValidationError, match="图片引用缺少"):
        MessageComponent(type=ComponentType.IMAGE_REF, filename="a.png")


def test_message_component_rejects_expression_marker_on_text() -> None:
    with pytest.raises(ValidationError, match="只有受控图片引用"):
        MessageComponent(
            type=ComponentType.TEXT,
            text="不能把文本伪装成表情包",
            is_expression=True,
        )


@pytest.mark.parametrize(
    ("component_type", "message"),
    [
        (ComponentType.AUDIO_REF, "语音引用缺少"),
        (ComponentType.FILE_REF, "文件引用缺少"),
    ],
)
def test_message_component_rejects_incomplete_attachment(
    component_type: ComponentType,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        MessageComponent(type=component_type, filename="incomplete.bin")
