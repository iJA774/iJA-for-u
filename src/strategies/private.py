"""私聊默认回复策略。"""

from domain.models import DecisionAction, StoredMessage, TurnDecision


class PrivateChatStrategy:
    """合法私聊消息始终进入生成，不使用群聊发言概率。"""

    def decide(self, session_id: str, pending: list[StoredMessage]) -> TurnDecision:
        return TurnDecision(
            session_id=session_id,
            action=DecisionAction.REPLY,
            strategy="private_always_reply",
            score=100,
            threshold=1,
            reason=f"私聊默认回复，合并 {len(pending)} 条连续消息",
            score_detail={"pending_count": len(pending), "debounced": len(pending) > 1},
            trigger_message_id=pending[-1].id,
        )
