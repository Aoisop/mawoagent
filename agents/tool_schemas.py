"""Built-in tool schemas shared by the agent runtime and tool dispatcher."""
from __future__ import annotations

from typing import Any, TypedDict


class _RequiredToolDefinition(TypedDict):
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolDef(_RequiredToolDefinition, total=False):
    deferred: bool


tool_definitions: list[ToolDef] = [
    {
        "name": "read_file",
        "description": "Read a file with line numbers. Returns at most 1000 lines and 24 KiB including line numbers, plus a short paging notice. Use offset and limit to page through large files; truncated output explains how to continue. Prefer edit_file over full replacement when only part of a file has been read.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to read"},
                "offset": {"type": "integer", "minimum": 1, "description": "First line to read (1-based, default: 1)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "Maximum lines to return (default: 1000); the byte limit may return fewer lines."},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to write"},
                "content": {"type": "string", "description": "The content to write to the file"},
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Edit a file by replacing an exact string match with new content. The old_string must match exactly (including whitespace and indentation).",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to edit"},
                "old_string": {"type": "string", "description": "The exact string to find and replace"},
                "new_string": {"type": "string", "description": "The string to replace it with"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "list_files",
        "description": "List files matching a glob pattern. Returns matching file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": 'Glob pattern to match files (e.g., "**/*.ts", "src/**/*")'},
                "path": {"type": "string", "description": "Base directory to search from. Defaults to current directory."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep_search",
        "description": "Search for a pattern in files. Returns matching lines with file paths and line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The Python regular expression pattern to search for"},
                "path": {"type": "string", "description": "Directory or file to search in. Defaults to current directory."},
                "include": {"type": "string", "description": 'File glob pattern to include (e.g., "*.ts", "*.py")'},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_shell",
        "description": "Execute a shell command and return its output. Use this for running tests, installing packages, git operations, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "timeout": {"type": "number", "description": "Timeout in milliseconds (default: 30000)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "skill",
        "description": "Invoke a registered skill by name. Skills are prompt templates loaded from .mawoagent/skills/. Returns the skill's resolved prompt to follow.",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "The name of the skill to invoke"},
                "args": {"type": "string", "description": "Optional arguments to pass to the skill"},
            },
            "required": ["skill_name"],
        },
    },
    {
        "name": "compact_context",
        "description": "Compact the current conversation context into structured session memory when the context is long, tool results are noisy, or a strategy reset is useful. This preserves task progress, current next steps, and tool-use experience, then continues from the folded memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief reason for compacting now, such as long context, many tool calls, repeated failures, or strategy change.",
                },
            },
        },
    },
    {
        "name": "skill_evolve",
        "description": "Persist an explicit reusable user correction or workflow preference into an existing skill. Creates a version snapshot before editing the skill.",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "The registered skill name to evolve"},
                "lesson": {"type": "string", "description": "Durable reusable rule to add to the skill"},
                "rationale": {"type": "string", "description": "Why this lesson should affect future similar tasks"},
                "target": {
                    "type": "string",
                    "enum": ["active", "project", "user"],
                    "description": "Which skill file to update. Defaults to active.",
                },
            },
            "required": ["skill_name", "lesson"],
        },
    },
    {
        "name": "skill_create",
        "description": "Create a new reusable skill from explicit durable workflow guidance when no suitable existing skill exists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Concise reusable skill name"},
                "description": {"type": "string", "description": "One-sentence description of what the skill does and when to use it"},
                "instructions": {"type": "string", "description": "Reusable SKILL.md body. Focus on durable method, constraints, and workflow, not one-off task content."},
                "when_to_use": {"type": "string", "description": "Trigger condition for auto-invocation"},
                "target": {
                    "type": "string",
                    "enum": ["project", "user"],
                    "description": "Where to create the skill. Defaults to project.",
                },
                "context": {
                    "type": "string",
                    "enum": ["inline", "fork"],
                    "description": "Skill execution mode. Defaults to inline.",
                },
                "user_invocable": {"type": "boolean", "description": "Whether users can invoke it manually with /<skill>. Defaults to false."},
                "allowed_tools": {"type": "string", "description": "Optional comma-separated allowed tools for fork mode"},
                "evidence": {"type": "string", "description": "Short user-provided evidence showing why this is reusable"},
            },
            "required": ["name", "description", "instructions"],
        },
    },
    {
        "name": "enter_plan_mode",
        "description": "Enter plan mode to switch to a read-only planning phase. In plan mode, you can only read files and write to the plan file.",
        "input_schema": {"type": "object", "properties": {}},
        "deferred": True,
    },
    {
        "name": "exit_plan_mode",
        "description": "Exit plan mode after you have finished writing your plan to the plan file.",
        "input_schema": {"type": "object", "properties": {}},
        "deferred": True,
    },
    {
        "name": "agent",
        "description": "Launch a sub-agent to handle a task autonomously. Sub-agents have isolated context and return their result. Types: 'explore' (read-only), 'plan' (read-only, structured planning), 'general' (full tools).",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Short (3-5 word) description of the sub-agent's task"},
                "prompt": {"type": "string", "description": "Detailed task instructions for the sub-agent"},
                "type": {"type": "string", "description": "Built-in or registered custom agent type. Default: general"},
            },
            "required": ["description", "prompt"],
        },
    },
    # ─── Tool search (deferred tool loader) ─────────────────────
    {
        "name": "tool_search",
        "description": "Search for available tools by name or keyword. Returns full schema definitions for matching deferred tools so you can use them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Tool name or search keywords"},
            },
            "required": ["query"],
        },
    },
    # ─── Memory tools (I6 / T17) ──────────────────────────────────────────
    {
        "name": "memory_save",
        "description": (
            "Save a durable memory about the user, their preferences, or this project. "
            "Always state where the content came from: source=user only when the user actually said it, "
            "source=repo when it is verifiable in the code or docs, source=inference when you worked it out. "
            "Inferences are read back later as if they were facts, so label them honestly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short name, unique enough to identify the memory later"},
                "description": {"type": "string", "description": "One line describing when this memory is useful"},
                "type": {
                    "type": "string",
                    "enum": ["user", "feedback", "project", "reference"],
                    "description": "user: preferences and background; feedback: a correction with why it matters; project: goals and decisions; reference: pointers to external resources",
                },
                "content": {"type": "string", "description": "The memory itself"},
                "source": {
                    "type": "string",
                    "enum": ["user", "repo", "inference"],
                    "description": "Where this came from. Defaults to user only if the user stated it directly.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["project", "global"],
                    "description": "project (default) applies to this workspace; global follows the user everywhere",
                },
            },
            "required": ["name", "description", "type", "content"],
        },
    },
    {
        "name": "memory_search",
        "description": "Search saved memories by keyword. Matches names, descriptions and bodies; newest first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword to look for"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Maximum results (default: 10)"},
                "include_archived": {"type": "boolean", "description": "Also search memories that were archived"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_archive",
        "description": (
            "Archive a memory: it stops appearing in the index and in recall, but stays on disk and can be "
            "restored. Prefer this over deleting when a memory is stale or was wrong."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "The memory file to archive, as shown in the index"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "memory_restore",
        "description": "Restore an archived memory back into the active set.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "The archived memory file to restore"},
            },
            "required": ["filename"],
        },
    },
]
