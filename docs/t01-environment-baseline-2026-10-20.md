# T01 环境与测试基线（2026-10-20）

对应 [迭代计划表](iteration-roadmap-2026-10-19.md) 中的 I0 / T01 / PR-01。本文记录本轮实际执行的修复、可复现步骤和测得的基线数字；未执行的项目单独标注，不与已完成项混写。

## 1. 本轮结论

- 本地 Python 环境已按 `requirements.lock` 重建，安装集合与锁文件逐条一致：0 项缺失、0 项多余，`pip check` 无冲突。
- 测试基线已实测并记录：Python 回归 **213 通过 / 6 跳过 / 380 秒**；前端 **30 通过**。
- 存在一个未闭合的版本缺口：本机只有 Python 3.13，而 CI 矩阵只覆盖 3.11 和 3.12。3.13 仅在 Windows 上验证过，详见第 5 节。
- T01 在可验证范围内已完成：环境可按锁文件重建，基线已实测记录。受本机解释器限制，README 推荐范围内的 3.11 / 3.12 未做本地验证。T02 的 5 个任务 fixture 已于同日落地，见 [fixture 说明](../tests/fixtures/tasks/README.md)。

## 2. 修复前的环境问题

| 问题 | 证据 | 影响 |
| --- | --- | --- |
| 虚拟环境不是在本仓库创建的 | 旧 `.venv/pyvenv.cfg` 的 `command` 行指向 `D:\mawoagent\BearCode\.venv` | 环境来源不可复现，与 README 描述的一次性创建流程不符 |
| 解释器指向 Microsoft Store 别名 | `home` 为 `...\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0` | 商店应用更新后该路径会变化，虚拟环境可能静默失效 |
| 本地版本与文档、CI 不一致 | 本地 3.13.14；README 写"3.11 或 3.12"；CI 矩阵为 3.11 / 3.12 | 本地基线跑在 CI 不验证的版本上，回归无法交叉确认 |
| 没有基线记录 | `docs/` 下无任何测试结果文档 | 后续改动缺少可比对的回归基准 |

## 3. 本轮操作与复现步骤

在仓库根目录执行（Windows）：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m unittest discover -s tests -q
```

前端校验需要 Node.js 22：

```bash
node --check web/app.js
node --test web/tests/*.test.mjs
```

`.venv/` 已被 `.gitignore` 覆盖，不进入版本库；依赖集合由 `requirements.lock` 固定，重建后应与锁文件完全一致。

## 4. 验证结果

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 解释器 | `.venv\Scripts\python.exe --version` | Python 3.13.14 |
| 依赖一致性 | `pip freeze` 与 `requirements.lock` 求差集 | 两个方向均为空 |
| 依赖完整性 | `python -m pip check` | `No broken requirements found.` |
| Python 回归 | `python -m unittest discover -s tests -q` | `Ran 213 tests` / `OK (skipped=6)` / 380.010s |
| 前端语法 | `node --check web/app.js` | 通过（Node.js 22.22.2） |
| 前端单测 | `node --test web/tests/*.test.mjs` | `# pass 30` / `# fail 0` / 310ms |

测试使用 mock 模型，不调用付费接口。上述数字是重建环境后重新执行的实测结果，不是静态解析或沿用历史记录。

T02 落地后新增 17 个 fixture 验收用例，完整套件为 230 个（`OK (skipped=6)`，402 秒），上表的 213 是 T01 交付时的口径。

## 5. 已知问题与限制

1. **Python 3.13 只在 Windows 本地验证。** CI 矩阵仍为 3.11 / 3.12（Linux、macOS、Windows）。本轮按既定范围未修改 CI，因此 3.13 在 Linux 与 macOS 上属于未测状态。建议后续单独增加一轮 3.13 的 CI 观察，稳定后再收紧 README 的表述。
2. **解释器来自 Microsoft Store 版 Python。** 该发行版的安装路径随商店更新变化，虚拟环境的 `home` 随之失效。长期建议改用 python.org 官方安装版重建环境，这对可复现性的影响大于版本号本身。
3. **测试日志存在干扰堆栈。** `agents/web.py` 的 `ConnectionAbortedError`（`WinError 10053`）由测试主动断连、服务端线程继续写入触发。用例判定为通过，但堆栈会混进 CI 的失败摘要，增加排错噪音。
4. **耗时余量有限。** 两次完整执行分别耗时 401 秒与 380 秒（同一 Python 3.13.14、同一台机器），波动约 5%。CI 的 `timeout-minutes: 10` 对应余量约 3 分钟；增加慢用例时需要同步评估该上限。

## 6. 本轮改动的文件

| 文件 | 改动 |
| --- | --- |
| `README.md` | 支持的 Python 版本改为 3.11–3.13 并注明验证范围；Windows 创建命令由 `py -3.12` 改为 `py -3`；补充 `pip check` 与测试的校验步骤；文档索引增加本文 |
| `docs/t01-environment-baseline-2026-10-20.md` | 新增本文 |

## 7. 未完成项

- **CI 覆盖 3.13**：需要一轮 Linux / macOS 的实际验证，本轮未做。
- **T02 之后**：任务样本尚无执行评测入口。模型侧的成功率、工具次数、token 与耗时对照属于 T09 和 T16，需要先完成隔离执行（I4）。
