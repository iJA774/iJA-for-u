# 宿主接入协议

`local-blacklist` 是 API v1 运行时插件。宿主读取 `runtime.toml`，动态调用 `runtime.py:create_plugin`，不得导入项目业务模块。

## 依赖

- Python 3.12+
- `pydantic`

## local-blacklist capability

工厂接收 `dict[str, object]`，其中必须存在 `local-blacklist`。该对象提供以下异步方法：

- `is_available() -> bool`：当前轮是否可开放 Skill；拉黑是本地副作用，通常恒为 `True`。
- `block_current_user(*, context, reason, caption) -> dict`：按当前 Turn 上下文拉黑发送者，返回 `{"value": {...}, "reply_draft": ReplyDraft}`。`context` 必须提供 `session_id` 与 `actor_id`（当前消息发送者）。

宿主拥有数据库、会话与 Channel；Skill 拥有工具名称、参数 schema、触发工作流与边界。能力对象不得让 Skill 获得任意数据库连接或文件系统根目录。

## 轮次上下文

通用装载器传给插件的 context 必须提供：

- `loaded_skills: set[str]`：`load()` 成功后由装载器加入 Skill 名称。
- `attempted_terminal_tools: set[str]`：限制同一轮重复调用终态工具。

## 插件结果

工具处理函数返回：

```json
{
  "value": {"blocked": true, "external_user_id": "...", "display_name": "..."},
  "reply_draft": "<宿主草稿对象>"
}
```

通用装载器负责把 `value` 写入工具审计，并把 `reply_draft` 交回宿主消息链路作为本轮最终回复。
