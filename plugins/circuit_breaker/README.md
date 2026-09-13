# 任务循环熔断器

`circuit_breaker` 是自动启用的 `tool_guard` 插件，保护私聊、群聊和周期任务
共用的原生工具循环。无需模型主动加载 Skill，也不额外调用检测模型。

## 默认规则

| 规则 | 默认阈值 | 行为 |
| --- | --- | --- |
| 相同工具、参数、结果连续重复 | 3 次 | 第 3 次结果完成审计后熔断，不执行第 4 次 |
| 相同调用及结果序列重复 | 长度 2–4，重复 3 次 | 识别 A→B→A→B→A→B 等短周期 |
| 连续工具失败 | 4 次 | 包含非法 JSON、参数校验、权限及执行失败；成功清零 |
| 整次执行时长 | 120 秒 | 从进入工具循环开始，包含模型等待和工具执行，超时取消 |

任意规则先达到阈值就终止。宿主原有 `tools.max_rounds` 和 `tools.max_calls`
仍独立生效，因此较长周期也可能先触发宿主上限。无工具的单次模型请求、模型
适配器内部重试和后台非工具推理不经过此插件，仍由各自超时和重试上限控制。

摘要忽略 JSON 键顺序、空白和调用 ID，使用完整结果（模型上下文截断前）判断。
相同参数但结果变化视为进展；结果相同也可能是正常轮询，因此这是启发式检测，
需要合法重复操作时可调高阈值。参数和结果只在本次调用的内存中计算 SHA-256，
检测历史最多保存 `repetitions × max_cycle_length` 个摘要，不写入日志或磁盘。

## 配置与插拔

在宿主已有的插件配置中设置，例如 `config/platform-plugins.local.toml`：

```toml
[platform_plugins.options.circuit_breaker]
repetitions = 3
max_cycle_length = 4
consecutive_failures = 4
timeout_seconds = 120.0
```

阈值范围依次为 2–10、1–16、2–20、0.1–3600 秒。拒绝未知字段、错误类型和
非有限数值；默认配置不依赖额外文件、第三方服务、凭据或管理员 scope。
非法配置明确失败；热重载失败保留旧代保护，不静默关闭熔断。

放入目录即可自动发现，禁用使用宿主 `platform_plugins.disabled` 中的
`"circuit_breaker"`，也可移出插件目录，然后发布插件新 generation 或重启。
在途任务持有原 generation 租约，仍遵守原来的规则；新任务使用新配置。
停用或移除后，宿主继续启动，轮次和调用数硬上限继续生效。

## 状态、失败与恢复

每次 `ToolLoop.run` 创建独立守卫；并发会话互不影响。同一次执行一旦熔断，
状态锁存，不因后续成功重新打开。下一次任务或用户显式重试使用新预算，插件
不会自动重试、补发消息、回滚外部动作或删除历史。

宿主抛出 `ToolLoopAbortedError`，错误码为 `tool_loop_circuit_open`，沿现有
聊天投递失败或周期任务失败路径保存。事件 `tool_loop.tripped` 和警告日志仅
包含会话／轮次／周期运行 ID、插件 ID、错误码及 `reason`：
`repeated_result`、`repeated_cycle`、`consecutive_failures` 或 `task_timeout`。
已完成的工具审计保持真实状态，批量响应中后续工具不会继续执行。

超时使用 asyncio 协作取消；正在执行的工具记录 `tool_execution_cancelled`，
明确提示外部副作用是否完成需核实。取消不意味着远端动作回滚，不自动重放。
宿主的外部取消保持 `CancelledError` 语义，不误报为熔断。依赖吞掉取消后若
返回，执行边界仍会检查截止时间，拒绝迟到草稿和后续调用。一直阻塞事件循环
或不归还控制权的代码无法由此机制强制中止，需要受管子进程的隔离和超时。

## 测试

```powershell
uv run pytest plugins/circuit_breaker/tests tests/unit/test_tool_guards.py tests/integration/test_tool_guard_runtime.py
```

插件只获得配置和只读观测，不获得存储、网络、进程或工具执行 capability；
执行权限继续由宿主工具白名单和 scope 校验控制，模型无法修改熔断阈值。
