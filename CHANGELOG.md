# 版本记录

## v1.0.1 — 2026-10-29

安全修复版本，不新增功能、不改命令行接口。针对 v1.0.0 的一次完整代码审计（33 项发现）逐条修复，
验证记录与 PoC 见 [docs/records/scans/code-audit-2026-10-28.md](docs/records/scans/code-audit-2026-10-28.md)。

**建议升级**：本次修复的多数问题需要用户主动打开一个仓库或运行一段命令才会触发，但触发条件是日常使用场景，不是异常配置。

### 安全修复

- **仓库内容按未信任处理**：`.mawoagent/skills`、`.mawoagent/agents`、`CLAUDE.md`、`.mawoagent/rules`
  在项目被 `/trust-project` 之前只显示名称，不进入系统提示词；`@include` 只能指向项目内，上限 64 KB。
- **项目 MCP 配置需要信任**：未信任仓库的 `.mcp.json` 不再被加载；远程 MCP 只接受 `http`/`https`，
  `file://` 曾被 urllib 的 `FileHandler` 接受并读取本地文件。
- **`/undo` 与快照**：回滚路径与 blob 摘要都做包含性校验，`meta.json` 里的 `../` 不再能写到工作区外。
- **权限语义**：`bypassPermissions` 不再跳过 deny 规则；`\\?\` 前缀不再剥离策略文件的保护属性；
  用户级 `~/.mawoagent/settings.json` 与信任登记表纳入绝对保护。
- **`.env` 只从当前目录读取**：此前 `find_dotenv(usecwd=True)` 仍会向上走到根目录，仓库可以借此
  把 API 端点指向自己，同时把用户真实密钥带过去。
- **`run_shell` 剥离密钥环境变量**：`*_API_KEY`、`*_TOKEN`、`*_SECRET` 等不再传给子进程。
- **审计日志**：命令串里的令牌也会脱敏；日志文件纳入受保护路径；追加加锁并保留三代轮转，读回时跨代合并。

### 修复

- `write_file` 写记忆目录不再用旧实现重建 `MEMORY.md`（会丢 `source`/`scope` 与条数上限）。
- 记忆召回在注入头里带上 `source`，`name`/`description` 的换行不再能伪造索引行。
- `plan` 模式禁止 `memory_save` / `memory_archive` / `memory_restore`。
- 调度：`permission_mode` 白名单（拒绝 `bypassPermissions`），`load_spec` 执行校验，命令改为参数数组。
- 延迟工具激活、提示词构建、`run_shell` 的 cwd 都跟随会话工作区，不再共用进程级状态。
- `list_files` / `grep_search` 对每个发现的路径应用工作区边界，`..` 模式直接拒绝。
- fork 技能的子代理有深度上限；副本模式不再跟随符号链接。

### 变更（升级时请留意）

- `bypassPermissions` 现在仍受 deny 规则约束：这是语义变更，依赖"yolo 可读被 deny 路径"的用法会失效。
- 未信任仓库的技能/代理只显示名称，调用会返回错误，直到执行 `/trust-project`。
- 未信任仓库的 `.mcp.json` 不会被加载，启动时会在日志中提示。

### 已知限制（未修复）

- Web 绑定 `0.0.0.0` 时防护不构成认证，详见 [docs/web-workspace.md](docs/web-workspace.md)。
- 默认允许读取工作区外的文件（`workspace_only_reads=false`），这与"本地编码助手"的定位一致。


## v1.0.0 — 2026-10-28

本版完成迭代 I7，路线图 21 个任务全部交付（I0–I7）。1.0 的含义是：接口与状态布局已稳定，后续变更是兼容性变更而不是返工。

### 新增

- **结构化输出与退出码**（T19）：`--output-format text|json|jsonl`（`--json` 为简写）。结构化模式下 stdout 只放记录，界面输出转 stderr；`jsonl` 每条一行且必定以 `result` 结尾，`json` 输出单个文档并附带事件列表。退出码区分：`0` 成功、`1` 失败、`2` 用法、`3` 预算耗尽、`130` 取消。
- **实例状态隔离**（T20）：新增 `workspace.project_root()`——有绑定工作区时以它为准，否则回退进程目录。记忆、项目会话、项目技能、自定义代理、技能进化、权限规则、项目信任与 MCP 配置全部改由它决定。`chat()` 在创建任务前绑定工作区，整轮生效，后台任务继承同一绑定。
- **可选 OS 调度**（T21）：`python -m agents.schedule` 支持 `add` / `list` / `show` / `remove` / `install` / `run`。调度记录 workspace、cron、时区、预算、重叠与重试策略；同项目写互斥（占用时记 `skipped_overlap`）；每次尝试写入 `runs/<name>.jsonl`；`install` 渲染 Windows `schtasks`、macOS launchd plist 或 Linux crontab 行，默认不落盘，需显式 `--apply`。

### 变更（升级时请留意）

- **状态目录随工作区**：当会话的 workspace 与进程目录不同（Web 多实例、子代理、显式传入 workspace），记忆与会话现在落在该工作区名下，而不是进程目录名下。命令行直接使用（workspace 即当前目录）行为不变。
- **`.env` 只从当前目录读取**：此前找不到时会向上回溯到安装目录旁的 `.env`，导致"配置取决于代码放在哪里"。
- **退出码变化**：一次性运行在预算耗尽时返回 `3` 而非 `0`。依赖 `0` 判断成功的脚本需要注意。
- **`--output-format` 需要 prompt**：交互式 REPL 没有结构化输出，未给 prompt 时返回 `2`。

### 修复

- 控制台无法编码 spinner 的盲文字符时，`UnicodeEncodeError` 从 UI 线程逸出并终止整个进程；不可编码字符现在降级为 `?`。
- 非固定时刻的 cron 表达式在 Windows/macOS 上会生成语义错误的条目，现在明确拒绝并说明原因。

### 已知限制

- **容器隔离未实测**：本机与 CI 均无 Docker 守护进程。到有 Docker 的机器执行 `python -m agents.sandbox_check`，返回 0 即通过。
- **远程 MCP 只对本地假服务器验证**，未连过真实服务端；OAuth 未实现，401 会明确报错。
- **浏览器端到端默认跳过**（需 Node 与浏览器），`MAWO_PLAYWRIGHT_E2E=1` 开启。
- **检索可能过匹配**：`code_review` 对 5 个 fixture 任务全部 rank 1，疑似关键词泛化，尚未处理。
- cron 只做语法与范围校验，不做时区换算；`timezone` 字段目前仅记录。

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
