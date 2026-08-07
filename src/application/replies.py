"""把模型终态文本规范化为可投递的回复草稿。"""

from __future__ import annotations

import re

from domain.errors import InvalidModelResponseError
from domain.models import MessageComponent, ReplyDraft, ReplyPlan

_MARKDOWN_LINE = re.compile(
    r"^\s*(?:```|~~~|#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s?|\||(?:-{3,}|\*{3,}|_{3,})\s*$)"
)
_MAX_AUTO_SPLIT_LINE_CHARS = 200


def draft_from_model_text(
    content: str,
    *,
    split_short_lines: bool,
) -> ReplyDraft:
    """构造文本草稿，并把明确的连续聊天短行恢复成独立消息。

    原生 ``send_messages`` 仍是多消息输出的权威协议。这里仅修复模型偶尔用
    换行模拟气泡的退化结果；空行、Markdown、代码和长段落保持为单条消息，
    避免破坏一条消息内部的结构化排版。
    """

    if split_short_lines:
        raw_lines = content.splitlines()
        if (
            2 <= len(raw_lines) <= 6
            and all(line.strip() for line in raw_lines)
            and all(
                len(line.strip()) <= _MAX_AUTO_SPLIT_LINE_CHARS
                and not line.startswith(("    ", "\t"))
                and _MARKDOWN_LINE.match(line) is None
                for line in raw_lines
            )
        ):
            texts = [line.strip() for line in raw_lines]
            return ReplyDraft(
                components=[MessageComponent.text_component(texts[0])],
                follow_up_components=[
                    [MessageComponent.text_component(text)] for text in texts[1:]
                ],
            )
    return ReplyDraft(components=[MessageComponent.text_component(content)])


def validate_reply_draft(draft: ReplyDraft, plan: ReplyPlan) -> ReplyDraft:
    """按冻结计划校验 Replyer 或终态工具生成的可见草稿。"""

    groups = [draft.components, *draft.follow_up_components]
    if any(not group for group in groups):
        raise InvalidModelResponseError("Replyer 生成了空消息气泡")
    if len(groups) > plan.max_visible_messages:
        raise InvalidModelResponseError(
            f"Replyer 生成了 {len(groups)} 条消息，超过计划上限 {plan.max_visible_messages}"
        )
    total_chars = sum(
        len(component.text or "")
        for group in groups
        for component in group
    )
    if total_chars > plan.max_total_chars:
        raise InvalidModelResponseError(
            f"Replyer 文本共 {total_chars} 字符，超过计划上限 {plan.max_total_chars}"
        )
    return draft
