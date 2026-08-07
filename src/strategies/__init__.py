"""私聊与群聊的显式策略。"""

from .group import GroupChatStrategy
from .private import PrivateChatStrategy

__all__ = ["GroupChatStrategy", "PrivateChatStrategy"]
