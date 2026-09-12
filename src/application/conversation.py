"""共享的对话理解：冻结目标、上下文关系和检索问题。"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from application.model_json import parse_model_json
from config import AppSettings
from domain.errors import InvalidModelResponseError
from domain.models import ChatType, SessionView, StoredMessage, TurnDecision
from observability.model_attempts import model_observation_scope
from ports import ModelProvider, ModelRequest
from prompting import PromptAssembler


class ConversationUnderstanding(BaseModel):
    """仅引用当前快照的语义结果，随 Turn 一起持久化。"""

    model_config = ConfigDict(extra="forbid")
    target_message_id: str | None
    relevant_message_ids: list[str] = Field(max_length=1000)
    history_message_ids: list[str] = Field(max_length=20)
    retrieval_query: str = Field(max_length=1200)
    utility: int = Field(ge=0, le=100)
    needs_history: bool
    needs_fresh_data: bool
    missing_information: list[str] = Field(max_length=8)
    ambiguous: bool
    is_question: bool


def needs_conversation_understanding(
    session: SessionView,
    pending: list[StoredMessage],
    history: list[StoredMessage],
) -> bool:
    """群聊理解参与价值；私聊仅在当前话语依赖前文时额外分析。"""
    text = "\n".join(item.plain_text for item in pending)
    if session.chat_type == ChatType.GROUP:
        return any(len(item.plain_text.strip()) >= 4 for item in pending)
    prior = [item for item in history if item.id not in {message.id for message in pending}]
    return bool(
        prior
        and (len(text.strip()) <= 16 or re.search(r"那个|这个|后来|之前|上次|接着|继续|刚才|前者|后者", text))
    )


async def understand_conversation(
    *,
    settings: AppSettings,
    model: ModelProvider,
    prompting: PromptAssembler,
    session: SessionView,
    pending: list[StoredMessage],
    history: list[StoredMessage],
    decision: TurnDecision,
) -> ConversationUnderstanding:
    """执行一次无副作用语义分析；失败保留待处理消息，不伪造本地语义结果。"""
    pending_ids = {item.id for item in pending}
    prior = [item for item in history if item.id not in pending_ids][-20:]
    forced = decision.trigger_message_id if decision.score_detail.get("forced") else None
    proposed = {
        "target_message_id": decision.trigger_message_id,
        "relevant_message_ids": (
            [item.id for item in pending]
            if session.chat_type == ChatType.PRIVATE
            else decision.score_detail.get("conversation_message_ids", [])
        ),
        "history_message_ids": [],
        "retrieval_query": "",
        "utility": decision.score,
        "needs_history": False,
        "needs_fresh_data": False,
        "missing_information": [],
        "ambiguous": False,
        "is_question": False,
    }
    messages = prompting.build_conversation_understanding(
        session=session,
        pending=pending,
        history=prior,
        proposed=proposed,
        forced_target=forced,
    )
    with model_observation_scope(task="chat.understand", session_id=session.id, turn_id=decision.id):
        result = await model.complete(
            ModelRequest(
                messages=messages,
                model=settings.model.name,
                temperature=0,
                # 结构化结果包含多条证据 ID，不能沿用聊天短回复的 500 token 上限。
                max_tokens=min(1800, settings.model.context_window_tokens // 4),
                json_mode=settings.model.supports_json_object,
            )
        )
    if not result.content:
        raise InvalidModelResponseError("对话理解没有返回结果")
    try:
        understanding = ConversationUnderstanding.model_validate(parse_model_json(result.content))
    except ValidationError as exc:
        raise InvalidModelResponseError("对话理解不符合结果契约") from exc
    selected = understanding.relevant_message_ids
    history_ids = understanding.history_message_ids
    if (
        len(selected) != len(set(selected))
        or not set(selected).issubset(pending_ids)
        or len(history_ids) != len(set(history_ids))
        or not set(history_ids).issubset({item.id for item in prior})
        or (understanding.target_message_id is not None and understanding.target_message_id not in selected)
        or (understanding.target_message_id is None and bool(selected))
        or (forced is not None and understanding.target_message_id != forced)
        or (
            session.chat_type == ChatType.PRIVATE
            and (
                understanding.target_message_id != decision.trigger_message_id or set(selected) != pending_ids
            )
        )
    ):
        raise InvalidModelResponseError("对话理解偏离当前消息快照或直接邀请目标")
    return understanding
