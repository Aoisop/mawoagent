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

Python **3.11, 3.12 and 3.13** all work. CI verifies 3.11 and 3.12 on Linux, macOS and Windows; 3.13 has been verified only on Windows so far, so treat it as untested on the other two platforms (see the [environment baseline](docs/t01-environment-baseline-2026-10-20.md)). The commands below target macOS / Linux. Git is used to fetch the code; ripgrep is optional. Node.js is not required to run the web workbench, but is needed for frontend tests or for MCP tools that depend on Node.js.

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

Two checks confirm a freshly built environment matches the pinned set: `python -m pip check` reports no broken requirements, and `python -m unittest discover -s tests -q` passes offline against the mock model. The results recorded for this repository are in the [environment baseline](docs/t01-environment-baseline-2026-10-20.md).

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
- **MCP external tools**: a stdio JSON-RPC MCP client based on the upstream implementation that exposes external MCP server tools as `mcp__server__tool`.
- **Sub-agents**: `explore`, `plan`, `general`, and custom sub-agents.
- **Session recovery and context compaction**: sessions are saved automatically; `--resume` and `/compact` are supported, and large tool results are truncated or persisted.
- **Per-turn snapshots and undo**: each turn keeps the original content of every file it changes; `/diff` and `/undo` expose it from the CLI and the web API, and undo refuses to overwrite edits made after the turn.

### Architecture

Core runtime path:

```text
User input
  -> agents/main.py (CLI) or agents/web.py (web)
  -> Agent.chat()
  -> build prompt / retrieve Skills / prefetch Memory / initialize MCP
  -> call an OpenAI-compatible or Anthropic-compatible model
  -> model returns text or a tool call
  -> Runtime checks permissions
  -> execute tool / Skill / MCP / sub-agent
  -> write the tool result back to the model
  -> save the session
  -> run skill usage tracking and online skill evolution in the background
```

### Directory layout

```text
mawoagent/
├── agents/
│   ├── main.py                    # CLI entry, REPL, argument parsing
│   ├── web.py                     # web backend: static pages, HTTP API, SSE events
│   ├── config.py                  # shared environment and model config for CLI, web, evaluation
│   ├── agent.py                   # Agent Runtime, model calls, tool dispatch, context compaction
│   ├── context_runtime.py         # context preflight and time-bounded Memory recall shared by both protocols
│   ├── tool_results.py            # model-independent tool result compaction
│   ├── result_storage.py          # persist large tool results, checkpoints, and previews
│   ├── streaming.py               # isolated streaming attempts and fallback output
│   ├── checkpoints.py             # tool execution checkpoints and interrupt recovery
│   ├── tools.py                   # built-in tools and permission system
│   ├── tool_schemas.py            # tool definitions and parameter schemas
│   ├── prompt.py                  # dynamic system prompt construction
│   ├── skills.py                  # Skills loading, retrieval, execution, creation, and evolution
│   ├── online_skill_evolution.py  # online Skill extraction and add/merge/discard decisions
│   ├── skill_evolution.py         # Skill persistence, version snapshots, audit stats
│   ├── memory.py                  # long-term memory system
│   ├── mcp_client.py              # MCP stdio JSON-RPC client
│   ├── subagent.py                # sub-agent configuration
│   ├── session.py                 # session persistence and recovery
│   └── ui.py                      # terminal UI output
├── web/                          # frontend sources, no bundling required
│   ├── index.html                # page structure
│   ├── styles.css                # graphite dark theme and responsive layout
│   ├── app.js                    # page interactions, API requests, SSE subscriptions
│   ├── chat-view.mjs             # Markdown rendering and partial message updates
│   ├── chat-scroll.mjs           # chat scroll control
│   ├── conversation-state.mjs    # session state, drafts, and attachments
│   └── tests/                    # frontend unit tests
├── .mawoagent/
│   ├── skills/                    # project-level Skills
│   └── skill-evolution/           # Skill self-evolution audit artifacts
├── data/                          # local benchmark data only, not distributed (docs/datasets.md)
├── docs/                          # web workbench, runtime reliability, and iteration strategy
├── tests/                         # Python regression tests
├── .github/                       # CI workflows
├── Dockerfile
├── .env.example                  # model configuration template
├── requirements.lock             # pinned dependencies used for installs
├── requirements.txt              # direct dependencies and version ranges
└── README.md
```

### Skill self-evolution

The defining feature of mawoagent is **Skill self-evolution**. It extracts reusable rules from explicit user feedback, stores them as candidates by default, and publishes them to a project-level or user-level `SKILL.md` only after evaluation or explicit manual review.

#### 1. Enable automatic self-evolution

`MAWOAGENT_AUTO_SKILL_EVOLUTION` is enabled by default. For explicit configuration, set it in `.env`:

```env
MAWOAGENT_AUTO_SKILL_EVOLUTION=1
MAWOAGENT_AUTO_SKILL_TARGET=project
```

What they mean:

- `MAWOAGENT_AUTO_SKILL_EVOLUTION=1`: enable online Skill self-evolution.
- `MAWOAGENT_AUTO_SKILL_TARGET=project`: write automatically created Skills to the current project's `.mawoagent/skills/`.

To accumulate personal Skills shared across all projects:

```env
MAWOAGENT_AUTO_SKILL_TARGET=user
```

Corresponding paths:

```text
project: <project>/.mawoagent/skills/<skill_name>/SKILL.md
user:    ~/.mawoagent/skills/<skill_name>/SKILL.md
```

#### 2. Start in a permission mode that allows writes

Saving Skill candidates in the background requires a permission mode that allows maintenance operations. Recommended:

```bash
MAWOAGENT_AUTO_SKILL_EVOLUTION=1 \
MAWOAGENT_AUTO_SKILL_TARGET=project \
python -m agents.main --accept-edits
```

A more aggressive mode is also available:

```bash
python -m agents.main --yolo
```

Do not use `--yolo` as a long-term default, because it skips confirmations. `--accept-edits` is the daily recommendation.

#### 3. Give reusable feedback

Self-evolution is not about turning every sentence into a Skill. It only fits stable, explicit rules that will still apply to similar tasks in the future.

Not suitable for accumulation:

```text
Write me a 500-word government report.
```

Suitable for accumulation:

```text
From now on, when writing government reports, work summaries, or research material, start with a usable draft by default instead of asking follow-up questions. Structure it as title, background, current situation, problem analysis, measures, and next steps, and keep the language formal and restrained.
```

Suitable for evolving an existing Skill:

```text
For this kind of government report, include key data metrics from now on; do not rely only on conceptual statements.
```

#### 4. How the evolution loop works

```text
Turn N: user task
  -> Agent produces a result
  -> save the pending extraction window
Turn N+1: user feedback
  -> merge into the previous window
  -> persist pending feedback with the session, then submit to the background queue (independent of this turn's success)
  -> online_ingest()
  -> Extractor extracts candidate Skills
  -> Maintainer decides add / merge / discard
  -> save isolated candidates: view with /skill-candidates
  -> /skill-eval validates merge candidates for existing Skills
  -> /skill-apply <id> publishes a candidate that passed evaluation
  -> write SKILL.md; /skill-rollback <name> restores the previous version
  -> record provenance, usage stats, and version snapshots
```

Core files:

```text
agents/agent.py
agents/online_skill_evolution.py
agents/skills.py
agents/skill_evolution.py
```

Audit artifacts:

```text
.mawoagent/skill-evolution/usage.jsonl
.mawoagent/skill-evolution/online_provenance.jsonl
.mawoagent/skill-evolution/online_skill_provenance.json
.mawoagent/skill-evolution/skill_usage_stats.json
.mawoagent/skill-evolution/history/
```

### Online Skill regression evaluation

Run `/skill-eval` in the REPL. The first run generates `replay_review.jsonl`; choose "redo the original task" or "feedback correction", bind the target message turn, confirm session ownership and acceptance criteria, then run it again. Questions and context are rebuilt from the source, and duplicate targets are counted once. The current version, the candidate, and the existing champion re-answer the same task batch; same-session windows are grouped, sample count and model calls are capped, and incomplete or abnormal comparisons never get promoted.

These are regression checks for text tasks. Their sources may have been involved in Skill learning and do not represent independent benchmark results. Rules are no longer guessed from Skill keywords; championship snapshots are stored separately. String requirements must pass by default, optional requirements use `{"text":"requirement", "required":false}`, and critical failures cannot be offset by other scores. The `json_schema` check can validate fields and types, and `MAWOAGENT_SKILL_JUDGE_MODEL` can select a separate judge model on the same service.

When a new Skill has no independent replay samples, you can explicitly use `/skill-apply <id> --manual-review` after review; this is a manual release and does not imply the evaluation passed. Background jobs have a concurrency cap and persist pending work; source logs are read as incremental indexes. `/cleanup-state [days]` previews old tool results and evaluation run directories, and deletes only with an explicit `--apply`; the current champion run directory is preserved.

Candidate publishing, permissions, acceptance criteria, and recovery are described in [runtime reliability and Skills publishing](docs/runtime-hardening.md).

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

### Folded comparison evaluation

Early self-run folded evaluation produced low numbers, so the `53.3%`, `20.2%`, and `44.7%` figures used in earlier explanatory material for GAIA / HLE metrics and memory-folding ablation **reuse results from the DeepAgent paper directly** (the paper PDF is not distributed with this repository) as quantitative references for the paper's method. The evaluation entry points are in the repository; the benchmark datasets are not (see `docs/datasets.md`). Fetch them from their publishers and run the evaluation yourself to see how mawoagent performs under your configuration.

Validate the cases first, then run the same cases with folding on and off. The run calls the model and saves per-case answers, scoring, traces before and after folding, and a summary.

```bash
python -m agents.eval_folding --cases tests/fixtures/folding_eval_smoke.jsonl --output .mawoagent/folding-eval/smoke-001 --validate-only
python -m agents.eval_folding --cases tests/fixtures/folding_eval_smoke.jsonl --output .mawoagent/folding-eval/smoke-001 --fold-after-turn 2
```

The evaluation entry point follows DeepAgent's three-way parallel memory generation (events, work, tools) and allows at most 3 folds per case by default; use `--fold-memory-mode single_json` and `--max-fold-limit N` for internal comparisons. `--fold-after-turn` is a fixed trigger added by mawoagent for short samples, not the paper's original trigger. The tool environment and grader still differ from the paper, so the resulting scores cannot be compared directly with the paper's tables.

Accuracy is computed only for pairs where both groups completed, the folding group actually folded, the non-folding group did not fold, and the case's tool requirements were reviewed. Invalid pairs and reasons are recorded in `summary.json`. Raw GAIA / HLE cases must first be reviewed for required tools and converted into JSONL with `required_tools`; text-only cases may also need network access that this entry point does not provide. `review_queue.jsonl` stores predictions that need manual review.

To keep a single model reply from growing too long, evaluation caps each main-model reply at 2048 tokens by default; adjust with `--max-output-tokens`. `--max-cost` is an estimate check after replies complete and does not replace the per-reply output cap.

The samples only verify the evaluation pipeline. For the source of the paper numbers, how to run your own evaluation, and the result conventions, use the paper as the authoritative source; the evaluation approach is described in [runtime reliability notes](docs/runtime-hardening.md).

### Development and verification

Python regression tests use a mock model and do not call paid models:

```bash
source .venv/bin/activate
python -m unittest discover -s tests -q
```

To check only the web backend:

```bash
python -m unittest discover -s tests -p 'test_web*.py' -q
```

Frontend syntax and unit tests use Node.js (CI uses Node.js 22) and require no npm packages:

```bash
node --check web/app.js
node --test web/tests/*.test.mjs
```

Coverage includes session increments, drafts and attachments, chat scrolling, Markdown rendering, and web permission and HTTP access boundaries. Model quality and Skill generalization require separate evaluation.

Coding-task fixtures with fixed initial states, allowed tools, budgets, and programmatic checks live in [tests/fixtures/tasks](tests/fixtures/tasks/README.md); `python -m unittest tests.test_task_fixtures` verifies that every fixture fails unsolved and passes against its reference solution.

### Iteration plan and progress

The roadmap is split into eight iterations (I0–I7) covering 21 tasks and six review-sized PRs. The first release is **I0–I2**: make a local code change reviewable before and recoverable after.

| Iteration | Tasks | Delivered |
| --- | --- | --- |
| I0 Baseline and development environment | 2 | 2 |
| I1 Access range and edit review | 3 | 3 |
| I2 File snapshots and undo | 2 | 2 |
| I3–I7 Code understanding, isolation, tools, learning, automation | 14 | 4 |

Current status: **11 of 21 tasks**: I0–I3 are complete and I4 has started with the execution backend abstraction. The [roadmap](docs/iteration-roadmap-2026-10-19.md) defines every task and its acceptance criteria; the [progress table](docs/iteration-progress.md) records what is delivered, what it was verified with, and which defects are still open.

### More documentation

- [Web workbench: UI features, startup, and runtime boundaries](docs/web-workspace.md)
- [Runtime reliability and the Skills publishing flow](docs/runtime-hardening.md)
- [Changelog](CHANGELOG.md)
- [Iteration roadmap (2026-10-19)](docs/iteration-roadmap-2026-10-19.md)
- [Iteration progress: delivered tasks and open risks](docs/iteration-progress.md)
- [T01 environment baseline: Python setup and test results (2026-10-20)](docs/t01-environment-baseline-2026-10-20.md)
- [Recent changes (2026-10-15)](最近修改记录-2026-10-15.md)

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

Python **3.11、3.12、3.13** 均可使用。CI 在 Linux、macOS 与 Windows 上验证 3.11 和 3.12；3.13 目前只在 Windows 上验证过，其余两个平台尚未测试（见[环境基线](docs/t01-environment-baseline-2026-10-20.md)）。以下命令适用于 macOS / Linux。Git 用于获取代码，ripgrep 可选；仅运行网页工作台不需要 Node.js。运行前端测试或使用依赖 Node.js 的 MCP 工具时，再准备 Node.js 环境。

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

可以用两条检查确认新建环境与锁定版本一致：`python -m pip check` 不报告依赖冲突；`python -m unittest discover -s tests -q` 在离线 mock 模型下全部通过。本仓库记录的实际结果见[环境基线](docs/t01-environment-baseline-2026-10-20.md)。

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

### 项目架构

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

### 目录结构

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
├── data/                          # 仅本地评测数据，不随仓库分发（见 docs/datasets.md）
├── docs/                          # Web 工作台、运行可靠性与迭代方向说明
├── tests/                         # Python 回归测试
├── .github/                       # CI 工作流
├── Dockerfile
├── .env.example                  # 模型配置模板
├── requirements.lock             # 固定版本依赖，安装时使用
├── requirements.txt              # 直接依赖与版本范围
└── README.md
```

### Skills 自进化

mawoagent 的核心特色是 **Skills 自进化**。它从用户明确反馈中抽取未来可复用的规则，默认保存候选；通过评测或明确的人工审核后，才发布到项目级或用户级 `SKILL.md`。

#### 1. 开启自动自进化

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

#### 2. 用允许写入的权限模式启动

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

#### 3. 给出可复用反馈

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

#### 4. 自进化链路如何工作

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

### 在线 Skill 回归评测

REPL 中运行 `/skill-eval`。首次生成 `replay_review.jsonl`，选择“原任务重做 / 反馈修正”、绑定目标消息轮次，确认会话归属和验收条件后再次运行。题目和上下文从来源重建，重复目标只计一次。当前版、候选和已有冠军在同一批任务上重新作答；同会话窗口固定分组，样本数与模型调用量有上限，异常或不完整比较不会晋级。

这些是文本任务的回归检查，来源可能参与过 Skill 学习，不代表独立 benchmark 成绩。规则不再从 Skill 关键词自动猜测；冠军快照单独保存。当前 `requirements` 的字符串默认必须通过，可选项使用 `{"text":"要求", "required":false}`，关键失败不能靠其他分数抵消。`json_schema` 检查可校验字段及类型，`MAWOAGENT_SKILL_JUDGE_MODEL` 可单独指定同服务的裁判模型。

新 Skill 或缺少独立回放样本时，可在审阅后明确使用 `/skill-apply <id> --manual-review`；这是人工发布，不代表通过评测。后台任务有并发上限并保存待办；来源日志按增量索引读取。`/cleanup-state [days]` 预览旧工具结果和评测运行目录，明确加 `--apply` 才删除，当前冠军运行目录保留。

当前候选发布、权限、验收条件和恢复流程详见 [运行可靠性与 Skills 发布流程](docs/runtime-hardening.md)。

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

### 折叠对照评测

mawoagent 早期自行试跑折叠评测时，得到的数据偏低。因此，早期说明材料中用于说明 GAIA / HLE 指标和记忆折叠消融的 `53.3%`、`20.2%`、`44.7%` **直接复用 DeepAgent 论文的结果**（论文 PDF 未随仓库分发），用于展示论文方法的量化参照。评测入口在仓库中，基准数据集不随仓库分发（见 `docs/datasets.md`），需要从各自发布方获取后运行，才能了解 mawoagent 在自己配置下的表现。

先校验案例，再运行同题的折叠开启/关闭对照。运行命令会调用模型；结果会保存逐题答案、判分、折叠前后的轨迹和汇总。

```bash
python -m agents.eval_folding --cases tests/fixtures/folding_eval_smoke.jsonl --output .mawoagent/folding-eval/smoke-001 --validate-only
python -m agents.eval_folding --cases tests/fixtures/folding_eval_smoke.jsonl --output .mawoagent/folding-eval/smoke-001 --fold-after-turn 2
```

评测入口默认采用参考 DeepAgent 源码的三路并行记忆生成（事件、工作、工具）和每题最多 3 次折叠；可通过 `--fold-memory-mode single_json`、`--max-fold-limit N` 做本项目内部对照。`--fold-after-turn` 是 mawoagent 为短样例增加的固定触发，不是论文原始触发方式。此入口的工具环境与判分器仍不同于论文，生成的分数不能直接对标论文表格。

汇总只对两组都完成、开启组确实折叠、关闭组未折叠且题目工具需求已审核的配对计算正确率；无效配对和原因保留在 `summary.json`。原始 GAIA/HLE 题目需先逐题审核所需工具并整理为带 `required_tools` 的 JSONL，文本题也可能需要当前入口不具备的联网能力。`review_queue.jsonl` 保存需人工复核的预测。

为避免单次模型回复过长，评测默认将主模型每次回复限制为 2048 token，可用 `--max-output-tokens` 调整；`--max-cost` 是回复完成后的估算检查，不能代替单次输出上限。

样例仅用于确认评测链路；论文数字以论文原文为准；自行评测的方式与结果口径见 [运行可靠性说明](docs/runtime-hardening.md)。

### 开发验证

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

带固定初始状态、允许工具、预算和程序化检查的编码任务样本放在 [tests/fixtures/tasks](tests/fixtures/tasks/README.md)；执行 `python -m unittest tests.test_task_fixtures` 会校验每个样本在未解决时验收失败、套用参考解后验收通过。

### 迭代计划与进度

迭代计划分为八个迭代（I0–I7），共 21 个任务和 6 个按评审单元拆分的 PR。首版范围是 **I0–I2**：让本地代码修改在动手前可审阅、改动后可恢复。

| 迭代 | 任务数 | 已交付 |
| --- | --- | --- |
| I0 基线与开发环境 | 2 | 2 |
| I1 访问范围与修改审阅 | 3 | 3 |
| I2 文件快照与撤销 | 2 | 2 |
| I3–I7 代码理解、隔离执行、工具、学习、自动化 | 14 | 4 |

当前进度：**21 个任务已完成 11 个**，I0–I3 全部完成，I4 已从执行后端抽象开始。[计划表](docs/iteration-roadmap-2026-10-19.md)定义每个任务及其验收标准，[进度表](docs/iteration-progress.md)记录已交付内容、对应的验证证据，以及尚未闭合的缺陷。

### 更多文档

- [Web 工作台：界面功能、启动与运行边界](docs/web-workspace.md)
- [运行可靠性与 Skills 发布流程](docs/runtime-hardening.md)
- [版本记录](CHANGELOG.md)
- [项目迭代计划表（2026-10-19）](docs/iteration-roadmap-2026-10-19.md)
- [迭代进度表：已交付任务与未闭合风险](docs/iteration-progress.md)
- [T01 环境基线：Python 环境与测试结果（2026-10-20）](docs/t01-environment-baseline-2026-10-20.md)
- [最近修改记录（2026-10-15）](最近修改记录-2026-10-15.md)

### 项目来源与署名

mawoagent 由 **Aoisop** 创建并维护，是在上游 claude-code-from-scratch 基础上继续演进的本地 Agent Runtime。

本项目的基础 Agent Runtime 参考并复用了 [claude-code-from-scratch](https://github.com/Windy3f3f3f3f/claude-code-from-scratch) 的实现，包括工具、提示词、Memory、MCP 和子 Agent 等模块。上游代码采用 MIT 许可，其版权与完整许可文本以上游仓库为准；仓库根目录的 [LICENSE](LICENSE) 适用于 mawoagent 自身代码，维护者与第三方组件信息见 [AUTHORS](AUTHORS) 与 [NOTICE](NOTICE)。

运行时数据统一保存在用户目录的 `~/.mawoagent/` 与项目内的 `.mawoagent/`。

### 关于猫猫

谨以此项目献给猫猫。

愿每一次对话都被好好记住，愿每一段长任务都有人陪着走完。

—— Aoisop
