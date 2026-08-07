from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image
from pydantic import BaseModel

from adapters.model import FakeModelProvider
from bootstrap import build_runtime
from domain.errors import InputValidationError
from domain.models import (
    ChatType,
    ComponentType,
    InboundMessage,
    MessageComponent,
    MessageRole,
    Participant,
    SessionView,
    StoredMessage,
    utc_now,
)
from plugins._host import InboundEnvelope, PlatformIngress, PluginContributionCatalog
from ports import ChannelRuntimeContext
from prompting import PromptAssembler
from tools import RegisteredTool, ToolContext, ToolRegistry


def _invalid_channel_manifest() -> str:
    return (
        'id = "broken"\n'
        'kind = "channel"\n'
        'entrypoint = "runtime:create_plugin"\n'
        '[capabilities]\n'
        'platform = "broken"\n'
        'display_name = "损坏清单"\n'
        'limitations = ["测试"]\n'
        '[capabilities.ingress]\n'
        'text = "sometimes"\n'
    )


def test_channel_manifest_rejects_incomplete_or_unknown_capability_status(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugins" / "broken"
    plugin.mkdir(parents=True)
    (plugin / "runtime.py").write_text(
        "def create_plugin(context): return None\n", encoding="utf-8"
    )
    (plugin / "plugin.toml").write_text(
        _invalid_channel_manifest(), encoding="utf-8"
    )

    with pytest.raises(InputValidationError, match="capabilities 无效"):
        PluginContributionCatalog(tmp_path)


def test_capability_registry_covers_four_platforms_and_prompt_projection(
    settings,
) -> None:
    registry = PluginContributionCatalog(
        settings.project_root, include_bundled=True
    ).channel_capabilities

    assert registry.supported_egress_components("qq") == frozenset({"text"})
    assert "image_ref" in registry.supported_egress_components("onebot")
    assert "file_ref" in registry.supported_egress_components("web-simulator")
    assert not registry.supports_ingress("qq", ComponentType.IMAGE_REF)
    assert registry.supports_ingress("onebot", ComponentType.IMAGE_REF)
    assert not registry.supports_processing("qq", "image_description")
    assert registry.supports_processing("onebot", "image_description")
    assert "image_description" not in registry.supported_prompt_projections("qq")
    assert "image_description" in registry.supported_prompt_projections("onebot")
    assert "placeholder/unsupported" in registry.prompt_summary(
        "wechat-service-account"
    )
    section = PromptAssembler._channel_capability_section(
        ChannelRuntimeContext(capability_summary=registry.prompt_summary("qq"))
    )
    assert section is not None
    assert section[0] == "当前平台模态边界"
    assert "真实出站组件仅有：text" in section[1]


@pytest.mark.asyncio
async def test_platform_ingress_rejects_placeholder_canonical_component(
    settings,
) -> None:
    """placeholder 只能由插件降级为文本，不能越过宿主边界伪装成 canonical 图片。"""

    registry = PluginContributionCatalog(
        settings.project_root,
        include_bundled=True,
    ).channel_capabilities
    ingress = PlatformIngress(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        registry,
    )
    envelope = InboundEnvelope(
        message=InboundMessage(
            platform="qq",
            account_id="qq-test",
            external_message_id="message-1",
            external_chat_id="chat-1",
            sender_id="user-1",
            sender_name="小明",
            chat_type=ChatType.PRIVATE,
            components=[
                MessageComponent(
                    type=ComponentType.IMAGE_REF,
                    attachment_id="asset-1",
                    filename="secret.png",
                    mime_type="image/png",
                    size=1,
                    sha256="0" * 64,
                    storage_path="attachments/secret.png",
                    description="未声明的图片描述",
                )
            ],
        ),
        session_display_name="能力边界",
    )

    with pytest.raises(InputValidationError, match="image_ref"):
        await ingress.accept(envelope)


def test_prompt_projection_excludes_undeclared_image_description(settings) -> None:
    """聊天与画像链即使混入富媒体，也只能读取 QQ manifest 声明的文本。"""

    registry = PluginContributionCatalog(
        settings.project_root,
        include_bundled=True,
    ).channel_capabilities
    message = StoredMessage(
        id="message-1",
        session_id="session-1",
        role=MessageRole.USER,
        sender_id="user-1",
        sender_name="小明",
        components=[
            MessageComponent.text_component("公开文本"),
            MessageComponent(
                type=ComponentType.IMAGE_REF,
                attachment_id="asset-1",
                filename="secret.png",
                mime_type="image/png",
                size=1,
                sha256="0" * 64,
                storage_path="attachments/secret.png",
                description="未声明图片描述不可进入 Prompt",
            ),
        ],
        created_at=utc_now(),
    )
    projected = PromptAssembler._project_message_content(
        message,
        channel_runtime=ChannelRuntimeContext(
            supported_prompt_projections=(
                registry.supported_prompt_projections("qq")
            )
        ),
    )

    assert projected == "公开文本"
    assert "未声明图片描述" not in projected
    assert "secret.png" not in projected

    assembler = PromptAssembler(
        settings.project_root / "prompts",
        channel_capabilities=registry,
    )
    now = utc_now()
    session = SessionView(
        id="session-1",
        platform="qq",
        account_id="qq-test",
        external_chat_id="chat-1",
        chat_type=ChatType.PRIVATE,
        display_name="QQ 测试",
        created_at=now,
        updated_at=now,
    )
    profile_prompt = assembler.build_profile_extraction(
        session=session,
        subject_id="user-1",
        messages=[message],
    )
    model_bound_calls = [
        profile_prompt,
        assembler.build_memory_consolidation(
            session=session,
            source_messages=[message],
            recent_messages=[message],
            existing_memories=[],
        ),
        assembler.build_jargon_learning(
            session=session,
            messages=[message],
        ),
        assembler.build_proactive_judge(
            session=session,
            facts=[],
            messages=[message],
            recent_proactive=[message],
            candidates=[],
            request_time=now.isoformat(),
            timezone="Asia/Shanghai",
        ),
    ]
    for request_messages in model_bound_calls:
        encoded_request = "\n".join(
            item.content or "" for item in request_messages
        )
        assert "公开文本" in encoded_request
        assert "未声明图片描述" not in encoded_request
        assert "secret.png" not in encoded_request


@pytest.mark.asyncio
async def test_skill_and_attachment_visibility_follow_current_platform_egress(
    settings,
) -> None:
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    await runtime.start()
    try:
        output = BytesIO()
        Image.new("RGB", (96, 96), "#6d5dfc").save(output, format="PNG")
        await runtime.expressions.upload_expression(
            content=output.getvalue(),
            mime_type="image/png",
            name="能力测试",
            emotion="开心",
        )
        qq_context = ToolContext(
            session_id="session-qq",
            actor_id="u1",
            supported_egress_components=(
                runtime.channel_capabilities.supported_egress_components("qq")
            ),
        )
        web_context = ToolContext(
            session_id="session-web",
            actor_id="u1",
            supported_egress_components=(
                runtime.channel_capabilities.supported_egress_components(
                    "web-simulator"
                )
            ),
        )
        assert "send-expression" not in await runtime.chat._available_skills(
            context=qq_context
        )
        assert "send-expression" in await runtime.chat._available_skills(
            context=web_context
        )

        qq = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="QQ 能力测试",
            external_chat_id="qq-capability",
            participants=[
                Participant(external_user_id="u1", display_name="小明")
            ],
            platform="qq",
            account_id="qq-test",
        )
        onebot = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="OneBot 能力测试",
            external_chat_id="onebot-capability",
            participants=[
                Participant(external_user_id="u2", display_name="小红")
            ],
            platform="onebot",
            account_id="onebot-test",
        )
        assert "send_attachment" not in runtime.chat._allowed_reactive_tools(
            qq, "u1"
        )
        assert "send_attachment" in runtime.chat._allowed_reactive_tools(
            onebot, "u2"
        )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_tool_execution_rechecks_required_egress_capability() -> None:
    class EmptyArguments(BaseModel):
        pass

    async def handler(arguments, context):
        del arguments, context
        return {"ok": True}

    registry = ToolRegistry(
        [
            RegisteredTool(
                name="image_action",
                description="测试图片动作",
                arguments_model=EmptyArguments,
                handler=handler,
                required_any_egress_components=frozenset({"image_ref"}),
            )
        ]
    )

    with pytest.raises(InputValidationError, match="真实出站能力"):
        await registry.execute(
            "image_action",
            {},
            ToolContext(
                session_id="s1",
                actor_id="u1",
                supported_egress_components=frozenset({"text"}),
            ),
        )
