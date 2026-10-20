# 编码任务 fixture（I0 / T02）

对应 [迭代计划表](../../../docs/iteration-roadmap-2026-10-19.md) 的 T02。这里存放受版本管理的任务样本、验收脚本和运行记录格式，供后续 I3 的开关对照（T09）与 I6 的执行型评测（T16）使用。

这批 fixture 只定义**任务本身**：初始状态、允许工具、预算、预期行为和程序化检查。它们不调用模型，也不判断模型好坏；真正的执行评测要等隔离执行（I4）与评测入口（T16）就绪。

## 目录结构

```
tests/fixtures/tasks/
├── manifest.json                 # 清单：任务元数据 + 初始状态摘要
├── regen_manifest.py             # 只刷新 initial_state 摘要，不改其他字段
└── <task-id>/
    ├── workspace/                # 固定初始版本，测试运行时复制到临时目录
    ├── check.py                  # 验收：check(workspace) -> {"passed": bool, "details": [...]}
    └── solution/                 # 参考解，覆盖到 workspace 后 check 必须通过
```

`workspace/` 永远不被原地修改。验收测试先复制到临时目录，再执行 `check`，因此跑测试不会污染 fixture。

## 任务字段（manifest.json）

| 字段 | 含义 |
| --- | --- |
| `id` | 任务标识，同时是目录名 |
| `title` | 一句话描述 |
| `category` | 能力维度，用于后续分类统计 |
| `allowed_tools` | 允许使用的工具白名单，必须是 Runtime 真实存在的工具名 |
| `budget` | `max_turns` 与 `max_tool_calls`，超预算按失败记录 |
| `expected` | 预期行为，人读的判定依据 |
| `initial_state` | `{相对路径: sha256}`，固定初始版本 |
| `check_timeout_s` | 验收脚本超时上限 |

初始状态摘要按 **LF 归一化后的字节**计算。Windows 检出若启用 `core.autocrlf=true` 会把工作区文件写成 CRLF，若直接对磁盘字节取哈希，每个任务都会显示"被修改过"。

## check.py 协议

```python
def check(workspace: Path) -> dict:
    return {"passed": bool, "details": ["..."]}
```

约定：

- 只读 `workspace`，不得写入。
- 判定基于**行为**，不要复述实现细节（否则换个正确写法就会被误判）。
- 抛异常视为 `error`，由 harness 统一记录，不会被静默当成通过。
- 需要跑子进程时显式设置 `timeout`。

## 运行记录格式

每次执行（含失败、超时、取消）都写一条 JSONL 记录，字段由 `tests/fixture_harness.py` 的 `build_run_record` 统一生成：

| 字段 | 说明 |
| --- | --- |
| `schema_version` | 当前为 1 |
| `run_id` | 本次运行标识 |
| `task_id` | 对应任务 |
| `status` | `passed` / `failed` / `timeout` / `error` / `cancelled` |
| `started_at` | ISO 8601，带时区 |
| `duration_ms` | 总耗时 |
| `model` | 被测模型 |
| `budget` | 本次生效的预算 |
| `usage` | 轮次、工具调用次数等实际消耗 |
| `check` | 验收结果（`passed`、`details`、`duration_ms`） |
| `artifacts` | 工作区副本、轨迹等附件位置 |
| `notes` | 自由备注 |

关键约束：**失败与超时同样落盘**。只统计成功样本会系统性高估效果，计划第 4 节"效果测量"要求记录全部成功和失败。

## 当前任务

| ID | 能力维度 | 核心考察 |
| --- | --- | --- |
| `t01-off-by-one` | `single_file_fix` | 按行为定位并修正计算的越界与边界 |
| `t02-config-rename` | `cross_file_rename` | 跨文件一致性改名，区分新名与旧名的前缀关系 |
| `t03-add-feature` | `feature_implementation` | 按测试实现功能，并拒绝非法输入 |
| `t04-scope-discipline` | `scope_discipline` | 只改允许的文件；拒绝"调大上限"这条捷径 |
| `t05-shell-verify` | `shell_diagnosis` | 必须实际跑测试才能定位的导入失败 |

`t04` 是负例设计：工作区自带的测试用 `MAX_ITEMS` 判断容量，因此把 `MAX_ITEMS` 从 10 改成 11 也能让自带测试通过。验收脚本额外固定配置值为 10，并单独采样第 11 个元素的边界，所以这条捷径通不过。

## 新增一个任务

1. 建目录 `tests/fixtures/tasks/<task-id>/{workspace,solution}` 并写入文件。
2. 写 `check.py`，遵守上面的协议，只判行为、只读不写。
3. 在 `manifest.json` 的 `tasks` 里补一条元数据，`initial_state` 先留空对象。
4. 运行 `python tests/fixtures/tasks/regen_manifest.py` 生成摘要。
5. 运行 `python -m unittest tests.test_task_fixtures -q`。

验收测试会自动校验：字段齐全、工具名真实存在、工作区与摘要一致、**原样工作区必须验收失败**、**参考解必须验收通过**。最后两条是 fixture 有效性的底线，缺一不可。
