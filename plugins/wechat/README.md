# 微信插件

本插件的准确接入对象是 **微信服务号**。微信没有向普通个人微信号或普通微信群开放通用机器人 API，因此本插件不会登录个人微信，不会注入微信客户端，也不采用逆向协议、模拟点击或非官方 Web Hook。

如果产品目标必须是“个人微信好友/微信群中的 Agent”，当前没有可由本项目合法直接实现的官方路径；应改用服务号会话，或另行评估企业微信官方能力，而不是把个人账号自动化包装成插件。

## 合规接入结论

服务号可以在微信开发者平台配置“消息与事件推送”URL。微信服务器用 GET 验证回调，随后把用户消息 POST 到该 URL。官方要求开发者服务器在 5 秒内响应，超时会断开并最多重试三次。

本插件收到回调后只做验签、可选 AES 解密、幂等入库和 Agent 排队，立即返回 `success`。生成结果通过官方“发送客服消息”接口异步发给用户。用户发送消息后，当前官方规则给服务号 5 条客服消息额度，有效期 48 小时；其他交互场景有各自更小的额度和时限，平台规则仍可能调整。

## 服务号配置

1. 在微信开发者平台取得服务号 `AppID`、`AppSecret`。
2. 在“消息与事件推送”中配置：
   - URL：`https://你的域名/platform-plugins/wechat/callback`
   - Token：与 `IJA_WECHAT_TOKEN` 完全一致
   - EncodingAESKey：与 `IJA_WECHAT_ENCODING_AES_KEY` 完全一致
   - 消息加解密方式：推荐“安全模式”
3. 将反向代理的公开 HTTPS 地址转发到 iJA 的本机回环监听地址。不要把 iJA 直接暴露到公网。
4. 按微信平台要求配置 API 调用 IP 白名单，并确认服务号拥有客服消息接口权限。

推荐用环境变量保存凭据：

```powershell
$env:IJA_PLATFORM_PLUGINS = "wechat"
$env:IJA_WECHAT_APP_ID = "服务号 AppID"
$env:IJA_WECHAT_APP_SECRET = "服务号 AppSecret"
$env:IJA_WECHAT_TOKEN = "自行生成的高强度 Token"
$env:IJA_WECHAT_ENCODING_AES_KEY = "43字符 EncodingAESKey"
uv run ija
```

非敏感选项可放进被 `.gitignore` 排除的 `config/local.toml`：

```toml
[platform_plugins]
enabled = ["wechat"]

[platform_plugins.options.wechat]
message_mode = "safe" # 官方推荐；调试时可显式改为 plaintext
request_timeout_seconds = 20
```

## 安全与消息语义

- 明文模式验证 `signature`；安全模式验证 `msg_signature`，使用官方规定的 AES-CBC 消息格式解密，并校验密文尾部 AppID。
- XML 使用禁用实体展开的解析器，回调正文受宿主大小上限约束。
- `MsgId` 用作幂等键；没有 `MsgId` 的消息使用发送者、时间和内容指纹。
- access_token 只在内存缓存，提前 60 秒失效；凭据不会返回给浏览器或写入消息历史。
- 客服接口明确返回错误码时记录 `FAILED`；响应无法解析时视为结果不确定，不自动重发。
- 服务号只有单聊语义，不创建微信群会话。当前版本出站仅支持文本；图片出站会明确失败。

## 官方文档

- [消息与事件推送接入](https://developers.weixin.qq.com/doc/service/guide/dev/push/)
- [消息加解密说明](https://developers.weixin.qq.com/doc/service/guide/dev/push/encryption.html)
- [接收普通消息](https://developers.weixin.qq.com/doc/service/guide/product/message/Receiving_standard_messages.html)
- [被动回复用户消息与 5 秒规则](https://developers.weixin.qq.com/doc/service/guide/product/message/Passive_user_reply_message.html)
- [客服消息介绍与发送额度](https://developers.weixin.qq.com/doc/service/guide/product/kf/intro.html)
- [获取接口调用凭据](https://developers.weixin.qq.com/doc/service/api/base/api_getaccesstoken.html)
- [发送客服消息](https://developers.weixin.qq.com/doc/service/api/customer/message/api_sendcustommessage.html)

