# 宿主接入协议

`qq-group-management` 是 API v1 运行时插件。宿主读取 `runtime.toml`，调用
`runtime.py:create_plugin`；Skill 不导入宿主业务模块或具体 Channel。

## 依赖

- Python 3.12+
- `pydantic`
- 当前会话对应的 OneBot 11/NapCat Channel

## Capability

工厂接收的能力映射必须包含 `qq-group-management`。Capability 只负责解析宿主
权威对象和转发 OneBot 动作，不得实现或缓存 Skill 权限策略：

- `is_available() -> bool`
- `get_current_group(*, session_id) -> dict`
- `inspect_member(*, session_id, user_id) -> dict`
- `resolve_recallable_message(*, session_id, message_id) -> dict | None`
- `recall_message(*, session_id, external_message_id) -> dict`
- `set_member_mute(*, session_id, target_user_id, duration_seconds) -> dict`
- `set_whole_mute(*, session_id, enable) -> dict`
- `kick_member(*, session_id, target_user_id, reject_add_request) -> dict`

`context` 由宿主注入 Skill runtime，至少包含 `session_id`、`actor_id`、
`authorization_scopes`、`loaded_skills` 和 `attempted_terminal_tools`。模型不得
传入或覆盖这些字段。

`runtime.py` 自己拥有并执行全部授权规则：加载时检查一次，在每个写操作前再检查
一次当前群、`channel:qq-group:manage` scope、发起者实时角色、iJA 实时角色和
目标角色层级。Capability 返回的数据只是实时证据，不能代替 Skill 脚本决策。

## 权限与目标解析

Skill runtime 必须：

1. 只接受当前 `session_id` 对应的 OneBot 群会话；
2. 用 `inspect_member` 实时验证发起者、iJA 及目标角色；
3. 仅允许发起者与 iJA 都是 `owner` 或 `admin` 的调用；
4. 从当前会话的权威消息记录解析撤回目标，拒绝模型直接提供外部消息 ID；
5. 拒绝机器人自身、发起者自身及同级或更高权限目标；
6. 每轮最多尝试一个群管理副作用，结果未知时不得自动重试。

宿主桥接仍需校验输入属于当前 Session，并且只开放固定 OneBot 动作；这是数据和
协议边界，不是授权策略。HTTP 写请求连接失败后结果属于“未知”，不得自动重试。
工具审计只记录必要参数摘要，不记录 access token、完整聊天正文或 NapCat 原始响应。
