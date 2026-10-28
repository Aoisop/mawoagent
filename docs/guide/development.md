# 开发与验证

> 返回：[文档索引](../README.md)

> 本文从 README 拆出，内容与 v1.0.0 时一致。修改请直接改本文件，README 只保留入口。

## English

### Development and verification

Python regression tests use a mock model and do not call paid models:

```bash
source .venv/bin/activate
python -m unittest discover -s tests -q
```

To check only the web backend:

```bash
python -m unittest discover -s tests -p 'test_web*.py' -q
```

Frontend syntax and unit tests use Node.js (CI uses Node.js 22) and require no npm packages:

```bash
node --check web/app.js
node --test web/tests/*.test.mjs
```

Coverage includes session increments, drafts and attachments, chat scrolling, Markdown rendering, and web permission and HTTP access boundaries. Model quality and Skill generalization require separate evaluation.

Coding-task fixtures with fixed initial states, allowed tools, budgets, and programmatic checks live in [tests/fixtures/tasks](../../tests/fixtures/tasks/README.md); `python -m unittest tests.test_task_fixtures` verifies that every fixture fails unsolved and passes against its reference solution.

## 简体中文

### 开发验证

Python 回归测试使用模拟模型，不请求付费模型：

```bash
source .venv/bin/activate
python -m unittest discover -s tests -q
```

仅检查 Web 后端时：

```bash
python -m unittest discover -s tests -p 'test_web*.py' -q
```

前端语法和单元测试使用 Node.js（CI 使用 Node.js 22），无需安装 npm 包：

```bash
node --check web/app.js
node --test web/tests/*.test.mjs
```

测试覆盖会话增量、草稿与附件、聊天滚动、Markdown 排版，以及 Web 权限与 HTTP 访问边界。模型效果和 Skill 泛化能力需要另行评测。

带固定初始状态、允许工具、预算和程序化检查的编码任务样本放在 [tests/fixtures/tasks](../../tests/fixtures/tasks/README.md)；执行 `python -m unittest tests.test_task_fixtures` 会校验每个样本在未解决时验收失败、套用参考解后验收通过。
