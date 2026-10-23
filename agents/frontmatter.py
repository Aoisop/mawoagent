"""Shared YAML frontmatter parser for memory and skills files.

The previous version only understood ``key: value`` and kept every value as a
raw string, so ``user-invocable: false`` stayed truthy and a list had to be
written as a comma-separated string. This is a small YAML subset instead:

* scalars with quotes, booleans, null and numbers
* block lists (``- item``) and flow lists (``[a, b]``)
* nested mappings by indentation, and flow maps (``{a: b}``)
* literal (``|``) and folded (``>``) block scalars, with ``-``/``+`` chomping
* ``#`` comments outside quotes

It is deliberately not a full YAML implementation and has no third-party
dependency. Content that does not fit the subset falls back to the old
line-by-line reading instead of raising, so an existing SKILL.md keeps working.
``format_frontmatter`` writes a form this module reads back exactly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}
_NULL = {"null", "~", ""}
_INT = re.compile(r"[-+]?\d+\Z")
_FLOAT = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+[eE][-+]?\d+|\d+\.\d*[eE][-+]?\d+)\Z")
_RESERVED_START = set("-?:,[]{}#&*!|>'\"%@`")


class _YamlError(ValueError):
    pass


@dataclass
class FrontmatterResult:
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""


# -- reading ---------------------------------------------------------------
def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank(line: str) -> bool:
    return not line.strip()


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _next_content(lines: list[str], start: int) -> int | None:
    index = start
    while index < len(lines):
        if not _is_blank(lines[index]) and not _is_comment(lines[index]):
            return index
        index += 1
    return None


def _strip_comment(text: str) -> str:
    in_single = in_double = False
    for index, char in enumerate(text):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            if index == 0 or text[index - 1] in " \t":
                return text[:index].rstrip()
    return text.rstrip()


def _unquote(text: str) -> Any:
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        try:
            return json.loads(text)
        except ValueError as exc:
            raise _YamlError(f"bad double-quoted string: {text}") from exc
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    return text


def _parse_scalar(text: str) -> Any:
    text = _strip_comment(text).strip()
    if not text:
        return ""
    if text[0] in "'\"":
        return _unquote(text)
    lowered = text.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    if lowered in _NULL:
        return None
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    return text


def _split_key(text: str) -> tuple[str, str] | None:
    """Split ``key: value`` on the first unquoted colon followed by space or end."""

    in_single = in_double = False
    for index, char in enumerate(text):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == ":" and not in_single and not in_double:
            remainder = text[index + 1:]
            if remainder and not remainder.startswith((" ", "\t")):
                continue
            key = _unquote(text[:index].strip())
            return str(key), remainder.strip()
    return None


def _split_flow_items(text: str) -> list[str]:
    items: list[str] = []
    depth = 0
    in_single = in_double = False
    current: list[str] = []
    for char in text:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char in "[{" and not in_single and not in_double:
            depth += 1
        elif char in "]}" and not in_single and not in_double:
            depth -= 1
        if char == "," and depth == 0 and not in_single and not in_double:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        items.append("".join(current).strip())
    return [item for item in items if item]


def _parse_flow(text: str) -> Any:
    text = _strip_comment(text).strip()
    if text.startswith("["):
        if not text.endswith("]"):
            raise _YamlError("unterminated flow list")
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item) for item in _split_flow_items(inner)]
    if text.startswith("{"):
        if not text.endswith("}"):
            raise _YamlError("unterminated flow map")
        inner = text[1:-1].strip()
        if not inner:
            return {}
        result: dict[str, Any] = {}
        for item in _split_flow_items(inner):
            pair = _split_key(item)
            if pair is None:
                raise _YamlError("bad flow mapping entry")
            result[str(pair[0])] = _parse_scalar(pair[1])
        return result
    return _parse_scalar(text)


def _parse_block_scalar(lines: list[str], start: int, parent_indent: int, indicator: str) -> tuple[str, int]:
    collected: list[str] = []
    block_indent: int | None = None
    index = start
    while index < len(lines):
        line = lines[index]
        if _is_blank(line):
            collected.append("")
            index += 1
            continue
        current = _indent_of(line)
        if current <= parent_indent:
            break
        if block_indent is None:
            block_indent = current
        collected.append(line[min(block_indent, len(line)):])
        index += 1

    while collected and collected[-1] == "":
        collected.pop()

    if indicator.startswith("|"):
        text = "\n".join(collected)
    else:
        text = " ".join(part.strip() for part in collected if part.strip())
    if not indicator.endswith("-") and text:
        text += "\n"
    return text, index


def _parse_map(lines: list[str], start: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    index = start
    while True:
        row = _next_content(lines, index)
        if row is None:
            return result, len(lines)
        line = lines[row]
        current = _indent_of(line)
        if current < indent:
            return result, row
        if current > indent:
            raise _YamlError(f"unexpected indentation at line {row + 1}")
        stripped = line.strip()
        if stripped.startswith("- "):
            return result, row

        pair = _split_key(stripped)
        if pair is None:
            # Legacy tolerance: a line without a readable key is skipped.
            index = row + 1
            continue
        key, rest = pair
        if rest in {"|", "|-", "|+", ">", ">-", ">+"}:
            value, index = _parse_block_scalar(lines, row + 1, indent, rest)
            result[key] = value
        elif rest == "":
            child = _next_content(lines, row + 1)
            if child is None or _indent_of(lines[child]) <= indent:
                result[key] = None
                index = row + 1
            else:
                value, index = _parse_node(lines, child, _indent_of(lines[child]))
                result[key] = value
        else:
            result[key] = _parse_flow(rest)
            index = row + 1


def _parse_list(lines: list[str], start: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    index = start
    while True:
        row = _next_content(lines, index)
        if row is None:
            return items, len(lines)
        line = lines[row]
        current = _indent_of(line)
        if current < indent:
            return items, row
        if current > indent:
            raise _YamlError(f"unexpected indentation at line {row + 1}")
        stripped = line.strip()
        if not stripped.startswith("- "):
            return items, row
        rest = stripped[2:].strip()
        if not rest:
            child = _next_content(lines, row + 1)
            if child is None or _indent_of(lines[child]) <= indent:
                items.append(None)
                index = row + 1
            else:
                value, index = _parse_node(lines, child, _indent_of(lines[child]))
                items.append(value)
            continue

        pair = _split_key(rest)
        if pair is not None and not rest.startswith(("[", "{")):
            # "- key: value" starts a mapping item that may continue below.
            block = [" " * (indent + 2) + rest]
            following = row + 1
            while following < len(lines):
                candidate = lines[following]
                if candidate.strip() and _indent_of(candidate) <= indent:
                    break
                block.append(candidate)
                following += 1
            value, _ = _parse_map(block, 0, indent + 2)
            items.append(value)
            index = following
        else:
            items.append(_parse_flow(rest))
            index = row + 1


def _parse_node(lines: list[str], start: int, indent: int) -> tuple[Any, int]:
    if lines[start].lstrip().startswith("- "):
        return _parse_list(lines, start, indent)
    return _parse_map(lines, start, indent)


def _legacy_parse(lines: list[str]) -> dict[str, Any]:
    """The original lenient reader, kept for content outside the subset."""

    meta: dict[str, Any] = {}
    for line in lines:
        colon = line.find(":")
        if colon == -1:
            continue
        key = line[:colon].strip()
        if key:
            meta[key] = line[colon + 1:].strip()
    return meta


def parse_frontmatter(content: str) -> FrontmatterResult:
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return FrontmatterResult(body=text.strip())

    end_idx = -1
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_idx = index
            break
    if end_idx == -1:
        return FrontmatterResult(body=text.strip())

    block = lines[1:end_idx]
    try:
        first = _next_content(block, 0)
        meta = {} if first is None else _parse_node(block, first, _indent_of(block[first]))[0]
        if not isinstance(meta, dict):
            raise _YamlError("frontmatter is not a mapping")
    except (_YamlError, IndexError, ValueError):
        meta = _legacy_parse(block)

    body = "\n".join(lines[end_idx + 1:]).strip()
    return FrontmatterResult(meta=meta, body=body)


# -- writing ---------------------------------------------------------------
def _needs_quoting(text: str) -> bool:
    if text == "" or text != text.strip():
        return True
    lowered = text.lower()
    if lowered in _TRUE | _FALSE | _NULL:
        return True
    if _INT.match(text) or _FLOAT.match(text):
        return True
    if text[0] in _RESERVED_START:
        return True
    if ": " in text or text.endswith(":") or " #" in text:
        return True
    return False


def _dump_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if _needs_quoting(text):
        return json.dumps(text, ensure_ascii=False)
    return text


def _dump_value(key: str, value: Any, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        lines = [f"{pad}{key}:"]
        for child_key, child in value.items():
            lines.extend(_dump_value(str(child_key), child, indent + 2))
        return lines
    if isinstance(value, (list, tuple)):
        if not value:
            return [f"{pad}{key}: []"]
        lines = [f"{pad}{key}:"]
        for item in value:
            if isinstance(item, dict) and item:
                inner = list(item.items())
                first_key, first_value = inner[0]
                if isinstance(first_value, (dict, list)):
                    lines.append(f"{pad}  -")
                    lines.extend(_dump_value(str(first_key), first_value, indent + 4))
                else:
                    lines.append(f"{pad}  - {first_key}: {_dump_scalar(first_value)}")
                for child_key, child in inner[1:]:
                    lines.extend(_dump_value(str(child_key), child, indent + 4))
            elif isinstance(item, (list, tuple)):
                lines.append(f"{pad}  - {_flow(item)}")
            else:
                lines.append(f"{pad}  - {_dump_scalar(item)}")
        return lines
    if isinstance(value, str) and "\n" in value:
        body = value[:-1] if value.endswith("\n") else value
        indicator = "|" if value.endswith("\n") else "|-"
        lines = [f"{pad}{key}: {indicator}"]
        lines.extend(f"{pad}  {line}" if line else "" for line in body.split("\n"))
        return lines
    return [f"{pad}{key}: {_dump_scalar(value)}"]


def _flow(items: list[Any] | tuple[Any, ...]) -> str:
    return "[" + ", ".join(_dump_scalar(item) for item in items) + "]"


def format_frontmatter(meta: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for key, value in meta.items():
        lines.extend(_dump_value(str(key), value, 0))
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


# -- coercion for callers --------------------------------------------------
def as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [text for item in value if (text := as_text(item))]
    if isinstance(value, dict):
        return [str(key) for key in value]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [part.strip().strip("'\"") for part in inner.split(",") if part.strip()]
    return [part.strip() for part in text.split(",") if part.strip()]
