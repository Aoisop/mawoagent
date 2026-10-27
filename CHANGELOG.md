# 版本记录

## v0.3.0 — 2026-10-27

本版完成迭代 I4–I6：可以隔离执行、能接浏览器与远程工具、会记录审计并能管理记忆。累计交付 18/21 个任务。

### 新增

- **执行后端与 Docker 入口**（T11）：`run_shell` 改走统一后端接口，本地后端保持原有行为。`MAWOAGENT_SANDBOX=docker` 时命令在容器内执行，只挂载工作区、默认无网络、限制内存/CPU/PID 并禁用提权；`MAWOAGENT_SANDBOX_MODE=copy` 在一次性副本上执行并报告会改动哪些文件。
- **容器内 Web 接入**（T12）：镜像补齐 `web/` 静态资源与端口声明；监听地址、允许的 Host 头可配置，回环仍是默认值。
- **Playwright 浏览器 MCP**（T13）：固定 `@playwright/mcp@0.0.83`，独立 profile，可导航本地页面并读取无障碍快照；退出后按容器名/进程树清理，用同一 profile 二次启动验证无残留。
- **远程 MCP 传输**（T14）：支持 streamable HTTP 与 SSE，协议版本固定并校验，`Mcp-Session-Id` 会话、`nextCursor` 分页、令牌引用、同源重定向与取消通知均已实现；401 明确说明 OAuth 未实现。
- **类型化工具结果**（T15）：文本结果逐字不变；图片按内容哈希落盘并只回传路径与大小；音频与资源链接保留类型；模型不支持时回传明确的能力行。
- **执行型评测**（T16）：`python -m agents.exec_eval` 真跑 fixture 任务，baseline 与 forced 同任务对照，自然检索与强制加载分开统计，证据不足不晋级；side query 成本按阶段分项。
- **记忆工具**（T17）：`memory_save` / `memory_search` / `memory_archive` / `memory_restore`；记忆带来源（user/repo/inference）与作用域，归档可恢复。
- **审计留痕**（T18）：每次工具调用写入 start/end 两条 JSONL，未结束的调用即崩溃形态；参数脱敏截断；订阅者只读；`GET /api/sessions/<id>/audit` 可查。

### 变更（升级时请留意）

- **MCP 命令解析**：Windows 上 `npx` 实为 `npx.cmd`，现在经 `shutil.which` 解析并对 `.cmd`/`.bat` 使用 `cmd /c`。此前 Windows 上所有基于 npx 的 MCP 服务都无法启动。
- **进程清理**：Windows 关闭连接改用 `taskkill /T /F`，此前只杀 `cmd.exe`，留下 node 与浏览器进程。
- **取消通知**：请求被取消时会向服务端发送 `notifications/cancelled`，stdio 传输因此会多一次写入。
- **记忆写入**：改用 `memory_save` 而非 `write_file`；新增 `source` / `scope` 字段，旧记忆按 `user` / `project` 读取。常驻索引上限 40 条。
- **Web 路由**：`dispatch` 先剥离查询串再匹配，`?limit=` 这类参数现在可用。
- 新增运行时状态：`.mawoagent/audit.jsonl`（审计日志），已加入 `.gitignore`。

### 修复

- 无文本块的 MCP 结果会把整个响应（含 base64）序列化进对话。
- 容器/进程退出后子进程残留，导致浏览器 profile 被占用。
- 在已结束的事件循环上关闭 stdio 管道会抛异常。
- 对已归档记忆再次归档时返回"未找到"而非"已归档"。

### 已知限制

- **容器隔离未实测**：本机与 CI 均无 Docker 守护进程。到有 Docker 的机器执行 `python -m agents.sandbox_check`，返回 0 即通过。
- **远程 MCP 只对本地假服务器验证**，未连过真实服务端。
- **浏览器端到端默认跳过**（需 Node 与浏览器），`MAWO_PLAYWRIGHT_E2E=1` 开启。
- **检索可能过匹配**：`code_review` 对 5 个 fixture 任务全部 rank 1，疑似关键词泛化，尚未处理。
- 执行型评测需要自备模型密钥，CI 只覆盖证据门槛逻辑。
- 实例状态隔离与自动调度（T20、T21）尚未实现。

## v0.2.0 — 2026-10-23

本版完成迭代 I1–I3：修改前可审阅、修改后可撤销、代码结构可查询。首版范围 I0–I2 至此全部交付。

### 新增

- **编辑前审阅**（T04）：`write_file` / `edit_file` 在执行前给出 unified diff，并与当前文件版本绑定。等待确认期间文件若被改动，本次不写入并要求重新读取。
- **轮次快照与撤销**（T06、T07）：每轮对话记录被修改文件的原始内容，`/diff [n]` 查看、`/undo [n] [--dry-run]` 撤销。撤销只回滚"当前内容仍等于该轮结束状态"的文件，之后的手工修改会被列为冲突并跳过。
- **仓库符号索引**（T08）：`/symbols` 输出结构总览，`/symbols <查询>` 检索符号。Python 用 `ast` 解析，JS/TS 用模式匹配并标注为近似。索引按文件 size+mtime 增量更新。
- **仓库地图注入**（T08）：系统提示词中附带一段预算内的项目结构，默认开启。
- **任务评测入口**（T09）：`python -m agents.eval_run` 逐任务记录 JSONL，支持按配置变体分组对照；`--mock` 供 CI 验证管线。
- **YAML frontmatter 子集**（T10）：支持布尔、数字、null、引号字符串、块/流式列表、缩进嵌套、字面量与折叠块标量、注释。

### 变更（升级时请留意）

- 写入或编辑**工作区之外**的文件现在需要确认；`dontAsk` 与 `plan` 模式下直接拒绝，`acceptEdits` 不会绕过。
- 需要确认但**没有确认通道**时（例如非交互运行），动作被拒绝，不再阻塞等待输入。
- 技能里的 `user-invocable: false` 现在**真正生效**（此前被当作非空字符串）。
- 系统提示词默认注入仓库地图。可用 `"context": {"repo_map": false}` 或环境变量 `MAWOAGENT_REPO_MAP=off` 关闭。
- 新增运行时状态：`.mawoagent/snapshots/`（轮次快照）与 `.mawoagent/repo-index.json`（符号索引），均已加入 `.gitignore`。

### 修复

- 测试进程在解释器退出阶段偶发崩溃，导致 CI 在用例全通过时仍报红。
- 新策略测试在两平台上的路径断言不成立（Windows 8.3 短名、macOS `/var` 符号链接）。
- fixture 的字节码缓存被当作"多余文件"，破坏初始状态校验。

### 已知限制

- 通过 `run_shell` 产生的文件改动不在快照范围内，撤销时会报为冲突而不是回滚。
- `/diff`、`/undo` 的 Web 端目前只有接口，没有界面入口。
- 技能夹具仍为 5 个（计划目标 20–30 个）；真实模型对照需要自备密钥运行，CI 只验证管线。

## v0.1.0 — 2026-10-21

开发者预览：完成 I0（环境与任务夹具）以及一轮安全扫描后的修复与依赖加固。
