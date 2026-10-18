"""In-place tool-result compression without model, filesystem or UI dependencies."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

SNIP_THRESHOLD = 0.60
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell"}
MICROCOMPACT_IDLE_S = 5 * 60
KEEP_RECENT_RESULTS = 3
OLD_RESULT_PLACEHOLDER = "[Old result cleared]"


def _text_results(messages: list[dict[str, Any]], *, use_openai: bool) -> Iterator[dict[str, Any]]:
    """Yield mutable result containers, leaving text and non-text blocks untouched."""
    for message in messages:
        if use_openai:
            if message.get("role") == "tool" and isinstance(message.get("content"), str):
                yield message
        elif message.get("role") == "user" and isinstance(message.get("content"), list):
            for block in message["content"]:
                if (isinstance(block, dict) and block.get("type") == "tool_result"
                        and isinstance(block.get("content"), str)):
                    yield block


def _anthropic_tool_index(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index once instead of scanning the full conversation for every result."""
    tools = {}
    for message in messages:
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                tools.setdefault(block["id"], block)
    return tools


def compact_tool_results(messages: list[dict[str, Any]], *, use_openai: bool,
                         utilization: float, idle_seconds: float = 0) -> None:
    """Budget large results, snip stale results, then clear idle history.

    Preserve the established protocol policies: OpenAI snips any old text tool
    result; Anthropic snips only known rereadable tools and superseded file reads.
    Message roles, call identifiers, non-tool text and non-text content are intact.
    """
    results = list(_text_results(messages, use_openai=use_openai))
    if utilization >= 0.5:
        budget = 15000 if utilization > 0.7 else 30000
        keep = (budget - 80) // 2
        for result in results:
            content = result["content"]
            if len(content) > budget:
                result["content"] = (content[:keep]
                    + f"\n\n[... budgeted: {len(content) - keep * 2} chars truncated ...]\n\n"
                    + content[-keep:])

    if utilization >= SNIP_THRESHOLD:
        candidates = [result for result in results if result["content"] != SNIP_PLACEHOLDER]
        tools = {} if use_openai else _anthropic_tool_index(messages)
        if not use_openai:
            candidates = [result for result in candidates
                          if tools.get(result.get("tool_use_id"), {}).get("name") in SNIPPABLE_TOOLS]
        if len(candidates) > KEEP_RECENT_RESULTS:
            stale = set(range(len(candidates) - KEEP_RECENT_RESULTS))
            if not use_openai:
                latest_reads = {}
                for index, result in enumerate(candidates):
                    tool = tools[result["tool_use_id"]]
                    arguments = tool.get("input") or {}
                    path = arguments.get("file_path") if isinstance(arguments, dict) else None
                    if tool.get("name") == "read_file" and isinstance(path, str) and path:
                        if path in latest_reads:
                            stale.add(latest_reads[path])
                        latest_reads[path] = index
            for index in stale:
                candidates[index]["content"] = SNIP_PLACEHOLDER

    if idle_seconds >= MICROCOMPACT_IDLE_S:
        remaining = [result for result in results
                     if result["content"] not in {SNIP_PLACEHOLDER, OLD_RESULT_PLACEHOLDER}]
        for result in remaining[:-KEEP_RECENT_RESULTS]:
            result["content"] = OLD_RESULT_PLACEHOLDER
