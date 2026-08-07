# OneBot 插件

除 OneBot 11 标准消息事件外，本插件还支持 NapCat 的
`notice.notify.input_status` 输入状态扩展，并将其作为独立 typing 事件交给
入站插件管线。typing 不会写入聊天历史；非白名单用户以及未配置群的事件
会在协议边界被过滤。

OneBot 清单声明依赖独立的 `delayed_reply` 入站插件，因此即使启动环境只写
`IJA_PLATFORM_PLUGINS=onebot`，宿主也会先加载私聊延迟回复插件。显式同时
列出两个插件同样有效，宿主会去重。

本插件通过 **OneBot 11 协议**把 iJA 接入 QQ 单聊和群聊。它自身不登录 QQ 账号，也不实现 OneBot 协议；账号登录、消息收发与协议实现由外部 OneBot 服务（如 NapCat、Lagrange.OneBot、go-cqhttp）承担，本插件只作为正向 WebSocket 客户端连接该服务并做消息适配。

## 与官方 QQ 插件的区别

`plugins/qq` 走腾讯 QQ 机器人开放平台（AppID/AppSecret + WebSocket Gateway + OpenAPI），是合规接入，但只支持文本，且使用匿名化 `openid`。

`plugins/onebot` 走 OneBot 协议，能力更完整（图片/文件/表情/引用/@ 等富消息），但依赖非官方的 OneBot 实现，登录的是普通 QQ 个人账号，存在账号风险与合规约束。两个插件 `platform` 不同（`onebot` vs `qq`），会话彼此隔离，可按需择一启用。

## 富消息支持边界

受当前核心 `ComponentType`（`text`/`mention`/`quote`/`image_ref`）约束：

- **入站**：解析 OneBot 全部常见消息段。`text`→文本组件、`at`→@ 组件、`reply`→记录回复目标；`image` 和带真实资源的 `mface` 会即时取回、校验并保存为 `IMAGE_REF`，`face`/`record`/`file` 保留语义文本占位。
- **出站**：文本→`text` 段、@→`at` 段、引用→`reply` 段、图片组件（`IMAGE_REF`）→`image` 段（读取受控 `storage_path` 转 base64 发送）。聊天收集或角色生成的表情由 `send-expression` Skill 发布成图片组件，因此可以主动发送；不会伪造 OneBot 原生商城表情 ID。语音和文件没有对应核心出站组件来源。

图片资源优先使用 NapCat 事件中的 URL 或内联数据，并兼容调用 OneBot `get_image(file)`。插件没有任意文件写权限：下载后的字节交给宿主 `AttachmentStore` 做大小、MIME 魔数、SHA-256、受控路径和原子写入。下载失败时保留 `[图片读取失败]`，不会把可能包含签名参数的临时 URL 写入聊天历史。被引用消息可能尚未入库，因此入站引用仍不构造会触发校验失败的 `QUOTE` 组件，仅保留 reply id 供出站使用。

## 配置

OneBot 服务通常部署在本机或内网。推荐使用进程环境变量，不要把凭据写入仓库：

```powershell
$env:IJA_PLATFORM_PLUGINS = "delayed_reply,onebot"
$env:IJA_ONEBOT_WS_URL = "ws://127.0.0.1:3001"
$env:IJA_ONEBOT_HTTP_URL = "http://127.0.0.1:3000"
$env:IJA_ONEBOT_ACCESS_TOKEN = "OneBot 服务配置的 access_token"
$env:IJA_ONEBOT_BOT_UIN = "机器人 QQ 号"
uv run ija
```

也可以在被 `.gitignore` 排除的 `config/local.toml` 中启用并配置非敏感选项：

```toml
[platform_plugins]
enabled = ["onebot"]

[platform_plugins.options.onebot]
ws_url = "ws://127.0.0.1:3001"
http_base_url = "http://127.0.0.1:3000"
bot_uin = "123456"
max_reconnect_attempts = 8
max_inbound_images = 4
max_image_bytes = 10485760
# 默认已允许常见 QQ 图片域名；自建兼容端点需要时可显式追加。
image_allowed_host_suffixes = [".qpic.cn", ".qq.com", ".qq.com.cn", ".gtimg.cn"]

[[platform_plugins.options.onebot.groups]]
group_id = "987654321"
require_at = true
allow_from = ["111111", "222222"]

# 群管理是高风险能力，只有精确 principal 获得 scope 后才会开放。
[[authorization.workspace_administrators]]
platform = "onebot"
account_id = "123456" # 必须与 bot_uin 一致
actor_id = "111111"   # 被授权的群主或管理员 QQ 号
scopes = ["channel:qq-group:manage"]
```

## QQ 群管理 Skill

项目内置 `skills/qq-group-management`，通过本插件调用 NapCat 的 OneBot 11 HTTP API，支持：

- 撤回当前群权威历史中的精确消息；
- 禁言或解除禁言指定成员；
- 开启或关闭全员禁言；
- 移出指定成员。

每次操作同时要求：当前发起者命中上方精确 principal、NapCat 实时确认发起者是群主或管理员、机器人实时角色足以管理目标。任一方只是普通成员时，本轮不会向模型暴露该 Skill；加载与执行阶段仍会再次校验，防止枚举后的角色变化。宿主不会信任本地会话为了维持领域不变量而临时推定的 `owner` 角色。成员目标应来自 `@`，撤回目标应来自引用或当前会话历史；Skill 不接受任意群号或直接外部消息 ID。单轮只允许一次管理副作用，写请求连接中断时不会自动重试。

## 运行行为

- 启动时作为客户端连接 OneBot 正向 WebSocket，订阅 `message` 事件，过滤自身消息。
- 私聊：`allow_from` 为空表示允许所有私聊；非空时仅放行白名单用户。
- 群聊：必须在 `groups` 白名单内才会处理；`require_at=true` 时需 `@机器人` 才触发，群成员 `allow_from` 非空时仅放行白名单成员。
- 入站 `reply` 段的外部 message_id 与被引用 id 一并编码进 `external_message_id`，出站回复时取出作为 OneBot `reply` 段，保证引用语义闭环。
- 当前 NapCat 会把商城表情以带子类型的 `image` 段上报；插件也兼容直接上报的 `mface` 段。带真实图片资源的表情会自动按内容去重并收进当前聊天人格图库；内置 `face` 没有真实图片资源，仍仅以表情 ID/摘要进入上下文。
- 聊天模型的“视觉输入”默认关闭；开启后可由主 LLM 直接读取最近最多 4 张已核验图片，或配置独立视觉模型只向主 LLM 提供派生描述。表情收集会优先使用同一视觉能力理解名称、情绪和用途。
- WebSocket 中断后指数退避重连，连续超过 `max_reconnect_attempts` 次失败后明确离线，不无限静默重试。
- OneBot API 返回非零 `retcode` 或 HTTP 错误时记录 `FAILED`；响应不是有效 JSON 时抛出 `RuntimeError`，不假装成功。

## 安全提示

OneBot 实现登录的是真实 QQ 个人号，具备发送任意消息、读取群成员等能力。请仅在本机或可信内网部署，配置 `access_token` 鉴权，并使用 `allow_from`/`groups` 最小化放行范围。`access_token` 仅用于服务端鉴权，不会写入日志、数据库或消息历史。

## 官方文档

- [OneBot 11 标准](https://github.com/botuniverse/onebot-11)
- [NapCat](https://napneko.github.io/)
- [Lagrange.OneBot](https://lagrangedev.github.io/Lagrange.OneBot/)
