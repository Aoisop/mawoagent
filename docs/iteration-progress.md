# 迭代进度表

对应 [迭代计划表（2026-10-19）](iteration-roadmap-2026-10-19.md)。本表只记录**按验收标准实际交付**的任务；仓库中既有的基础代码按计划口径不计入完成。

最后更新：2026-10-23

## 总览

| 指标 | 数值 |
| --- | --- |
| 计划任务 | 21（T01–T21） |
| 已交付 | 11（T01–T08、T10、T11） |
| 进行中 | 0 |
| 未开始 | 10 |
| 迭代完成 | 4 / 8（I0、I1、I2、I3） |
| 首版范围 I0–I2 | 7 / 7 任务（完成） |
| PR 完成 | 6 / 6（PR-01–PR-06） |

## 迭代进度

| 迭代 | 优先级 | 任务数 | 已交付 | 状态 |
| --- | --- | --- | --- | --- |
| I0 基线与开发环境 | P0 | 2 | 2 | ✅ 完成 |
| I1 访问范围与修改审阅 | P0 | 3 | 3 | ✅ 完成 |
| I2 文件快照与撤销 | P0 | 2 | 2 | ✅ 完成 |
| I3 代码理解与任务对照 | P1 | 3 | 3 | ✅ 完成 |
| I4 隔离执行 | P1 | 2 | 2 | 🚧 T11 有未验证项，T12 未开始 |
| I5 浏览器与远程工具 | P1 | 3 | 0 | 未开始 |
| I6 技能学习与记忆闭环 | P2 | 3 | 0 | 未开始 |
| I7 自动化与受控并发 | P2 | 3 | 0 | 未开始 |

## 任务明细

| 任务 | 迭代 | 状态 | 落点 | 验收证据 |
| --- | --- | --- | --- | --- |
| T01 环境与启动流程 | I0 | ✅ | `README.md`、`docs/t01-environment-baseline-2026-10-20.md` | `6f4034d`；依赖与 `requirements.lock` 双向差集为空；`pip check` 无冲突 |
| T02 任务 fixture 与验收脚本 | I0 | ✅ | `tests/fixtures/tasks/`、`tests/fixture_harness.py` | `26da0d1`；5 个任务，原样必须失败、参考解必须通过；17 个用例 |
| T03 工作区实例与访问策略 | I1 | ✅ | `agents/workspace.py`、`agents/tools.py`、`agents/tool_runtime.py` | `259641e`；`..`／绝对路径／symlink／junction／UNC／跨盘符均有覆盖；状态目录与策略文件单独处理 |
| T04 编辑提案与 diff 审阅 | I1 | ✅ | `agents/tools.py`、`agents/tool_runtime.py`、`agents/ui.py`、`agents/web.py` | 执行前生成 unified diff 并绑定文件版本指纹；批准后复查版本，期间文件变化则整体不写；拒绝不落盘；同意卡片沿用现有审批 UI，diff 以等宽文本滚动显示 |
| T05 策略配置与来源展示 | I1 | ✅ | `agents/policy.py`、`agents/web.py` | `259641e`；复用 `settings.json`；来源逐项记录；未信任项目层被忽略；密钥脱敏 |
| T06 轮次快照与恢复 | I2 | ✅ | `agents/snapshots.py`、`agents/tools.py`、`agents/agent.py` | 每轮首次改动前留底；排除 `.mawoagent`/`.git`/依赖目录、超限文件与二进制；保留最近 30 轮并受总容量约束；进程中断的轮次标记为 `interrupted` |
| T07 `/diff`、`/undo` | I2 | ✅ | `agents/main.py`、`agents/web.py` | CLI 支持 `/diff [n]`、`/undo [n] [--dry-run]`；Web 提供 `GET …/diff` 与 `POST …/undo`；撤销前逐文件比对，轮次后被改动的文件列为冲突并跳过 |
| T08 Repo Map 与符号搜索 | I3 | ✅ | `agents/repo_index.py`、`agents/prompt.py`、`agents/main.py` | Python 走 `ast`，JS/TS 走模式匹配并标注为近似；增量索引按 size+mtime 复用；跳过依赖/状态目录与超限文件；`/symbols` 查询，提示词注入可用 `context.repo_map` 或环境变量关闭 |
| T09 任务对照与消融 | I3 | ✅ | `agents/eval_run.py` | `python -m agents.eval_run` 逐任务记录 JSONL（成功/失败/超时/异常都留痕）；`--mock` 供 CI 验证管线；变体按仓库地图开关分组并汇总成功率、工具次数、token、耗时 |
| T10 Skill YAML frontmatter | I3 | ✅ | `agents/frontmatter.py`、`agents/skills.py`、`agents/memory.py` | 无第三方依赖的 YAML 子集：引号/布尔/null/数字、块与流式列表、缩进嵌套、字面量与折叠块标量、注释；旧格式回退；`format_frontmatter` 可精确回读 |
| T11 执行后端与 Docker 入口 | I4 | ✅ | `agents/sandbox.py`、`Dockerfile.sandbox`、`agents/tools.py` | 统一 `ExecutionBackend.run()`/`ExecResult`；本地后端复刻原行为（同 shell、同会话、同进程树清理）；Docker 后端只挂工作区、`--network none`、内存/CPU/PID 上限、`no-new-privileges`、超时按容器名清理；`MAWOAGENT_SANDBOX` 选择后端。**未验证**：无 Docker 环境，容器内行为未实测；「独立副本模式」未实现 |
| T12 容器状态映射与 Web 接入 | I4 | ⬜ | — | 依赖 T11；需要会话/记忆/Web 在容器内可用，并保留 Host/Origin/CSRF 防护 |
| T13 浏览器 MCP（Playwright） | I5 | ⬜ | — | `.mcp.json` 有配置，但 Windows 上必然启动失败（见已知缺陷） |
| T14 远程 MCP 传输 | I5 | ⬜ | — | `mcp_client.py` 无 SSE／streamable 传输 |
| T15 工具结果类型化 | I5 | ⬜ | — | 现有结果存储不计入 |
| T16 执行型评测入口 | I6 | ⬜ | — | `online_skill_eval.py` 目前是文本回放 |
| T17 记忆管理工具 | I6 | ⬜ | — | `tool_schemas.py` 中无记忆工具 |
| T18 审计钩子 | I6 | ⬜ | — | 无审计钩子实现 |
| T19 JSONL 输出与退出码 | I7 | ⬜ | — | `main.py` 无 JSONL／output-format 选项 |
| T20 实例状态隔离 | I7 | ⬜ | — | 大量模块仍直接读取 `Path.cwd()` |
| T21 OS 级自动调度 | I7 | ⬜ | — | 未开始 |

## PR 进度

| PR | 内容 | 状态 |
| --- | --- | --- |
| PR-01 | T01–T02：环境、测试基线、5 个任务 fixture | ✅ 已合并 |
| PR-02 | T03、T05：WorkspaceContext、访问范围、策略配置 | ✅ 已合并 |
| PR-03 | T04：编辑提案与 Web/CLI diff 审阅 | ✅ 已合并 |
| PR-04 | T06–T07：轮次快照、diff/undo | ✅ 已合并（T04 仍待补） |
| PR-05 | T08–T09：Repo Map 与任务对照 | ✅ 已合并（真实对照需自备密钥运行） |
| PR-06 | T10：Skill YAML | ✅ 已合并 |

## 下一步

**I0–I3 已全部交付**，I4 从执行后端抽象开始。 I3 的 T09 提供了可运行的评测入口，但 fixture 目前仍是 5 个（计划要求 20–30 个），且真实对照需要模型密钥，不属于 CI 范围。下一步是 I4 隔离执行，它是无人值守写入的前置条件。

仍未闭合的两处界面/接口落差：T07 的 Web 端只有接口、没有按钮；授权卡片里的 diff 是纯文本，没有按增删着色。

## 已知缺陷与风险

| 问题 | 影响 | 涉及任务 |
| --- | --- | --- |
| `.mcp.json` 使用 `npx`，Windows 上实际是 `npx.cmd`，子进程启动报 `WinError 2` | 两个 MCP 服务在 Windows 上从未成功启动；此前还会先弹授权再失败 | T13；I4 的"安装缺失有清晰错误" |
| ~~完整测试偶发在解释器退出阶段崩溃~~ | 已修复（`cbb635a`）：请求处理线程在退出阶段向 stderr 打异常栈触发致命锁错误；改为吞掉常规断连异常 | 原关联 I7／T19 |
| `test_multiprocess_usage_updates_do_not_lose_counts` 在完整套件中偶发 `PermissionError` | Windows 上多进程与临时目录清理竞争；单独运行 3/3 通过，尚未修复 | 与 I7／T20 的实例隔离相关 |
| 无确认通道时依赖阻塞式 `input()` | 已在 `259641e` 中改为拒绝；仍需在 T19 统一退出与拒绝语义 | T19 |

## 测试基线

| 口径 | 结果 |
| --- | --- |
| 完整套件 | 395 个用例，`OK (skipped=8)`，约 240 秒 |
| CI 矩阵 | 3 个平台 × Python 3.11/3.12 加前端作业，最近一次（run #21）全部通过 |
| 前端 | `node --check web/app.js` 通过；30 个单测通过 |
| 跳过原因 | 2 个符号链接用例（Windows 需开发者模式）；其余为既有平台条件跳过 |
