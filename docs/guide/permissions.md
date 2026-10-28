# 会话、权限与运行边界

> 返回：[文档索引](../README.md)

> 本文从 README 拆出，内容与 v1.0.0 时一致。修改请直接改本文件，README 只保留入口。

## English

### Sessions, permissions, and runtime boundaries

By default only the most recently updated session of the current project is resumed. `--resume-id <session-id>` selects a specific session; recovery is refused when the project path or API protocol differs, so operation context from another project cannot leak into the current directory.

Session recovery also preserves Skill-learning context, the pending feedback window, and stable conversation grouping. Unknown models default to a 32000-token capacity; use `--context-window N` or `MAWOAGENT_CONTEXT_WINDOW` to set the actual capacity.

The budget covers the main model, auxiliary calls, and running sub-agents. `/clear` only clears context and does not reset the accumulated cost of the current process. The default input/output estimate remains USD 3/15 per million tokens, configurable with `--input-price`, `--output-price`, or `MAWOAGENT_INPUT_PRICE_PER_MILLION` / `MAWOAGENT_OUTPUT_PRICE_PER_MILLION` in `.env`. The budget is an estimate based on returned usage; concurrent requests already sent, SDK-internal retries, and unknown usage from failed requests can still exceed the estimated cap.

`Ctrl+C` cancels the current task; shell timeouts and cancellations clean up the process group (macOS / Linux). On exit, model clients and MCP are closed, background Skill tasks are given at most 5 seconds, and unfinished tasks are then cancelled; `MAWOAGENT_SHUTDOWN_TIMEOUT` adjusts the wait.

`/skill-eval` waits for current background Skill tasks to finish before reading source records and skill snapshots; this wait does not use the shutdown timeout and does not cancel learning tasks.

Online Skill creation, evolution, retirement, and manual `/skill-create`, `/skill-evolve`, and `/skill-restore` share the same permission checks. Without explicit allow rules, the default mode and `--dont-ask` never write or move Skills automatically; `--accept-edits` still honors deny rules. Background tasks never show confirmation prompts; operations that require confirmation are rejected. Archived Skills can be restored with `/skill-restore <name>`.

Starting an MCP server itself also requires permission; project-configured commands cannot be executed implicitly through tool discovery. The default mode and `--accept-edits` confirm each start command; plan mode does not start MCP, and `--dont-ask` refuses to start without an explicit allow rule. Use `mcp_start(server-name)` to configure start rules; start permission and the permission to call `mcp__server__tool` are checked separately. If plan approval encounters closed input, no approval callback, or an invalid result, plan mode is retained and execution is not authorized.

Project-level allow rules require trusting the current configuration with `/trust-project` first; project-level deny rules always apply. Trust binds the project path and a configuration digest, so permission or MCP configuration changes require trusting again. Every tool re-checks the current mode at execution time, and MCP calls show their parameters and ask for confirmation each time. Tools in truncated responses are not executed.

File tools operate on the given path and never substitute a non-existent absolute path with a same-named file in the current directory. Context compaction requires complete, correctly typed structured memory; when the model fails, output is truncated, fields are missing, or the task state is empty, compaction is skipped and the original message history and compaction count are preserved.

Sessions, Skill statistics, source indexes, and evaluation state use atomic replacement via temporary files; read-modify-write of statistics, source indexes, Skill evolution, replay pools, and the champion registry uses cooperative file locks. Corrupt JSON is preserved and reported. File locks cannot stop an external editor that ignores them from overwriting a file, and multi-file updates are not equivalent to a database transaction.

## 简体中文

### 会话、权限与运行边界

默认只恢复当前项目的最近更新会话。也可使用 `--resume-id <会话ID>` 指定会话；项目路径或 API 协议不一致时拒绝恢复，避免把其他项目的操作上下文带入当前目录。

恢复会话也保留 Skill 学习上下文、待反馈窗口和稳定的对话分组。未知模型默认按 32000 token 容量处理，可通过 `--context-window N` 或 `MAWOAGENT_CONTEXT_WINDOW` 指定实际容量。

预算覆盖主模型、辅助调用及运行中的子 Agent，`/clear` 只清理上下文，不重置本次进程的累计费用。默认输入/输出估价仍为每百万 token 3/15 美元，可通过 `--input-price`、`--output-price` 或 `.env` 的 `MAWOAGENT_INPUT_PRICE_PER_MILLION`、`MAWOAGENT_OUTPUT_PRICE_PER_MILLION` 修改。预算是基于已返回用量的估算；已发出的并发请求、SDK 内部重试和失败请求的未知用量仍可能超出估算上限。

Ctrl+C 会取消当前任务；Shell 超时和取消会清理其进程组（macOS/Linux）。退出时会关闭模型客户端和 MCP，并最多等待后台 Skill 任务 5 秒，再取消未完成任务；可用 `MAWOAGENT_SHUTDOWN_TIMEOUT` 调整等待时间。

`/skill-eval` 会等待当前后台 Skill 任务完成，再读取来源记录和技能快照；此等待不使用退出超时，也不会因此取消学习任务。

在线 Skill 创建、演化、淘汰及手动 `/skill-create`、`/skill-evolve`、`/skill-restore` 共用权限检查。没有显式 allow 规则时，默认及 `--dont-ask` 模式不会自动写入或移走 Skill；`--accept-edits` 下仍遵守 deny 规则。后台任务不会弹出确认提示；需要确认的操作会被拒绝。归档后可输入 `/skill-restore <名称>` 恢复。

MCP Server 启动本身也需要权限，不能通过发现工具隐式执行项目配置中的命令。默认及 `--accept-edits` 模式逐个确认启动命令；计划模式不启动 MCP，`--dont-ask` 下没有显式 allow 规则时拒绝启动。可用 `mcp_start(服务名)` 配置启动规则；启动权限与 `mcp__服务名__工具名` 的调用权限分别检查。计划审批遇到输入关闭、无审批回调或无效结果时保留计划模式，不授权执行。

项目级 allow 规则须先通过 `/trust-project` 信任当前配置；项目级 deny 始终生效。信任绑定项目路径和配置摘要，修改权限或 MCP 配置后需要重新信任。每个工具在执行时重新检查当前模式，MCP 调用逐次展示参数并确认。截断响应中的工具不会执行。

文件工具按传入路径操作，不会把不存在的绝对路径替换为当前目录中的同名文件。上下文压缩要求完整且类型正确的结构化记忆；模型失败、截断、字段缺失或任务状态为空时跳过压缩，保留原始消息历史和压缩次数。

Session、Skill 统计、来源索引和评测状态使用临时文件原子替换；统计、来源索引、Skill 演化、回放池和冠军注册表的读改写使用协作文件锁。损坏 JSON 会保留并报错。文件锁不能阻止未使用该锁的外部编辑器覆盖文件，多文件更新也不等同于数据库事务。
