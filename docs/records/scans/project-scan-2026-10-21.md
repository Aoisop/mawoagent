# mawoagent 项目全面扫描报告

> **历史记录**：本文记录当时的判断与状态，不再随代码更新。当前行为请以 [项目 README](../../../README.md) 与 [文档索引](../../README.md) 中的现行文档为准。


扫描日期：2026-10-21（夜间补充轮）｜范围：`agents/`、`tests/`、`web/`、`docs/`、CI、依赖、仓库治理
方法：静态通读 + 运行验证（272 用例本地全绿）+ 与 `docs/GitHub风险扫描-2026-10-21.md` 交叉复核。

> 说明：本次扫描独立于 10-21 早前那份 GitHub 风险扫描。早前报告的 P0/P1 大部分已修复（见第 1 节），本报告聚焦**尚未解决的问题**与**新增发现**。

---

## 一、基线结论

| 项 | 状态 |
| --- | --- |
| 本地测试 | ✅ 272 passed, 8 skipped（跳过均为 Windows 平台限制） |
| 依赖漏洞 | ✅ 30 个锁定包，OSV 复查 0 命中；`requirements.lock` 含 438 个 sha256 hash |
| 凭据泄露 | ✅ 无硬编码密钥；`.env` 从未进入 Git 历史 |
| 早前 P0（certifi / yolo 绕过 / data 分发） | ✅ 已修复 |
| 早前 P1（MCP 版本钉死 / 受保护路径 / 依赖完整性） | ✅ 已修复 |
| 早前 P2（data 历史清理 / 实例状态 / 治理） | ⚠️ 大部分未做 |

**整体判断**：这是一个安全基线明显高于同类本地 Agent 项目的代码库。Web 四重校验（Host/Origin/Sec-Fetch-Site/CSRF）+ 严格 CSP、workspace 策略层、原子写、崩溃恢复检查点、依赖 hash 锁定都做得扎实。剩余问题集中在**跨平台缺陷**、**架构演进瓶颈**、**仓库治理**三块。

---

## 二、阻断级（P0）：影响核心功能

### 2.1 Windows 上 MCP 启动必然失败

`agents/mcp_client.py:114` 用 `create_subprocess_exec` 直接执行命令：

```python
self._process = await asyncio.create_subprocess_exec(
    self.command, *self.args, ...
)
```

`.mcp.json` 配置的是 `"command": "npx"`。Windows 上 `npx` 是 `npx.cmd` 批处理包装器，`CreateProcess` 不走 `PATHEXT` 解析，实测 `WinError 2`（文件找不到）。需改为先 `shutil.which()` 解析出完整可执行路径（或在 Windows 上自动补 `.cmd`）。

影响：README "Core capabilities" 宣传 MCP，但 Windows 用户实际用不了。CI 三平台矩阵里没有 MCP 集成测试，所以没暴露。

### 2.2 Docker 镜像缺少 `web/` 目录

Dockerfile 只 `COPY agents ./agents` 和 `README.md`，但 Web 入口 `agents/web.py` 的静态资源来自 `web/`（`ASSETS / "index.html"`）。镜像内跑 `python -m agents.web` 会 404。另：`ENTRYPOINT` 指向 CLI 而非 Web，需明确镜像目标用途并补 `COPY web ./web`。

另注：`npx -y playwright@1.64.0-alpha-...` 已钉版本（好评），但该 alpha 版本号绑定 `@playwright/mcp@0.0.83` 的内部依赖，升级 MCP 时必须同步。

---

## 三、高优先（P1）：安全与正确性

### 3.1 Web 平台层 20s 超时 vs 10 分钟长任务

`agents/web.py:440`：

```python
return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=20)
```

聊天本身用 `asyncio.create_task` 后台跑、可跑 600s（`web.py:320`），但 `/api/sessions` 列表、`/api/library`、技能评测等走 `call()` 的同步桥接都受 20s 硬顶。评测报告稍大或磁盘稍慢就会 `concurrent.futures.TimeoutError`，且该异常未被 `safe_error` 友好处理。建议：按路由区分超时（IO 类 20s，计算/评测类放开）或全部改异步派发。

### 3.2 无请求速率限制

`do_POST` 限了 body 512KB + 强制 JSON + CSRF，但没有速率限制。本机服务配合 Host/Origin 校验后实际暴露面小（只听 127.0.0.1），但浏览器里任意页面若能构造同源请求（依赖 CSRF 令牌难度高），或本机恶意进程扫描端口，可无节制触发模型调用 → 账单打爆。建议加简单的令牌桶（每会话/每 IP），或至少对 `/api/sessions/*` 发消息加冷却。

### 3.3 记忆目录仍按 `Path.cwd()` 分片

`agents/memory.py:52-59`：

```python
def _project_hash() -> str:
    return hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:16]
```

`workspace.py` 已经把可访问范围改成实例属性（ContextVar 绑定），但记忆分片仍是**进程 cwd**。多工作区并发（Web 多会话指向不同项目、或 CLI 切目录）时记忆互相串台/丢失。这正是早前评估文档"硬伤 1"。建议：记忆根目录改用 `WorkspaceContext.root`，与 workspace 策略统一。

### 3.4 模块级可变缓存在多会话下共享

| 位置 | 变量 | 影响 |
| --- | --- | --- |
| `tools.py:382` | `_activated_tools: set` | tool_search 激活结果全进程共享，跨会话可见 |
| `tools.py:491` | `_cached_rules` + signature | 依赖 `Path.cwd()` 做签名，多工作区互相污染 |
| `skills.py:38` | `_cached_skills` + signature | 同上 |
| `subagent.py:89` | `_cached_custom_agents` | 同上 |

单实例 CLI 无碍；Web 服务多会话/多工作区并发是实际使用形态，建议把这四个缓存挂到 `WorkspaceContext` 或 `Agent` 实例上，或至少按 `(cwd, root)` 双键缓存。

### 3.5 `data/` 历史仍含 674 个第三方数据集文件

`git rm --cached` 已做、`.gitignore` 已加，但**远端历史仍包含全部内容**，本地 `.git-mawoagent/objects` 37MB。GAIA/HLE 属 gated 数据集，公开历史继续充当再分发渠道。彻底解决需 `git filter-repo` + force push（影响 12 fork），或至少在 README/NOTICE 声明"历史遗留，勿 clone 深历史"。这是**许可合规**问题而非纯技术问题，需要维护者决策。

---

## 四、中优先（P2）：工程质量

### 4.1 无 SAST / secret scanning / coverage

CI 只有 unittest + node test + `git diff --check` + 周更 pip-audit。缺：
- 静态安全扫描（bandit / semgrep）
- secret 扫描（gitleaks）
- 覆盖率统计（coverage.py，本项目 272 用例但不知覆盖率）

建议给 `tests.yml` 加 coverage 上报、给 `audit.yml` 加 gitleaks + bandit。

### 4.2 无 `pyproject.toml` / 版本常量

`requirements.txt` 下界宽松（`anthropic>=0.25.0`），lock 钉在 0.97.0——下界与实际差距巨大，`pip install -r requirements.txt`（不带 lock）会拿到完全不同的树。建议：
- 补 `pyproject.toml`（项目可 `pip install -e .`，同时承载 ruff/mypy/pytest 配置）
- 把 `requirements.txt` 下界收紧到 lock 附近，或干脆只留 lock 并在 README 说明

### 4.3 无 lint / 类型检查流水线

`.ruff_cache` 存在说明本地用过 ruff，但 ruff/mypy 都没进 lock 也没进 CI。`agents/agent.py` 1934 行、95 个函数/类、5 个 mixin（`BackgroundSkillTasks, ToolExecution, ContextRuntime, StreamAttempts, ToolCheckpoints`），MRO 依赖隐式顺序。建议至少：ruff 进 CI（`--select E,F,I`），mypy `--ignore-missing-imports` 先宽松起步。

### 4.4 `agent.py` 单文件过大

1934 行承载：重试/退避、token 计数、上下文窗口、工具转换、计划模式、记忆折叠、检查点、流式解析、成本核算、会话恢复。配合 5 个 mixin 隐式耦合（`self.permission_mode`、`self._plan_file_path` 等跨文件属性），可维护性风险高。建议按职责拆出 `planning.py`、`cost.py`、`folding.py`，mixin 改显式组合（`self.tools_runtime` 等）。

### 4.5 错误处理静默吞异常

多处 `except Exception: pass`（`agent.py:1179/1213`、`tools.py:210`、`prompt.py:161/179`、`background_jobs.py:85`、`mcp_client.py:180`）。部分场景合理（尽力而为的辅助功能），但：
- `agent.py:1179` `save_folded_session_memory` 失败静默 → 折叠记忆丢失无感知
- `prompt.py:161` rule 文件读失败静默 → 系统提示规则悄悄缺失

建议至少 `logging.debug/exception` 留痕，避免"无声失败"。

### 4.6 注释语言风格不一致

`agent.py` 大量中文注释（`#大结果持久化`），`workspace.py`/`policy.py`/`tools.py` 多为英文 docstring。开源仓库建议统一（英文 docstring + 可选中文注释），降低国际协作者门槛。

---

## 五、低优先（P3）：细节与打磨

| # | 问题 | 位置 | 建议 |
| --- | --- | --- | --- |
| 1 | `MODEL_CONTEXT` 表硬编码且不全 | `agent.py:97` | 未知模型回落 32k 窗口，gpt-4.1/claude 新模型会误判；建议配置化或从 API `/models` 读 |
| 2 | `_get_max_output_tokens` 用字符串嗅探 | `agent.py:115` | 同上，建议模型能力表外部化 |
| 3 | 无 `SECURITY.md` | 仓库根 | 补漏洞上报渠道 |
| 4 | 无 CODEOWNERS / issue 模板 | `.github/` | 可选 |
| 5 | 仅 1 个 tag（v0.1.0），无 CHANGELOG | — | 迭代中，可接受；发稳定版前补 |
| 6 | README 目录树与实际模块不一致 | README.md | 早前已发现，需同步 |
| 7 | `最近修改记录-*.md` 放仓库根 | 仓库根 | 建议挪 `docs/` 并入 `CHANGELOG` |
| 8 | web/tests 仅 3 个 .mjs 测试 | `web/tests/` | 覆盖 chat-view/conversation-state/scroll，`app.js` 339 行无单测 |
| 9 | `redact_secrets` 正则漏常见 key 格式 | `policy.py:41` | `_TOKEN_VALUE` 只匹配 sk/ghp/gho/xox，漏 AWS `AKIA`、OpenAI 新格式、JWT |
| 10 | `eval_folding.py` 用 `subprocess.run(["git",...])` 无 timeout | `eval_folding.py:346` | 挂死风险（虽 check=False），加 timeout=5 |

---

## 六、做得好的（值得保持）

1. **Web 安全面设计完整**：Host 白名单 + Origin 同源 + `Sec-Fetch-Site` + Cookie CSRF token + 严格 CSP（`default-src 'self'`）+ `X-Frame-Options: DENY`。前端 `escape()`/`escapeHTML()` 全覆盖 `innerHTML` 拼接，markdown 渲染器 `safeLink()` 拒绝 `javascript:`/`data:` 等危险 scheme，有专门测试。
2. **workspace 策略层设计成熟**：UNC 路径不解析直接分类（避免触发网络访问）、`commonpath` 跨盘保护、`realpath` 判定、`str.lstrip("./")` 陷阱有注释、受保护路径含 rules/skills/agents。
3. **原子写 + 崩溃安全**：`storage.py` mkstemp + fsync + `os.replace` + 目录 fsync，工具结果 >30KB 独立落盘 + 预览进 checkpoint。
4. **MCP 防御**：配置发现阶段不隐式执行项目命令（`mcp_client.py:350` 注释），每次确认展示具体参数，8MiB 消息上限。
5. **依赖治理**：lock 全 hash 钉死 + 周更 pip-audit + dependabot 仅管 actions（避免与 lock hash 机制冲突的噪音 PR）+ CI action 钉 commit SHA。
6. **测试规模**：272 用例，workspace 策略单文件 41 个测试，边界覆盖（符号链接/UNC/权限/折叠）扎实。

---

## 七、建议行动顺序

| 优先级 | 动作 | 工作量 |
| --- | --- | --- |
| P0 | 修 Windows MCP 启动（`shutil.which` 解析） | 0.5 天 |
| P0 | Dockerfile 补 `COPY web ./web` | 10 分钟 |
| P1 | `call()` 超时按路由分级 | 0.5 天 |
| P1 | 记忆/缓存改挂 WorkspaceContext | 1 天 |
| P1 | `data/` 历史清理决策（filter-repo 或声明遗留） | 0.5 天 + 公告 |
| P2 | CI 加 bandit + gitleaks + coverage | 0.5 天 |
| P2 | 补 pyproject.toml，收紧 requirements.txt 下界 | 0.5 天 |
| P2 | ruff/mypy 进 CI | 0.5 天 |
| P2 | agent.py 拆分 + 静默异常加日志 | 2 天 |
| P3 | SECURITY.md、README 同步、注释统一 | 1 天 |

总估计：约 7 人日到可发布稳定版的水平。
