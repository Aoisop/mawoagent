# mawoagent 项目文档总览与完善版

本文档是在现有 wiki 文档基础上的统一总结和补充入口。原有文档保留为专题说明，本文用于快速建立项目全貌：mawoagent 是什么、核心链路如何运行、各模块承担什么职责、技术亮点怎么表达、评测结果说明什么，以及后续阅读源码应该从哪里开始。

建议把本文作为项目 wiki 的第一篇总览文档使用。需要深入某个专题时，再跳转到：

- `wiki/从0到1学习了解项目.md`
- `wiki/架构设计.md`
- `wiki/核心源码阅读指南.md`
- `wiki/技术亮点.md`
- `wiki/Skills自进化逻辑与实现思路.md`
- `wiki/评测部分.md`
- `wiki/简历包装.md`

## 1. 项目一句话定位

mawoagent 是一个基于 Python 实现的本地命令行 Coding Agent Runtime。它的核心不是把大模型包装成聊天 CLI，而是实现一套自研 Agentic Harness：统一编排模型推理、工具调用、权限控制、上下文管理、会话保存、Skills、自进化、Memory、MCP 和子 Agent。

更准确地说：

```text
mawoagent = 模型推理能力 + 本地工具执行能力 + Runtime 权限边界 + 长期上下文 + 可演化 Skills
```

模型只负责理解任务、推理和提出 tool call。真正访问文件、执行 Shell、调用 MCP、运行子 Agent、保存 session 和沉淀 Skill 的，是 mawoagent Runtime。

## 2. 核心价值

mawoagent 的价值可以概括为四点：

| 价值 | 说明 |
|------|------|
| 可运行 | 支持 REPL 和 one-shot，能真实读写文件、搜索代码、执行命令、调用工具 |
| 可控 | 所有环境动作都经过工具层和权限层，Plan Mode、危险命令检查、编辑前读取保护降低误操作风险 |
| 可扩展 | 内置工具、Skills、MCP、子 Agent 和项目规则都可以扩展 |
| 可沉淀 | Memory 保存事实和偏好，Skills 保存可复用方法，在线自进化把用户反馈转成 `SKILL.md` |

所以它适合作为：

- 学习 Claude Code 类工具底层机制的工程样例。
- 个人本地 Coding Agent 的二次开发基础。
- 展示 Agentic Harness、工具调用、权限控制、长期记忆和自进化能力的项目作品。

## 3. 总体架构

mawoagent 可以按六层理解：

| 层级 | 主要文件 | 职责 |
|------|----------|------|
| 入口层 | `agents/main.py`、`agents/ui.py` | CLI 参数、REPL、one-shot、终端交互 |
| Harness 层 | `agents/agent.py` | Agent Loop、模型协议适配、工具调度、会话保存、上下文压缩 |
| 能力层 | `agents/tools.py`、`agents/skills.py`、`agents/memory.py`、`agents/mcp_client.py`、`agents/subagent.py` | 文件工具、Shell、Skills、Memory、MCP、子 Agent |
| Prompt 层 | `agents/prompt.py` | 动态构建 system prompt，注入项目规则、Memory、Skills、子 Agent |
| 演化层 | `agents/online_skill_evolution.py`、`agents/skill_evolution.py`、`agents/online_skill_eval.py` | Skill 抽取、add / merge / discard、版本审计、质量评测 |
| 持久化层 | `.mawoagent/`、`~/.mawoagent/`、`~/.mawoagent/`、`~/.mawoagent/` | 保存项目级 Skill、用户级 Skill、Memory、Session、审计记录和大工具结果 |

架构主线如下：

```text
用户输入
  -> CLI / REPL
  -> Agent.chat()
  -> 构建 Prompt / 检索 Skills / 预取 Memory / 初始化 MCP
  -> 调用 OpenAI-compatible 或 Anthropic-compatible 模型
  -> 模型返回文本或 tool call
  -> Harness 做权限检查和工具分发
  -> 执行内置工具 / Skill / MCP / 子 Agent
  -> tool result 回写模型
  -> 多轮循环直到完成
  -> 保存 Session
  -> 后台执行 Skill usage tracking 和 online skill evolution
```

这条链路体现了项目的关键设计：模型不直接操作环境，Runtime 才是控制中心。

## 4. 一次请求如何执行

一次用户请求进入系统后，大致经历以下步骤：

1. `agents/main.py` 解析命令行参数、加载 `.env`、确定模型协议和权限模式。
2. 创建 `Agent` 实例，初始化工具列表、消息历史、system prompt 和运行时状态。
3. 用户输入进入 `Agent.chat()`。
4. 首轮调用时初始化 MCP Server，并把 MCP 工具合并进工具列表。
5. 检索相关 Skills，把 `<retrieved_skills>` 注入当前用户消息。
6. 异步预取相关 Memory，必要时刷新 system prompt。
7. 根据协议选择 `_chat_openai()` 或 `_chat_anthropic()`。
8. 模型返回普通文本或 tool call。
9. tool call 进入 `_execute_tool_call()`。
10. Runtime 根据工具类型路由到内置工具、Skill、MCP 或子 Agent。
11. 具体工具执行前经过 `check_permission()`。
12. 工具结果回写模型，模型继续推理。
13. 任务结束后自动保存 session。
14. 后台记录 Skill 使用效果，并根据下一轮用户反馈触发在线 Skill 自进化。

可以把它理解为一个循环：

```text
推理 -> 行动意图 -> 权限检查 -> 执行动作 -> 观察结果 -> 继续推理
```

这就是 mawoagent 和普通聊天工具的根本区别。

## 5. 核心模块职责

### 5.1 `agents/main.py`

负责“用户怎么进入系统”：

- 解析 `--model`、`--api-base`、`--resume`、`--plan`、`--accept-edits`、`--yolo`、`--dont-ask`。
- 从当前目录或父目录加载 `.env`。
- 判断 OpenAI-compatible 或 Anthropic-compatible。
- 创建 `Agent`。
- 启动 REPL 或执行 one-shot。
- 处理 `/skills`、`/memory`、`/compact`、`/plan`、`/skill-evolve` 等 REPL 命令。

### 5.2 `agents/agent.py`

负责“任务怎么跑起来”，是整个项目的 Runtime 中心：

- 管理模型客户端和双协议消息历史。
- 组织 Agent Loop。
- 初始化 MCP。
- 检索 Skills 和预取 Memory。
- 分发 tool call。
- 执行 Skill、MCP 和子 Agent。
- 处理 Plan Mode。
- 触发上下文压缩和会话记忆折叠。
- 自动保存和恢复 session。
- 调度 Skills usage tracking 和 online evolution。

如果只读一个核心文件，应优先读 `agents/agent.py` 的 `Agent.chat()` 和 `_execute_tool_call()`。

### 5.3 `agents/tools.py`

负责“模型能做什么，以及能不能做”：

- 文件读取：`read_file`
- 文件写入：`write_file`
- 精确编辑：`edit_file`
- 文件列表：`list_files`
- 代码搜索：`grep_search`
- Shell 执行：`run_shell`
- Skill 创建和演化：`skill_create`、`skill_evolve`
- 上下文压缩：`compact_context`
- Plan Mode 相关工具

权限边界也在这里落地。关键保护包括：

- Plan Mode 下阻断普通写操作和 Shell。
- 危险 Shell、新建文件、Skill 创建和演化需要确认。
- 编辑已有文件前必须先读取，并记录 mtime。
- 文件被外部修改后，需要重新读取再编辑。

### 5.4 `agents/prompt.py`

负责“模型每轮看到什么系统上下文”。

system prompt 会动态拼接：

- 当前工作目录、日期、平台和 Shell。
- Git 分支、最近提交、工作区状态。
- `CLAUDE.md` 和 `.mawoagent/rules/*.md` 项目规则。
- Memory 系统说明和 Memory 索引。
- Available Skills。
- Available Sub Agents。
- 延迟工具提示。

这说明 mawoagent 的 prompt 不是固定字符串，而是 Runtime 状态的一部分。

### 5.5 `agents/skills.py`

负责 Skills 的发现、解析、检索和执行。

Skills 以 `SKILL.md` 为载体，分为：

```text
项目级：<project>/.mawoagent/skills/<skill_name>/SKILL.md
用户级：~/.mawoagent/skills/<skill_name>/SKILL.md
```

Skill 适合保存“可复用方法”，例如代码审查流程、文档写作规范、重构检查清单和任务输出格式。

Skills 支持两种执行模式：

| 模式 | 说明 |
|------|------|
| `inline` | 把 Skill prompt 注入当前上下文，由主 Agent 继续执行 |
| `fork` | 创建隔离子 Agent 执行 Skill，适合复杂或独立任务 |

### 5.6 `agents/memory.py`

负责长期 Memory 的保存与召回。

Memory 和 Skill 的区别是：

| 类型 | 保存内容 | 示例 |
|------|----------|------|
| Memory | 事实、偏好、项目背景、历史决策 | 用户喜欢中文回答；某接口已废弃；项目部署方式 |
| Skills | 方法、流程、风格、可复用操作步骤 | review 输出结构；政府报告写作格式；重构检查流程 |

Memory 按项目路径 hash 隔离，避免不同项目的长期记忆互相污染。

### 5.7 `agents/mcp_client.py`

负责 MCP 外部工具接入。

mawoagent 自己实现 stdio JSON-RPC MCP Client，主要流程是：

```text
读取 MCP 配置
  -> 启动 MCP Server 子进程
  -> initialize
  -> tools/list
  -> 包装成 mcp__server__tool
  -> 模型调用时路由到 tools/call
```

MCP 的意义是让外部搜索、浏览器、数据库、内部系统等能力进入同一套 Agent 工具体系。

### 5.8 `agents/subagent.py`

负责子 Agent 配置和发现。

内置子 Agent 包括：

| 类型 | 特点 | 适用场景 |
|------|------|----------|
| `explore` | 只读探索 | 搜索代码、定位实现、收集上下文 |
| `plan` | 只读规划 | 输出实施方案、拆解任务 |
| `general` | 独立执行局部任务 | 把复杂任务拆到隔离上下文中完成 |

项目也支持通过 `.mawoagent/agents/*.md` 定义自定义子 Agent，并用 `allowed-tools` 控制工具范围。

## 6. 自主会话记忆折叠

长程任务中，原始对话、工具结果、失败尝试和中间分析会不断膨胀。简单截断历史会丢失关键状态，简单摘要又容易忽略工具经验。mawoagent 的自主会话记忆折叠把长对话压成面向继续执行的结构化 session state。

折叠状态分三层：

| 层级 | 保存内容 |
|------|----------|
| `episode_memory` | 任务描述、关键事件、整体进展 |
| `working_memory` | 当前子目标、阻塞点、待办事项、下一步动作 |
| `tool_memory` | 已用工具、有效参数、失败原因、工具返回模式 |

触发方式有两类：

- 模型主动调用 `compact_context`。
- Runtime 在上下文接近阈值时自动触发。

它的价值不是单纯省 token，而是让 Agent 在长任务中完成状态重组：保留目标、证据、工具经验和下一步方向，减少失败路径继续污染后续推理。

需要注意：当前会话记忆折叠是 session 级状态折叠，不等同于完整的跨项目长期 Memory。折叠产物会保存到 `.mawoagent/sessions/`，主要用于任务延续和审计。

## 7. Skills 自进化闭环

Skills 自进化是 mawoagent 最有辨识度的能力之一。它解决的问题是：用户在对话中给出的稳定偏好和工作方法，不能只停留在当前上下文里，而应该沉淀成未来可复用的 `SKILL.md`。

自进化链路如下：

```text
当前轮用户任务 + assistant 回复
  -> 保存 pending window
下一轮用户输入作为反馈证据
  -> Extractor 抽取最多一个可复用候选
  -> Maintainer 判断 add / merge / discard
  -> create_skill_file() 或 evolve_skill_file()
  -> 记录 provenance、history、usage stats
```

### 7.1 为什么使用 pending window

系统不会在当前任务结束后马上让模型“猜自己学到了什么”。它会先保存当前轮窗口，等待下一轮用户输入。

例如下一轮用户说：

```text
以后这类报告都要先给可直接使用的初稿，不要连续追问。
```

这类反馈更像稳定偏好，适合被抽取成 Skill。pending window 让系统把“上一轮任务 + 上一轮回答 + 下一轮反馈”一起作为证据，减少误沉淀一次性内容。

### 7.2 Extractor 的职责

Extractor 只负责抽取候选，不负责写文件。

它只应抽取：

- 未来同类任务仍适用的方法。
- 用户明确表达的输出偏好。
- 稳定纠正。
- 可复用工作流。

它不应抽取：

- 一次性任务内容。
- 隐私、密钥、账号、URL。
- 精确日期和临时参数。
- 只有 assistant 自己推测出来的规则。

### 7.3 Maintainer 的职责

Maintainer 负责维护 Skill 集合，输出三类动作：

| 动作 | 含义 |
|------|------|
| `add` | 新增独立 Skill |
| `merge` | 合并进已有 Skill |
| `discard` | 重复、低价值、证据不足或不适合沉淀 |

实现上会先做相似 Skill 检索和 exact identity 判断，优先 merge，谨慎 add，避免 Skill 数量失控。

### 7.4 审计和治理

自进化不是黑盒写 prompt。相关产物包括：

```text
.mawoagent/skill-evolution/usage.jsonl
.mawoagent/skill-evolution/online_provenance.jsonl
.mawoagent/skill-evolution/online_skill_provenance.json
.mawoagent/skill-evolution/skill_usage_stats.json
.mawoagent/skill-evolution/history/
.mawoagent/skill-evolution/pruned/
```

这些文件分别支持：

- 查看 Skill 来源。
- 回溯每次 add / merge / discard。
- 保存演化前版本快照。
- 统计 retrieved / relevant / used。
- 归档长期无效 Skill。

## 8. Skills 质量评测

仅仅自动创建 Skill 不代表它一定正确。mawoagent 还设计了在线 Skills 评测链路，用来观察 Skill 是否真正有效。

评测流程可以概括为：

```text
读取 provenance / usage stats / active skills
  -> 构造 replay pool
  -> 从 SKILL.md 编译规则
  -> 对历史 assistant 输出做规则评测
  -> 可选 LLM judge
  -> 生成 candidate variants
  -> replay 候选版本
  -> 满足 gate 时写入 champion 记录
```

关键点：

- replay 样本来自真实在线沉淀记录。
- 规则评测包括非空、JSON、表格、引用来源、结论前置等可程序化检查。
- LLM judge 用于判断回复是否满足 Skill 指令。
- candidate variants 是候选改进，不会自动覆盖 active `SKILL.md`。
- champion 是本地健康版本记录，用于质量观察和后续人工决策。

这个设计让 Skills 自进化具备可回放、可比较、可审计的工程边界。

## 9. 工具与权限体系

mawoagent 的安全边界不是只靠 prompt，而是落在 Runtime 和工具层。

权限模式包括：

| 模式 | 含义 |
|------|------|
| `default` | 默认模式，风险操作需要确认 |
| `acceptEdits` | 自动接受编辑类操作 |
| `bypassPermissions` | 跳过确认 |
| `plan` | 只读规划，阻断普通写操作和 Shell |
| `dontAsk` | 需要确认的操作自动拒绝 |

关键工程保护：

- 模型只发 tool call，本地动作由 Runtime 执行。
- `check_permission()` 贴近真实工具执行点。
- Plan Mode 将“分析计划”和“执行修改”分离。
- `edit_file` 依赖前置读取和 mtime 校验。
- 危险 Shell、新建文件、Skill 创建和 Skill 演化进入确认路径。

这部分是项目从“能跑”走向“可控”的关键。

## 10. 数据集与论文结果

仓库包含 GAIA 165 题和 HLE 500 题，合计 665 题，可供读者自行运行评测。文档中出现的 GAIA `53.3%`、HLE `20.2%` 和关闭记忆折叠后 GAIA `44.7%` 均来自 [DeepAgent 论文](../paper/deepAgents.pdf)的表 2、表 3，属于 DeepAgent-32B-RL 及其消融实验，**不是 mawoagent 的实测成绩**。本仓库没有对应的 mawoagent 逐题预测、运行轨迹、判分记录和完整实验配置，不能从论文百分比反推本项目的通过题数。

详细的数据文件、指标口径和自行评测所需记录见[评测部分](折叠评测部分.md)。

简历或答辩可表述为：

```text
整理 GAIA 165 题与 HLE 500 题作为后续评测数据；实现三层会话记忆折叠。文档引用 DeepAgent 论文的结果作为研究背景，尚未把它们作为 mawoagent 成绩报告。
```

## 11. 推荐源码阅读路线

建议按“主链路 -> 工具边界 -> 长期上下文 -> 自进化 -> 扩展能力”的顺序读。

1. `agents/main.py`
2. `agents/agent.py`
3. `agents/tools.py`
4. `agents/prompt.py`
5. `agents/skills.py`
6. `agents/online_skill_evolution.py`
7. `agents/skill_evolution.py`
8. `agents/online_skill_eval.py`
9. `agents/session_memory.py`
10. `agents/memory.py`
11. `agents/mcp_client.py`
12. `agents/subagent.py`
13. `agents/session.py`
14. `agents/ui.py`
15. `agents/frontmatter.py`

最低阅读目标：

- 能说清一次请求怎么从 `main.py` 进入 `Agent.chat()`。
- 能画出模型 tool call 到工具执行再到 tool result 回写的流程。
- 能解释权限模式、Plan Mode、编辑前读取和 mtime 校验。
- 能区分 Memory、session memory 和 Skills。
- 能解释 pending window、Extractor、Maintainer、provenance 和 usage stats。
- 能说明 MCP 和子 Agent 为什么属于扩展能力边界。

## 12. 适合讲解的技术亮点

可以将 mawoagent 的技术亮点浓缩成以下几条：

1. 自研 Agentic Harness，统一编排模型推理、tool call 解析、权限检查、工具执行、结果回写、会话保存和上下文压缩。
2. 支持 OpenAI-compatible 和 Anthropic-compatible 双协议，底层消息格式不同，但共享工具、Memory、Skills、MCP、权限和 session 体系。
3. 设计自主会话记忆折叠机制，将长程对话折叠为 `episode_memory`、`working_memory`、`tool_memory` 三层结构化状态。
4. 实现 Memory + Skills 分层长期上下文，Memory 保存事实和偏好，Skills 保存可复用方法。
5. 实现在线 Skills 自进化，通过 pending window、Extractor、Maintainer 完成反馈抽取、add / merge / discard 和 `SKILL.md` 落盘。
6. 构建 Skills 质量评测机制，通过 replay pool、规则评测、LLM judge、candidate variants 和 champion 记录观察 Skill 质量。
7. 自研 stdio JSON-RPC MCP Client，把外部工具包装为统一的 `mcp__server__tool`。
8. 支持 explore / plan / general 子 Agent，用隔离上下文处理探索、规划和局部任务。
9. 工具权限系统具备 Plan Mode、危险命令确认、编辑前读取和 mtime 校验等硬边界。
10. 整理 GAIA / HLE 共 665 道题，为后续按 Pass@1 评测及记忆折叠消融做准备。

## 13. 面试表达精简版

30 秒版本：

```text
mawoagent 是我用 Python 实现的自进化命令行 Coding Agent Runtime。它的核心是自研 Agentic Harness，不是简单调用模型 API，而是统一编排模型推理、工具调用、权限控制、上下文压缩、Memory、Skills、MCP 和子 Agent。项目最大的亮点是两块：一是自主会话记忆折叠，把长程任务压成结构化 session state 后继续执行；二是 Skills 自进化，把用户下一轮反馈作为证据，经过 Extractor 和 Maintainer 沉淀成可复用 SKILL.md，并通过 provenance、usage stats 和 replay 评测保证可审计。
```

简历 bullet 版本：

```text
- 自研 Python Agentic Harness，统一编排 OpenAI / Anthropic 兼容模型、工具调用、权限检查、结果回写、会话保存和上下文压缩。
- 设计三层结构化会话记忆折叠机制，通过 episode_memory、working_memory、tool_memory 保留长程任务状态和工具经验。
- 实现在线 Skills 自进化链路，通过 pending window 捕捉用户反馈，并由 Extractor / Maintainer 完成候选抽取、相似检索、add / merge / discard 和 SKILL.md 落盘。
- 构建 Skills 质量评测机制，基于 provenance 构造 replay pool，结合规则评测、LLM judge、candidate variants 和 champion 记录进行质量观察。
- 整理 GAIA / HLE 共 665 道题作为后续评测数据；DeepAgent 论文数字仅作为参考，不列为 mawoagent 成绩。
```

## 14. 当前实现边界

为了避免过度包装，项目边界应明确说明：

- mawoagent 是本地命令行 Coding Agent Runtime，不是完整 IDE。
- 自主会话记忆折叠是 session 级状态重组，不等同于完整跨项目长期记忆。
- Memory 和 Skills 分工不同，Memory 记录事实和偏好，Skills 记录方法和流程。
- Skills 自进化依赖用户反馈作为主要证据，不应沉淀一次性 payload、隐私、密钥、URL 或临时参数。
- champion 是评测产物和健康版本记录，不会自动覆盖 active `SKILL.md`。
- LLM judge 和候选版本试跑依赖可用模型调用；不可用时会退化为程序规则评测。
- GAIA / HLE 的论文结果必须标明来源；目前只有本地题目数据，没有可复核的 mawoagent Pass@1 成绩。

## 15. 后续可完善方向

如果继续迭代文档和项目，可以优先考虑：

- 给 wiki 增加统一目录页，把现有专题文档按“入门、架构、源码、能力、评测、简历”分类。
- 在架构图旁增加核心函数跳转表，方便读者从文档直接定位代码。
- 为 Skills 自进化补一张“从用户反馈到 SKILL.md 落盘”的最小示例。
- 为 Memory、session memory、Skills 增加对比案例，降低概念混淆。
- 补充一个最小 MCP Server 接入示例。
- 补充一个自定义子 Agent 示例。
- 把 GAIA / HLE 评测脚本、数据来源、运行命令和结果文件路径进一步文档化。

## 16. 最短学习路径

如果只有半天时间，按这个顺序学习：

```text
README.md
  -> wiki/mawoagent项目文档总览与完善版.md
  -> wiki/架构设计.md
  -> agents/main.py
  -> agents/agent.py::Agent.chat()
  -> agents/agent.py::_execute_tool_call()
  -> agents/tools.py::tool_definitions
  -> agents/tools.py::check_permission()
```

如果目标是答辩或面试，再补充：

```text
wiki/技术亮点.md
  -> wiki/Skills自进化逻辑与实现思路.md
  -> wiki/评测部分.md
  -> wiki/简历包装.md
```

读完这些内容后，应能完整讲清楚 mawoagent 的主线：

```text
自研 Harness
  -> 工具和权限闭环
  -> 长程会话记忆折叠
  -> Memory + Skills 分层上下文
  -> 用户反馈驱动 Skills 自进化
  -> MCP 和子 Agent 扩展能力边界
  -> 通过 GAIA / HLE 和 Skills replay 评测观察效果
```
