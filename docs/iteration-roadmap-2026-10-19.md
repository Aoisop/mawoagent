# mawoagent 迭代计划表

> 返回：[文档索引](README.md)

制定日期：2026-10-19。依据：本轮迭代方案可行性复核（复核记录未随仓库分发）。

目标：在现有 Python Runtime、Web 与 CLI 上，逐步交付可审阅、可恢复、可验证的编码与技能学习能力。

计划按验收里程碑排序，不设尚无人员与投入依据的日历承诺。下列功能均为待实施范围；已存在的代码作为基础复用，不计为本计划已交付。

## 1. 总体迭代表

| 迭代 | 优先级 | 目标 | 核心任务 | 前置依赖 | 可交付结果 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| I0：基线与开发环境 | P0 | 建立可重复的开发和验收起点 | 重建可用 Python 开发环境；运行现有测试；整理能力现状；定义任务、事件与成本记录格式；准备首批 5 个任务 fixture | 无 | 环境操作说明、测试基线、任务样本与验收规则 | 推荐 Python 版本可安装并启动；现有 Python/前端测试完成并记录结果；5 个任务均有初始状态、预期结果和程序化检查 |
| I1：访问范围与修改审阅 | P0 | 修改前能看清内容及允许范围 | 引入实例级 WorkspaceContext/访问策略；统一文件路径检查；建立带基准版本的编辑提案；Web/CLI 展示编辑前 diff；批准/拒绝绑定提案版本 | I0 | 文件访问策略、编辑提案、Web/CLI 审阅流程 | 越界访问按策略拒绝或请求明确扩展范围；批准期间文件变化需重新审阅；现有 Plan Mode 与权限规则不被绕过 |
| I2：文件快照与撤销 | P0 | 修改后可检查和恢复 | 按用户轮次记录 before/after；实现 /diff、/undo 和 Web 对应入口；撤销前检查内容冲突；关联执行日志；定义快照排除、容量和保留策略 | I1 | 轮次快照、差异查看、冲突安全撤销 | 用户原有 dirty tree、暂存内容和后续手改得到保留；创建/修改/删除/改名可恢复或明确拒绝；中断后能识别快照状态 |
| I3：代码理解与任务对照 | P1 | 降低定位成本并测量实际收益 | Repo Map v1；Python、JS/TS 符号搜索；增量缓存；预算内可选摘要；修复 Skill YAML frontmatter；任务集扩至 20–30 项 | I0–I2 | search_symbols 工具、可关闭的 Repo Map、兼容性修复、对照报告 | 忽略规则和权限均生效；修改/删除/改名/切分支后索引正确；固定模型与预算进行开关对照，记录成功率、工具次数、token 和耗时 |
| I4：隔离执行 | P1；无人值守写入的前置条件 | 将执行限制在明确环境中 | 抽执行后端；Docker opt-in；CLI 与本地 MCP 子进程同环境运行；限制挂载、权限、资源和网络；稳定 workspace 身份；完善容器启动诊断 | I1–I2 | Docker 执行入口、隔离策略、跨平台启动说明、集成测试 | 容器不能访问未挂载宿主路径；可写范围明确；取消/超时后进程清理正确；恢复 session 不混淆宿主/容器路径；安装缺失有清晰错误 |
| I5：浏览器与远程工具 | P1 | 交付可验收的外部工具能力 | 固定并验收 Playwright MCP；抽 MCP 传输接口；增加 Streamable HTTP；处理协议协商、工具分页、认证引用、会话和取消；为非文本结果定义内容类型 | I4；可提前开发本地测试服务与传输单测 | 浏览器文本操作闭环、Remote MCP v1、类型化结果接口 | 本地测试页导航/读取/点击/输入成功；拒绝授权不执行；远程 JSON/SSE、鉴权失败、会话失效、超时/取消均有测试；截图未接通时不声称模型可见 |
| I6：技能学习与记忆闭环 | P1/P2 | 证明学习带来收益且可回退 | Agent Loop 执行型评测；无 Skill/当前/候选比较；自然检索与强制加载分测；开发集与保留集隔离；Memory 管理工具与来源/版本；内部审计 hooks | I3–I5 | 执行评测报告、发布证据、记忆管理、审计事件 | 使用真实文件与测试结果判分；无关 Skill 负例不被污染；候选发布绑定评测正文和版本；记忆修改可审阅、可归档、可回退 |
| I7：自动化与受控并发 | P2，按需求交付 | 支持脚本运行和多会话扩展 | JSONL 事件、稳定退出状态、no-input 策略；进一步消除全局状态；只读子 Agent 并行与共享预算；有明确需求后接 OS 调度 | I4；并发先完成实例状态隔离，学习数据依赖 I6 的版本记录 | Headless 接口、只读并行、多工作区验证、可选调度入口 | 无输入时不会挂在审批；未知副作用不自动重放；并行不超预留预算；两个 workspace/session 不串状态；同工作区写任务互斥 |

I0 的任务与测量格式贯穿后续迭代。I3 的实测先使用受控 fixture、限定工具和人工监督；任意 Shell 的无人值守编码评测须等待 I4 隔离验收。允许提前做原型，但上线与发布必须满足对应依赖。

## 2. 分阶段任务与代码落点

表中的新模块名为建议名称，实施时以最小合理拆分为准。

| 任务 ID | 迭代 | 任务 | 主要代码落点 | 验收重点 |
| --- | --- | --- | --- | --- |
| T01 | I0 | 修复本地 Python 环境，记录干净环境安装与启动流程 | [requirements.lock](../requirements.lock)、[README.md](../README.md)、CI | 不提交 .venv；按锁文件可重建；运行已有测试，不以静态解析替代 |
| T02 | I0 | 制定任务 fixture、验收脚本和运行记录格式 | [tests/](../tests)、新增受版本管理的评测 fixture 与清单 | 每项有固定初始版本、允许工具、预算、预期行为；失败/超时也计入记录 |
| T03 | I1 | 注入工作区实例与访问策略，区分资源范围和授权提示 | [agent.py](../agents/agent.py)、[tools.py](../agents/tools.py)、[tool_runtime.py](../agents/tool_runtime.py)、新增 workspace 模块 | 覆盖 ..、绝对路径、symlink、junction、UNC；Runtime 自有状态目录按专用能力访问；路径策略不冒充 Shell 沙箱 |
| T04 | I1 | 编辑准备、diff 审阅、版本绑定与执行前复查 | [tools.py](../agents/tools.py)、[web.py](../agents/web.py)、[ui.py](../agents/ui.py)、[app.js](../web/app.js) | 用户批准的是准确文件版本与 patch；拒绝不落盘；失效提案重新生成；先支持批准/拒绝 |
| T05 | I1 | 统一策略配置解析与来源展示 | [config.py](../agents/config.py)、[project_trust.py](../agents/project_trust.py) | 复用现有配置文件；新增安全配置纳入信任指纹；覆盖顺序清晰；密钥脱敏 |
| T06 | I2 | 轮次快照、恢复日志与冲突检查 | [checkpoints.py](../agents/checkpoints.py)、[storage.py](../agents/storage.py)、新增文件快照模块 | 保留既有工具执行检查点；快照完整后才标记可恢复；多文件恢复被中断后能继续检查，不宣称跨文件原子事务 |
| T07 | I2 | /diff、/undo 与 Web 对应操作 | [main.py](../agents/main.py)、[web.py](../agents/web.py)、[app.js](../web/app.js) | 恢复前预览；检查轮后 hash；不改用户 Git 索引/历史；网络、数据库及工作区外副作用不纳入文件撤销承诺 |
| T08 | I3 | 符号提取、缓存、失效处理与查询 | 新增 repo_index.py；[tool_schemas.py](../agents/tool_schemas.py)、[tools.py](../agents/tools.py)、[subagent.py](../agents/subagent.py) | 缓存包含项目、内容与解析版本；损坏可重建；不支持语言回退搜索；索引不暴露被拒绝文件 |
| T09 | I3 | 任务相关排序、预算内摘要与消融 | [prompt.py](../agents/prompt.py)、[context_runtime.py](../agents/context_runtime.py)、评测入口 | 摘要可关闭，不逐轮重复累加；先简单排序；PageRank 只有实测需要时再加入 |
| T10 | I3 | 安全 YAML 解析与 Skill 来源提示 | [frontmatter.py](../agents/frontmatter.py)、[skills.py](../agents/skills.py)、[subagent.py](../agents/subagent.py) | 多行、列表、布尔、引号、嵌套字段可解析；旧 Skill 可读写；同名优先级保留或显式迁移 |
| T11 | I4 | 执行后端与 Docker 入口 | [Dockerfile](../Dockerfile)、[main.py](../agents/main.py)、[mcp_client.py](../agents/mcp_client.py)、新增执行后端 | 非特权、无 Docker socket、固定依赖；资源/网络/挂载明确；区分直接可写 workspace 与独立副本模式 |
| T12 | I4 | 容器状态映射与 Web 接入兼容 | [session.py](../agents/session.py)、[memory.py](../agents/memory.py)、[web.py](../agents/web.py)、Dockerfile | 首版先验收容器 CLI；Web 若纳入发布，补静态资源与监听/端口映射，保留 Host/Origin/CSRF 防护 |
| T13 | I5 | Playwright MCP 文本浏览器端到端 | [.mcp.json](../.mcp.json)、[mcp_client.py](../agents/mcp_client.py)、集成 fixture | 固定版本；独立浏览器 profile；本地页面操作可检查；进程退出后无遗留 |
| T14 | I5 | Remote MCP 与连接生命周期 | [mcp_client.py](../agents/mcp_client.py)、[config.py](../agents/config.py)、[project_trust.py](../agents/project_trust.py) | 固定兼容协议版本；JSON/SSE、分页、session、认证引用、重定向和取消正确；OAuth 未实现时明确不支持 |
| T15 | I5 | 类型化工具结果基础 | [tool_results.py](../agents/tool_results.py)、[result_storage.py](../agents/result_storage.py)、[agent.py](../agents/agent.py)、session/Web | 文本行为兼容；非文本保留类型与资源引用；UI/模型不支持时返回明确能力状态，避免把 base64 当正文注入 |
| T16 | I6 | 执行型评测与候选发布证据 | [online_skill_eval.py](../agents/online_skill_eval.py)、[skill_candidates.py](../agents/skill_candidates.py)、新增执行评测入口 | 当前文本回放继续保留；独立保留集；强制加载和自然检索分开；报告包括 side query 成本；证据不足不自动晋级 |
| T17 | I6 | Memory 保存/更新/搜索/归档与版本 | [memory.py](../agents/memory.py)、tool_schemas.py、tool_runtime.py、Web | 来源与作用域可见；用户偏好/项目事实/推断区分；归档可恢复；常驻记忆占用上下文有上限 |
| T18 | I6 | 内部审计 hooks | tool_runtime.py、agent.py、Web 事件接口 | 先交付只读审计；失败可诊断；后续可修改参数的 hook 必须重新校验并授权；不自动导入仓库插件 |
| T19 | I7 | JSONL、退出码、无输入执行 | main.py、config.py、agent.py、ui.py | 成功/失败/预算超限/取消可区分；stdout 结构化、日志单独输出；无输入时拒绝未授权动作 |
| T20 | I7 | 实例状态隔离与只读并行 | tools.py、ui.py、web.py、subagent.py、agent.py | 清理全局工具激活/UI 等状态；两个工作区不串状态；双协议行为一致；共享预算预留、取消与结果归属可靠 |
| T21 | I7 | 可选 OS 调度接入 | 新增调度配置/入口、main.py、运行文档 | 记录 workspace、时区、预算、重叠与重试策略；同项目写互斥；结果持久化；未知副作用不自动重试 |

## 3. 首个可发布版本

**首版范围：I0–I2。** 交付可控修改闭环，随后发布 Repo Map 增量版本。

| PR 顺序 | 范围 | 依赖 | 必须附带的验证 |
| --- | --- | --- | --- |
| PR-01 | T01–T02：开发环境、现有测试基线、5 个任务 fixture | 无 | 干净环境复现说明、测试实际结果、fixture 的确定性检查 |
| PR-02 | T03、T05：WorkspaceContext、文件访问范围和策略配置 | PR-01 | 路径越界、配置变更信任失效、权限模式组合测试 |
| PR-03 | T04：编辑提案与 Web/CLI diff 审阅 | PR-02 | 批准/拒绝、等待期间文件变化、执行前权限变更测试 |
| PR-04 | T06–T07：轮次快照、diff/undo、恢复日志 | PR-03 | dirty tree、暂存内容、后续手改、创建/删除/改名、中断恢复测试 |
| PR-05 | T08–T09：Repo Map 最小版与任务对照 | PR-04；fixture 设计可提前 | 索引失效测试、权限测试、固定条件下的开关对照报告 |
| PR-06 | T10：Skill YAML 与来源兼容性 | PR-01 后可独立准备 | 旧格式兼容、多行/嵌套/列表解析、序列化回读、来源优先级测试 |

PR 是评审单元，不要求一个 PR 承担整轮迭代。任务过大时继续拆分；只有完整通过 I0–I2 的发布门槛，才宣称首版可控修改能力已交付。

## 4. 统一验收规则

| 维度 | 规则 |
| --- | --- |
| 正确性 | 相关现有回归测试通过；新增测试覆盖真实边界与故障，不只复述实现 |
| 跨平台 | 核心文件/配置/会话行为在 Windows、macOS、Linux 验证；平台专属能力显式标注支持范围 |
| 授权一致性 | Web、CLI、双模型协议及子 Agent 经相同 Runtime 策略检查；恢复不扩大权限 |
| 可恢复性 | 保存失败不得标记操作已安全完成；未知副作用保留未知状态；冲突不能静默覆盖 |
| 效果测量 | 固定任务、模型、版本与预算；记录全部成功和失败；功能实现与效果提升分别报告 |
| 候选晋级 | 阈值在使用独立保留集前冻结；不根据测试集结果反复调参；候选有回归或证据不足时保持待审核 |
| 可观测性 | 事件包含 session/turn/tool 标识、状态、耗时和成本；敏感信息脱敏 |
| 数据迁移 | 延续 .mawoagent/MAWOAGENT_*；持久化结构有版本号与旧数据兼容策略 |
| 发布材料 | 每轮附范围、运行方式、实际验证、支持限制、回退方式和未完成项 |

Repo Map 等效果实验先报告实际差异与样本数。首批任务集用于发现问题，不据此宣称具有统计上稳定的泛化收益；更大范围推广前扩大独立样本并复测。

## 5. 后续需求池及启动条件

| 功能 | 当前安排 | 启动条件 |
| --- | --- | --- |
| PageRank、更广语言、LSP 引用 | Repo Map 后续增强 | 简单符号排序在真实任务中出现可复现漏检或误排，且改进收益覆盖延迟/维护成本 |
| 向量/混合记忆检索 | 后置实验 | 有标注查询集证明现有召回不足；确定隐私、成本与部署约束 |
| 多模态与截图理解 | I5 类型接口后的独立迭代 | 有明确截图任务；双协议、存储、UI、模型能力检测和预算链路可一起交付 |
| 多文件结构化 patch | I2 后按需求增强 | 高频编辑失败已有样本；具备基准版本检查、歧义拒绝与中断恢复 |
| 自动提交到用户仓库 | 可选能力 | 轮次快照稳定；明确用户索引、hooks、提交粒度与授权规则 |
| 完整 TUI / ACP | 后置入口 | 有明确终端/编辑器需求；Runtime 事件、审批、取消与恢复协议稳定 |
| 可写多 Agent / supervisor | 后置 | 独立 worktree/副本、共享预算、冲突集成和测试流程均已验收 |
| 通用 daemon / 独立服务 | 后置 | JSONL/实例隔离已稳定，OS 调度无法满足持续接入需求 |
| MCP Registry 搜索/安装 | 后置 | Remote MCP 稳定；具备具体包版本、启动命令、权限与信任展示 |
| MCP Apps | 单独立项 | 有真实交互式工具需求，完成 UI 资源隔离、CSP 与消息桥设计 |
| 公开插件 SDK | 后置 | 内部 hook 接口稳定；定义版本兼容、执行信任与异常处理 |
| SWE-bench 子集 | I4、I6 后接入 | 隔离执行与任务 harness 稳定；固定实例、依赖、官方判分与预算 |
| GAIA/HLE | 研究需求池 | 通用 Agent 成为明确目标，具备所需工具/多模态能力与独立预算 |

## 6. 执行跟踪

| 字段 | 填写规则 |
| --- | --- |
| 负责人 | 启动具体任务时指定；每个任务一位主责 |
| 状态 | 待开始 → 实施中 → 待验证 → 待评审 → 已交付；阻塞时写明原因与解除条件 |
| 关联变更 | 记录 PR 或 commit；原型不直接标记已交付 |
| 验证证据 | 测试日志、fixture 结果、对照报告或演示记录 |
| 日期 | 确定人员、投入与范围后填写计划/实际起止日；范围变化同步更新 |

开始实施时优先启动 PR-01，完成可复现基线后进入工作区策略与编辑审阅。
