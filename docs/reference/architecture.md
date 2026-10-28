# 架构与目录结构

> 返回：[文档索引](../README.md)

> 本文从 README 拆出，内容与 v1.0.0 时一致。修改请直接改本文件，README 只保留入口。

## English

### Architecture

Core runtime path:

```text
User input
  -> agents/main.py (CLI) or agents/web.py (web)
  -> Agent.chat()
  -> build prompt / retrieve Skills / prefetch Memory / initialize MCP
  -> call an OpenAI-compatible or Anthropic-compatible model
  -> model returns text or a tool call
  -> Runtime checks permissions
  -> execute tool / Skill / MCP / sub-agent
  -> write the tool result back to the model
  -> save the session
  -> run skill usage tracking and online skill evolution in the background
```

### Directory layout

```text
mawoagent/
├── agents/
│   ├── main.py                    # CLI entry, REPL, argument parsing
│   ├── web.py                     # web backend: static pages, HTTP API, SSE events
│   ├── config.py                  # shared environment and model config for CLI, web, evaluation
│   ├── agent.py                   # Agent Runtime, model calls, tool dispatch, context compaction
│   ├── context_runtime.py         # context preflight and time-bounded Memory recall shared by both protocols
│   ├── tool_results.py            # model-independent tool result compaction
│   ├── result_storage.py          # persist large tool results, checkpoints, and previews
│   ├── streaming.py               # isolated streaming attempts and fallback output
│   ├── checkpoints.py             # tool execution checkpoints and interrupt recovery
│   ├── tools.py                   # built-in tools and permission system
│   ├── tool_schemas.py            # tool definitions and parameter schemas
│   ├── prompt.py                  # dynamic system prompt construction
│   ├── skills.py                  # Skills loading, retrieval, execution, creation, and evolution
│   ├── online_skill_evolution.py  # online Skill extraction and add/merge/discard decisions
│   ├── skill_evolution.py         # Skill persistence, version snapshots, audit stats
│   ├── memory.py                  # long-term memory system
│   ├── mcp_client.py              # MCP stdio JSON-RPC client
│   ├── subagent.py                # sub-agent configuration
│   ├── session.py                 # session persistence and recovery
│   └── ui.py                      # terminal UI output
├── web/                          # frontend sources, no bundling required
│   ├── index.html                # page structure
│   ├── styles.css                # graphite dark theme and responsive layout
│   ├── app.js                    # page interactions, API requests, SSE subscriptions
│   ├── chat-view.mjs             # Markdown rendering and partial message updates
│   ├── chat-scroll.mjs           # chat scroll control
│   ├── conversation-state.mjs    # session state, drafts, and attachments
│   └── tests/                    # frontend unit tests
├── .mawoagent/
│   ├── skills/                    # project-level Skills
│   └── skill-evolution/           # Skill self-evolution audit artifacts
├── data/                          # local benchmark data only, not distributed (docs/datasets.md)
├── docs/                          # web workbench, runtime reliability, and iteration strategy
├── tests/                         # Python regression tests
├── .github/                       # CI workflows
├── Dockerfile
├── .env.example                  # model configuration template
├── requirements.lock             # pinned dependencies used for installs
├── requirements.txt              # direct dependencies and version ranges
└── README.md
```

## 简体中文

### 项目架构

核心运行链路：

```text
用户输入
  -> agents/main.py（CLI）或 agents/web.py（网页）
  -> Agent.chat()
  -> 构建 Prompt / 检索 Skills / 预取 Memory / 初始化 MCP
  -> 调用 OpenAI-compatible 或 Anthropic-compatible 模型
  -> 模型返回文本或 tool call
  -> Runtime 做权限检查
  -> 执行工具 / Skill / MCP / 子 Agent
  -> tool result 回写模型
  -> 保存 Session
  -> 后台执行 Skill usage tracking 和 online skill evolution
```

### 目录结构

```text
mawoagent/
├── agents/
│   ├── main.py                    # CLI 入口、REPL、参数解析
│   ├── web.py                     # Web 后端：静态页面、HTTP API、SSE 流式事件
│   ├── config.py                  # CLI、Web、评测共用的环境和模型配置
│   ├── agent.py                   # Agent Runtime、模型调用、工具调度、上下文压缩
│   ├── context_runtime.py         # 两种协议共用的上下文预检和限时 Memory 召回
│   ├── tool_results.py            # 无模型依赖的工具结果压缩策略
│   ├── result_storage.py          # 大工具结果单独落盘、检查点与上下文保留预览
│   ├── streaming.py               # 流式尝试隔离与失败输出回退
│   ├── checkpoints.py             # 工具执行检查点与中断恢复
│   ├── tools.py                   # 内置工具和权限系统
│   ├── tool_schemas.py            # 工具定义与参数 Schema
│   ├── prompt.py                  # System prompt 动态构建
│   ├── skills.py                  # Skills 加载、检索、执行、创建和演化封装
│   ├── online_skill_evolution.py  # 在线 Skill 抽取和 add/merge/discard 决策
│   ├── skill_evolution.py         # Skill 落盘、版本快照、审计统计
│   ├── memory.py                  # 长期记忆系统
│   ├── mcp_client.py              # MCP stdio JSON-RPC 客户端
│   ├── subagent.py                # 子 Agent 配置
│   ├── session.py                 # 会话保存与恢复
│   └── ui.py                      # 终端 UI 输出
├── web/                          # 前端源码，无需打包
│   ├── index.html                # 页面结构
│   ├── styles.css                # 石墨深色主题与响应式布局
│   ├── app.js                    # 页面交互、API 请求与 SSE 订阅
│   ├── chat-view.mjs             # Markdown 排版与消息局部更新
│   ├── chat-scroll.mjs           # 聊天滚动控制
│   ├── conversation-state.mjs    # 会话状态、草稿与附件
│   └── tests/                    # 前端单元测试
├── .mawoagent/
│   ├── skills/                    # 项目级 Skills
│   └── skill-evolution/           # Skills 自进化审计产物
├── data/                          # 仅本地评测数据，不随仓库分发（见 docs/datasets.md）
├── docs/                          # Web 工作台、运行可靠性与迭代方向说明
├── tests/                         # Python 回归测试
├── .github/                       # CI 工作流
├── Dockerfile
├── .env.example                  # 模型配置模板
├── requirements.lock             # 固定版本依赖，安装时使用
├── requirements.txt              # 直接依赖与版本范围
└── README.md
```
