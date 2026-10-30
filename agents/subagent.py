"""子代理系统 —— 内置代理类型 + 自定义代理类型的 fork-return 模式。
镜像了 Claude Code 的 AgentTool：explore（只读）、plan（结构化）、general（全量工具），
另外支持通过 .mawoagent/agents/*.md 定义用户自定义代理。"""

from __future__ import annotations

from pathlib import Path

from .frontmatter import parse_frontmatter
from .tools import tool_definitions, ToolDef

# ─── Read-only tools (for explore and plan agents) ──────────

# explore / plan 子代理只能拿到这几个只读工具，避免它们修改项目文件或系统状态。
READ_ONLY_TOOLS = {"read_file", "list_files", "grep_search"}

# explore 子代理的系统提示词：定位为“代码库搜索专家”，强调只读、快速搜索和清晰汇报。
EXPLORE_PROMPT = """You are a file search specialist for mawoagent. You excel at thoroughly navigating and exploring codebases.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
- Creating new files (no write_file, touch, or file creation of any kind)
- Modifying existing files (no edit_file operations)
- Deleting files (no rm or deletion)
- Running ANY commands that change system state

Your role is EXCLUSIVELY to search and analyze existing code.

Your strengths:
- Rapidly finding files using glob patterns
- Searching code and text with powerful regex patterns
- Reading and analyzing file contents

Guidelines:
- Use list_files for broad file pattern matching
- Use grep_search for searching file contents with regex
- Use read_file when you know the specific file path you need to read
- Adapt your search approach based on the thoroughness level specified by the caller

NOTE: You are meant to be a fast agent that returns output as quickly as possible. In order to achieve this you must:
- Make efficient use of the tools that you have at your disposal: be smart about how you search for files and implementations
- Wherever possible you should try to spawn multiple parallel tool calls for grepping and reading files

Complete the user's search request efficiently and report your findings clearly."""

# plan 子代理的系统提示词：只读分析当前代码结构，并输出结构化的实现计划。
PLAN_PROMPT = """You are a Plan agent — a READ-ONLY sub-agent specialized for designing implementation plans.

IMPORTANT CONSTRAINTS:
- You are READ-ONLY. You only have access to read_file, list_files, and grep_search.
- Do NOT attempt to modify any files.

Your job:
- Analyze the codebase to understand the current architecture
- Design a step-by-step implementation plan
- Identify critical files that need modification
- Consider architectural trade-offs

Return a structured plan with:
1. Summary of current state
2. Step-by-step implementation steps
3. Critical files for implementation
4. Potential risks or considerations"""

# general 子代理的系统提示词：允许使用除 agent 外的完整工具集，适合独立完成更复杂任务。
GENERAL_PROMPT = """You are an agent for mawoagent. Given the user's message, you should use the tools available to complete the task. Complete the task fully—don't gold-plate, but don't leave it half-done. When you complete the task, respond with a concise report covering what was done and any key findings — the caller will relay this to the user, so it only needs the essentials.

Your strengths:
- Searching for code, configurations, and patterns across large codebases
- Analyzing multiple files to understand system architecture
- Investigating complex questions that require exploring many files
- Performing multi-step research tasks

Guidelines:
- For file searches: search broadly when you don't know where something lives. Use read_file when you know the specific file path.
- For analysis: Start broad and narrow down. Use multiple search strategies if the first doesn't yield results.
- Be thorough: Check multiple locations, consider different naming conventions, look for related files.
- NEVER create files unless they're absolutely necessary for achieving your goal. ALWAYS prefer editing an existing file to creating a new one."""

# ─── Custom agent discovery ─────────────────────────────────

# 自定义代理发现结果的进程内缓存；读取 .mawoagent/agents/*.md 后会复用，避免每次调用 agent 工具都扫目录。
_cached_custom_agents: dict[str, dict] | None = None
_cached_agent_signature = None


def _discover_custom_agents() -> dict[str, dict]:
    """发现用户级和项目级自定义代理，并按代理名称返回配置。"""
    global _cached_custom_agents, _cached_agent_signature
    from .project_trust import is_project_trusted
    from .workspace import project_root

    project = project_root()
    roots = [Path.home() / ".mawoagent" / "agents", project / ".mawoagent" / "agents"]
    signature = []
    for root in roots:
        for path in sorted(root.glob("*.md")):
            try:
                info = path.stat()
                signature.append((str(path.resolve()), info.st_mtime_ns, info.st_size, info.st_ino))
            except OSError:
                continue
    signature = (str(project.resolve()), is_project_trusted(project), tuple(signature))
    if _cached_custom_agents is not None and signature == _cached_agent_signature:
        return _cached_custom_agents

    agents: dict[str, dict] = {}
    # User-level (lower priority)
    _load_agents_from_dir(Path.home() / ".mawoagent" / "agents", agents)
    # Project-level (higher priority, overwrites) - only when the checkout is trusted.
    project_agents = project / ".mawoagent" / "agents"
    if is_project_trusted(project):
        _load_agents_from_dir(project_agents, agents)
    else:
        _load_agent_names_only(project_agents, agents)

    _cached_custom_agents = agents
    _cached_agent_signature = signature
    return agents


def _load_agent_names_only(directory: Path, agents: dict[str, dict]) -> None:
    """Show that a project defines an agent without reading what it says."""

    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if entry.suffix != ".md" or entry.stem in agents:
            continue
        agents[entry.stem] = {
            "name": entry.stem,
            "description": "",
            "allowed_tools": [],
            "system_prompt": "",
            "trusted": False,
        }


def _load_agents_from_dir(directory: Path, agents: dict[str, dict]) -> None:
    """从指定目录读取 Markdown 代理定义，并合并到 agents 字典中。"""
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if not entry.suffix == ".md":
            continue
        try:
            raw = entry.read_text(encoding="utf-8")
            result = parse_frontmatter(raw)
            meta = result.meta
            name = meta.get("name") or entry.stem
            allowed_tools = None
            if "allowed-tools" in meta:
                # allowed-tools 是逗号分隔的工具白名单；缺省时会在 get_sub_agent_config 中开放全部非 agent 工具。
                allowed_tools = [s.strip() for s in meta["allowed-tools"].split(",")]
            agents[name] = {
                "name": name,
                "description": meta.get("description", ""),
                "allowed_tools": allowed_tools,
                "system_prompt": result.body,
            }
        except Exception:
            # 单个自定义代理文件解析失败时不影响整个程序启动或其他代理加载。
            pass




def get_sub_agent_config(agent_type: str) -> dict:
    """根据代理类型生成 Agent 运行时需要的 system_prompt 和 tools 配置。"""
    custom = _discover_custom_agents().get(agent_type)
    if custom:
        if not custom.get("trusted", True):
            raise ValueError(
                f"agent type {agent_type!r} is defined in this project but was not loaded: "
                "the project is not trusted. Run /trust-project to enable it."
            )
        if custom["allowed_tools"] is not None:
            # 自定义代理显式声明工具白名单时，只授予白名单中的工具。
            tools = [t for t in tool_definitions if t["name"] in custom["allowed_tools"] and t["name"] != "agent"]
        else:
            # 不允许子代理再调用 agent 工具，避免递归创建子代理导致控制流复杂化。
            tools = [t for t in tool_definitions if t["name"] != "agent"]
        return {"system_prompt": custom["system_prompt"], "tools": tools}

    # 内置 explore / plan 使用相同的只读工具集合。
    read_only = [t for t in tool_definitions if t["name"] in READ_ONLY_TOOLS]

    if agent_type == "explore":
        return {"system_prompt": EXPLORE_PROMPT, "tools": read_only}
    elif agent_type == "plan":
        return {"system_prompt": PLAN_PROMPT, "tools": read_only}
    else:  # general
        return {"system_prompt": GENERAL_PROMPT, "tools": [t for t in tool_definitions if t["name"] != "agent"]}


# ─── 可用的agent类型(for system prompt) ──────────────


def get_available_agent_types() -> list[dict[str, str]]:
    """返回系统提示词中可展示的全部代理类型说明，包括内置代理和自定义代理。"""
    types = [
        {"name": "explore", "description": "Fast, read-only codebase search and exploration"},
        {"name": "plan", "description": "Read-only analysis with structured implementation plans"},
        {"name": "general", "description": "Full tools for independent tasks"},
    ]
    for name, defn in _discover_custom_agents().items():
        description = defn["description"] if defn.get("trusted", True) else "(not loaded: project is not trusted)"
        types.append({"name": name, "description": description})
    return types


def build_agent_descriptions() -> str:
    """把自定义代理类型格式化成 Markdown，供主 Agent 注入到系统提示词中。"""
    types = get_available_agent_types()
    if len(types) <= 3:
        return ""  # Only built-in types, already in system prompt

    custom = types[3:]
    lines = ["\n# Custom Agent Types", ""]
    for t in custom:
        lines.append(f"- **{t['name']}**: {t['description']}")
    return "\n".join(lines)


def reset_agent_cache() -> None:
    """清空自定义代理缓存；测试或运行中刷新 .mawoagent/agents 配置时使用。"""
    global _cached_custom_agents
    _cached_custom_agents = None
