# mawoagent

mawoagent 是一个由 Aoisop 创建和维护的、基于 Python 的本地 **Agent Runtime**，同时提供网页工作台和命令行入口。它把模型推理、工具执行、权限确认、会话恢复、长期记忆、Skills 与自进化流程连接在一起，适合用于项目分析、编程辅助，也适合学习和扩展 Agent 的运行机制。

项目重点是 **Harness**：模型提出操作意图，Runtime 负责判断权限、执行工具、保存状态和回写结果。网页与 CLI 共用这套能力。

## 快速启动：前端与后端

**运行 `python -m agents.web`，即可同时启动后端 API 和前端页面。** 当前前端采用原生 HTML / CSS / JavaScript，由同一个 Python 服务提供，无需安装 npm 依赖或单独运行前端开发服务器。

| 部分 | 源码位置 | 启动或访问方式 |
| --- | --- | --- |
| 后端服务 | `agents/web.py`、`agents/` | `python -m agents.web` |
| 前端页面 | `web/` | 后端启动后，打开 <http://127.0.0.1:8765> |
| 命令行入口 | `agents/main.py` | `python -m agents.main`，按需使用 |

### 1. 准备环境并安装依赖

推荐使用 Python **3.11 或 3.12**，以下命令适用于 macOS / Linux。Git 用于获取代码，ripgrep 可选；仅运行网页工作台不需要 Node.js。运行前端测试或使用依赖 Node.js 的 MCP 工具时，再准备 Node.js 环境。

在 **mawoagent 仓库根目录**执行：

```bash
# 确认 Python 版本
python3 --version

# 首次使用时创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 使用固定版本依赖安装
python -m pip install -r requirements.lock
```

如果默认 `python3` 版本不合适，可将创建虚拟环境的命令改为 `python3.12 -m venv .venv`。后续启动时只需激活已有虚拟环境，不必重复创建或安装。

### 2. 配置模型连接

先创建本地配置文件；已有 `.env` 时保留原文件：

```bash
test -f .env || cp .env.example .env
```

用编辑器打开项目根目录的 `.env`，将密钥、接口地址和模型 ID 替换为你实际使用的服务配置。下面两种方式**选择一种**；如果使用服务专用变量，需要删除或注释模板里已启用的 `APIKEY`、`API`，避免混用配置。

**OpenAI-compatible 接口：**

```env
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://your-provider.example/v1
MODEL=your-model-id
```

**Anthropic-compatible 接口：**

```env
ANTHROPIC_API_KEY=your-api-key
ANTHROPIC_BASE_URL=https://your-provider.example/anthropic
MODEL=your-model-id
```

上述地址和模型名称都是占位值，请以服务商提供的配置为准。也支持通用变量 `APIKEY` / `API`。

配置读取规则：

- 从当前工作目录向上查找 `.env`；终端中已设置的同名环境变量优先于 `.env`。
- 通用 `API` 优先于服务专用的 Base URL；通用密钥也优先于服务专用密钥。
- `OPENAI_BASE_URL` 与 `ANTHROPIC_BASE_URL` 明确选择各自协议；使用通用 `API` 时，原生 `api.anthropic.com`、路径以 `/anthropic` 结尾或包含 `/anthropic/` 的地址使用 Anthropic 协议，其余使用 OpenAI 协议。
- 模型名称使用 `MODEL`；CLI 还可用 `--model` 覆盖。Web 入口通过环境配置选择模型。
- 修改 `.env` 后需重启服务。密钥保留在后端，不在前端页面录入或展示。

暂未配置模型时，也可以打开工作台查看页面和已有数据；发送任务前需要完成配置。

### 3. 启动后端服务

在仓库根目录打开终端：

```bash
source .venv/bin/activate
python -m agents.web
```

看到以下提示说明服务已经启动，请保持终端运行：

```text
mawoagent Web → http://127.0.0.1:8765
按 Ctrl+C 停止；模型配置沿用项目 .env。
```

也可以直接调用虚拟环境中的 Python，省去激活步骤：

```bash
.venv/bin/python -m agents.web
```

### 4. 打开前端页面

在浏览器访问 **[http://127.0.0.1:8765](http://127.0.0.1:8765)**。

前端会连接同一地址下的 `/api/*`，通过 SSE 接收回复和工具执行状态。可以在工作台创建会话、描述任务、查看执行过程，并在需要权限时允许或拒绝；Skills、评测、记忆和设置也从侧栏进入。

前端文件由后端直接提供，**不需要 `npm install`、`npm run dev` 或构建步骤**。不要双击 `web/index.html`，也不要用独立静态服务器代替 `agents.web`：完整界面需要同源 API、会话 Cookie 和请求校验。

### 5. 端口、重启与开发调试

默认端口为 `8765`，如需更换：

```bash
python -m agents.web --port 8766
```

此时访问 [http://127.0.0.1:8766](http://127.0.0.1:8766)。端口更改后，前端会自动使用新的同源地址，无需单独修改 API 配置。

| 修改内容 | 生效方式 |
| --- | --- |
| `web/` 中已有的 HTML、CSS、JS 文件 | 保存后刷新浏览器；没有自动热更新 |
| Python 后端代码或 `.env` | 在服务终端按 `Ctrl+C`，再运行启动命令，随后刷新页面 |
| 新增前端模块或静态资源文件 | 在 `agents/web.py` 的静态资源白名单中注册路径，重启服务后刷新页面 |

关闭浏览器不会停止服务。停止服务请在启动它的终端按 `Ctrl+C`。平时使用一个服务进程操作当前项目即可；切换端口时注意打开对应的新地址。

### 常见启动问题

| 现象 | 处理方式 |
| --- | --- |
| `ModuleNotFoundError: No module named 'anthropic'` 等依赖错误 | 激活 `.venv` 后重新安装 `requirements.lock`，或直接用 `.venv/bin/python` 启动 |
| `No module named agents` | 确认终端位于 mawoagent 仓库根目录 |
| `Address already in use` | 停止占用该端口的旧服务，或用 `--port 8766` 启动并访问新地址 |
| 页面一直停在“正在加载”，按钮或图标无反应 | 如果刚更新代码，先重启 Python 服务再刷新页面；旧进程可能未注册新增的前端模块 |
| 模型未配置、鉴权失败或模型不存在 | 核对密钥、Base URL、模型 ID，以及终端环境变量与 `.env` 的优先级；修改后重启 |
| 出现请求校验失败或连接中断 | 确认服务仍在运行，刷新当前端口的页面，以重新建立本地会话 |

Web 服务固定监听 `127.0.0.1`，面向本机单用户使用。当前 `Dockerfile` 默认启动 CLI，且未包含 `web/` 静态资源；网页工作台请按上述本地方式启动。更多界面功能与运行边界见 [Web 工作台说明](docs/web-workspace.md)。

## 命令行使用

CLI 与网页是两个可选入口，使用网页时无需同时启动 CLI。下面命令同样在仓库根目录、已激活虚拟环境的终端中执行。

```bash
# 交互式对话
python -m agents.main

# 执行一次任务
python -m agents.main "总结这个项目的目录结构和核心模块"

# 只读分析并生成计划
python -m agents.main --plan "分析 Skills 检索逻辑应该如何优化"

# 恢复当前项目的最近会话
python -m agents.main --resume

# 查看全部参数和内置命令
python -m agents.main --help
```

进入交互模式后，可以直接输入任务，也可使用 `/plan`、`/skills`、`/memory`、`/compact` 等命令。

## 核心能力

- **网页工作台**：对话、流式回复、工具授权、任务详情、Skills、评测、记忆与设置，共用本地 Runtime。
- **通用 Agent Runtime**：统一编排模型、工具、权限、上下文、Memory、Skills 和外部扩展。
- **完整 Agent Loop**：模型请求、tool call 解析、权限检查、工具执行、tool result 回写、继续推理、会话保存形成闭环。
- **OpenAI / Anthropic 双协议**：支持 OpenAI-compatible 和 Anthropic-compatible 接口。
- **工具系统与权限控制**：支持读写文件、精确编辑、搜索、Shell、Skill 调用、子 Agent 和 MCP 工具；Plan Mode 下阻断写操作和 Shell。
- **Skills 体系**：通过项目级和用户级 `SKILL.md` 保存可复用方法，支持检索、调用、inline / fork 执行和版本化演化。
- **长期 Memory**：按项目路径 hash 隔离记忆，保存用户偏好、项目背景、历史决策和参考资料。
- **Skills 自进化**：从用户反馈中抽取可复用规则，保存隔离候选，经评测或人工审核后发布到 `SKILL.md`。
- **MCP 外部工具扩展**：基于上游实现的 stdio JSON-RPC MCP Client，把外部 MCP Server 工具包装为 `mcp__server__tool`。
- **子 Agent**：支持 `explore`、`plan`、`general` 以及自定义子 Agent。
- **会话恢复和上下文压缩**：自动保存 session，支持 `--resume`、`/compact`，并对大工具结果做截断或持久化。

## 项目架构

核心运行链路：

```text
用户输入
  -> agents/main.py（CLI）或 agents/web.py（网页）
  -> Agent.chat()
  -> 构建 Prompt / 检索 Skills / 预取 Memory / 初始化 MCP
  -> 调用 OpenAI-compatible 或 Anthropic-compatible 模型
  -> 模型返回文本或 tool call
  -> Runtime 做权限检查
  -> 执行工具 / Skill / MCP / 子 Agent
  -> tool result 回写模型
  -> 保存 Session
  -> 后台执行 Skill usage tracking 和 online skill evolution
```

## 目录结构

```text
mawoagent/
├── agents/
│   ├── main.py                    # CLI 入口、REPL、参数解析
│   ├── web.py                     # Web 后端：静态页面、HTTP API、SSE 流式事件
│   ├── config.py                  # CLI、Web、评测共用的环境和模型配置
│   ├── agent.py                   # Agent Runtime、模型调用、工具调度、上下文压缩
│   ├── context_runtime.py         # 两种协议共用的上下文预检和限时 Memory 召回
│   ├── tool_results.py            # 无模型依赖的工具结果压缩策略
│   ├── result_storage.py          # 大工具结果单独落盘、检查点与上下文保留预览
│   ├── streaming.py               # 流式尝试隔离与失败输出回退
│   ├── checkpoints.py             # 工具执行检查点与中断恢复
│   ├── tools.py                   # 内置工具和权限系统
│   ├── tool_schemas.py            # 工具定义与参数 Schema
│   ├── prompt.py                  # System prompt 动态构建
│   ├── skills.py                  # Skills 加载、检索、执行、创建和演化封装
│   ├── online_skill_evolution.py  # 在线 Skill 抽取和 add/merge/discard 决策
│   ├── skill_evolution.py         # Skill 落盘、版本快照、审计统计
│   ├── memory.py                  # 长期记忆系统
│   ├── mcp_client.py              # MCP stdio JSON-RPC 客户端
│   ├── subagent.py                # 子 Agent 配置
│   ├── session.py                 # 会话保存与恢复
│   └── ui.py                      # 终端 UI 输出
├── web/                          # 前端源码，无需打包
│   ├── index.html                # 页面结构
│   ├── styles.css                # 石墨深色主题与响应式布局
│   ├── app.js                    # 页面交互、API 请求与 SSE 订阅
│   ├── chat-view.mjs             # Markdown 排版与消息局部更新
│   ├── chat-scroll.mjs           # 聊天滚动控制
│   ├── conversation-state.mjs    # 会话状态、草稿与附件
│   └── tests/                    # 前端单元测试
├── .mawoagent/
│   ├── skills/                    # 项目级 Skills
│   └── skill-evolution/           # Skills 自进化审计产物
├── docs/                          # Web 使用与运行可靠性说明
├── tests/                         # Python 回归测试
├── Dockerfile
├── .env.example                  # 模型配置模板
├── requirements.lock             # 固定版本依赖，安装时使用
├── requirements.txt              # 直接依赖与版本范围
└── README.md
```

## Skills 自进化

mawoagent 的核心特色是 **Skills 自进化**。它从用户明确反馈中抽取未来可复用的规则，默认保存候选；通过评测或明确的人工审核后，才发布到项目级或用户级 `SKILL.md`。

### 1. 开启自动自进化

默认 `MAWOAGENT_AUTO_SKILL_EVOLUTION` 是开启的。为了明确配置，建议在 `.env` 中写：

```env
MAWOAGENT_AUTO_SKILL_EVOLUTION=1
MAWOAGENT_AUTO_SKILL_TARGET=project
```

含义：

- `MAWOAGENT_AUTO_SKILL_EVOLUTION=1`：启用在线 Skill 自进化。
- `MAWOAGENT_AUTO_SKILL_TARGET=project`：自动新增的 Skill 写入当前项目 `.mawoagent/skills/`。

如果希望沉淀为所有项目共享的个人 Skill：

```env
MAWOAGENT_AUTO_SKILL_TARGET=user
```

对应路径：

```text
project: <project>/.mawoagent/skills/<skill_name>/SKILL.md
user:    ~/.mawoagent/skills/<skill_name>/SKILL.md
```

### 2. 用允许写入的权限模式启动

后台保存 Skill 候选需要当前权限模式允许维护操作。推荐使用：

```bash
MAWOAGENT_AUTO_SKILL_EVOLUTION=1 \
MAWOAGENT_AUTO_SKILL_TARGET=project \
python -m agents.main --accept-edits
```

也可以使用更激进的模式：

```bash
python -m agents.main --yolo
```

不建议长期默认使用 `--yolo`，因为它会跳过确认。日常推荐 `--accept-edits`。

### 3. 给出可复用反馈

自进化不是把每一句话都写成 Skill。它只适合沉淀稳定、明确、未来同类任务仍适用的规则。

不太适合沉淀：

```text
帮我写一篇 500 字政府报告。
```

适合沉淀：

```text
以后写政府报告、工作汇报、调研材料时，默认先给可直接使用的初稿，不要连续追问；结构按“标题、背景、主要情况、问题分析、工作举措、下一步计划”组织，语言要正式克制。
```

适合演化已有 Skill：

```text
以后这类政府报告还要加入关键数据指标，不能只写概念性表述。
```

### 4. 自进化链路如何工作

```text
第 N 轮用户任务
  -> Agent 输出结果
  -> 保存 pending extraction window
第 N+1 轮用户反馈
  -> 合并进上一轮 window
  -> 随会话持久化待提交反馈，再提交后台队列（不依赖本轮回答成功）
  -> online_ingest()
  -> Extractor 抽取候选 Skill
  -> Maintainer 判断 add / merge / discard
  -> 保存隔离候选：/skill-candidates 查看
  -> /skill-eval 验证已有 Skill 的合并候选
  -> /skill-apply <id> 发布通过评测的候选
  -> 写入 SKILL.md；/skill-rollback <name> 可恢复旧版本
  -> 记录 provenance、usage stats 和版本快照
```

核心文件：

```text
agents/agent.py
agents/online_skill_evolution.py
agents/skills.py
agents/skill_evolution.py
```

审计产物：

```text
.mawoagent/skill-evolution/usage.jsonl
.mawoagent/skill-evolution/online_provenance.jsonl
.mawoagent/skill-evolution/online_skill_provenance.json
.mawoagent/skill-evolution/skill_usage_stats.json
.mawoagent/skill-evolution/history/
```

## 在线 Skill 回归评测

REPL 中运行 `/skill-eval`。首次生成 `replay_review.jsonl`，选择“原任务重做 / 反馈修正”、绑定目标消息轮次，确认会话归属和验收条件后再次运行。题目和上下文从来源重建，重复目标只计一次。当前版、候选和已有冠军在同一批任务上重新作答；同会话窗口固定分组，样本数与模型调用量有上限，异常或不完整比较不会晋级。

这些是文本任务的回归检查，来源可能参与过 Skill 学习，不代表独立 benchmark 成绩。规则不再从 Skill 关键词自动猜测；冠军快照单独保存。当前 `requirements` 的字符串默认必须通过，可选项使用 `{"text":"要求", "required":false}`，关键失败不能靠其他分数抵消。`json_schema` 检查可校验字段及类型，`MAWOAGENT_SKILL_JUDGE_MODEL` 可单独指定同服务的裁判模型。

新 Skill 或缺少独立回放样本时，可在审阅后明确使用 `/skill-apply <id> --manual-review`；这是人工发布，不代表通过评测。后台任务有并发上限并保存待办；来源日志按增量索引读取。`/cleanup-state [days]` 预览旧工具结果和评测运行目录，明确加 `--apply` 才删除，当前冠军运行目录保留。

当前候选发布、权限、验收条件和恢复流程详见 [运行可靠性与 Skills 发布流程](docs/runtime-hardening.md)。此前版本的评测流程说明见 [在线 Skills 评测实现说明](在线Skills评测实现说明.md)。

## 会话、权限与运行边界

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

## 折叠对照评测

mawoagent 早期自行试跑折叠评测时，得到的数据偏低。因此，Wiki 中用于说明 GAIA / HLE 指标和记忆折叠消融的 `53.3%`、`20.2%`、`44.7%` **直接复用 [DeepAgent 论文](paper/deepAgents.pdf)的结果**，用于展示论文方法的量化参照。数据集和评测入口都在仓库中，想了解 mawoagent 在自己配置下的表现，可以自行运行。

先校验案例，再运行同题的折叠开启/关闭对照。运行命令会调用模型；结果会保存逐题答案、判分、折叠前后的轨迹和汇总。

```bash
python -m agents.eval_folding --cases data/folding_eval_smoke.jsonl --output .mawoagent/folding-eval/smoke-001 --validate-only
python -m agents.eval_folding --cases data/folding_eval_smoke.jsonl --output .mawoagent/folding-eval/smoke-001 --fold-after-turn 2
```

评测入口默认采用参考 DeepAgent 源码的三路并行记忆生成（事件、工作、工具）和每题最多 3 次折叠；可通过 `--fold-memory-mode single_json`、`--max-fold-limit N` 做本项目内部对照。`--fold-after-turn` 是 mawoagent 为短样例增加的固定触发，不是论文原始触发方式。此入口的工具环境与判分器仍不同于论文，生成的分数不能直接对标论文表格。

汇总只对两组都完成、开启组确实折叠、关闭组未折叠且题目工具需求已审核的配对计算正确率；无效配对和原因保留在 `summary.json`。原始 GAIA/HLE 题目需先逐题审核所需工具并整理为带 `required_tools` 的 JSONL，文本题也可能需要当前入口不具备的联网能力。`review_queue.jsonl` 保存需人工复核的预测，详情见 [项目评测.md](项目评测.md)。

为避免单次模型回复过长，评测默认将主模型每次回复限制为 2048 token，可用 `--max-output-tokens` 调整；`--max-cost` 是回复完成后的估算检查，不能代替单次输出上限。

样例仅用于确认评测链路；论文数字的出处、自行评测方式和结果口径见 [项目评测.md](项目评测.md)。

## 开发验证

Python 回归测试使用模拟模型，不请求付费模型：

```bash
source .venv/bin/activate
python -m unittest discover -s tests -q
```

仅检查 Web 后端时：

```bash
python -m unittest discover -s tests -p 'test_web*.py' -q
```

前端语法和单元测试使用 Node.js（CI 使用 Node.js 22），无需安装 npm 包：

```bash
node --check web/app.js
node --test web/tests/*.test.mjs
```

测试覆盖会话增量、草稿与附件、聊天滚动、Markdown 排版，以及 Web 权限与 HTTP 访问边界。模型效果和 Skill 泛化能力需要另行评测。

## 更多文档

- [Web 工作台：界面功能、启动与运行边界](docs/web-workspace.md)
- [运行可靠性与 Skills 发布流程](docs/runtime-hardening.md)
- [项目评测](项目评测.md)
- [在线 Skills 评测实现说明](在线Skills评测实现说明.md)
- [项目深度分析与迭代方向](项目深度分析与迭代方向.md)

## 项目来源与署名

mawoagent 由 **Aoisop** 创建并维护，是在上游 claude-code-from-scratch 基础上继续演进的本地 Agent Runtime。

本项目的基础 Agent Runtime 参考并复用了 [claude-code-from-scratch](https://github.com/Windy3f3f3f3f/claude-code-from-scratch) 的实现，包括工具、提示词、Memory、MCP 和子 Agent 等模块。上游代码采用 MIT 许可，原作者的版权与完整许可文本见仓库根目录 [LICENSE](LICENSE)，应始终保留。mawoagent 的修改与新增部分由 Aoisop 维护；完整署名见 [AUTHORS](AUTHORS) 与 [NOTICE](NOTICE)。

运行时数据统一保存在用户目录的 `~/.mawoagent/` 与项目内的 `.mawoagent/`。

## 关于猫猫

谨以此项目献给猫猫。

愿每一次对话都被好好记住，愿每一段长任务都有人陪着走完。

—— Aoisop
