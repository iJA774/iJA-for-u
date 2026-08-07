# 私聊延迟回复插件

该插件只缓冲私聊入站消息，群聊始终立即放行。默认行为：

- 收到私聊消息后等待 3 秒；新消息会重新开始 3 秒计时。
- 3 秒窗口内收到同一用户的 typing 事件时，从该事件起等待 5 秒。
- 同一个 5 秒窗口内的后续 typing 不会继续延长。
- 5 秒窗口内收到消息时，回到新的 3 秒窗口。
- 超时后按原始顺序一次性向聊天核心提交本批全部消息。

可选配置：

```toml
[platform_plugins.options.delayed_reply]
message_delay_seconds = 3
typing_delay_seconds = 5
```

OneBot/NapCat 的 typing 来源是
`notice.notify.input_status` 扩展事件。插件停止时会先提交仍在缓冲的消息，
避免正常关机丢失最近一批私聊。

`onebot` 的插件清单已把本插件声明为依赖；只启用 `onebot` 时也会自动加载。
运行日志会记录每次 3 秒等待、首次 typing 触发的 5 秒等待以及最终批次大小，
但不会记录聊天正文。
