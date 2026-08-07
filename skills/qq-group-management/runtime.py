"""QQ 群管理 Skill 的可移植运行时入口。"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

SKILL_NAME = "qq-group-management"
CAPABILITY_NAME = "qq-group-management"
MANAGE_SCOPE = "channel:qq-group:manage"
MAX_MUTE_SECONDS = 30 * 24 * 60 * 60
_TURN_GUARD = "qq-group-management:side-effect"


class QQGroupManagementHost(Protocol):
    """宿主提供无策略的数据解析和 OneBot 动作桥接。"""

    async def is_available(self) -> bool: ...

    async def get_current_group(self, *, session_id: str) -> dict[str, str]: ...

    async def inspect_member(
        self,
        *,
        session_id: str,
        user_id: str,
    ) -> dict[str, str]: ...

    async def resolve_recallable_message(
        self,
        *,
        session_id: str,
        message_id: str,
    ) -> dict[str, str] | None: ...

    async def recall_message(
        self,
        *,
        session_id: str,
        external_message_id: str,
    ) -> dict[str, Any]: ...

    async def set_member_mute(
        self,
        *,
        session_id: str,
        target_user_id: str,
        duration_seconds: int,
    ) -> dict[str, Any]: ...

    async def set_whole_mute(
        self,
        *,
        session_id: str,
        enable: bool,
    ) -> dict[str, Any]: ...

    async def kick_member(
        self,
        *,
        session_id: str,
        target_user_id: str,
        reject_add_request: bool,
    ) -> dict[str, Any]: ...


class _StrictArguments(BaseModel):
    """禁止模型夹带未声明字段。"""

    model_config = ConfigDict(extra="forbid")


class RecallMessageArguments(_StrictArguments):
    """撤回当前群内的一条权威历史消息。"""

    message_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=300)


class SetMemberMuteArguments(_StrictArguments):
    """设置单个成员的禁言时长；零表示解除。"""

    target_user_id: str = Field(pattern=r"^[1-9][0-9]{4,19}$")
    duration_seconds: int = Field(ge=0, le=MAX_MUTE_SECONDS)
    reason: str = Field(min_length=1, max_length=300)


class SetWholeMuteArguments(_StrictArguments):
    """开启或关闭当前群的全员禁言。"""

    enable: bool
    reason: str = Field(min_length=1, max_length=300)


class KickMemberArguments(_StrictArguments):
    """移出成员需要显式确认短语。"""

    target_user_id: str = Field(pattern=r"^[1-9][0-9]{4,19}$")
    reject_add_request: bool = False
    confirmation: Literal["确认移出"]
    reason: str = Field(min_length=1, max_length=300)


@dataclass(frozen=True, slots=True)
class PortableTool:
    """由宿主通用装载器读取的工具描述。"""

    name: str
    description: str
    arguments_model: type[BaseModel]
    handler: Callable[[BaseModel, Any], Awaitable[dict[str, Any]]]
    terminal: bool = False
    required_scopes: frozenset[str] = frozenset({MANAGE_SCOPE})


@dataclass(frozen=True, slots=True)
class _ManagementContext:
    """Skill 在一次工具调用前重新取得的实时权限快照。"""

    session_id: str
    group_id: str
    actor_id: str
    bot_id: str
    actor_role: str
    bot_role: str


class QQGroupManagementPlugin:
    """拥有工具契约、实时授权、目标层级和单轮副作用闸门。"""

    name = SKILL_NAME
    load_required_scopes = frozenset({MANAGE_SCOPE})

    def __init__(self, host: QQGroupManagementHost) -> None:
        self.host = host

    async def is_available(self) -> bool:
        """能力枚举只检查宿主路由，不在这里查询或缓存群权限。"""

        return await self.host.is_available()

    async def load(self, context: Any) -> dict[str, Any]:
        """加载时即时验证双方角色；普通成员不能加载本 Skill。"""

        state = await self._authorize(context)
        return {
            "ready": True,
            "platform": "onebot",
            "group_id": state.group_id,
            "actor_role": state.actor_role,
            "bot_role": state.bot_role,
            "max_actions_this_turn": 1,
        }

    def tools(self) -> list[PortableTool]:
        return [
            PortableTool(
                name="recall_qq_group_message",
                description=(
                    "撤回当前 QQ 群中一条已精确定位的权威历史消息。message_id 必须来自当前会话，"
                    "不能直接填写 NapCat 外部消息 ID；仅当 iJA 与请求者均为群主或管理员时可用，"
                    "每轮只能执行一次群管理副作用。"
                ),
                arguments_model=RecallMessageArguments,
                handler=self._recall_message,
            ),
            PortableTool(
                name="set_qq_group_member_mute",
                description=(
                    "禁言或解除禁言当前 QQ 群的指定成员；duration_seconds=0 表示解除，"
                    "正数最大 2592000 秒。仅当 iJA 与请求者均为群主或管理员时可用，"
                    "且必须由用户明确给出成员和时长。"
                ),
                arguments_model=SetMemberMuteArguments,
                handler=self._set_member_mute,
            ),
            PortableTool(
                name="set_qq_group_whole_mute",
                description=(
                    "开启或关闭当前 QQ 群全员禁言。仅当 iJA 与请求者均为群主或管理员时可用；"
                    "只在用户明确要求全员禁言或解除时调用。"
                ),
                arguments_model=SetWholeMuteArguments,
                handler=self._set_whole_mute,
            ),
            PortableTool(
                name="kick_qq_group_member",
                description=(
                    "把指定成员移出当前 QQ 群。仅在用户明确要求踢出目标后调用，"
                    "且 iJA 与请求者均为群主或管理员时可用；confirmation 必须为“确认移出”；"
                    "这是难恢复的高风险操作。"
                ),
                arguments_model=KickMemberArguments,
                handler=self._kick_member,
            ),
        ]

    @staticmethod
    def _claim_turn(context: Any) -> None:
        """在外部调用前占用本轮额度，未知结果时也禁止自动重试。"""

        attempted = context.attempted_terminal_tools
        if _TURN_GUARD in attempted:
            raise ValueError("本轮已经尝试过一次 QQ 群管理操作，请先核对群状态")
        attempted.add(_TURN_GUARD)

    async def _recall_message(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        args = RecallMessageArguments.model_validate(arguments)
        state = await self._authorize(context)
        message_id = args.message_id.removeprefix("message:").strip()
        message = await self.host.resolve_recallable_message(
            session_id=state.session_id,
            message_id=message_id,
        )
        if message is None:
            raise ValueError("当前群可召回窗口中不存在该消息")
        if str(message.get("message_id") or "") != message_id:
            raise RuntimeError("宿主返回的消息与当前会话查询目标不一致")
        external_message_id = str(message.get("external_message_id") or "")
        if not external_message_id:
            raise ValueError("该消息缺少 OneBot 外部消息 ID，无法撤回")
        await self._authorize_message_target(state, message)
        self._claim_turn(context)
        self._require_success(
            await self.host.recall_message(
                session_id=state.session_id,
                external_message_id=external_message_id,
            ),
            "撤回消息",
        )
        return {
            "value": {
                "success": True,
                "action": "recall_message",
                "group_id": state.group_id,
                "message_id": str(message.get("message_id") or message_id),
            }
        }

    async def _set_member_mute(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        args = SetMemberMuteArguments.model_validate(arguments)
        state = await self._authorize(context)
        target_user_id = self._validate_qq_id(args.target_user_id, "目标成员")
        target = await self._authorize_member_target(state, target_user_id)
        self._claim_turn(context)
        self._require_success(
            await self.host.set_member_mute(
                session_id=state.session_id,
                target_user_id=target_user_id,
                duration_seconds=args.duration_seconds,
            ),
            "设置成员禁言",
        )
        return {
            "value": {
                "success": True,
                "action": (
                    "unmute_member" if args.duration_seconds == 0 else "mute_member"
                ),
                "group_id": state.group_id,
                "target_user_id": target_user_id,
                "target_display_name": target["display_name"],
                "duration_seconds": args.duration_seconds,
            }
        }

    async def _set_whole_mute(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        args = SetWholeMuteArguments.model_validate(arguments)
        state = await self._authorize(context)
        self._claim_turn(context)
        self._require_success(
            await self.host.set_whole_mute(
                session_id=state.session_id,
                enable=args.enable,
            ),
            "设置全员禁言",
        )
        return {
            "value": {
                "success": True,
                "action": (
                    "enable_whole_mute" if args.enable else "disable_whole_mute"
                ),
                "group_id": state.group_id,
                "enable": args.enable,
            }
        }

    async def _kick_member(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        args = KickMemberArguments.model_validate(arguments)
        state = await self._authorize(context)
        target_user_id = self._validate_qq_id(args.target_user_id, "目标成员")
        target = await self._authorize_member_target(state, target_user_id)
        self._claim_turn(context)
        self._require_success(
            await self.host.kick_member(
                session_id=state.session_id,
                target_user_id=target_user_id,
                reject_add_request=args.reject_add_request,
            ),
            "移出成员",
        )
        return {
            "value": {
                "success": True,
                "action": "kick_member",
                "group_id": state.group_id,
                "target_user_id": target_user_id,
                "target_display_name": target["display_name"],
                "reject_add_request": args.reject_add_request,
            }
        }

    async def _authorize(self, context: Any) -> _ManagementContext:
        """每次加载或执行前实时验证 scope、双方角色和当前群边界。"""

        scopes = getattr(context, "authorization_scopes", frozenset())
        if MANAGE_SCOPE not in scopes:
            raise ValueError(f"当前身份缺少 QQ 群管理授权 scope: {MANAGE_SCOPE}")
        session_id = str(getattr(context, "session_id", "") or "")
        actor_id = str(getattr(context, "actor_id", "") or "")
        if not session_id or not actor_id:
            raise ValueError("QQ 群管理只能由当前消息发送者发起")
        actor_id = self._validate_qq_id(actor_id, "当前发起者")
        group = await self.host.get_current_group(session_id=session_id)
        if str(group.get("session_id") or "") != session_id:
            raise RuntimeError("宿主返回的会话与当前工具上下文不一致")
        if str(group.get("platform") or "") != "onebot":
            raise ValueError("QQ 群管理只能用于当前 OneBot 群会话")
        group_id = self._validate_qq_id(str(group.get("group_id") or ""), "当前群")
        bot_id = self._validate_qq_id(str(group.get("bot_id") or ""), "机器人")
        actor = await self._member_info(session_id, actor_id)
        bot = await self._member_info(session_id, bot_id)
        actor_role = actor["role"]
        bot_role = bot["role"]
        if actor_role not in {"owner", "admin"}:
            raise ValueError("当前发起者不是 QQ 群主或管理员，不能使用本 Skill")
        if bot_role not in {"owner", "admin"}:
            raise ValueError("iJA 不是 QQ 群主或管理员，不能使用本 Skill")
        return _ManagementContext(
            session_id=session_id,
            group_id=group_id,
            actor_id=actor_id,
            bot_id=bot_id,
            actor_role=actor_role,
            bot_role=bot_role,
        )

    async def _authorize_member_target(
        self,
        state: _ManagementContext,
        target_user_id: str,
    ) -> dict[str, str]:
        if target_user_id == state.actor_id:
            raise ValueError("不得通过群管理工具操作发起者本人")
        if target_user_id == state.bot_id:
            raise ValueError("不得通过群管理工具操作 iJA 自身")
        target = await self._member_info(state.session_id, target_user_id)
        self._ensure_outranks(state.actor_role, target["role"], "发起者")
        self._ensure_outranks(state.bot_role, target["role"], "iJA")
        return target

    async def _authorize_message_target(
        self,
        state: _ManagementContext,
        message: dict[str, str],
    ) -> None:
        sender_id = str(message.get("sender_id") or "")
        if sender_id == "agent":
            return
        sender_id = self._validate_qq_id(sender_id, "消息发送者")
        target = await self._member_info(state.session_id, sender_id)
        if sender_id != state.actor_id:
            self._ensure_outranks(state.actor_role, target["role"], "发起者")
        self._ensure_outranks(state.bot_role, target["role"], "iJA")

    async def _member_info(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, str]:
        result = await self.host.inspect_member(
            session_id=session_id,
            user_id=user_id,
        )
        returned_user_id = str(result.get("user_id") or "")
        if returned_user_id != user_id:
            raise RuntimeError("宿主返回的 QQ 群成员与查询目标不一致")
        role = str(result.get("role") or "")
        if role not in {"owner", "admin", "member"}:
            raise RuntimeError("宿主返回了无效的 QQ 群成员角色")
        return {
            "user_id": returned_user_id,
            "display_name": str(
                result.get("display_name") or f"用户{user_id[-6:]}"
            ),
            "role": role,
        }

    @staticmethod
    def _validate_qq_id(value: str, label: str) -> str:
        normalized = str(value).strip()
        if re.fullmatch(r"[1-9][0-9]{4,19}", normalized) is None:
            raise ValueError(f"{label} QQ 号无效")
        return normalized

    @staticmethod
    def _ensure_outranks(
        controller_role: str,
        target_role: str,
        controller_label: str,
    ) -> None:
        ranks = {"member": 1, "admin": 2, "owner": 3}
        if ranks.get(controller_role, 0) <= ranks.get(target_role, 0):
            raise ValueError(
                f"{controller_label}权限不足，不能管理同级或更高权限成员"
            )

    @staticmethod
    def _require_success(result: dict[str, Any], label: str) -> None:
        if not isinstance(result, dict) or result.get("success") is not True:
            raise RuntimeError(f"{label}未返回明确成功结果")


def create_plugin(
    capabilities: dict[str, Any],
) -> QQGroupManagementPlugin:
    """创建插件；宿主只需提供声明的稳定 capability。"""

    try:
        host = capabilities[CAPABILITY_NAME]
    except KeyError as exc:
        raise RuntimeError(f"宿主缺少能力: {CAPABILITY_NAME}") from exc
    return QQGroupManagementPlugin(host)
