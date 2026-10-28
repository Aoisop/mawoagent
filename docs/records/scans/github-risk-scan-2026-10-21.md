# mawoagent GitHub 风险扫描报告

> **历史记录**：本文记录当时的判断与状态，不再随代码更新。当前行为请以 [项目 README](../../../README.md) 与 [文档索引](../../README.md) 中的现行文档为准。


扫描日期：2026-10-21 ｜ 扫描对象：`Aoisop/mawoagent`（GitHub 公开仓库 + 本地工作区）

## 扫描范围与方法

| 维度 | 数据来源（一手） |
| --- | --- |
| 仓库画像 | GitHub REST API：`/repos/Aoisop/mawoagent`、`/commits`、`/releases`、`/branches`、`/actions/runs` |
| Python 依赖漏洞 | OSV.dev `querybatch` + `vulns/{id}`，对 `requirements.lock` 全部 30 个包精确到版本查询 |
| npm 依赖 | npm registry API（`@playwright/mcp`、`@upstash/context7-mcp`） |
| 凭据泄露 | 全仓库正则扫描 + `git log --all --diff-filter=A -- .env` + `git log --all -S` 历史检索 |
| 代码安全 | 逐行审阅新增的 `agents/workspace.py`、`agents/policy.py`、`tools.py` 权限入口 |
| 许可证 | `LICENSE`、`NOTICE`、远端目录树（确认 `data/` 是否随仓库分发） |

**未覆盖**：GitHub Advanced Security 的 secret scanning / code scanning 结果（需仓库管理员权限，API 未返回）；
组织级配置、fork 网络中的派生仓库内容；运行时动态渗透测试。

---

## 一、结论摘要

仓库整体是**安全的公开仓库**：没有硬编码凭据，没有遥测上报，`.env` 从未进入历史，
Web 服务只绑本地并有四重校验。风险集中在三处，且都可修：

| 级别 | 风险 | 是否已实际暴露 |
| --- | --- | --- |
| **P0** | `certifi 2026.4.22` 打包了被入侵的根 CA，修复版已发布 | 是（所有 HTTPS 调用依赖它） |
| **P0** | `--yolo`（`bypassPermissions`）短路了刚加入的工作区边界 | 是（I1 新代码的边界在 yolo 下等于不存在） |
| **P0** | `data/` 76 MB 第三方基准数据集随 MIT 仓库公开分发，NOTICE 无逐数据集许可 | 是（远端已确认存在） |
| **P1** | MCP 依赖无版本约束（`@latest` / 无 tag），首次运行即执行远端最新代码 | 是 |
| **P1** | `requirements.lock` 无 hash，CI action 用 tag 而非 SHA，无 dependabot | 是 |
| **P1** | 受保护文件清单漏了 `rules/`、`skills/`、`agents/`，构成持久化提示注入路径 | 是（可构造） |
| **P2** | 实例级状态未清理（`_activated_tools`、权限缓存、memory hash） | 是（多会话时） |
| **P2** | 无 Release / 无 SECURITY.md / 无分支保护证据 / CI 无安全扫描 | 是 |

---

## 二、P0：应立即处理

### 2.1 certifi 内置了私钥泄露的根 CA（GHSA-6q48-6c7w-9gw2）

```text
包:        certifi 2026.4.22
问题:      bundled CA store 含 emSign ECC Root CA - G3；
           其下 8 张 eMudhra 中间 CA 证书私钥已被泄露
CVSS:      4.0 AV:N/AC:H/AT:P/PR:N/UI:N/VC:H/VI:H
修复版本:   2026.10.19
```

**为什么这条最严重**：这个项目的每一次模型调用（OpenAI / Anthropic / 兼容端点）都经过
`httpx → anyio → ssl`，而信任链来自 certifi 的 CA bundle。持有那批泄露中间 CA 私钥的人，
可以为任意域名签发被本机信任的证书——也就是说，**在具备中间人位置的前提下，可以解密/篡改 API 流量**，
包括 API key 与完整对话内容。

`AC:H` 说明需要中间人位置（同网段、恶意热点、被入侵的上游代理），不是远程直接打穿。
但这是全链路信任根的问题，升级成本接近零，没有理由不修。

### 2.2 `--yolo` 让新加的工作区边界失效

`agents/tools.py` 的 `check_permission()` 第一行：

```python
def check_permission(tool_name, inp, mode, plan_file_path=None, workspace=None):
    if mode == "bypassPermissions":
        return {"action": "allow"}          # ← 直接返回，后面全部跳过
    ...
    scope_result = _workspace_policy_check(tool_name, inp, mode, workspace or current_workspace())
```

而 I1 新增的 `_workspace_policy_check()`（`tools.py:645`）本意是：写工作区外、写受保护文件、
写运行时状态目录时都需要确认。在 `--yolo` 下这三条**全部不生效**。

这不是"yolo 本来就该放开"那么简单：
- `dontAsk`（CI 模式）在越界时是**拒绝**，行为正确；
- `plan` 是拒绝，正确；
- 只有 `bypassPermissions` 完全绕过，且 `workspace.py` 的注释还写着"这定义了会话可访问的范围"——
  文档与实现不一致，使用者会误判。

**建议**（按改动量排序）：

1. 最小修法：把 `_workspace_policy_check` 移到 `bypassPermissions` 判断**之前**，
   在 bypass 下仍 `allow`，但至少对 `protected` 文件（`settings.json`、`.mcp.json`）返回 `deny`——
   拒绝"会话改写自己的策略"这条路径。约 5 行。
2. 语义修法：明确三种模式的区别并写进 README：
   `bypass` = 跳过确认但保留硬拒绝；`dontAsk` = 越界即拒；`default` = 逐次确认。

### 2.3 `data/` 的许可证问题

远端 `main` 分支的根目录树里确实包含 `data/`（674 个文件，约 76 MB），与本地一致。

```text
LICENSE:  MIT（仅覆盖 Aoisop 的代码）
NOTICE:   "data/*: third-party research artifacts"
          ↑ 只有一句概括，没有逐数据集许可、没有原始来源链接、没有引用要求
```

涉及的数据集：GAIA、HLE、ToolBench、ToolHop、API-Bank、ALFWorld、WebShop、RestBench。

风险不在"用了别人的数据"，而在**把一个 MIT 仓库当成这些数据的再分发渠道**：

- GAIA 与 HLE 在 HuggingFace 上是 **gated 数据集**，访问需登录并同意使用条款；
  公开仓库直接镜像其内容，通常不满足这类条款（以各数据集官方页面为准，发布前应逐一核对）。
- ToolBench 代码为 Apache-2.0，但其数据含模型生成内容，另有限制条款。
- API-Bank / RestBench 的许可需回原论文仓库确认。
- `data/API-Bank/init_database/ImageCaptioning.json` 内含大量第三方图库 URL（alamy、shutterstock 等），
  属引用而非再分发，但企业合规审查仍会问。

**实际后果**：轻则收到 DMCA/下架请求，重则学术使用时被质疑数据来源合规；企业采用会在合规审查环节直接卡住。

**建议**：

```bash
# 1) 从当前树移除（保留本地文件）
git rm -r --cached data && printf "\ndata/\n" >> .gitignore

# 2) 提供可复现的获取脚本，而不是分发内容
#    scripts/fetch_data.py  → 下载 + sha256 校验 + 版本记录

# 3) 若要清历史（体积才会真正变小），需要 git filter-repo 重写历史
#    注意：这会影响已有 12 个 fork，需提前公告
```

同时把 `NOTICE` 改成表格式：数据集名 / 版本或快照日期 / 许可 / 原始链接 / 本项目如何使用。

---

## 三、P1：应尽快处理

### 3.1 MCP 依赖无版本约束（供应链 RCE 面）

```json
"context7":  { "command": "npx", "args": ["-y", "@upstash/context7-mcp"] }
"playwright":{ "command": "npx", "args": ["@playwright/mcp@latest"] }
```

- `npx -y` 会在**首次运行时下载并执行远端最新代码**，以当前用户权限运行，且能读写工作区；
- `@playwright/mcp` 当前最新 `0.0.83`（0.x 语义：minor 可破坏），`@upstash/context7-mcp` 为 `4.4.4`；
- 任何人 clone 后第一次对话就触发下载 → 项目实际上把执行权交给了两个 npm 包的发布流水线。

同类问题也在 `Dockerfile`：`npx -y playwright@latest install` 使镜像构建不可复现。

**建议**：

```json
"args": ["@playwright/mcp@0.0.83"]
"args": ["@upstash/context7-mcp@4.4.4"]
```

Dockerfile 同样固定版本，并把 npm 包版本记录进 `requirements.lock` 旁边的一份 `mcp-packages.json`。

### 3.2 依赖完整性与 CI 供应链

| 问题 | 证据 | 影响 |
| --- | --- | --- |
| `requirements.lock` 无 hash | `grep -c sha256 requirements.lock` → `0` | `pip install -r` 不校验包完整性；上游被投毒无法发现 |
| CI action 用 tag | `actions/checkout@v4`、`setup-python@v5`、`setup-node@v4` | tag 可被移动；供应链最佳实践是固定 commit SHA |
| 无 dependabot | `.github/dependabot.yml` 不存在 | 本次 4 个漏洞包不会被自动提醒 |
| CI 无安全扫描 | `tests.yml` 只有 unittest + node test | 无 SAST、无依赖审计、无 secret 扫描 |
| 3.13 未覆盖 | 矩阵 3.11/3.12；本机 `.venv` 是 3.13.14 | 用户实际运行的组合未验证 |

注意 CI 的 `permissions: contents: read` 是正确的（最小权限），也没有使用 `pull_request_target`，
这两点做得对。

### 3.3 受保护文件清单不完整 → 持久化提示注入路径

`workspace.py` 的 `PROTECTED_RELATIVE_PATHS` 只有两项：

```python
PROTECTED_RELATIVE_PATHS = (".mawoagent/settings.json", ".mcp.json")
```

但工作区内还有三类文件会**进入后续所有会话的系统提示**：

| 路径 | 作用 | 读取位置 |
| --- | --- | --- |
| `.mawoagent/rules/*.md` | 直接拼进系统提示的 "## Rules" 段 | `prompt.py:146-165` |
| `.mawoagent/skills/**/SKILL.md` | 技能正文，可被检索并展开 | `skills.py` |
| `.mawoagent/agents/*.md` | 自定义子代理的系统提示 | `subagent.py:90` |

攻击链是成立的：

```text
① Agent 读到被投毒的网页/文件（提示注入）
② 写入 .mawoagent/rules/policy.md（在 workspace 内 → 只需一次普通写入确认）
③ 此后每个会话的系统提示都携带攻击者写的规则，且不再需要任何确认
```

相比之下 `settings.json` 被保护、`.mcp.json` 被保护，而**影响更大的 rules 却没保护**。
建议把上述三个路径加入 `PROTECTED_RELATIVE_PATHS`，或在写入时要求独立确认。

### 3.4 其余已确认的依赖漏洞（可利用性低，但应顺手升级）

| 包 | 版本 | 问题 | 修复 | 本项目可利用性 |
| --- | --- | --- | --- | --- |
| anyio | 4.12.1 | TLSStream 用 IDNA 2003 编码主机名，可致证书欺骗（GHSA-82r6-8w77-94w6） | 4.14.2 | 低：只访问 ASCII 域名 |
| anyio | 4.12.1 | process-pool worker 的 stderr 未排空，可挂起（GHSA-5p39-cfhj-2xmp） | 4.14.2 | 低：未使用 to_process |
| idna | 3.13 | 特制输入绕过 CVE-2024-3651 修复，CPU 耗尽（GHSA-65pc-fj4g-8rjx） | 3.15 | 低：域名来自本地配置 |
| python-dotenv | 1.2.1 | `set_key()` 跟随符号链接，可覆盖任意文件（GHSA-mf9w-mj56-hr94） | 1.2.2 | 极低：只用 `load_dotenv`，不用 `set_key` |

已实测：四个修复版本与现有依赖树**无冲突**（`pip install --dry-run` 退出码 0）。

---

## 四、P2：结构与治理

### 4.1 实例级状态仍未隔离

I1 已开工（新增 `workspace.py`、`policy.py`，提交 `259641e`），但两处仍是进程级：

```text
tools.py:382   _activated_tools: set[str] = set()      ← 模块级，tool_search 激活后全进程共享
tools.py:486   _cached_rules / _cached_rules_signature ← 按 cwd 缓存
memory.py:52   _project_hash() → sha256(Path.cwd())    ← 记忆按进程 cwd 分片
```

`workspace.py` 已经把"可访问范围"从进程属性变成了实例属性（ContextVar 绑定），
但记忆目录仍是 `Path.cwd()` —— 一个实例指向 A 目录、另一个指向 B 目录时，两份记忆无法区分。
这正是 `docs/迭代方案可行性评估-2026-10-20.md` 里"硬伤 1/2"指出的问题，目前尚未处理。

### 4.2 仓库治理

| 项 | 现状 | 建议 |
| --- | --- | --- |
| Release / tag | **0 个 release，0 个 tag** | 用户无法锁定版本；建议按迭代打 tag |
| SECURITY.md | 404（不存在） | 补漏洞上报渠道与响应时限 |
| 分支保护 | 单分支 `main`，无保护证据 | 开启 required review + 禁止 force push |
| CODEOWNERS / issue 模板 | 无 | 可选 |
| Secret scanning / push protection | API 未返回该字段 | 到 Settings → Code security 手动确认 |
| 热度 | 创建 2026-10-18，3 天 57 star / 12 fork / 29 watchers | **不要用 star 数当安全信号**；3 天这个量级也可能含刷量 |
| 代码成熟度 | 有 CI、213 用例、但无 release | 属"早期项目"，生产使用需自行评估 |

---

## 五、做得好的地方（同样需要记录）

避免只看问题，以下几点确实优于同类项目：

1. **零硬编码凭据**。全仓库扫描只命中测试里的假值（`sk-live-abcdefghijkl`，用于验证 `redact_secrets` 脱敏），
   不是真实密钥。
2. **`.env` 从未进入 Git 历史**（`git log --all --diff-filter=A -- .env` 为空），只提交 `.env.example`。
3. **无遥测**。grep `telemetry|analytics|sentry|posthog` 全空，所有会话/记忆默认落本地。
4. **Web 服务只绑本地 + 四重校验**：Host 白名单、Origin 同源、`Sec-Fetch-Site`、Cookie token + CSRF header，
   配合严格 CSP。攻击面控制得比多数本地工具好。
5. **CI 最小权限**：`permissions: contents: read`，无 `pull_request_target`，无 secrets 注入。
6. **新增的 `workspace.py` 质量不错**：UNC 路径**不解析直接分类**（避免触发网络访问）、
   `commonpath` 跨盘保护、`realpath` 判定、`str.lstrip("./")` 这类陷阱有注释说明。

---

## 六、修复清单（按性价比排序）

| # | 动作 | 命令 / 改动 | 预计 |
| --- | --- | --- | --- |
| 1 | 升级 4 个漏洞包并重生成 lock | `pip install -U "certifi>=2026.10.19" "anyio>=4.14.2" "idna>=3.15" "python-dotenv>=1.2.2"` | 10 min |
| 2 | 固定 MCP 版本 | `.mcp.json` 改 `@playwright/mcp@0.0.83`、`@upstash/context7-mcp@4.4.4` | 5 min |
| 3 | 修 `bypassPermissions` 短路 | 把 workspace 检查移到 bypass 前，protect 文件硬拒 | 30 min |
| 4 | 补受保护路径 | `PROTECTED_RELATIVE_PATHS` 加 `rules/`、`skills/`、`agents/` | 20 min |
| 5 | 从公开树移除 `data/` | `git rm -r --cached data` + 下载脚本 + NOTICE 表格 | 0.5 天 |
| 6 | 开 secret scanning + dependabot | GitHub Settings + `.github/dependabot.yml`（pip / npm / actions） | 15 min |
| 7 | CI action 固定 SHA | `gh api repos/actions/checkout/git/ref/tags/v4` 取 commit SHA 后替换 | 20 min |
| 8 | 补 `SECURITY.md` + 首个 release tag | 说明上报渠道、响应时限、支持版本 | 1 h |
| 9 | lock 加 hash | `pip-compile --generate-hashes` 或 `--require-hashes` | 1 h |
| 10 | 3.13 入 CI 矩阵 | 改 1 行 | 5 min |

第 1–4 项合计约 1 小时，能消掉全部 P0 与最实际的 P1；第 5 项是唯一需要决策的（涉及历史重写与 fork）。

---

## 七、一句话总结

这是一个**代码质量与运行时安全都高于平均水平**的早期项目（权限四模式、路径归一化、原子写、213 个测试都真实存在），
它的问题不在"代码写得糙"，而在**边界定义与供应链纪律**：
`--yolo` 把刚建好的边界绕过去了，`data/` 把别人的数据集当成自己的代码一起发了，
MCP 把执行权交给了 `@latest`，而证书信任根还停在了一个已知私钥泄露的版本上。
前两条一小时能修，第三条需要一次明确的许可决策。
