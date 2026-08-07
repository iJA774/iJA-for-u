"""模型上下文的保守 token 估算与历史裁剪。"""

from __future__ import annotations

import math
import re

from ports import ModelMessage

_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def estimate_text_tokens(text: str) -> int:
    """估算文本 token，宁可略高估也不让调用点突破模型窗口。"""

    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    # CJK 在常见 tokenizer 中通常接近一字一 token；其他字符按四字符一 token，
    # 再计入 JSON、角色标签等固定开销。
    return cjk + math.ceil(other / 4) + 4


def estimate_messages_tokens(messages: list[ModelMessage]) -> int:
    """估算一组 Chat Completions 消息的输入 token。"""

    return sum(
        estimate_text_tokens(message.content or "")
        + 8
        + 85 * len(message.images)
        for message in messages
    )


def trim_history_to_budget(
    messages: list[ModelMessage],
    *,
    budget: int,
    required_indices: set[int] | None = None,
) -> list[ModelMessage]:
    """从最新消息向前保留历史，并保证指定当前消息不会被静默裁掉。"""

    required = set(required_indices or ())
    if any(index < 0 or index >= len(messages) for index in required):
        raise ValueError("必需历史消息索引越界")
    if required:
        return _trim_with_required_messages(
            messages,
            budget=budget,
            required_indices=required,
        )
    if budget < 32:
        return []
    selected: list[ModelMessage] = []
    used = 0
    for message in reversed(messages):
        cost = estimate_messages_tokens([message])
        if cost <= budget - used:
            selected.append(message)
            used += cost
            continue
        if not selected:
            # 预留 Chat 消息自身的角色/封装开销，避免裁剪后的内容刚好填满
            # budget 却在加入消息结构后再次越过模型窗口。
            content_budget = budget - used - 8
            if content_budget >= 4:
                selected.append(_truncate_message(message, content_budget))
        break
    return list(reversed(selected))


def _trim_with_required_messages(
    messages: list[ModelMessage],
    *,
    budget: int,
    required_indices: set[int],
) -> list[ModelMessage]:
    """先为必需消息保留预算，再用最新历史填充剩余窗口。"""

    minimum_per_required = 32
    if budget < minimum_per_required * len(required_indices):
        raise ValueError("模型输入预算不足以容纳当前用户消息")

    selected: dict[int, ModelMessage] = {}
    used = 0
    ordered_required = sorted(required_indices)
    for position, index in enumerate(ordered_required):
        remaining_required = len(ordered_required) - position - 1
        allocation = budget - used - minimum_per_required * remaining_required
        message = messages[index]
        cost = estimate_messages_tokens([message])
        if cost > allocation:
            content_budget = allocation - 8
            if content_budget < 4:
                raise ValueError("模型输入预算不足以容纳当前用户消息")
            message = _truncate_required_message(message, content_budget)
            cost = estimate_messages_tokens([message])
        if cost > allocation:
            raise ValueError("当前用户消息裁剪后仍超过模型输入预算")
        selected[index] = message
        used += cost

    for index in range(len(messages) - 1, -1, -1):
        if index in selected:
            continue
        message = messages[index]
        cost = estimate_messages_tokens([message])
        if cost > budget - used:
            break
        selected[index] = message
        used += cost
    return [selected[index] for index in sorted(selected)]


def _truncate_required_message(
    message: ModelMessage, budget: int
) -> ModelMessage:
    """裁剪必需消息时完整保留应用层稳定索引首行。"""

    text = message.content or ""
    marker = "[应用层消息索引，固定首行]"
    if not text.startswith(marker) or "\n" not in text:
        return _truncate_message(message, budget)
    header, body = text.split("\n", maxsplit=1)
    header_text = header + "\n"
    header_cost = estimate_text_tokens(header_text)
    body_budget = budget - header_cost
    if body_budget < 4:
        raise ValueError("模型输入预算不足以保留当前用户消息索引")
    clipped_body = _truncate_message(
        message.model_copy(update={"content": body, "images": []}),
        body_budget,
    ).content
    return message.model_copy(
        update={
            "content": header_text + (clipped_body or ""),
            "images": [],
        }
    )


def _truncate_message(message: ModelMessage, budget: int) -> ModelMessage:
    """只裁剪模型投影，权威消息正文保持不变。"""

    text = message.content or ""
    if estimate_text_tokens(text) <= budget:
        return message
    # 以字符比例快速收缩，再用估算器收敛，保留用户输入首尾的关键约束与问题。
    target_chars = max(32, int(len(text) * budget / estimate_text_tokens(text)))
    half = max(1, (target_chars - 1) // 2)
    clipped = f"{text[:half]}…{text[-half:]}"
    while len(clipped) > 2 and estimate_text_tokens(clipped) > budget:
        half = max(1, half - max(1, half // 8))
        clipped = f"{text[:half]}…{text[-half:]}"
    return message.model_copy(update={"content": clipped, "images": []})
