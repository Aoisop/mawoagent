# mawoagent 最近修改记录

> **历史记录**：本文记录当时的判断与状态，不再随代码更新。当前行为请以 [项目 README](../../README.md) 与 [文档索引](../README.md) 中的现行文档为准。


记录日期：2026-10-21。

本轮处理 `docs/GitHub风险扫描-2026-10-21.md` 中的**三个 P0 与六个 P1**，P2 仍未动。

P0（第 1–3 节）已随 commit `a601451` 推送到 `origin/main`；P1 见第 6 节。
`data/` 的远端历史清理需要单独决策，见第 3 节。

## 1. P0-1：四个依赖存在已知漏洞

| 包 | 原版本 → 新版本 | 问题 | 严重度 |
| --- | --- | --- | --- |
| [certifi](https://pypi.org/project/certifi/) | 2026.4.22 → **2026.10.19** | 内置 `emSign ECC Root CA - G3`，其下 8 张中间 CA 私钥已泄露（GHSA-6q48-6c7w-9gw2）。模型调用的每一次 HTTPS 请求都经过它提供的 CA bundle | 高（需中间人位置） |
| [anyio](https://pypi.org/project/anyio/) | 4.12.1 → **4.15.1** | TLSStream 用 IDNA 2003 编码主机名，可致证书欺骗（GHSA-82r6-8w77-94w6）；process-pool worker 的 stderr 未排空可挂起（GHSA-5p39-cfhj-2xmp） | 中 |
| [idna](https://pypi.org/project/idna/) | 3.13 → **3.20** | 特制输入绕过 CVE-2024-3651 的修复，造成 CPU 耗尽（GHSA-65pc-fj4g-8rjx） | 低 |
| [python-dotenv](https://pypi.org/project/python-dotenv/) | 1.2.1 → **1.2.4** | `set_key()` 跟随符号链接，可覆盖任意文件（GHSA-mf9w-mj56-hr94）。本项目只用 `load_dotenv`，不触发该路径 | 低 |

`typing_extensions` 随 anyio 升级从 4.15.0 变为 4.16.0，一并写入锁文件。

`requirements.lock` 同步更新；升级后对全部 30 个锁定包复查 OSV，**命中数为 0**。
升级前实测 `pip install --dry-run` 与现有依赖树无冲突。

## 2. P0-2：`--yolo` 绕过了刚建立的工作区边界

去掉确认 ≠ 允许会话改写约束自己的规则。这是本轮唯一的代码安全问题。

### 原来的行为

```python
def check_permission(tool_name, inp, mode, plan_file_path=None, workspace=None):
    if mode == "bypassPermissions":
        return {"action": "allow"}      # 直接返回
    ...
    scope_result = _workspace_policy_check(...)   # I1 新增的边界，永远到不了
```

`dontAsk` 越界时拒绝、`plan` 越界时拒绝，都正确；只有 `bypassPermissions` 把 I1 的
`_workspace_policy_check()` 整段跳过。`workspace.py` 的文档还写着"这定义了会话可访问的范围"，
实现与文档不一致。

### 现在的行为

| 模式 | 普通文件 | 工作区外 | 策略文件 |
| --- | --- | --- | --- |
| `default` | 逐次确认 | 确认 | 确认 |
| `acceptEdits` | 允许 | 确认 | 确认 |
| `dontAsk` | 允许 | 拒绝 | 拒绝 |
| `plan` | 只允许 plan 文件 | 拒绝 | 拒绝 |
| `bypassPermissions` | 允许 | 允许 | **拒绝（本轮新增）** |

改动落在 [tools.py](../../agents/tools.py)：新增 `_policy_write_denied()`，在 `bypassPermissions`
分支内先判定"是否在写策略文件"，命中则 `deny` 并说明原因；其余写入保持原有放行语义。
未绑定 workspace 时返回 `None`，历史行为不变（`tests/test_tool_boundaries_v3.py` 的
`read_file` 用例依赖这一点）。

### 顺带扩展了受保护文件清单

[workspace.py](../../agents/workspace.py) 的 `PROTECTED_RELATIVE_PATHS` 原来只有
`.mawoagent/settings.json` 和 `.mcp.json`。但还有三类文件会被**读进后续会话的提示词**：

| 路径 | 进入位置 |
| --- | --- |
| `.mawoagent/rules/*.md` | 系统提示的 `## Rules` 段（[prompt.py:146](../../agents/prompt.py#L146)） |
| `.mawoagent/skills/**/SKILL.md` | 技能检索与展开（[skills.py](../../agents/skills.py)） |
| `.mawoagent/agents/*.md` | 自定义子代理的系统提示（[subagent.py:90](../../agents/subagent.py#L90)） |

不保护这三类的后果是一条完整链：模型读到被投毒的内容 → 用一次普通 `write_file` 写入
`.mawoagent/rules/x.md` → 此后每个会话的系统提示都携带该指令，且不再需要任何确认。
现在三者也计入 protected，写入需确认，`bypassPermissions` 下直接拒绝。

### 顺带修复 Windows 下的目录前缀匹配

`_is_protected()` 原来用 `os.path.normcase()` 规范化路径。在 Windows 上，`normcase` 会把
`/` 改写成 `\`，随后与 `key + "/"` 比较时永远不相等。此前清单里只有两个**精确文件路径**，
走的是 `candidate == key` 分支，所以这个缺陷一直没有暴露；一旦加入目录型条目（`rules/` 等）
就会整体失效。现改为保持 `/` 分隔、仅对大小写做 `casefold()`。

### 测试

[tests/test_workspace_policy.py](../../tests/test_workspace_policy.py) 新增 4 个用例：
- `test_instruction_bearing_files_are_protected`：rules / skills / agents 计入 protected
- `test_bypass_permissions_still_refuses_policy_file_writes`：5 类策略文件在 bypass 下均 deny
- `test_bypass_permissions_still_allows_ordinary_writes`：普通文件仍 allow
- `test_bypass_permissions_without_a_workspace_keeps_legacy_behaviour`：无 workspace 时兼容

## 3. P0-3：`data/` 不再随仓库分发

`data/` 里有 674 个文件、约 74 MB 第三方基准数据（GAIA、HLE、ToolBench、ToolHop、API-Bank、
ALFWorld、WebShop、RestBench）。仓库是 MIT，但这些数据不属于本项目，其中 GAIA 与 HLE
在 Hugging Face 上是 gated 数据集，公开镜像通常不满足其使用条款。

| 动作 | 结果 |
| --- | --- |
| `git rm -r --cached data` | 674 个文件移出索引，**本地文件完整保留** |
| [.gitignore](../../.gitignore) 增 `data/` | 后续不会误提交 |
| [NOTICE](../../NOTICE) 改写 | 明确"数据不随仓库分发"，不再用一句 `data/*` 概括 |
| 新增 [docs/datasets.md](../datasets.md) | 逐数据集说明用途、上游项目与获取方式，并标注哪些是 gated |
| `data/folding_eval_smoke.jsonl` 复制到 [tests/fixtures/](../../tests/fixtures/) | 这个文件是本项目自有的冒烟用例，跟着 `data/` 一起被忽略会让新 clone 无法运行 `eval_folding`。README 的 4 处命令引用已同步改为新路径，并实测 `--validate-only` 通过 |

**未完成，需要决策**：远端 `main` 的历史里仍然包含这 674 个文件。`git rm --cached` 只影响
下一次提交，仓库体积不会变小。彻底移除需要 `git filter-repo` 重写历史并 force push，
会影响已有的 12 个 fork，因此本轮没有执行。若决定执行，建议先公告、再打 tag、最后重写。

## 4. 附带修复：Windows 上的锁文件竞争（既有 flaky，不属于三项 P0）

这一项不是本轮任务的一部分，是跑全量回归时暴露出来的既有问题，修它只是为了恢复"全绿"基线。

**原问题**：[storage.py](../../agents/storage.py) 的 `state_lock()` 直接 `open("a+b")` 打开 `.lock` 边车文件。
Windows 上多个进程同时首次创建同一个锁文件时，会偶发 `PermissionError: [Errno 13] Permission denied`，
导致 `record_skill_usage_judgments()` 直接抛错。复现率约 1/3。

**现在的行为**：新增 `_open_lock_file()`，在 Windows 上对该错误做 10 ms 间隔、上限 5 秒的重试；
其他平台或非瞬时错误照旧立即抛出。锁的语义没有变化——只是把"瞬时拒绝"变成"短暂等待"，
不会放宽互斥，也不会吞掉真实的权限问题（超时后仍然抛）。

**验证**：`test_multiprocess_usage_updates_do_not_lose_counts` 连续 5 次通过（修复前 3 次里失败 1 次）。

## 5. 验证

```text
$ .venv/Scripts/python.exe -m unittest tests.test_workspace_policy tests.test_permissions tests.test_tool_boundaries_v3
Ran 63 tests in 6.180s
OK (skipped=6)

$ OSV querybatch（30 个锁定包）
包数量: 30 | 命中: 0
```

手动核对 `bypassPermissions`：

```text
OK  bypass write .mawoagent/settings.json: deny
OK  bypass write .mcp.json: deny
OK  bypass write .mawoagent/rules/x.md: deny
OK  bypass write .mawoagent/skills/a/SKILL.md: deny
OK  bypass write notes.txt: allow
--- 无 workspace（历史行为）---
  allow
```

全量回归：`python -m unittest discover -s tests -q` → **272 个用例通过，8 个跳过，无失败**
（190 秒，全程 mock 模型，无 API 费用）。

> 用例数从 10-20 的 213 增长到 272，是项目在此期间新增的测试（`test_task_fixtures`、
> `test_workspace_policy` 等），不是本轮添加的；本轮只新增 4 个权限用例。

## 6. P1：供应链与 CI 纪律

### 6.1 MCP 依赖固定版本

[.mcp.json](../../.mcp.json) 原来两处都没有版本：`@upstash/context7-mcp`（无 tag）和 `@playwright/mcp@latest`。
`npx -y` 在首次运行时会下载并执行远端最新代码，以当前用户权限运行——等于把执行权交给两个 npm 包的发布流水线。

| 位置 | 原值 | 现值 |
| --- | --- | --- |
| `.mcp.json` context7 | `@upstash/context7-mcp` | `@upstash/context7-mcp@4.4.4` |
| `.mcp.json` playwright | `@playwright/mcp@latest` | `@playwright/mcp@0.0.83` |
| [Dockerfile](../../Dockerfile) | `playwright@latest` | `playwright@1.64.0-alpha-1790635538000` |

Dockerfile 那一行不是随手挑的：`@playwright/mcp@0.0.83` 自己就把 `playwright` 与 `playwright-core`
钉在 `1.64.0-alpha-1790635538000`（一个 alpha 预发布）。如果镜像里装 `playwright@latest`（当前 1.64.1），
浏览器二进制的 revision 会和 MCP 期望的不一致。

**这条也说明"固定版本"不是万能的**：上游自己就在依赖 alpha 版本，我们能做的是让镜像与 MCP 对齐，
并不能消除"上游用预发布版"这个事实。

### 6.2 lock 文件加 hash

[requirements.lock](../../requirements.lock) 原来只有 `包==版本`，`pip install` 不校验下载内容；
上游包被替换或投毒无法发现。

现在为 30 个包补齐 sha256——同一版本的多平台 wheel 与 sdist **全部列出**，保证 Windows / macOS / Linux
都能匹配到对应文件。文件从 31 行变成 469 行。

pip 的行为是：**只要有 requirement 带 `--hash`，整次安装自动进入 `--require-hashes` 模式**，
所有依赖都必须固定版本并带 hash（本 lock 已包含全部传递依赖）。因此 CI 的命令不需要改。

验证方式是在干净虚拟环境里真实安装，而不只是 `--dry-run`。

### 6.3 CI action 固定到 commit SHA

| Action | 原引用 | 固定为 |
| --- | --- | --- |
| `actions/checkout` | `@v4` | `@11d5960a326750d5838078e36cf38b85af677262` |
| `actions/setup-python` | `@v5` | `@a26af69be951a213d495a4c3e4e4022e16d87065` |
| `actions/setup-node` | `@v4` | `@49933ea5288caeca8642d1e84afbd3f7d6820020` |

SHA 取自 GitHub API 的 `git/ref/tags/<tag>`，不是凭记忆填写；行尾保留 `# v4` 这类注释便于人工对照，
后续由 Dependabot 负责升级。

### 6.4 其余三项

| 项 | 改动 |
| --- | --- |
| Python 3.13 入矩阵 | `python: ['3.11', '3.12', '3.13']`。本机 `.venv` 就是 3.13.14，此前 CI 从未覆盖 |
| Dependabot | 新增 [.github/dependabot.yml](../../.github/dependabot.yml)：**仅 github-actions**，每周一 03:00 分组升级（原因见下） |
| 依赖审计 | 新增 [.github/workflows/audit.yml](../../.github/workflows/audit.yml)：每周一 03:00 UTC 跑 `pip-audit -r requirements.lock --strict`，也可手动触发 |

**为什么 Dependabot 只保留 actions**：配置推送后它立刻开了两个 PR。actions 那个有效（`#25` 已通过 CI）；
Python 那个只改了 `requirements.txt` 的下限（`openai>=1.0.0`→`>=3.35.1`、`jsonschema==4.25.1`→`==4.26.0`），
**完全没有动 `requirements.lock`**——而 CI 装的是 lock。也就是说那个 PR 合了也不会改变运行时版本，
反而让 `requirements.txt` 与 lock 的版本不一致。

Dependabot 不会重算 hash，所以把 pip 生态从配置里去掉：漏洞发现交给每周的 `pip-audit`（它直接读 lock），
版本升级走人工 `pip-compile --generate-hashes --output-file requirements.lock requirements.txt`，
命令写在 `dependabot.yml` 的注释里。已开的 Python PR 建议直接关闭。

审计工作流**故意独立于测试工作流**：新发布的 advisory 不应该把无关的 PR 变红，但它必须在每周定时任务里
响亮地失败。这是"不打扰日常、不放过漏洞"的折中。

### 6.5 P1 验证

```text
$ pip install --dry-run --ignore-installed --require-hashes -r requirements.lock
Would install Pygments-2.20.0 … typing_extensions-4.16.0     ← 30 个包全部解析

$ python -m venv <临时目录>
$ pip install -q --require-hashes -r requirements.lock
install_exit=0
$ python -c "import anthropic, openai, jsonschema, dotenv, rich"
imports ok

YAML：tests.yml / audit.yml / dependabot.yml 均解析通过
```

## 7. 仍未处理（P2）

| 级别 | 事项 | 原因 |
| --- | --- | --- |
| P2 | `_activated_tools` / 权限缓存 / `memory._project_hash()` 实例化 | 与 I1 的 WorkspaceContext 收尾一起做更合适 |
| P2 | Release / tag / SECURITY.md / 分支保护 | 仓库治理 |
| P2 | `data/` 的远端历史清理 | 需 `git filter-repo` + force push，影响 12 个 fork |

完整依据见 [GitHub风险扫描-2026-10-21.md](scans/github-risk-scan-2026-10-21.md)。
