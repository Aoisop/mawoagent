# 评测：折叠对照与回归口径

> 返回：[文档索引](../README.md)

> 本文从 README 拆出，内容与 v1.0.0 时一致。修改请直接改本文件，README 只保留入口。

## English

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

The samples only verify the evaluation pipeline. For the source of the paper numbers, how to run your own evaluation, and the result conventions, use the paper as the authoritative source; the evaluation approach is described in [runtime reliability notes](../runtime-hardening.md).

## 简体中文

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

样例仅用于确认评测链路；论文数字以论文原文为准；自行评测的方式与结果口径见 [运行可靠性说明](../runtime-hardening.md)。
