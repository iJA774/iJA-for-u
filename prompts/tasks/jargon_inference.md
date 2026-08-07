# 黑话语义推断任务

你只负责依据候选词条的真实证据上下文推断含义，不生成聊天回复。

- 判断它是否确实是黑话；普通词或证据显示为人名时应拒绝。
- `meaning` 要说明当前语境中的具体含义和适用场景，不能只复述词条。
- 证据不足时保留候选：`status` 使用 `candidate`，`meaning` 留空，置信度应低。
- 确认为黑话且含义足够明确时使用 `active`；确认为非黑话时使用 `rejected`。
- 不把助手先前的猜测当作可靠定义。

只输出 JSON 对象：

```json
{
  "inferences": [
    {
      "jargon_id": "jargon_x",
      "status": "active",
      "meaning": "当前语境下的含义与使用场景",
      "confidence": 0.82
    }
  ]
}
```
