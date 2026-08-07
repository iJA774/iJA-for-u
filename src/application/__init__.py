"""聊天用例与运行时协调。"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import ChatService, IngressResult

__all__ = ["ChatService", "IngressResult"]


def __getattr__(name: str):
    """延迟暴露服务类型，避免子模块导入时形成 scheduling 循环。"""

    if name in __all__:
        from .service import ChatService, IngressResult

        return {"ChatService": ChatService, "IngressResult": IngressResult}[name]
    raise AttributeError(name)
