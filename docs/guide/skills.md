# Skills：格式、自进化与回归评测

> 返回：[文档索引](../README.md)

> 本文从 README 拆出，内容与 v1.0.0 时一致。修改请直接改本文件，README 只保留入口。

## English

### Skill self-evolution

The defining feature of mawoagent is **Skill self-evolution**. It extracts reusable rules from explicit user feedback, stores them as candidates by default, and publishes them to a project-level or user-level `SKILL.md` only after evaluation or explicit manual review.

#### 1. Enable automatic self-evolution

`MAWOAGENT_AUTO_SKILL_EVOLUTION` is enabled by default. For explicit configuration, set it in `.env`:

```env
MAWOAGENT_AUTO_SKILL_EVOLUTION=1
MAWOAGENT_AUTO_SKILL_TARGET=project
```

What they mean:

- `MAWOAGENT_AUTO_SKILL_EVOLUTION=1`: enable online Skill self-evolution.
- `MAWOAGENT_AUTO_SKILL_TARGET=project`: write automatically created Skills to the current project's `.mawoagent/skills/`.

To accumulate personal Skills shared across all projects:

```env
MAWOAGENT_AUTO_SKILL_TARGET=user
```

Corresponding paths:

```text
project: <project>/.mawoagent/skills/<skill_name>/SKILL.md
user:    ~/.mawoagent/skills/<skill_name>/SKILL.md
```

#### 2. Start in a permission mode that allows writes

Saving Skill candidates in the background requires a permission mode that allows maintenance operations. Recommended:

```bash
MAWOAGENT_AUTO_SKILL_EVOLUTION=1 \
MAWOAGENT_AUTO_SKILL_TARGET=project \
python -m agents.main --accept-edits
```

A more aggressive mode is also available:

```bash
python -m agents.main --yolo
```

Do not use `--yolo` as a long-term default, because it skips confirmations. `--accept-edits` is the daily recommendation.

#### 3. Give reusable feedback

Self-evolution is not about turning every sentence into a Skill. It only fits stable, explicit rules that will still apply to similar tasks in the future.

Not suitable for accumulation:

```text
Write me a 500-word government report.
```

Suitable for accumulation:

```text
From now on, when writing government reports, work summaries, or research material, start with a usable draft by default instead of asking follow-up questions. Structure it as title, background, current situation, problem analysis, measures, and next steps, and keep the language formal and restrained.
```

Suitable for evolving an existing Skill:

```text
For this kind of government report, include key data metrics from now on; do not rely only on conceptual statements.
```

#### 4. How the evolution loop works

```text
Turn N: user task
  -> Agent produces a result
  -> save the pending extraction window
Turn N+1: user feedback
  -> merge into the previous window
  -> persist pending feedback with the session, then submit to the background queue (independent of this turn's success)
  -> online_ingest()
  -> Extractor extracts candidate Skills
  -> Maintainer decides add / merge / discard
  -> save isolated candidates: view with /skill-candidates
  -> /skill-eval validates merge candidates for existing Skills
  -> /skill-apply <id> publishes a candidate that passed evaluation
  -> write SKILL.md; /skill-rollback <name> restores the previous version
  -> record provenance, usage stats, and version snapshots
```

Core files:

```text
agents/agent.py
agents/online_skill_evolution.py
agents/skills.py
agents/skill_evolution.py
```

Audit artifacts:

```text
.mawoagent/skill-evolution/usage.jsonl
.mawoagent/skill-evolution/online_provenance.jsonl
.mawoagent/skill-evolution/online_skill_provenance.json
.mawoagent/skill-evolution/skill_usage_stats.json
.mawoagent/skill-evolution/history/
```

### Online Skill regression evaluation

Run `/skill-eval` in the REPL. The first run generates `replay_review.jsonl`; choose "redo the original task" or "feedback correction", bind the target message turn, confirm session ownership and acceptance criteria, then run it again. Questions and context are rebuilt from the source, and duplicate targets are counted once. The current version, the candidate, and the existing champion re-answer the same task batch; same-session windows are grouped, sample count and model calls are capped, and incomplete or abnormal comparisons never get promoted.

These are regression checks for text tasks. Their sources may have been involved in Skill learning and do not represent independent benchmark results. Rules are no longer guessed from Skill keywords; championship snapshots are stored separately. String requirements must pass by default, optional requirements use `{"text":"requirement", "required":false}`, and critical failures cannot be offset by other scores. The `json_schema` check can validate fields and types, and `MAWOAGENT_SKILL_JUDGE_MODEL` can select a separate judge model on the same service.

When a new Skill has no independent replay samples, you can explicitly use `/skill-apply <id> --manual-review` after review; this is a manual release and does not imply the evaluation passed. Background jobs have a concurrency cap and persist pending work; source logs are read as incremental indexes. `/cleanup-state [days]` previews old tool results and evaluation run directories, and deletes only with an explicit `--apply`; the current champion run directory is preserved.

Candidate publishing, permissions, acceptance criteria, and recovery are described in [runtime reliability and Skills publishing](../runtime-hardening.md).

## 简体中文

### Skills 自进化

mawoagent 的核心特色是 **Skills 自进化**。它从用户明确反馈中抽取未来可复用的规则，默认保存候选；通过评测或明确的人工审核后，才发布到项目级或用户级 `SKILL.md`。

#### 1. 开启自动自进化

默认 `MAWOAGENT_AUTO_SKILL_EVOLUTION` 是开启的。为了明确配置，建议在 `.env` 中写：

```env
MAWOAGENT_AUTO_SKILL_EVOLUTION=1
MAWOAGENT_AUTO_SKILL_TARGET=project
```

含义：

- `MAWOAGENT_AUTO_SKILL_EVOLUTION=1`：启用在线 Skill 自进化。
- `MAWOAGENT_AUTO_SKILL_TARGET=project`：自动新增的 Skill 写入当前项目 `.mawoagent/skills/`。

如果希望沉淀为所有项目共享的个人 Skill：

```env
MAWOAGENT_AUTO_SKILL_TARGET=user
```

对应路径：

```text
project: <project>/.mawoagent/skills/<skill_name>/SKILL.md
user:    ~/.mawoagent/skills/<skill_name>/SKILL.md
```

#### 2. 用允许写入的权限模式启动

后台保存 Skill 候选需要当前权限模式允许维护操作。推荐使用：

```bash
MAWOAGENT_AUTO_SKILL_EVOLUTION=1 \
MAWOAGENT_AUTO_SKILL_TARGET=project \
python -m agents.main --accept-edits
```

也可以使用更激进的模式：

```bash
python -m agents.main --yolo
```

不建议长期默认使用 `--yolo`，因为它会跳过确认。日常推荐 `--accept-edits`。

#### 3. 给出可复用反馈

自进化不是把每一句话都写成 Skill。它只适合沉淀稳定、明确、未来同类任务仍适用的规则。

不太适合沉淀：

```text
帮我写一篇 500 字政府报告。
```

适合沉淀：

```text
以后写政府报告、工作汇报、调研材料时，默认先给可直接使用的初稿，不要连续追问；结构按“标题、背景、主要情况、问题分析、工作举措、下一步计划”组织，语言要正式克制。
```

适合演化已有 Skill：

```text
以后这类政府报告还要加入关键数据指标，不能只写概念性表述。
```

#### 4. 自进化链路如何工作

```text
第 N 轮用户任务
  -> Agent 输出结果
  -> 保存 pending extraction window
第 N+1 轮用户反馈
  -> 合并进上一轮 window
  -> 随会话持久化待提交反馈，再提交后台队列（不依赖本轮回答成功）
  -> online_ingest()
  -> Extractor 抽取候选 Skill
  -> Maintainer 判断 add / merge / discard
  -> 保存隔离候选：/skill-candidates 查看
  -> /skill-eval 验证已有 Skill 的合并候选
  -> /skill-apply <id> 发布通过评测的候选
  -> 写入 SKILL.md；/skill-rollback <name> 可恢复旧版本
  -> 记录 provenance、usage stats 和版本快照
```

核心文件：

```text
agents/agent.py
agents/online_skill_evolution.py
agents/skills.py
agents/skill_evolution.py
```

审计产物：

```text
.mawoagent/skill-evolution/usage.jsonl
.mawoagent/skill-evolution/online_provenance.jsonl
.mawoagent/skill-evolution/online_skill_provenance.json
.mawoagent/skill-evolution/skill_usage_stats.json
.mawoagent/skill-evolution/history/
```

### 在线 Skill 回归评测

REPL 中运行 `/skill-eval`。首次生成 `replay_review.jsonl`，选择“原任务重做 / 反馈修正”、绑定目标消息轮次，确认会话归属和验收条件后再次运行。题目和上下文从来源重建，重复目标只计一次。当前版、候选和已有冠军在同一批任务上重新作答；同会话窗口固定分组，样本数与模型调用量有上限，异常或不完整比较不会晋级。

这些是文本任务的回归检查，来源可能参与过 Skill 学习，不代表独立 benchmark 成绩。规则不再从 Skill 关键词自动猜测；冠军快照单独保存。当前 `requirements` 的字符串默认必须通过，可选项使用 `{"text":"要求", "required":false}`，关键失败不能靠其他分数抵消。`json_schema` 检查可校验字段及类型，`MAWOAGENT_SKILL_JUDGE_MODEL` 可单独指定同服务的裁判模型。

新 Skill 或缺少独立回放样本时，可在审阅后明确使用 `/skill-apply <id> --manual-review`；这是人工发布，不代表通过评测。后台任务有并发上限并保存待办；来源日志按增量索引读取。`/cleanup-state [days]` 预览旧工具结果和评测运行目录，明确加 `--apply` 才删除，当前冠军运行目录保留。

当前候选发布、权限、验收条件和恢复流程详见 [运行可靠性与 Skills 发布流程](../runtime-hardening.md)。
