# QQ 插件

本插件通过腾讯 **QQ 机器人开放平台**把 iJA 接入 QQ 单聊和群聊。它不会登录、托管或模拟操作普通 QQ 个人账号，也不使用 OneBot、NTQQ Hook、NapCat 等非官方账号协议。

## 合规接入结论

QQ 官方允许机器人被添加到群聊/频道，也允许用户与机器人单聊。创建机器人后会获得 `AppID` 和 `AppSecret`；服务端用二者换取 Access Token，通过 WebSocket Gateway 接收事件，并通过 HTTPS OpenAPI 回复消息。腾讯还明确提供了第三方 Agent 扫码连接 SDK，但本插件当前采用手动配置凭据，避免额外引入 Node.js 配对进程。

机器人上线前仍需按开放平台要求完成主体信息、功能说明、内容安全与发布审核。全量群消息只有在机器人获得相应能力、且群主允许接收全部消息时才会推送；没有该权限时仍可通过群内 `@机器人` 触发。

## 配置

先在 [QQ 开放平台](https://q.qq.com/) 创建机器人。不要把真实密钥写入仓库；推荐使用进程环境变量：

```powershell
$env:IJA_PLATFORM_PLUGINS = "qq"
$env:IJA_QQ_APP_ID = "机器人 AppID"
$env:IJA_QQ_APP_SECRET = "机器人 AppSecret"
uv run ija
```

也可以在被 `.gitignore` 排除的 `config/local.toml` 中启用插件，并只把非敏感选项放在这里：

```toml
[platform_plugins]
enabled = ["qq"]

[platform_plugins.options.qq]
intents = 33554432 # GROUP_AND_C2C_EVENT (1 << 25)
request_timeout_seconds = 20
gateway_open_timeout_seconds = 15
max_reconnect_attempts = 8
```

`AppSecret` 只用于服务端换取 Access Token。插件不会把凭据写入日志、数据库、浏览器或消息历史。

## 运行行为

- 启动时向官方接口换取 Access Token，获取 WSS 地址并建立 Gateway 连接。
- 订阅 `C2C_MESSAGE_CREATE`、`GROUP_AT_MESSAGE_CREATE` 和已授权时的 `GROUP_MESSAGE_CREATE`。
- 使用 `msg_id + msg_idx` 形成幂等入站标识；Gateway 重连补发不会重复入库。
- 响应式回复优先携带原消息 `msg_id`，周期任务和主动消息则遵守 QQ 用户开关、频控与审核规则。
- Gateway 会保存 Session 与序列号并优先 Resume；连续 8 次重连失败后明确离线，不无限静默重试。
- QQ OpenAPI 明确返回错误码时记录 `FAILED`；请求发出后无法确认结果时记录 `UNKNOWN`，不会盲目重发。

当前版本只发送文本。收到的图片、语音或文件会以类型占位文本进入上下文；出站图片会明确失败，避免把本地路径或未上传素材伪装成发送成功。频道（Guild）接入不在本阶段范围。

## 官方文档

- [启动接入与 AppID/AppSecret](https://bot.q.qq.com/wiki/develop/api-v2/)
- [第三方 Agent 接入](https://bot.q.qq.com/wiki/agent-qqbot/)
- [获取 Access Token](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html)
- [WebSocket Gateway](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html)
- [事件订阅 Intents](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/payload.html)
- [消息收发、时效与频控](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)
- [QQ 机器人运营规范](https://bot.q.qq.com/wiki/business/)

