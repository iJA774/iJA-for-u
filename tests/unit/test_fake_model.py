"""确定性 Fake 模型的用户可见回复回归测试。"""

from __future__ import annotations

import pytest

from adapters.model import FakeModelProvider
from ports import ModelMessage, ModelRequest


@pytest.mark.asyncio
async def test_fake_model_does_not_echo_application_message_index() -> None:
    """Prompt 的稳定索引属于应用层控制数据，不应进入聊天气泡。"""

    provider = FakeModelProvider()
    request = ModelRequest(
        messages=[
            ModelMessage(
                role="user",
                content=(
                    '[应用层消息索引，固定首行]{"message_id":"msg-1",'
                    '"sender_id":"u-1"}\n你好，小佳'
                ),
            )
        ],
        model="fake",
        temperature=0,
        max_tokens=128,
    )

    result = await provider.complete(request)

    assert result.content == "我听到了：你好，小佳"
