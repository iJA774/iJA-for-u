"""响应式回复的可审计规划层。"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import ValidationError

from application.conversation import ConversationUnderstanding
from domain.errors import InputValidationError
from domain.models import (
    ChatType,
    ComponentType,
    DecisionAction,
    ReplyEvidenceMode,
    ReplyMode,
    ReplyPlan,
    ReplyTargetReason,
    SessionView,
    StoredMessage,
    TurnDecision,
)

__all__ = ["ReplyPlan", "ReplyPlanner"]


class ReplyPlanner:
    """冻结回复目标、证据策略和输出边界，不生成用户可见内容。"""

    _LEGACY_PLAN_FIELDS = frozenset(
        {
            "objective",
            "response_mode",
            "address_sender_id",
            "evidence_policy",
        }
    )
    _REQUEST_SIGNAL = re.compile(r"(帮我|帮忙|能不能|可以吗|要不要|你觉得|你认为|咋看|怎么看)")

    def plan(
        self,
        *,
        session: SessionView,
        decision: TurnDecision,
        pending: list[StoredMessage],
    ) -> ReplyPlan:
        """根据权威 Turn 快照生成可持久化计划。"""

        self._validate_inputs(session=session, decision=decision, pending=pending)
        target = self._resolve_target(decision=decision, pending=pending)
        relevant = self._select_relevant_messages(
            session=session,
            target=target,
            pending=pending,
            decision=decision,
        )
        relevant_text = "\n".join(item.plain_text for item in relevant if item.plain_text)
        raw_understanding = decision.score_detail.get("conversation_understanding")
        understanding = (
            ConversationUnderstanding.model_validate(raw_understanding)
            if raw_understanding is not None else None
        )
        needs_history = understanding.needs_history if understanding else False
        needs_fresh_data = understanding.needs_fresh_data if understanding else False
        evidence_mode = self._evidence_mode(
            needs_history=needs_history,
            needs_fresh_data=needs_fresh_data,
        ) if understanding else ReplyEvidenceMode.ADAPTIVE
        preferred_tools: list[str] = []
        if needs_history:
            preferred_tools.extend(["search_messages", "fetch_source"])

        is_question = understanding.is_question if understanding else self._is_question(relevant_text)
        response_mode = (
            ReplyMode.DIRECT_ANSWER if is_question else ReplyMode.ACKNOWLEDGE_AND_CONTINUE
        )
        if session.chat_type == ChatType.GROUP:
            response_mode = (
                ReplyMode.GROUP_FORCED_REPLY
                if bool(decision.score_detail.get("forced"))
                else ReplyMode.GROUP_CONCISE_PARTICIPATION
            )

        return ReplyPlan(
            objective="回应目标消息的主要意图，并保持与当前会话连续；不要改为回应批次中的其他人。",
            response_mode=response_mode,
            target_message_id=target.id,
            address_sender_id=target.sender_id,
            relevant_message_ids=tuple(item.id for item in relevant),
            target_reason=self._target_reason(
                session=session,
                decision=decision,
                target=target,
            ),
            evidence_mode=evidence_mode,
            evidence_policy=self._evidence_policy(evidence_mode),
            preferred_tools=tuple(dict.fromkeys(preferred_tools)),
            ask_follow_up=understanding.ambiguous if understanding else False,
            max_visible_messages=2 if session.chat_type == ChatType.GROUP else 6,
            max_total_chars=1600 if session.chat_type == ChatType.GROUP else 12_000,
        )

    def restore(
        self,
        *,
        payload: object,
        session: SessionView,
        decision: TurnDecision,
        pending: list[StoredMessage],
    ) -> ReplyPlan:
        """读取冻结计划；为明确识别的旧 Turn 从原始快照重建 v2 运行时投影。"""

        if payload is None:
            return self.plan(session=session, decision=decision, pending=pending)
        if not isinstance(payload, Mapping):
            raise InputValidationError("已持久化的 ReplyPlan 必须是对象")
        schema_version = payload.get("schema_version")
        if schema_version is None and self._LEGACY_PLAN_FIELDS.issubset(payload):
            return self.plan(session=session, decision=decision, pending=pending)
        if schema_version != 2:
            raise InputValidationError("已持久化的 ReplyPlan 版本未知或内容损坏")
        try:
            plan = ReplyPlan.model_validate(payload)
        except ValidationError as exc:
            raise InputValidationError("已持久化的 ReplyPlan 不符合 v2 契约") from exc
        self._validate_restored_plan(
            plan=plan,
            session=session,
            decision=decision,
            pending=pending,
        )
        return plan

    @staticmethod
    def _validate_inputs(
        *,
        session: SessionView,
        decision: TurnDecision,
        pending: list[StoredMessage],
    ) -> None:
        """在规划边界验证 Turn、Session 和消息快照的一致性。"""

        if decision.action != DecisionAction.REPLY:
            raise InputValidationError("只有回复决策可以生成 ReplyPlan")
        if decision.session_id != session.id:
            raise InputValidationError("Turn 决策与 Session 不一致")
        if not pending:
            raise InputValidationError("ReplyPlan 缺少待处理消息")
        if any(item.session_id != session.id for item in pending):
            raise InputValidationError("ReplyPlan 消息快照包含其他 Session 的消息")

    @staticmethod
    def _resolve_target(
        *,
        decision: TurnDecision,
        pending: list[StoredMessage],
    ) -> StoredMessage:
        """优先使用策略层冻结的触发消息，禁止退化为批次最后发送者。"""

        if decision.trigger_message_id is None:
            return pending[-1]
        target = next(
            (item for item in pending if item.id == decision.trigger_message_id),
            None,
        )
        if target is None:
            raise InputValidationError("Turn 触发消息不在待处理消息快照中")
        return target

    @staticmethod
    def _select_relevant_messages(
        *,
        session: SessionView,
        target: StoredMessage,
        pending: list[StoredMessage],
        decision: TurnDecision,
    ) -> list[StoredMessage]:
        """群聊沿用策略冻结的对话；旧 Turn 沿用原有发送者快照语义。"""

        conversation_ids = decision.score_detail.get("conversation_message_ids")
        if session.chat_type == ChatType.GROUP and conversation_ids is not None:
            if (
                not isinstance(conversation_ids, list)
                or not all(isinstance(item, str) for item in conversation_ids)
                or target.id not in conversation_ids
                or not set(conversation_ids).issubset({item.id for item in pending})
            ):
                raise InputValidationError("群聊对话候选与 Turn 快照不一致")
            candidates = [item for item in pending if item.id in conversation_ids]
        else:
            candidates = (
                pending
                if session.chat_type == ChatType.PRIVATE
                else [item for item in pending if item.sender_id == target.sender_id]
            )
        if len(candidates) <= 20:
            return candidates
        latest = candidates[-19:]
        if target in latest:
            return candidates[-20:]
        return [target, *latest]

    @classmethod
    def _target_reason(
        cls,
        *,
        session: SessionView,
        decision: TurnDecision,
        target: StoredMessage,
    ) -> ReplyTargetReason:
        if session.chat_type == ChatType.GROUP and decision.score_detail.get("forced"):
            if any(
                item.type == ComponentType.MENTION and item.target_id == "agent"
                for item in target.components
            ):
                return ReplyTargetReason.DIRECT_MENTION
            if any(
                item.type == ComponentType.QUOTE and item.target_id == "agent"
                for item in target.components
            ):
                return ReplyTargetReason.DIRECT_QUOTE
        if cls._is_question(target.plain_text):
            return ReplyTargetReason.QUESTION
        if cls._REQUEST_SIGNAL.search(target.plain_text):
            return ReplyTargetReason.REQUEST
        return ReplyTargetReason.LATEST_PENDING

    @staticmethod
    def _is_question(text: str) -> bool:
        return bool(
            re.search(r"[？?]\s*$", text)
            or re.search(r"(怎么|如何|为什么|有没有|吗|呢)", text)
        )

    @staticmethod
    def _evidence_mode(
        *,
        needs_history: bool,
        needs_fresh_data: bool,
    ) -> ReplyEvidenceMode:
        if needs_history and needs_fresh_data:
            return ReplyEvidenceMode.HISTORY_AND_FRESH_TOOL_REQUIRED
        if needs_history:
            return ReplyEvidenceMode.HISTORY_SOURCE_REQUIRED
        if needs_fresh_data:
            return ReplyEvidenceMode.FRESH_TOOL_REQUIRED
        return ReplyEvidenceMode.CONVERSATION_ONLY

    @staticmethod
    def _evidence_policy(mode: ReplyEvidenceMode) -> str:
        policies = {
            ReplyEvidenceMode.ADAPTIVE: (
                "先判断回答真正缺少哪项事实。当前对话能回答就直接回应；"
                "只有缺失的历史证据才搜索并精确回源，只有外部当前状态才用实时工具核验。"
                "情绪分享和已给出上下文的延续不因出现日期、之前等词而查询。"
                "指代存在多个可能对象时先澄清；没有核验能力时明确说明。"
            ),
            ReplyEvidenceMode.CONVERSATION_ONLY: (
                "只使用冻结消息快照、当前画像和已召回记忆；不确定时明确说明。"
            ),
            ReplyEvidenceMode.HISTORY_SOURCE_REQUIRED: (
                "涉及历史原话时必须先搜索并精确回源，不把记忆摘要冒充原话。"
            ),
            ReplyEvidenceMode.FRESH_TOOL_REQUIRED: (
                "涉及时效状态时优先使用本轮已开放的实时工具核验；没有对应工具时明确说明无法核验。"
            ),
            ReplyEvidenceMode.HISTORY_AND_FRESH_TOOL_REQUIRED: (
                "历史原话必须搜索并精确回源，时效状态必须使用本轮已开放的实时工具核验。"
            ),
        }
        return policies[mode]

    @staticmethod
    def _validate_restored_plan(
        *,
        plan: ReplyPlan,
        session: SessionView,
        decision: TurnDecision,
        pending: list[StoredMessage],
    ) -> None:
        """拒绝被篡改或与原 Turn 快照漂移的持久化计划。"""

        ReplyPlanner._validate_inputs(
            session=session,
            decision=decision,
            pending=pending,
        )
        messages_by_id = {item.id: item for item in pending}
        target = messages_by_id.get(plan.target_message_id)
        if target is None:
            raise InputValidationError("ReplyPlan 目标消息不属于原 Turn")
        if decision.trigger_message_id and plan.target_message_id != decision.trigger_message_id:
            raise InputValidationError("ReplyPlan 目标消息与策略触发消息不一致")
        if target.sender_id != plan.address_sender_id:
            raise InputValidationError("ReplyPlan 回复对象与目标消息发送者不一致")
        if any(message_id not in messages_by_id for message_id in plan.relevant_message_ids):
            raise InputValidationError("ReplyPlan 相关消息越出原 Turn 快照")
        if session.chat_type == ChatType.GROUP:
            conversation_ids = decision.score_detail.get("conversation_message_ids")
            if conversation_ids is not None:
                expected = ReplyPlanner._select_relevant_messages(
                    session=session, target=target, pending=pending, decision=decision
                )
                if plan.relevant_message_ids != tuple(item.id for item in expected):
                    raise InputValidationError("群聊 ReplyPlan 偏离冻结的对话候选")
            elif any(
                messages_by_id[message_id].sender_id != plan.address_sender_id
                for message_id in plan.relevant_message_ids
            ):
                # 已持久化的旧 v2 计划仍可能被人工重试，保留其原始证据边界。
                raise InputValidationError("群聊 ReplyPlan 混入了其他成员的消息")
