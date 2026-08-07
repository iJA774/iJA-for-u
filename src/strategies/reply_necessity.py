"""独立重写的群聊回复必要性评分器。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from domain.models import ComponentType, MessageRole, Participant, StoredMessage, utc_now

SHORT_REACTIONS = {"哈哈", "哈哈哈", "草", "笑死", "好", "嗯", "啊", "哦", "6", "666", "？", "?"}
QUESTION_TERMS = ("怎么", "如何", "为什么", "有没有")
REQUEST_TERMS = ("帮我", "帮忙", "能不能", "可以吗", "要不要")
OPINION_TERMS = ("你觉得", "你认为", "咋看", "怎么看", "有什么建议")
MEDIA_PLACEHOLDER_RE = re.compile(
    r"""
    \[(?:图片|图像|表情|动画表情|语音|音频|文件|image|photo|sticker|emoji|audio|file)
       (?:(?::|：)[^\]]*|读取失败|消息)?\]
    |\[(?:mface|bface)表情\]
    |\[(?:forward|xml|json)消息\]
    |\[QQ\s+[^\]]+\]
    |<(?:image|photo|sticker|emoji|audio|file)(?:\s+[^>]*)?>
    |\[CQ:(?:image|record|video|file|face)(?:,[^\]]*)?\]
    """,
    re.IGNORECASE | re.VERBOSE,
)
INVISIBLE_NOISE_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff\ufffc]")
WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ReplyScore:
    score: int
    detail: dict[str, int | float | str | bool]
    forced: bool
    trigger_message_id: str | None


def _has_question(text: str) -> bool:
    if any(term in text for term in QUESTION_TERMS):
        return True
    return bool(re.search(r"[吗呢？?](?:$|[。！!~～…])", text) and 4 <= len(text) <= 120)


def _clean_semantic_text(text: str) -> str:
    """移除平台媒体占位与不可见噪声，只保留可用于语义门控的正文。"""

    without_placeholders = MEDIA_PLACEHOLDER_RE.sub(" ", text)
    without_invisible = INVISIBLE_NOISE_RE.sub("", without_placeholders)
    return WHITESPACE_RE.sub(" ", without_invisible).strip()


def _semantic_texts(messages: list[StoredMessage]) -> list[str]:
    """只读取文本组件，避免附件文件名或视觉描述被误判成长问题。"""

    return [
        cleaned
        for message in messages
        for component in message.components
        if component.type == ComponentType.TEXT
        if (cleaned := _clean_semantic_text(component.text or ""))
    ]


def _is_short_reaction(texts: list[str]) -> bool:
    cleaned = [text.strip() for text in texts if text.strip()]
    return bool(cleaned) and all(len(text) <= 8 and text in SHORT_REACTIONS for text in cleaned)


def _is_forced(message: StoredMessage, agent_id: str) -> bool:
    for component in message.components:
        if component.type == ComponentType.MENTION and component.target_id == agent_id:
            return True
        if component.type == ComponentType.QUOTE and component.target_id == agent_id:
            return True
    return False


def _quotes_other_member(message: StoredMessage, agent_id: str) -> bool:
    return any(
        component.type == ComponentType.QUOTE
        and component.target_id not in {None, "", agent_id}
        for component in message.components
    )


def _mentions_other_member(message: StoredMessage, agent_id: str) -> bool:
    return any(
        component.type == ComponentType.MENTION
        and component.target_id not in {None, "", agent_id}
        for component in message.components
    )


def _addresses_named_member(
    messages: list[StoredMessage],
    *,
    participants: list[Participant],
) -> bool:
    """识别“名字 + 问题/请求”，避免 Agent 抢答明确问给其他成员的话。"""

    for message in messages:
        names = {
            participant.display_name.strip()
            for participant in participants
            if participant.external_user_id not in {
                message.sender_id,
                "agent",
            }
            and participant.display_name.strip()
        }
        for text in _semantic_texts([message]):
            if not (
                _has_question(text)
                or any(term in text for term in REQUEST_TERMS)
                or any(term in text for term in OPINION_TERMS)
            ):
                continue
            if any(
                re.search(
                    rf"(?:^|[\s，,。！？!?、])@?{re.escape(name)}"
                    rf"(?:[\s，,：:]|你|觉得|认为|能|可|怎|为)",
                    text,
                )
                for name in names
            ):
                return True
    return False


def score_group_reply(
    pending: list[StoredMessage],
    recent_history: list[StoredMessage],
    *,
    agent_id: str,
    trigger_count: int,
    frequency_factor: float,
    participants: list[Participant] | None = None,
    now: datetime | None = None,
) -> ReplyScore:
    """按相关性、内容、积压和最近存在感计算可解释分数。"""

    if not pending:
        return ReplyScore(0, {"reason": "没有待处理消息"}, False, None)
    trigger_count = max(1, trigger_count)
    frequency_factor = max(0.0, min(1.0, frequency_factor))
    forced_message = next((message for message in reversed(pending) if _is_forced(message, agent_id)), None)
    if forced_message:
        return ReplyScore(
            100,
            {"forced": True, "relevance": 100, "frequency_factor": 1.0},
            True,
            forced_message.id,
        )

    texts = _semantic_texts(pending)
    semantic_message_count = sum(bool(_semantic_texts([message])) for message in pending)
    combined = "\n".join(texts)
    removed_noise_message_count = sum(
        bool(message.plain_text) and not _semantic_texts([message])
        for message in pending
    )
    if not texts:
        return ReplyScore(
            0,
            {
                "forced": False,
                "content": 0,
                "content_reasons": "仅媒体或噪声占位",
                "pressure": 0,
                "pending_count": len(pending),
                "semantic_message_count": 0,
                "removed_noise_message_count": removed_noise_message_count,
                "presence_penalty": 0,
                "continuity_score": 0,
                "human_thread_penalty": 0,
                "other_target_penalty": 0,
                "distinct_pending_senders": len(
                    {message.sender_id for message in pending}
                ),
                "assistant_ratio": 0.0,
                "presence_window_seconds": 300,
                "presence_message_count": 0,
                "trigger_count": trigger_count,
                "frequency_factor": frequency_factor,
            },
            False,
            pending[-1].id,
        )
    content_score = 0
    reasons: list[str] = []
    if any(_has_question(text) for text in texts):
        content_score += 15
        reasons.append("问题")
        if any("你" in text for text in texts):
            content_score += 15
            reasons.append("面向在场者的问题")
    if any(term in combined for term in REQUEST_TERMS):
        content_score += 20
        reasons.append("请求")
    if any(term in combined for term in OPINION_TERMS):
        content_score += 20
        reasons.append("征询")
    if len(combined) >= 40:
        content_score += 5
        reasons.append("长文本")
    if len(combined) >= 120:
        content_score += 10
        reasons.append("较长文本")
    if _is_short_reaction(texts):
        content_score -= 25
        reasons.append("短反应")

    pending_ids = {message.id for message in pending}
    prior_history = [message for message in recent_history if message.id not in pending_ids]
    continuity_score = 0
    if prior_history and prior_history[-1].role == MessageRole.ASSISTANT:
        continuity_score = 20
        reasons.append("承接 Agent 上轮")
    human_thread_penalty = 0
    distinct_senders = {message.sender_id for message in pending}
    if len(distinct_senders) >= 2:
        human_thread_penalty += 15
        reasons.append("多人对话进行中")
    if any(_quotes_other_member(message, agent_id) for message in pending):
        human_thread_penalty += 20
        reasons.append("引用其他群成员")
    other_target_penalty = 0
    if any(_mentions_other_member(message, agent_id) for message in pending):
        other_target_penalty += 35
        reasons.append("@其他群成员")
    if _addresses_named_member(
        pending,
        participants=participants or [],
    ):
        other_target_penalty += 35
        reasons.append("明确询问其他群成员")

    pending_ratio = semantic_message_count / trigger_count
    pressure_score = (
        min(100, round(50 * pending_ratio * pending_ratio))
        if pending_ratio <= 1
        else min(100, 50 + round(50 * math.log1p(pending_ratio - 1) / math.log(5)))
    )

    snapshot_time = now or utc_now()
    presence_start = snapshot_time - timedelta(minutes=5)
    window = [
        message
        for message in recent_history
        if presence_start <= message.created_at <= snapshot_time
    ][-50:]
    assistant_count = sum(message.role == MessageRole.ASSISTANT for message in window)
    assistant_ratio = assistant_count / len(window) if window else 0
    presence_penalty = 0
    if assistant_ratio > 0.25:
        presence_penalty = min(25, round(25 * (assistant_ratio - 0.25) / 0.35))
    # 先限制基础热度，再扣除 Agent 存在感，避免高积压时惩罚被 100 分上限吞掉。
    base_score = min(
        100,
        round((content_score + pressure_score + continuity_score) * frequency_factor),
    )
    final_score = max(
        0,
        base_score
        - presence_penalty
        - human_thread_penalty
        - other_target_penalty,
    )
    return ReplyScore(
        final_score,
        {
            "forced": False,
            "content": content_score,
            "content_reasons": ",".join(reasons) or "普通",
            "pressure": pressure_score,
            "pending_count": len(pending),
            "semantic_message_count": semantic_message_count,
            "removed_noise_message_count": removed_noise_message_count,
            "trigger_count": trigger_count,
            "presence_penalty": presence_penalty,
            "continuity_score": continuity_score,
            "human_thread_penalty": human_thread_penalty,
            "other_target_penalty": other_target_penalty,
            "distinct_pending_senders": len(distinct_senders),
            "assistant_ratio": round(assistant_ratio, 3),
            "presence_window_seconds": 300,
            "presence_message_count": len(window),
            "frequency_factor": round(frequency_factor, 3),
        },
        False,
        pending[-1].id,
    )
