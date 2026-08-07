# OneBot 11 / NapCat 动作映射

以下映射按 NapCat OneBot 11 HTTP API 使用，所有请求均为 `POST application/json`：

| 能力 | 动作 | 请求字段 |
|---|---|---|
| 查询实时成员角色 | `/get_group_member_info` | `group_id`, `user_id` |
| 撤回消息 | `/delete_msg` | `message_id` |
| 单成员禁言/解除 | `/set_group_ban` | `group_id`, `user_id`, `duration`；`0` 为解除 |
| 全员禁言开关 | `/set_group_whole_ban` | `group_id`, `enable` |
| 移出成员 | `/set_group_kick` | `group_id`, `user_id`, `reject_add_request` |

协议成功必须同时满足 HTTP 成功且 `retcode == 0`。成员角色只接受 `owner`、`admin`、`member`。

官方参考：

- [NapCat：获取群成员信息](https://napcat.apifox.cn/226657019e0)
- [NapCat：撤回消息](https://napcat.apifox.cn/226919954e0)
- [NapCat：群组禁言](https://napcat.apifox.cn/226656791e0)
- [NapCat：全员禁言](https://napcat.apifox.cn/226656802e0)
- [NapCat：群组踢人](https://napcat.apifox.cn/226656748e0)
