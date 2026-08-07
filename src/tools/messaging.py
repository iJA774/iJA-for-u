"""同一 Turn 内多条可见消息的结构化回复工具。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from domain.errors import InputValidationError, NotFoundError
from domain.models import ComponentType, MessageComponent, ReplyDraft
from ports import AttachmentValidator, ChatRepository
from tools.registry import RegisteredTool, ToolContext, ToolOutcome


class SendMessagesArguments(BaseModel):
    """限制消息数量和单条长度，避免模型把长文机械切成大量气泡。"""

    model_config = ConfigDict(extra="forbid")

    messages: list[str] = Field(
        min_length=2,
        max_length=6,
        description=(
            "按发送顺序排列的独立聊天气泡；每一项只表示一条消息，"
            "不要在一个数组项里用换行模拟多条消息。"
        ),
    )


class SendAttachmentArguments(BaseModel):
    """主动发送当前会话已上传的语音或文件。"""

    model_config = ConfigDict(extra="forbid")

    attachment_id: str = Field(min_length=1, max_length=200)
    caption: str | None = Field(
        default=None,
        max_length=1000,
        description="语音前可附带的简短说明；发送普通文件时必须省略。",
    )


def build_messaging_tools(
    store: ChatRepository | None = None,
    attachments: AttachmentValidator | None = None,
) -> list[RegisteredTool]:
    """构造只准备回复草稿、不直接执行外部发送的终态工具。"""

    async def send_messages(
        arguments: BaseModel, context: ToolContext
    ) -> ToolOutcome:
        del context
        assert isinstance(arguments, SendMessagesArguments)
        texts = [item.strip() for item in arguments.messages]
        if any(not item for item in texts):
            # Pydantic 只能约束列表长度；空白语义在受信任边界集中拒绝。
            raise InputValidationError("多消息回复不能包含空白消息")
        if any(len(item) > 4000 for item in texts):
            raise InputValidationError("单条消息不能超过 4000 个字符")
        return ToolOutcome(
            value={"prepared": True, "message_count": len(texts)},
            reply_draft=ReplyDraft(
                components=[MessageComponent.text_component(texts[0])],
                follow_up_components=[
                    [MessageComponent.text_component(text)] for text in texts[1:]
                ],
            ),
        )

    tools = [
        RegisteredTool(
            name="send_messages",
            description=(
                "最终回复包含 2 到 6 个应当依次独立发送的聊天短句或节奏点时必须调用；"
                "messages 中每一项会按顺序成为一条真实消息。"
                "禁止在普通文本或单个 messages 项中用换行假装多个气泡；"
                "普通单条回复不要调用。"
            ),
            arguments_model=SendMessagesArguments,
            handler=send_messages,
            terminal=True,
        )
    ]
    if store is None or attachments is None:
        return tools

    async def send_attachment(
        arguments: BaseModel, context: ToolContext
    ) -> ToolOutcome:
        assert isinstance(arguments, SendAttachmentArguments)
        session = await store.get_session(context.session_id)
        if session is None:
            raise NotFoundError("会话不存在")
        if session.platform != "onebot":
            raise InputValidationError("当前平台不支持主动发送语音或文件")
        component = await store.get_session_attachment(
            context.session_id, arguments.attachment_id
        )
        if component is None:
            raise NotFoundError("当前会话中不存在该附件")
        if component.type not in {ComponentType.AUDIO_REF, ComponentType.FILE_REF}:
            raise InputValidationError("send_attachment 只支持语音或普通文件")
        attachments.validate_attachment_ref(component)
        caption = (arguments.caption or "").strip()
        if component.type == ComponentType.FILE_REF and caption:
            raise InputValidationError("OneBot 文件上传必须独立发送，不能附带说明")
        components = [component.model_copy(deep=True)]
        if caption:
            components.insert(0, MessageComponent.text_component(caption))
        return ToolOutcome(
            value={
                "prepared": True,
                "attachment_id": component.attachment_id,
                "attachment_type": component.type.value,
            },
            reply_draft=ReplyDraft(components=components),
        )

    tools.append(
        RegisteredTool(
            name="send_attachment",
            description=(
                "仅在 OneBot 会话中，主动发送当前会话历史里已经上传的语音或文件。"
                "传入附件组件的 attachment_id；普通文件必须单独发送，不能带 caption。"
                "图片仍通过普通回复能力发送。"
            ),
            arguments_model=SendAttachmentArguments,
            handler=send_attachment,
            terminal=True,
            required_any_egress_components=frozenset(
                {
                    ComponentType.AUDIO_REF.value,
                    ComponentType.FILE_REF.value,
                }
            ),
        )
    )
    return tools
