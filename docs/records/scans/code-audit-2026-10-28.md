# 代码级安全审计（2026-10-28）

> 返回：[文档索引](../README.md)

范围：`agents/` 全部 45 个模块（15,205 行）、`web/` 前端、CI 与依赖配置。
方法：AST 危险构造扫描 + 9 个高风险模块逐行通读 + 对关键怀疑点写 PoC 复现。
版本：v1.0.0（提交 `ef7aa5f`）。

## 摘要

| ID | 严重度 | 问题 | 状态 |
| --- | --- | --- | --- |
| H1 | 高 | 仓库内容（skills / agents / CLAUDE.md / rules）无信任门控直接进入系统提示词 | 已确认 |
| H2 | 高 | `CLAUDE.md` 的 `@include` 可读取任意本地文件并随提示词发送给模型 | 机制已确认 |
| H3 | 中高 | `/undo` 信任 `meta.json` 里的路径，可写到工作区之外 | PoC 已复现 |
| M1 | 中 | `write_file` 直接写记忆目录会重写 MEMORY.md，丢失来源/作用域与条数上限 | PoC 已复现 |
| M2 | 中 | 延迟工具激活是进程级全局状态，跨会话共享 | PoC 已复现 |
| M3 | 中 | `run_shell` 继承完整父进程环境，API 密钥对命令可见 | 已确认 |
| M4 | 中 | 系统提示词按 `cwd` 构建，与 Agent 的 workspace 可能不一致 | 已确认 |
| M5 | 中 | 副本模式下 `copytree` 跟随符号链接，可能复制工作区外的巨型目录 | 已确认 |
| M6 | 中 | `bypassPermissions` 保护了 `write_file` 写策略文件，但 `run_shell` 可写同一文件 | 已确认 |
| L1 | 低 | `is_dangerous()` 从未被调用，危险命令检测是死代码 | 已确认 |
| L2 | 低 | Docker 后端缺少 `--read-only`/`--cap-drop`，默认以容器内 root 运行 | 设计取舍 |
| L3 | 低 | 文件写入的版本检查与 `os.replace` 之间存在 TOCTOU 窗口 | 已确认 |
| L4 | 低 | 数值型环境变量解析失败会以 traceback 崩溃 | 已确认 |

没有发现：已提交的密钥、`eval`/`exec`/`pickle` 等动态执行、SQL 注入面、TLS 校验关闭、明显的路径穿越入口（除 H3）。

## H1 仓库内容无信任门控直接进入系统提示词

**位置**：`agents/skills.py:81`（`discover_skills`）、`agents/subagent.py:117`（`_load_agents_from_dir`）、`agents/prompt.py:168`（`load_claude_md`）、`agents/prompt.py:146`（`_load_rules_dir`）

`is_project_trusted()` 已经用于门控 `.mawoagent/settings.json` 和 `.mcp.json`，但同样的仓库内容没有门控：

```python
# skills.py:101-102
project_dir = project / ".mawoagent" / "skills"
_load_skills_from_dir(project_dir, "project", skills, overwrite=False)
```

`discover_skills()` 的结果进入 `build_system_prompt()`（`prompt.py:219`），把每个技能的 `description` 与 `when_to_use` 写进系统提示词；调用技能时 `prompt_template` 全文作为提示词执行。

**PoC 输出**

```
project trusted: False
skill visible   : True
```

**影响**：克隆一个仓库并运行 `mawoagent`，仓库作者就能把自己的文本放进模型上下文。配合 `--yolo` 或 `acceptEdits`，这等于"打开仓库即执行"。对编码 Agent 来说，"在别人的仓库里运行"是主要场景，不是边缘情况。

**建议**：把 `.mawoagent/skills|agents|rules|CLAUDE.md` 纳入 `project_trust` 门控；未信任时降级为"只显示技能名与来源，不注入正文"，并在首个会话提示一次。

## H2 `@include` 可读取任意本地文件

**位置**：`agents/prompt.py:107`

```python
_INCLUDE_RE = _re.compile(r"^@(\./[^\s]+|~/[^\s]+|/[^\s]+)$", _re.MULTILINE)
```

`~/` 与 `/` 前缀被显式支持，`./../../x` 也能匹配 `\./[^\s]+`，解析后不校验是否仍在项目内：

```python
if raw.startswith("~/"):
    resolved = Path.home() / raw[2:]
elif raw.startswith("/"):
    resolved = Path(raw)
else:
    resolved = base_path / raw
resolved = resolved.resolve()
```

**PoC 输出**（`CLAUDE.md` 内容为 `@~/secret.txt`）

```
'ct Instructions (CLAUDE.md)\n<!-- not found: ~/secret.txt -->'
```

`not found` 证明它确实按 `Path.home()/secret.txt` 去读了；文件存在时就会读入。

**影响**：仓库里写一行 `@~/.ssh/id_rsa`，用户在该仓库启动时，密钥内容进入系统提示词，随首次请求发送给模型服务商。这是"读仓库"到"上传本机文件"的转换，不需要任何工具调用，也不受权限模式约束。

**建议**：限制 `@include` 只能指向工作区内的相对路径；需要外部文件时改为显式配置项。同时对 include 结果加字节上限。

## H3 `/undo` 信任状态文件中的路径

**位置**：`agents/snapshots.py:455`

```python
target = workspace / entry.path        # entry.path 来自 meta.json，未校验
```

`Turn.from_meta()`（`snapshots.py:135`）直接把 JSON 里的 `entries` 反序列化成 `Entry`，`undo_turn` 不做包含性检查。

**PoC 输出**

```
restored: ['../../escaped.txt'] | file exists outside: True
content: written outside the workspace
```

实际落点：`C:\Users\...\AppData\Local\Temp\escaped.txt`，工作区之外。

**影响**：任何能改写 `.mawoagent/snapshots/*/meta.json` 的主体都能借 `/undo` 写任意位置。模型在 `--yolo` 下可以直接 `write_file` 到该路径（`.mawoagent` 属于 runtime_state，默认模式下需要确认，yolo 模式下不需要）。

**建议**：`undo_turn` 内对每个 `entry.path` 做 `resolve()` 并校验仍在 `workspace` 内，越界记为 `skipped`。

## M1 `write_file` 写记忆文件会丢掉来源与上限

**位置**：`agents/tools.py:196`

`_auto_update_memory_index` 是 T17 之前的实现，在 `write_file` 落到记忆目录时触发，自行拼装索引：

```python
lines.append(f"- **[{n}]({f.name})** ({t}) — {d}")
```

它不读 `source`/`scope`，也不受 `MAX_INDEX_ENTRIES` 约束，且直接 `write_text` 覆盖，未走 `state_lock` 与原子写。

**PoC 输出**

```
index after memory_save : - **[guessed thing](...)** (project/inference, project) — d
index after write_file  : - **[guessed thing](...)** (project) — d
```

**影响**：T17 引入的"来源可见"约束，只要模型用 `write_file` 而不是 `memory_save` 写一次记忆就被抹掉；条数上限同样失效。两套实现并存是根因。

**建议**：删除该函数，改调 `memory.save_memory` 或直接 `_update_memory_index()`。

## M2 延迟工具激活是进程级全局状态

**位置**：`agents/tools.py:475`、`896`、`481`

```python
_activated_tools: set[str] = set()
...
_activated_tools.update(tool["name"] for tool in matches)
```

`get_active_tool_definitions()` 用这个集合过滤，所有 Agent 实例共享。

**PoC 输出**

```
agent B before A searched: False
agent B after  A searched: True
```

**影响**：一个会话调用 `tool_search` 后，同进程的所有会话都会看到被激活的工具。对 CLI 无感，对 Web 多会话是状态串味——正是 T20 声称解决的问题，工具激活这一项漏了。

**建议**：把集合移到 `Agent` 实例上（构造时复制默认集），或放进 `workspace_scope` 同级的 ContextVar。

## M3 `run_shell` 继承完整环境变量

**位置**：`agents/sandbox.py:68-74`

```python
process = await asyncio.create_subprocess_shell(
    command, ..., env={**os.environ, **env} if env else None, ...)
```

不传 `env` 时子进程同样继承父进程全部环境变量。

**影响**：`APIKEY`/`OPENAI_API_KEY` 等对命令可见；`env` 的输出会被写入会话 JSON 与工具结果文件。用本地后端时这是设计使然（真实终端也是继承环境），但密钥进入模型上下文这件事没有被明确处理。

**建议**：执行前剥离已知密钥变量（`APIKEY`、`*_API_KEY`、`*_TOKEN`），或至少在每个会话提示一次"shell 可读到密钥"。

## M4 系统提示词按 `cwd` 构建

**位置**：`agents/prompt.py:171`、`186`、`229`、`247`；`agents/agent.py:311`、`624`

`build_system_prompt()` 不接收 workspace，`{{cwd}}`、`CLAUDE.md`、`.mawoagent/rules`、仓库地图全部取自 `Path.cwd()`。T20 把记忆/会话/技能目录改成了跟随 workspace，但提示词没跟上。

**影响**：

- `exec_eval` 在临时副本上跑任务，提示词里的 `{{cwd}}` 和仓库地图仍指向仓库本身；评测测到的行为与实际工作区不符。
- Web 多实例或显式传入 workspace 时，加载的是进程目录的 `CLAUDE.md`，不是项目的那份。

**建议**：`build_system_prompt(workspace=None)`，内部用 `project_root()`；`Agent.__init__` 把自己的 `workspace` 传进去。

## M5 副本模式跟随符号链接

**位置**：`agents/sandbox.py:317`

```python
shutil.copytree(source, target, ignore=shutil.ignore_patterns(*_COPY_IGNORE))
```

`copytree` 默认 `symlinks=False`，会跟进符号链接并复制其指向的内容。工作区里一个指向 `/` 或大目录的链接，会让每次 `run_shell`（copy 模式）尝试复制整个目录树。

**建议**：`symlinks=True` 保留链接，或复制前检测并跳过指向工作区外的链接。

## M6 `bypassPermissions` 的文件保护可被 shell 旁路

**位置**：`agents/tools.py:761`、`799`

`_policy_write_denied` 只拦截 `write_file`/`edit_file`，注释写明"yolo 意味着不询问，不意味着可以改写约束自己的规则"。但 `run_shell` 在 yolo 下完全放行：

```bash
echo '{"permissions":{"deny":[]}}' > .mawoagent/settings.json
```

**影响**：策略文件保护只对文件工具成立，对 shell 不成立。文档若声称"策略文件永不被自动批准"，这个说法对 shell 是假的。

**建议**：要么接受它（`run_shell` 本来就无法约束），并在文档中改成"仅约束文件工具"；要么在 yolo 下对 shell 命令做一次策略路径扫描（成本低，覆盖 `> .mawoagent/`、`>> .mawoagent/`、`tee .mawoagent/`）。

## 低危

- **L1** `tools.py:548` `is_dangerous()` 无调用点。危险命令检测实际上是死代码，`DANGEROUS_PATTERNS` 也不覆盖 `command rm`、`$(rm ...)` 等。要么接进确认提示，要么删除以免误以为有保护。
- **L2** `sandbox.py:169-185`：Docker 参数有 `--network none`、`--memory`、`--cpu`、`--pids-limit`、`no-new-privileges`，但没有 `--read-only`、`--cap-drop ALL`、`--tmpfs /tmp`，也未默认 `--user`。容器内 root 且可写全盘。属加固项。
- **L3** `tools.py:169-177`：`_file_version` 校验与 `os.replace` 之间没有锁，两个并发写入可能都通过校验。窗口很小，本地并发才会触发。
- **L4** `config.py:94-105`：`float(os.environ[...])` 遇到非数字直接抛异常，启动即 traceback。
- `schedule.py:355` 默认 `os.system` 作为 `apply=True` 的执行器。命令由内部拼接（名称已白名单校验），但 Windows 路径含 `"` 会破坏 `schtasks /TR` 的引号。

## 已确认无问题的部分

- **Web 服务**：Host 白名单（防 DNS rebinding）、Origin 与 `Sec-Fetch-Site` 双检、会话 cookie 与 CSRF token 均用 `compare_digest`、请求体 512 KB 上限、CSP 与 `nosniff`/`X-Frame-Options: DENY`、静态资源取自固定表。未发现跨站或路径穿越入口。
- **MCP 客户端**：协议版本固定校验、重定向限 3 次且限同源、令牌只从环境变量读取、取消时发送 `notifications/cancelled`、stdout 帧大小受限。
- **权限判定**：deny 规则优先于 allow，且 `bypassPermissions` 之外的所有模式都会经过 `_workspace_policy_check`；`workspace.classify()` 用 realpath 判断，`..` 与符号链接无法伪装成工作区内路径。
- **快照写入**：`record()` 对工作区外的路径直接忽略（`relative_to` 失败即跳过），不会记录越界条目。
- **存储**：原子写使用同目录临时文件 + `fsync` + `os.replace`；锁文件跨进程生效。
- **依赖**：`pip check` 无冲突；已跟踪文件未发现密钥。

## 未覆盖

- Docker 隔离的实际效果（本机无守护进程），只审了参数构造。
- 真实远程 MCP 服务端的行为。
- `web/` 前端 416 行只做了入口审计，未做完整的 DOM XSS 检查（当前无 `innerHTML` 拼接用户内容的迹象）。
- 依赖 CVE 比对（离线环境无法查询公告库）。

## 建议修复顺序

1. **H2**（一行限制 + 上限）：影响最直接，改动最小。
2. **H1**（信任门控）：需要产品决策"未信任仓库如何降级展示"，但这是编码 Agent 的核心威胁模型。
3. **H3**（undo 包含性校验）：三行改动，消除任意写。
4. **M1/M2**（两处回归）：直接对应 T17/T20 的验收标准，属于"已交付但被绕过"。
5. **M4**（提示词跟随 workspace）：影响 exec_eval 结论的有效性。
6. M3/M5/M6 与低危项按需处理。
