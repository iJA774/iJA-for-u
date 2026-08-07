---
name: skill-creator
description: 当用户明确要求创建或改进 iJA Skill、把重复工作沉淀为带参考资料或可复用脚本的 Skill，或查看、运行、批准、拒绝此前工坊任务时使用。脚本限于 main(data) 的 JSON 输入输出契约，创建时经过静态安全扫描和真实沙箱 smoke test，安装后也只能通过工坊沙箱运行。不会自行安装；安装必须由当前用户在新消息中逐字给出工具返回的确认短语。
---

# Skill Creator

本 Skill 是 Skill 工坊的薄入口。把任务状态、Builder Worker、脚本沙箱、静态校验和原子安装交给插件提供的 `skill-builder` 托管服务。

## 创建草案

1. 确认用户明确要求创建 Skill，而不是只在讨论设计。
2. 从已完成工作的原始证据中提炼稳定的 kebab-case 名称、中文显示名、触发描述、目标和不超过 20 条工作步骤。不要把未经验证的猜测写成规则。
3. 只在重复计算或确定性转换确实值得复用时添加脚本。每个脚本：
   - 使用唯一 snake_case 名称并实现唯一的 `main(data)`。
   - 只接收和返回 JSON 值，不读文件、环境变量、网络或进程状态。
   - 只使用合成、非敏感 `smoke_input`；不传聊天原文、凭据、隐私正文或真实生产数据。
   - 只导入安全报告允许的纯计算模块。需要完整模块清单和限制时读取[安全边界](references/security-boundary.md)。
4. 根据产物类型选择原子链路：
   - 不含脚本：调用一次 `create_skill_draft`。
   - 含脚本：先调用 `begin_skill_draft`，只提交轻量元数据；取得 job ID 后调用 `add_skill_script`。单脚本保持 `finalize=true`，多脚本仅在最后一个脚本设为 `true`。
   - 已使用 `begin` 但没有脚本时，调用 `finalize_skill_draft`。
   不要把元数据和源码重新合并进 `create_skill_draft`。若预检拒绝源码，按 finding 修改设计；不要弱化规则或编码绕过。
5. 创建回合优先在有限工具轮次内完成构建并向用户交付 job ID、revision、验证结果、脚本安全证据、文件清单和确认短语。用户在后续消息要求完整审查时，再使用 `preview_skill_draft_file` 分块读取；只要 `complete=false`，就将 `next_offset` 用作下一次 `offset`。
6. 展示工具返回的全局安装与拒绝确认短语后停止。不能在创建草案的同一条用户消息中安装。

## 运行已安装脚本

1. 只对由工坊安装且 revision 未漂移的脚本调用 `run_skill_script`。
2. 传入 Skill 名、脚本名和完成任务所需的最小 JSON 输入。不要把秘密当作普通输入传递。
3. 以工具返回的 `execution_id`、源码摘要、策略版本和 JSON 结果为执行证据。
4. 工具拒绝、超时、资源耗尽或返回不可序列化结果时，明确报告失败；不要改用 shell 或直接 Python 执行来规避沙箱。

## 审批和拒绝

- 只有创建任务的同一用户在同一 Session 的新消息中仅发送工具返回的 `确认全局安装 <skill-name> <job-id> <revision>`，才调用 `install_skill_draft`，并把该完整短语原样传入。引用、否定、解释或附加文字都不算确认。
- 拒绝时也要求当前消息与工具返回的 `拒绝草案 <skill-name> <job-id> <revision>` 完全相等。含糊意见、修改建议或沉默都不等于拒绝。
- 同名 Skill 已存在时拒绝覆盖；不得删除、重命名或改写旧版本来规避。
- 安装完成后只说明文件已原子安装，并明确宿主需要冷启动才能刷新 Skill 快照。

## 安全边界

- 不生成 Skill runtime、shell、PowerShell、原生二进制、包安装器或可访问宿主文件的脚本。
- 不把聊天原文、凭据、用户数据或本地绝对路径写进 Skill；只传递必要的结构化、非敏感需求。
- 需要解释静态规则、进程隔离、网络、资源限制或安装权限时，读取[安全边界](references/security-boundary.md)。不得把它描述成容器或 OS 强沙箱。
