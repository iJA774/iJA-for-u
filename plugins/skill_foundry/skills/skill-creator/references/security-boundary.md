# 安全边界

## 目录

- [能力范围](#能力范围)
- [脚本契约](#脚本契约)
- [分层防护](#分层防护)
- [明确不支持](#明确不支持)
- [安装与执行完整性](#安装与执行完整性)
- [非强沙箱声明](#非强沙箱声明)

## 能力范围

Skill 工坊可创建两类产物：

- `instruction-only`：`SKILL.md`、`agents/openai.yaml` 和可选 `references/*.md`。
- `instruction-and-scripts`：在上述文件外包含 `scripts/*.py`。

插件声明 `workspace:skills:write`。宿主在 Skill 可见性、加载、工具定义与执行处统一门控；工坊不按名称绕过授权。

## 脚本契约

脚本必须定义唯一的 `main(data)`：

- `data` 必须是至多 64KB 的有限 JSON 值。
- 返回值必须可编码为至多 64KB 的有限 JSON。
- 创建时的 `smoke_input` 必须是合成数据，单项至多 16KB；明显的 token、password、secret、cookie、authorization、private key 等字段会在任务落盘前拒绝。
- 单个源码至多 32KB，最多 8 个，总源码至多 160KB。
- 允许的导入仅包括 `collections`、`datetime`、`decimal`、`fractions`、`functools`、`itertools`、`json`、`math`、`re`、`statistics`。运行时拿到的是逐项 allowlist 的能力投影，不是原始模块；例如模块内部的 `sys`、`operator`、`random` 不会暴露。不允许子模块、相对导入、星号导入、私有名称或投影外导出。

## 分层防护

1. **落盘前预检**：解析 AST，检查语法、复杂度、顶层副作用、导入、危险调用、私有/双下划线反射、明显凭据和敏感路径。预检失败时不创建 job，不写 workbench。
2. **受限语言环境**：运行时只提供 allowlist builtins 和已预载安全模块；没有 `open`、`input`、`print`、`eval`、`exec`、`compile`、`getattr`、`globals`、`locals`、`type`、`object` 或任意动态导入。
3. **Python audit policy**：拒绝文件访问与修改、网络、注册表、`ctypes`、子进程、进程控制和链接操作。
4. **独立进程**：每次 smoke test 和正式运行都使用最小环境变量启动短生命周期 Python 隔离进程，源码经标准输入传入；生成代码不接收脚本路径或宿主环境。
5. **资源上限**：父进程设置 4 秒 wall timeout。Windows 可用时使用 Job Object 限制 2 秒 CPU、256MiB 内存和 1 个进程；POSIX 可用时使用 `RLIMIT_CPU`、`RLIMIT_AS`、`RLIMIT_FSIZE` 和 `RLIMIT_NOFILE`。实际生效项随执行结果返回，不伪造成功。
6. **重复检测**：创建前、生成后 smoke test、安装后每次运行都会重新扫描源码。任何阶段的源码摘要不一致都会拒绝。

## 明确不支持

工坊拒绝以下高敏感或难以隔离的能力：

- shell、PowerShell、批处理、任意命令或子进程；
- 文件系统读写、目录枚举、环境变量、注册表、凭据库；
- 网络、Socket、HTTP、数据库连接或外部发送；
- `ctypes`、原生扩展、动态库、包安装、反射逃逸、序列化代码加载；
- 线程、异步后台任务、多进程、常驻服务；
- 自动生成或执行 `runtime.py`；
- 读取真实聊天原文、用户文件或生产数据作为 smoke test。

需要这些能力的 Skill 只能保留说明和人工审核步骤，不能借由编码、拆分字符串或换工具绕过。

## 安装与执行完整性

- 正式安装由控制服务唯一持有。任务绑定宿主注入的用户与 Session；模型不能覆盖身份。
- 安装确认短语完整绑定“全局安装”、Skill 名称、job ID 与冻结 revision。当前用户新消息去除首尾空白后必须与短语完全相等。
- 控制服务在触碰正式目录前持久化 `INSTALLING` 事务。staging 位于 `data/skill-workbench/<job-id>/installing`，不会被 Skill 扫描误认为已安装。
- 安装只在 draft、staging、target 的完整 manifest 与冻结 revision 精确一致时提交。同名 Skill 不覆盖，失败不修改旧目录。
- `run_skill_script` 只查找唯一的 `INSTALLED` 任务，复核安装目录完整 manifest、脚本 SHA-256 与 smoke test 证据后才运行。
- 每次成功执行在任务目录写入独立审计记录，只保存调用身份、策略版本以及输入/输出摘要，不保存输入和输出正文。
- 安装成功后需要冷启动宿主，新的 Skill 才进入启动时快照。

## 非强沙箱声明

以上措施是严格的受限 Python 子集、独立进程和尽可能启用的 OS 资源上限，不是容器、虚拟机、低权限专用账户或完整 OS 强制访问控制。它不能承诺抵御 Python 解释器、标准库或操作系统本身的未知漏洞。

因此仅允许短小、可完整审查、纯 JSON 计算脚本。安全报告显示 OS 资源上限未生效时仍有语言策略和父进程 timeout，但不能把该运行描述为 OS 强沙箱。
