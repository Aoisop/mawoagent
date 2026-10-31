# mawoagent

[English](#en) · [简体中文](#zh)

<a name="en"></a>

## English

mawoagent is a local, Python-based **Agent Runtime** created and maintained by Aoisop, with both a web workbench and a command-line entry point. It connects model reasoning, tool execution, permission confirmation, session recovery, long-term memory, Skills, and self-evolution workflows, making it useful for project analysis and coding assistance as well as for learning how an agent runtime works and extending it.

The focus is the **Harness**: the model proposes actions, and the Runtime decides permissions, executes tools, persists state, and writes results back. The web UI and the CLI share the same capabilities.

### Quick start: frontend and backend

**Run `python -m agents.web` to start both the backend API and the frontend.** The frontend is plain HTML / CSS / JavaScript served by the same Python process. There is no need to install npm dependencies or run a separate frontend dev server.

| Part | Source | How to start or access |
| --- | --- | --- |
| Backend service | `agents/web.py`, `agents/` | `python -m agents.web` |
| Frontend page | `web/` | Open <http://127.0.0.1:8765> after the backend starts |
| CLI entry point | `agents/main.py` | `python -m agents.main`, when needed |

#### 1. Prepare the environment and install dependencies

Python **3.11, 3.12 and 3.13** all work. CI verifies 3.11 and 3.12 on Linux, macOS and Windows; 3.13 has been verified only on Windows so far, so treat it as untested on the other two platforms (see the [environment baseline](docs/records/t01-environment-baseline-2026-10-20.md)). The commands below target macOS / Linux. Git is used to fetch the code; ripgrep is optional. Node.js is not required to run the web workbench, but is needed for frontend tests or for MCP tools that depend on Node.js.

Run these commands in the **mawoagent repository root**:

```bash
# Check the Python version
python3 --version

# Create a virtual environment on first use
python3 -m venv .venv
source .venv/bin/activate

# Install from the pinned dependency set
python -m pip install -r requirements.lock
```

If the default `python3` is not suitable, create the environment with `python3.12 -m venv .venv` instead, substituting whichever supported version you actually have. After the first setup, you only need to activate the existing environment; do not create or install it again.

On Windows, the same steps can be done in PowerShell:

```powershell
py -3 -m venv .venv          # or py -3.12, py -3.11, py -3.13
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
Copy-Item .env.example .env   # skip if .env already exists
```

Start later with `.venv\Scripts\python.exe -m agents.web`, or activate the environment with `.venv\Scripts\Activate.ps1` and run `python -m agents.web`.

Two checks confirm a freshly built environment matches the pinned set: `python -m pip check` reports no broken requirements, and `python -m unittest discover -s tests -q` passes offline against the mock model. The results recorded for this repository are in the [environment baseline](docs/records/t01-environment-baseline-2026-10-20.md).

#### 2. Configure the model connection

Create the local configuration file first; keep an existing `.env`:

```bash
test -f .env || cp .env.example .env
```

Open `.env` in the repository root and replace the key, endpoint URL, and model ID with your provider's values. Choose **one** of the two approaches below. If you use provider-specific variables, remove or comment out the `APIKEY` and `API` entries enabled in the template so the configurations are not mixed.

**OpenAI-compatible endpoint:**

```env
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://your-provider.example/v1
MODEL=your-model-id
```

**Anthropic-compatible endpoint:**

```env
ANTHROPIC_API_KEY=your-api-key
ANTHROPIC_BASE_URL=https://your-provider.example/anthropic
MODEL=your-model-id
```

The URLs and model names above are placeholders; use the values from your provider. The generic `APIKEY` / `API` variables are also supported.

Configuration rules:

- `.env` is looked up upward from the current working directory; environment variables already set in the shell take precedence over `.env`.
- The generic `API` takes precedence over provider-specific base URLs; the generic key likewise takes precedence over provider-specific keys.
- `OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL` select their protocol explicitly. When using the generic `API`, native `api.anthropic.com`, URLs ending in `/anthropic`, or URLs containing `/anthropic/` use the Anthropic protocol; everything else uses the OpenAI protocol.
- The model name comes from `MODEL`; the CLI can override it with `--model`. The web entry point selects the model through environment configuration.
- Restart the service after editing `.env`. Keys stay on the backend and are never entered or displayed in the frontend.

Without a configured model you can still open the workbench to browse the UI and existing data, but configuration is required before sending tasks.

**File access policy** (optional, in `.mawoagent/settings.json`):

```json
{
  "security": {
    "workspace_only_reads": false,
    "workspace_only_writes": true,
    "protected_paths": [
      ".mawoagent/settings.json",
      ".mcp.json",
      ".mawoagent/rules",
      ".mawoagent/skills",
      ".mawoagent/agents"
    ]
  }
}
```

Reads inside the workspace are always allowed. With the defaults above, reading outside the workspace is allowed, while writing outside it asks for confirmation; `dontAsk` and `plan` turn that request into a denial, and `acceptEdits` does not bypass it. A matching `permissions.allow` rule is the explicit way to widen the range for one tool or path.

`protected_paths` covers the files that define the policy itself and the files that are read into later prompts: the settings file, `.mcp.json`, `.mawoagent/rules/`, `.mawoagent/skills/` and `.mawoagent/agents/`. Writes there need confirmation, and `--yolo` (`bypassPermissions`) denies them outright — skipping confirmations must not let a session rewrite the rules that constrain it.

Resolution order is built-in defaults, then `~/.mawoagent/settings.json`, then the project file. The project layer only applies when its exact content is trusted; editing it invalidates that trust and drops the project layer until the project is trusted again. Paths are classified on their real form, so `..`, symbolic links and junctions cannot step outside the root while appearing local.

This governs the built-in file tools only. `run_shell` runs with the user's own OS permissions and is not sandboxed by the policy.

#### 3. Start the backend

Open a terminal at the repository root:

```bash
source .venv/bin/activate
python -m agents.web
```

When you see the following, the service is up; keep the terminal running:

```text
mawoagent Web → http://127.0.0.1:8765
按 Ctrl+C 停止；模型配置沿用项目 .env。
```

You can also call the virtual environment's Python directly, skipping activation:

```bash
.venv/bin/python -m agents.web
```

#### 4. Open the frontend

Visit **[http://127.0.0.1:8765](http://127.0.0.1:8765)** in your browser.

The frontend talks to `/api/*` on the same origin and receives replies and tool status over SSE. From the workbench you can create sessions, describe tasks, watch execution, and allow or deny permission requests. Skills, evaluation, memory, and settings are reachable from the sidebar.

Frontend files are served directly by the backend, so **no `npm install`, `npm run dev`, or build step is required**. Do not open `web/index.html` directly and do not put it behind a separate static server: the full UI needs the same-origin API, the session cookie, and request validation.

#### 5. Ports, restarts, and development

The default port is `8765`; to change it:

```bash
python -m agents.web --port 8766
```

Then open [http://127.0.0.1:8766](http://127.0.0.1:8766). The frontend automatically uses the new same-origin address; there is no separate API configuration to update.

> **Binding to `0.0.0.0` hands the agent to the network.** `MAWOAGENT_WEB_HOST=0.0.0.0` disables every protection the UI has: the `Host` allowlist defends against DNS rebinding, not against a client that sets the header; `GET /` issues a session cookie without credentials; the CSRF token is returned by `GET /api/config`; and a pending approval can be approved by the same client that triggered it. Use an SSH tunnel instead — see [docs/web-workspace.md](docs/web-workspace.md).

| Change | How it takes effect |
| --- | --- |
| Existing HTML, CSS, or JS files in `web/` | Save and refresh the browser; there is no hot reload |
| Python backend code or `.env` | Press `Ctrl+C` in the service terminal, start again, then refresh the page |
| New frontend module or static asset | Register the path in the static whitelist in `agents/web.py`, restart, then refresh |

Closing the browser does not stop the service. Press `Ctrl+C` in the terminal that started it. Use one service process for the current project; when you switch ports, open the corresponding URL.

#### Common startup problems

| Symptom | Resolution |
| --- | --- |
| `ModuleNotFoundError: No module named 'anthropic'` or similar dependency errors | Activate `.venv` and reinstall `requirements.lock`, or start with `.venv/bin/python` |
| `No module named agents` | Make sure the terminal is at the mawoagent repository root |
| `Address already in use` | Stop the old service on that port, or start with `--port 8766` and open the new address |
| The page stays on "Loading" and buttons or icons do nothing | After updating code, restart the Python service and refresh; an old process may not have registered new frontend modules |
| Model not configured, authentication failure, or unknown model | Check the key, base URL, model ID, and the precedence between shell environment variables and `.env`; restart after changes |
| Request validation failure or a dropped connection | Confirm the service is still running and refresh the page on the current port to re-establish the local session |

The web service always listens on `127.0.0.1` and is intended for single-user local use. The current `Dockerfile` starts the CLI by default and does not include the `web/` static assets; run the workbench locally as described above. See [Web workbench notes](docs/web-workspace.md) for UI features and runtime boundaries.

### CLI usage

The CLI and the web UI are two optional entry points; you do not need both. These commands also run at the repository root with the virtual environment activated.

```bash
# Interactive conversation
python -m agents.main

# Run a single task
python -m agents.main "summarize this project's directory structure and core modules"

# Read-only analysis and planning
python -m agents.main --plan "how should the Skills retrieval logic be improved?"

# Resume the most recent session for the current project
python -m agents.main --resume

# Show all options and built-in commands
python -m agents.main --help
```

In interactive mode you can type tasks directly or use commands such as `/plan`, `/skills`, `/memory`, and `/compact`.

Every user turn is snapshotted: the first time a file changes, its previous content is kept under `.mawoagent/snapshots`. `/diff` shows what the latest turn changed, `/undo` reverts it, and `/undo 2` reaches one turn further back. Files edited by hand after the turn are reported as conflicts and left alone. The same two operations are available over the web API at `GET /api/sessions/<id>/diff` and `POST /api/sessions/<id>/undo`.

A write or an edit is shown as a unified diff before it runs, bound to the exact file version it was built from. If the file changes while you are deciding, nothing is written and the change has to be repeated against the new content.

Shell commands run through a pluggable backend. `MAWOAGENT_SANDBOX=local` (default) runs on the host; `MAWOAGENT_SANDBOX=docker` runs each command in a container built from `Dockerfile.sandbox`, with only the workspace mounted, no network, and memory/CPU/PID limits. The container path is implemented and unit-tested but has not been exercised against a real daemon yet; run `python -m agents.sandbox_check` there. `MAWOAGENT_SANDBOX_MODE=copy` runs commands on a throwaway copy and reports what they would change.

`/symbols` outlines the workspace and `/symbols <query>` searches it. The same index feeds a short repository map in the system prompt, capped by `context.repo_map_budget`. Set `"context": {"repo_map": false}` in `settings.json`, or `MAWOAGENT_REPO_MAP=off`, to stop injecting it; the index is cached under `.mawoagent/repo-index.json` and only re-parsed for files that changed.

### Core capabilities

- **Web workbench**: chat, streaming replies, tool authorization, task details, Skills, evaluation, memory, and settings, all sharing the local Runtime.
- **General Agent Runtime**: unified orchestration of models, tools, permissions, context, Memory, Skills, and external extensions.
- **Complete Agent Loop**: model request, tool-call parsing, permission checks, tool execution, tool-result write-back, continued reasoning, and session persistence form a closed loop.
- **Dual protocol**: supports OpenAI-compatible and Anthropic-compatible endpoints.
- **Tool system and permission control**: file read/write, precise edits, search, shell, Skill invocation, sub-agents, and MCP tools; Plan Mode blocks writes and shell commands.
- **Skills**: reusable methods stored in project-level and user-level `SKILL.md` files, with retrieval, invocation, inline / fork execution, and versioned evolution.
- **Long-term Memory**: memories isolated by a hash of the project path, storing user preferences, project background, past decisions, and reference material.
- **Skill self-evolution**: extracts reusable rules from user feedback, stores isolated candidates, and publishes them to `SKILL.md` only after evaluation or explicit manual review.
- **Typed tool results**: text results keep their exact previous form, while images and resource links are stored once and referenced by path, so a base64 payload never enters the conversation.
- **MCP external tools**: a stdio JSON-RPC MCP client based on the upstream implementation that exposes external MCP server tools as `mcp__server__tool`.
- **Sub-agents**: `explore`, `plan`, `general`, and custom sub-agents.
- **Session recovery and context compaction**: sessions are saved automatically; `--resume` and `/compact` are supported, and large tool results are truncated or persisted.
- **Per-turn snapshots and undo**: each turn keeps the original content of every file it changes; `/diff` and `/undo` expose it from the CLI and the web API, and undo refuses to overwrite edits made after the turn.

### Documentation

| Topic | Document |
| --- | --- |
| Architecture and directory layout | [docs/reference/architecture.md](docs/reference/architecture.md) |
| Skills: format, self-evolution, regression evaluation | [docs/guide/skills.md](docs/guide/skills.md) |
| Sessions, permissions, runtime boundaries | [docs/guide/permissions.md](docs/guide/permissions.md) |
| Folding and evaluation methodology | [docs/guide/evaluation.md](docs/guide/evaluation.md) |
| Development and verification | [docs/guide/development.md](docs/guide/development.md) |
| CLI, configuration, scheduling, web, datasets | [docs/README.md](docs/README.md) |
### Iteration plan and progress

The roadmap is split into eight iterations (I0–I7) covering 21 tasks and six review-sized PRs.

| Iteration | Tasks | Delivered |
| --- | --- | --- |
| I0 Baseline and development environment | 2 | 2 |
| I1 Access range and edit review | 3 | 3 |
| I2 File snapshots and undo | 2 | 2 |
| I3 Code understanding and task fixtures | 3 | 3 |
| I4 Isolated execution | 2 | 2 |
| I5 Browser and remote tools | 3 | 3 |
| I6 Learning and automation | 3 | 3 |
| I7 Packaging and release | 3 | 3 |

Current status: **21 of 21 tasks**, released as **v1.0.0**. Two things still need another environment to verify: behaviour inside a container, and a real remote MCP server. The [roadmap](docs/iteration-roadmap-2026-10-19.md) defines every task and its acceptance criteria; the [progress table](docs/iteration-progress.md) records what is delivered, what it was verified with, and which defects are still open.

### More documentation

Everything else lives in [docs/](docs/README.md), which lists each document and what question it answers:

- [CLI, configuration, scheduling, web, datasets](docs/README.md) — the index
- [Architecture and directory layout](docs/reference/architecture.md)
- [Skills: format, self-evolution, regression evaluation](docs/guide/skills.md)
- [Sessions, permissions and runtime boundaries](docs/guide/permissions.md)
- [Evaluation methodology](docs/guide/evaluation.md)
- [Development and verification](docs/guide/development.md)
- [Runtime reliability and the Skills publishing flow](docs/runtime-hardening.md)
- [Web workbench](docs/web-workspace.md)
- [Changelog](CHANGELOG.md)

### Origin and attribution

mawoagent is created and maintained by **Aoisop** and continues to evolve from the upstream claude-code-from-scratch project as a local Agent Runtime.

The base Agent Runtime references and reuses the implementation of [claude-code-from-scratch](https://github.com/Windy3f3f3f3f/claude-code-from-scratch), including tools, prompts, Memory, MCP, and sub-agents. Upstream code is MIT-licensed; its copyright and full license text are authoritative in the upstream repository. The [LICENSE](LICENSE) in this repository covers mawoagent's own code, and maintainer and third-party component information is in [AUTHORS](AUTHORS) and [NOTICE](NOTICE).

Runtime data is stored under the user directory `~/.mawoagent/` and inside the project at `.mawoagent/`.

### About the cat

This project is dedicated to the cat.

May every conversation be remembered well, and may every long task have someone to walk it to the end.

— Aoisop

<a name="zh"></a>
## 简体中文

mawoagent 是一个由 Aoisop 创建和维护的、基于 Python 的本地 **Agent Runtime**，同时提供网页工作台和命令行入口。它把模型推理、工具执行、权限确认、会话恢复、长期记忆、Skills 与自进化流程连接在一起，适合用于项目分析、编程辅助，也适合学习和扩展 Agent 的运行机制。

项目重点是 **Harness**：模型提出操作意图，Runtime 负责判断权限、执行工具、保存状态和回写结果。网页与 CLI 共用这套能力。

### 快速启动：前端与后端

**运行 `python -m agents.web`，即可同时启动后端 API 和前端页面。** 当前前端采用原生 HTML / CSS / JavaScript，由同一个 Python 服务提供，无需安装 npm 依赖或单独运行前端开发服务器。

| 部分 | 源码位置 | 启动或访问方式 |
| --- | --- | --- |
| 后端服务 | `agents/web.py`、`agents/` | `python -m agents.web` |
| 前端页面 | `web/` | 后端启动后，打开 <http://127.0.0.1:8765> |
| 命令行入口 | `agents/main.py` | `python -m agents.main`，按需使用 |

#### 1. 准备环境并安装依赖

Python **3.11、3.12、3.13** 均可使用。CI 在 Linux、macOS 与 Windows 上验证 3.11 和 3.12；3.13 目前只在 Windows 上验证过，其余两个平台尚未测试（见[环境基线](docs/records/t01-environment-baseline-2026-10-20.md)）。以下命令适用于 macOS / Linux。Git 用于获取代码，ripgrep 可选；仅运行网页工作台不需要 Node.js。运行前端测试或使用依赖 Node.js 的 MCP 工具时，再准备 Node.js 环境。

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

如果默认 `python3` 版本不合适，可将创建虚拟环境的命令改为 `python3.12 -m venv .venv`，替换成本机实际安装的受支持版本。后续启动时只需激活已有虚拟环境，不必重复创建或安装。

Windows 用户可以用 PowerShell 完成同样的步骤：

```powershell
py -3 -m venv .venv          # 也可用 py -3.12、py -3.11、py -3.13
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
Copy-Item .env.example .env   # 已有 .env 时跳过
```

后续启动使用 `.venv\Scripts\python.exe -m agents.web`，或先执行 `.venv\Scripts\Activate.ps1` 再运行 `python -m agents.web`。

可以用两条检查确认新建环境与锁定版本一致：`python -m pip check` 不报告依赖冲突；`python -m unittest discover -s tests -q` 在离线 mock 模型下全部通过。本仓库记录的实际结果见[环境基线](docs/records/t01-environment-baseline-2026-10-20.md)。

#### 2. 配置模型连接

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

**文件访问策略**（可选，写在 `.mawoagent/settings.json`）：

```json
{
  "security": {
    "workspace_only_reads": false,
    "workspace_only_writes": true,
    "protected_paths": [
      ".mawoagent/settings.json",
      ".mcp.json",
      ".mawoagent/rules",
      ".mawoagent/skills",
      ".mawoagent/agents"
    ]
  }
}
```

工作区内的读取始终允许。按上面的默认值，读取工作区之外放行，写入工作区之外则要求确认；`dontAsk` 与 `plan` 模式下该确认转为拒绝，`acceptEdits` 不会绕过它。要为某个工具或路径显式扩大范围，应添加对应的 `permissions.allow` 规则。

`protected_paths` 覆盖两类文件：定义策略本身的文件，以及会被读进后续提示词的文件——设置文件、`.mcp.json`、`.mawoagent/rules/`、`.mawoagent/skills/`、`.mawoagent/agents/`。写入这些位置需要确认；`--yolo`（`bypassPermissions`）下则直接拒绝，因为"跳过确认"不能变成"允许会话改写约束自己的规则"。

解析顺序为：内置默认值、`~/.mawoagent/settings.json`、项目配置文件。项目层只在配置内容被精确信任时生效；修改该文件会使信任失效，项目层随之停用。路径按真实形态判定，`..`、符号链接与 junction 都无法在看起来本地的情况下越出根目录。

该策略只约束内置文件工具。`run_shell` 以用户自身的系统权限运行，不受此策略沙箱限制。

#### 3. 启动后端服务

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

#### 4. 打开前端页面

在浏览器访问 **[http://127.0.0.1:8765](http://127.0.0.1:8765)**。

前端会连接同一地址下的 `/api/*`，通过 SSE 接收回复和工具执行状态。可以在工作台创建会话、描述任务、查看执行过程，并在需要权限时允许或拒绝；Skills、评测、记忆和设置也从侧栏进入。

前端文件由后端直接提供，**不需要 `npm install`、`npm run dev` 或构建步骤**。不要双击 `web/index.html`，也不要用独立静态服务器代替 `agents.web`：完整界面需要同源 API、会话 Cookie 和请求校验。

#### 5. 端口、重启与开发调试

默认端口为 `8765`，如需更换：

```bash
python -m agents.web --port 8766
```

此时访问 [http://127.0.0.1:8766](http://127.0.0.1:8766)。端口更改后，前端会自动使用新的同源地址，无需单独修改 API 配置。

> **绑定 `0.0.0.0` 等于把这个 Agent 交给网络。** 设置 `MAWOAGENT_WEB_HOST=0.0.0.0` 会让界面现有的所有防护同时失效：`Host` 白名单防的是浏览器 DNS rebinding，挡不住自己填这个头的客户端；`GET /` 不需要任何凭据就下发会话 Cookie；CSRF 标识由 `GET /api/config` 明文返回；待审批的操作可以被同一个客户端自行放行。需要远程访问请用 SSH 隧道，详见 [docs/web-workspace.md](docs/web-workspace.md)。

| 修改内容 | 生效方式 |
| --- | --- |
| `web/` 中已有的 HTML、CSS、JS 文件 | 保存后刷新浏览器；没有自动热更新 |
| Python 后端代码或 `.env` | 在服务终端按 `Ctrl+C`，再运行启动命令，随后刷新页面 |
| 新增前端模块或静态资源文件 | 在 `agents/web.py` 的静态资源白名单中注册路径，重启服务后刷新页面 |

关闭浏览器不会停止服务。停止服务请在启动它的终端按 `Ctrl+C`。平时使用一个服务进程操作当前项目即可；切换端口时注意打开对应的新地址。

#### 常见启动问题

| 现象 | 处理方式 |
| --- | --- |
| `ModuleNotFoundError: No module named 'anthropic'` 等依赖错误 | 激活 `.venv` 后重新安装 `requirements.lock`，或直接用 `.venv/bin/python` 启动 |
| `No module named agents` | 确认终端位于 mawoagent 仓库根目录 |
| `Address already in use` | 停止占用该端口的旧服务，或用 `--port 8766` 启动并访问新地址 |
| 页面一直停在“正在加载”，按钮或图标无反应 | 如果刚更新代码，先重启 Python 服务再刷新页面；旧进程可能未注册新增的前端模块 |
| 模型未配置、鉴权失败或模型不存在 | 核对密钥、Base URL、模型 ID，以及终端环境变量与 `.env` 的优先级；修改后重启 |
| 出现请求校验失败或连接中断 | 确认服务仍在运行，刷新当前端口的页面，以重新建立本地会话 |

Web 服务固定监听 `127.0.0.1`，面向本机单用户使用。当前 `Dockerfile` 默认启动 CLI，且未包含 `web/` 静态资源；网页工作台请按上述本地方式启动。更多界面功能与运行边界见 [Web 工作台说明](docs/web-workspace.md)。

### 命令行使用

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

每一轮对话都会留快照：某次修改发生前的内容会存到 `.mawoagent/snapshots`。`/diff` 查看最近一轮改了什么，`/undo` 撤销它，`/undo 2` 再往前一轮。若文件在该轮之后被手工改过，撤销会将其列为冲突并跳过。同样两个操作也提供 Web 接口：`GET /api/sessions/<id>/diff` 与 `POST /api/sessions/<id>/undo`。

写入或编辑在执行前会以 unified diff 呈现，并绑定提案所依据的文件版本。若在你审阅期间文件发生变化，则不会写入，需要基于新内容重新发起。

Shell 命令走可插拔的执行后端：`MAWOAGENT_SANDBOX=local`（默认）在宿主机执行；`MAWOAGENT_SANDBOX=docker` 则在由 `Dockerfile.sandbox` 构建的容器里执行，只挂载工作区、无网络、限制内存与 CPU。容器路径已实现并有单元测试，但尚未在真实 Docker 守护进程上验证；到有 Docker 的机器上执行 `python -m agents.sandbox_check` 即可。`MAWOAGENT_SANDBOX_MODE=copy` 会在副本上执行命令并报告改动，不触碰工作区。

`/symbols` 输出工作区符号总览，`/symbols <查询>` 用于检索。同一份索引还会在系统提示词里生成一段简短的仓库地图，长度受 `context.repo_map_budget` 限制。在 `settings.json` 里设置 `"context": {"repo_map": false}`，或设置环境变量 `MAWOAGENT_REPO_MAP=off`，即可关闭注入；索引缓存在 `.mawoagent/repo-index.json`，只对发生变化的文件重新解析。

### 核心能力

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
- **轮次快照与撤销**：每轮记录被修改文件的原始内容，CLI 与 Web 接口提供 `/diff` 和 `/undo`；撤销不会覆盖该轮之后的手工修改。

### 文档

| 主题 | 文档 |
| --- | --- |
| 架构与目录结构 | [docs/reference/architecture.md](docs/reference/architecture.md) |
| Skills：格式、自进化、回归评测 | [docs/guide/skills.md](docs/guide/skills.md) |
| 会话、权限与运行边界 | [docs/guide/permissions.md](docs/guide/permissions.md) |
| 折叠对照与评测口径 | [docs/guide/evaluation.md](docs/guide/evaluation.md) |
| 开发与验证 | [docs/guide/development.md](docs/guide/development.md) |
| 命令行、配置、调度、Web、数据集 | [docs/README.md](docs/README.md) |

### 迭代计划与进度

迭代计划分为八个迭代（I0–I7），共 21 个任务和 6 个按评审单元拆分的 PR。

| 迭代 | 任务数 | 已交付 |
| --- | --- | --- |
| I0 基线与开发环境 | 2 | 2 |
| I1 访问范围与修改审阅 | 3 | 3 |
| I2 文件快照与撤销 | 2 | 2 |
| I3 代码理解与任务夹具 | 3 | 3 |
| I4 隔离执行 | 2 | 2 |
| I5 浏览器与远程工具 | 3 | 3 |
| I6 学习与自动化 | 3 | 3 |
| I7 打包与发布 | 3 | 3 |

当前进度：**21 个任务全部完成**，已发布 **v1.0.0**。仍有两项需要在其他环境验证：容器内行为与真实远程 MCP 服务端。[计划表](docs/iteration-roadmap-2026-10-19.md)定义每个任务及其验收标准，[进度表](docs/iteration-progress.md)记录已交付内容、对应的验证证据，以及尚未闭合的缺陷。

### 更多文档

其余文档都在 [docs/](docs/README.md)，索引里写明每份文档回答什么问题：

- [文档索引](docs/README.md)
- [架构与目录结构](docs/reference/architecture.md)
- [Skills：格式、自进化、回归评测](docs/guide/skills.md)
- [会话、权限与运行边界](docs/guide/permissions.md)
- [评测口径](docs/guide/evaluation.md)
- [开发与验证](docs/guide/development.md)
- [运行可靠性与 Skills 发布流程](docs/runtime-hardening.md)
- [Web 工作台](docs/web-workspace.md)
- [版本记录](CHANGELOG.md)

### 项目来源与署名

mawoagent 由 **Aoisop** 创建并维护，是在上游 claude-code-from-scratch 基础上继续演进的本地 Agent Runtime。

本项目的基础 Agent Runtime 参考并复用了 [claude-code-from-scratch](https://github.com/Windy3f3f3f3f/claude-code-from-scratch) 的实现，包括工具、提示词、Memory、MCP 和子 Agent 等模块。上游代码采用 MIT 许可，其版权与完整许可文本以上游仓库为准；仓库根目录的 [LICENSE](LICENSE) 适用于 mawoagent 自身代码，维护者与第三方组件信息见 [AUTHORS](AUTHORS) 与 [NOTICE](NOTICE)。

运行时数据统一保存在用户目录的 `~/.mawoagent/` 与项目内的 `.mawoagent/`。

### 关于猫猫

谨以此项目献给猫猫。

愿每一次对话都被好好记住，愿每一段长任务都有人陪着走完。

—— Aoisop
