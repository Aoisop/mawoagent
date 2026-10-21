# 迭代进度表

对应 [迭代计划表（2026-10-19）](iteration-roadmap-2026-10-19.md)。本表只记录**按验收标准实际交付**的任务；仓库中既有的基础代码按计划口径不计入完成。

最后更新：2026-10-21

## 总览

| 指标 | 数值 |
| --- | --- |
| 计划任务 | 21（T01–T21） |
| 已交付 | 4（T01、T02、T03、T05） |
| 进行中 | 0 |
| 未开始 | 17 |
| 迭代完成 | 1 / 8（I0） |
| 首版范围 I0–I2 | 4 / 7 任务 |
| PR 完成 | 2 / 6（PR-01、PR-02） |

## 迭代进度

| 迭代 | 优先级 | 任务数 | 已交付 | 状态 |
| --- | --- | --- | --- | --- |
| I0 基线与开发环境 | P0 | 2 | 2 | ✅ 完成 |
| I1 访问范围与修改审阅 | P0 | 3 | 2 | 🚧 进行中（缺 T04） |
| I2 文件快照与撤销 | P0 | 2 | 0 | 未开始 |
| I3 代码理解与任务对照 | P1 | 3 | 0 | 未开始 |
| I4 隔离执行 | P1 | 2 | 0 | 未开始 |
| I5 浏览器与远程工具 | P1 | 3 | 0 | 未开始 |
| I6 技能学习与记忆闭环 | P2 | 3 | 0 | 未开始 |
| I7 自动化与受控并发 | P2 | 3 | 0 | 未开始 |

## 任务明细

| 任务 | 迭代 | 状态 | 落点 | 验收证据 |
| --- | --- | --- | --- | --- |
| T01 环境与启动流程 | I0 | ✅ | `README.md`、`docs/t01-environment-baseline-2026-10-20.md` | `6f4034d`；依赖与 `requirements.lock` 双向差集为空；`pip check` 无冲突 |
| T02 任务 fixture 与验收脚本 | I0 | ✅ | `tests/fixtures/tasks/`、`tests/fixture_harness.py` | `26da0d1`；5 个任务，原样必须失败、参考解必须通过；17 个用例 |
| T03 工作区实例与访问策略 | I1 | ✅ | `agents/workspace.py`、`agents/tools.py`、`agents/tool_runtime.py` | `259641e`；`..`／绝对路径／symlink／junction／UNC／跨盘符均有覆盖；状态目录与策略文件单独处理 |
| T04 编辑提案与 diff 审阅 | I1 | ⬜ 未开始 | `agents/tools.py`、`agents/web.py`、`agents/ui.py`、`web/app.js` | 待实现：批准绑定准确版本、失效提案重新生成、先支持批准/拒绝 |
| T05 策略配置与来源展示 | I1 | ✅ | `agents/policy.py`、`agents/web.py` | `259641e`；复用 `settings.json`；来源逐项记录；未信任项目层被忽略；密钥脱敏 |
| T06 轮次快照与恢复 | I2 | ⬜ | — | 现有 `checkpoints.py` 是工具执行检查点，不是轮次文件快照，不计入 |
| T07 `/diff`、`/undo` | I2 | ⬜ | — | 命令尚不存在 |
| T08 Repo Map 与符号搜索 | I3 | ⬜ | — | `repo_index.py` 尚不存在 |
| T09 任务对照与消融 | I3 | ⬜ | — | 依赖 T08 与 fixture 执行入口 |
| T10 Skill YAML frontmatter | I3 | ⬜ | — | `frontmatter.py` 目前只做 `key: value` 行解析，不支持列表/多行/布尔/嵌套 |
| T11 执行后端抽象 | I4 | ⬜ | — | 无后端抽象；`Dockerfile` 存在但没有执行入口 |
| T12 容器状态映射与 Web 接入 | I4 | ⬜ | — | 依赖 T11 |
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
| PR-03 | T04：编辑提案与 Web/CLI diff 审阅 | ⬜ 依赖已满足，可开始 |
| PR-04 | T06–T07：轮次快照、diff/undo | ⬜ 依赖 PR-03 |
| PR-05 | T08–T09：Repo Map 与任务对照 | ⬜ 依赖 PR-04 |
| PR-06 | T10：Skill YAML | ⬜ 可独立提前 |

## 下一步

按依赖顺序，**T04 是唯一无阻塞的下一步**，它补齐 I1 的最后一块。T10 可以并行准备，但要等 PR-01 之后的评审节奏，且与 T08 共用 `search_symbols`／索引的输出格式，建议放在 I3 一起做。

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
| 完整套件 | 268 个用例，`OK (skipped=8)`，约 210 秒 |
| CI 矩阵 | 3 个平台 × Python 3.11/3.12 加前端作业，最近一次（run #21）全部通过 |
| 前端 | `node --check web/app.js` 通过；30 个单测通过 |
| 跳过原因 | 2 个符号链接用例（Windows 需开发者模式）；其余为既有平台条件跳过 |
