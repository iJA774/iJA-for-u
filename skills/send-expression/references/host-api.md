# 宿主接入协议

`send-expression` 是 API v1 运行时插件。宿主读取 `runtime.toml`，动态调用 `runtime.py:create_plugin`，不得导入项目业务模块。

## 依赖

- Python 3.12+
- `pydantic`
- 独立 CLI 额外需要 `httpx` 与 `Pillow`

## expression-library capability

工厂接收 `dict[str, object]`，其中必须存在 `expression-library`。该对象提供以下异步方法：

- `availability(*, context=None) -> dict`：按本轮 `context.character_id` 返回 `character_id`、`base_image_sha256`、完整 `expression_names`、带名称/情绪/描述/来源的 `expression_catalog`、`can_generate`、容量及模型状态。
- `is_available(*, context=None) -> bool`：当前轮所用人格是否可开放 Skill。
- `get_by_generation_key(key) -> asset | None`：恢复周期任务已经落库的生成素材。
- `select_for_reply(**kwargs) -> dict`：按冻结的角色/形象快照对 `query` 做本地 Top-K 检索，并可在低置信度时调用视觉复选；返回精确名称、素材 ID 和不含路径的选择审计元数据。
- `prepare_reply(**kwargs) -> reply_draft`：按已由 Skill 校验的名称、动作、提示词和画像版本执行受控存储/生成，并返回宿主可发送的回复草稿。

宿主拥有数据库、人物形象、图片 Provider、历史媒体和 Channel；Skill 拥有工具名称、参数 schema、触发工作流、名称规则、提示词规则及恢复决策。能力对象不得让 Skill 获得任意数据库连接或文件系统根目录。

## 轮次上下文

通用装载器传给插件的 context 必须提供：

- `loaded_skills: set[str]`：`load()` 成功后由装载器加入 Skill 名称。
- `loaded_skill_versions: dict[str, tuple[str, str | None]]`：插件保存角色与 base_image 快照。
- `attempted_terminal_tools: set[str]`：限制同一轮重复调用终态工具。
- `schedule_run_id: str | None`：周期生成的幂等键；普通聊天传 `None`。

这些字段只描述一次工具轮次，不是长期权威状态。宿主不得允许模型直接构造或覆盖 context。

## 插件结果

工具处理函数返回：

```json
{
  "value": {
    "prepared": true,
    "action": "select",
    "name": "开心鼓掌",
    "expression_id": "expression_xxx",
    "selection": {
      "mode": "semantic_direct",
      "visual_attempted": false,
      "visual_used": false,
      "candidate_count": 8
    }
  },
  "reply_draft": "<宿主草稿对象>"
}
```

通用装载器负责把 `value` 写入工具审计，并把 `reply_draft` 交回宿主消息链路。

## 独立运行

无需宿主时使用 `scripts/expression_cli.py`。它以文件夹作为轻量图库，实现查看、精确复用和 Images Edits 生成；详见脚本 `--help`。该路径不会访问本项目数据库、配置或 Channel。
