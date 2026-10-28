#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import difflib
import fnmatch
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from agents.memory import get_memory_dir
from agents.sandbox import get_backend
from agents.snapshots import record_before_change
from agents.tool_schemas import ToolDef, tool_definitions
from agents.workspace import WorkspaceContext, current_workspace, resolve_tool_path

ToolInput = dict[str, Any]
SyncToolHandler = Callable[[ToolInput], str]
PermissionMode = Literal["default", "plan", "acceptEdits", "bypassPermissions", "dontAsk"]

READ_TOOLS = {"read_file", "list_files", "grep_search", "compact_context"}
EDIT_TOOLS = {"write_file", "edit_file", "skill_evolve", "skill_create", "skill_prune", "skill_restore", "state_cleanup"}


#并发安全的工具可以并行运行（只读，无副作用）
CONCURRENCY_SAFE_TOOLS = {"read_file", "list_files", "grep_search"}


#----------------------工具调用----------------------------

def _resolve_tool_path(raw_path: str) -> Path:
    """Resolve against the workspace bound to the current tool call.

    Without a bound workspace the historical behaviour is kept, so direct
    callers and unit tests that never enter the runtime are unaffected.
    """
    return resolve_tool_path(raw_path)


@dataclass(frozen=True)
class FileVersion:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> FileVersion:
        # Windows reports a different st_ctime for path stat and open-handle
        # fstat, so ctime cannot participate in change detection there.
        changed_ns = 0 if os.name == "nt" else value.st_ctime_ns
        return cls(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, changed_ns)


class _FileResult(str):
    """Attach the version of the actual open file without changing tool output."""
    def __new__(cls, text: str, path: Path, version: FileVersion):
        result = super().__new__(cls, text)
        result.path = str(path)
        result.version = version
        return result


MAX_READ_BYTES = 24 * 1024
MAX_READ_LINES = 1000
_UNTRACKED = object()


def _file_version(path: Path) -> FileVersion | None:
    try:
        return FileVersion.from_stat(path.stat())
    except FileNotFoundError:
        return None


def _read_page(handle, offset: int, limit: int) -> str:
    """Read a line window with bounded buffers, including a final empty line."""
    for _ in range(offset - 1):
        piece = handle.readline(MAX_READ_BYTES + 1)
        if not piece:
            return f"No lines found at offset {offset}."
        while not piece.endswith("\n"):
            piece = handle.readline(MAX_READ_BYTES + 1)
            if not piece:
                return f"No lines found at offset {offset}."

    numbered: list[str] = []
    remaining = MAX_READ_BYTES
    for number in range(offset, offset + limit):
        piece = handle.readline(MAX_READ_BYTES + 1)
        line = piece[:-1] if piece.endswith("\n") else piece
        encoded = line.encode("utf-8")
        prefix = f"{number:4d} | "
        cost = len(prefix.encode("utf-8")) + len(encoded) + 1
        if cost > remaining:
            if numbered:
                notice = f"Byte limit reached. Continue with read_file(offset={number}, limit={limit})."
            else:
                beginning = encoded[:max(0, remaining - len(prefix.encode("utf-8")) - 1)].decode("utf-8", errors="ignore")
                numbered.append(prefix + beginning)
                notice = (f"Line {number} exceeds the {MAX_READ_BYTES}-byte limit; only its beginning is shown. "
                          f"Use run_shell for a narrower character range within this line; "
                          f"read_file(offset={number + 1}, limit={limit}) continues with the next line.")
            return ("\n".join(numbered) + f"\n\n[Truncated: {notice} "
                    "Partial file: prefer edit_file to preserve unseen content.]")
        numbered.append(prefix + line)
        remaining -= cost
        if not piece.endswith("\n"):
            result = "\n".join(numbered)
            if offset > 1:
                result += "\n\n[Partial file: earlier lines were omitted. Prefer edit_file to preserve unseen content.]"
            return result
    return ("\n".join(numbered) +
            f"\n\n[Truncated: Line limit reached. Continue with read_file(offset={offset + limit}, limit={limit}). "
            "Partial file: prefer edit_file to preserve unseen content.]")


def _read_file(inp: ToolInput) -> str:
    try:
        path = _resolve_tool_path(inp["file_path"]).resolve()
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            before = FileVersion.from_stat(os.fstat(handle.fileno()))
            result = _read_page(handle, inp.get("offset", 1), inp.get("limit", MAX_READ_LINES))
            after = FileVersion.from_stat(os.fstat(handle.fileno()))
        if before != after or _file_version(path) != before:
            return "Error reading file: file changed during the read. Please read_file again."
        return _FileResult(result, path, before)
    except Exception as e:
        return f"Error reading file: {e}"


def _replace_file(path: Path, content: str, expected_version=_UNTRACKED) -> FileVersion:
    """Stage content, then verify the observed version immediately before commit."""
    # Keep the pre-image before anything touches the file, so a turn can be
    # diffed and undone afterwards even if the write itself fails.
    record_before_change(path)
    current = _file_version(path)
    if expected_version is _UNTRACKED:
        expected_version = current
    if current != expected_version:
        raise ValueError("file was modified externally; please read_file again before writing")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if current is not None:
                mode = stat.S_IMODE(path.stat().st_mode)
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), mode)
                else:
                    os.chmod(temporary, mode)
            handle.write(content)
            handle.flush()
            staged_version = FileVersion.from_stat(os.fstat(handle.fileno()))
            if os.name == "nt":
                # Windows does not generally permit renaming an open file.
                # Its stat ctime is creation time and survives rename/link, so
                # retain the observed staging version instead of restatting
                # the destination and accidentally adopting an external edit.
                handle.close()
            if _file_version(path) != expected_version:
                raise ValueError("file was modified externally; please read_file again before writing")
            if expected_version is None:
                # Link rather than replace so a file created since the check is
                # never silently overwritten. The staged file is in the same directory.
                os.link(temporary, path)
                temporary.unlink()
            else:
                os.replace(temporary, path)
            return staged_version if handle.closed else FileVersion.from_stat(os.fstat(handle.fileno()))
    finally:
        temporary.unlink(missing_ok=True)


def _write_file(inp: ToolInput, expected_version=_UNTRACKED) -> str:
    try:
        path = _resolve_tool_path(inp["file_path"]).resolve()
        version = _replace_file(path, inp["content"], expected_version)
        _auto_update_memory_index(str(path))
        lines = inp["content"].split("\n")
        line_count = len(lines)
        preview = "\n".join(f"{i + 1:4d} | {l}" for i, l in enumerate(lines[:30]))
        trunc = f"\n  ... ({line_count} lines total)" if line_count > 30 else ""
        return _FileResult(f"Successfully wrote to {inp['file_path']} ({line_count} lines)\n\n{preview}{trunc}", path, version)
    except Exception as e:
        return f"Error writing file: {e}"

def _auto_update_memory_index(file_path:str) -> None:
    try:
        mem_dir = str(get_memory_dir())
        if file_path.startswith(mem_dir) and file_path.endswith(".md") and not file_path.endswith("MEMORY.md"):
            mem_path = Path(mem_dir)
            lines = ["# Memory Index", ""]
            for f in sorted(mem_path.glob("*.md")):
                if f.name == "MEMORY.md":
                    continue
                try:
                    raw = f.read_text(encoding="utf-8")
                    name_match = re.search(r"^name:\s*(.+)$", raw, re.MULTILINE)
                    type_match = re.search(r"^type:\s*(.+)$", raw, re.MULTILINE)
                    desc_match = re.search(r"^description:\s*(.+)$", raw, re.MULTILINE)
                    if name_match and type_match:
                        n = name_match.group(1).strip()
                        t = type_match.group(1).strip()
                        d = desc_match.group(1).strip()
                        lines.append(f"- **[{n}]({f.name})** ({t}) — {d}")
                except Exception:
                    pass
            (mem_path / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass

#-------------------------编辑助手，符号规范化+差异化------------------------

#将各种Unicode引号字符统一转换为标准的ASCII直引号。
def _normalize_quotes(s: str) -> str:
    s = re.sub("[\u2018\u2019\u2032]", "'", s)
    s = re.sub('[\u201c\u201d\u2033]', '"', s)
    return s

#查询匹配到的字符串
def _find_actual_string(file_content: str, search_string: str) -> str | None:
    if search_string in file_content:
        return search_string
    norm_search = _normalize_quotes(search_string)
    norm_file = _normalize_quotes(file_content)
    idx = norm_file.find(norm_search)
    if idx != -1:
        return file_content[idx:idx + len(search_string)]
    return None

#生成一个简单的文本差异格式
def _generate_diff(old_content: str, old_string: str, new_string: str) -> str:
    before_change = old_content.split(old_string)[0]
    line_num = before_change.count("\n") + 1
    old_lines = old_string.split("\n")
    new_lines = new_string.split("\n")

    parts = [f"@@ -{line_num},{len(old_lines)} +{line_num},{len(new_lines)} @@"]
    for l in old_lines:
        parts.append(f"- {l}")
    for l in new_lines:
        parts.append(f"+ {l}")
    return "\n".join(parts)

#编辑文件
def _proposed_edit(path: Path, inp: ToolInput) -> tuple[str, str, str]:
    """Return the content before, the content after, and a quote-normalization note."""

    content = path.read_text(encoding="utf-8", errors="replace")
    actual = _find_actual_string(content, inp["old_string"])
    if not actual:
        raise ValueError(f"old_string not found in {inp['file_path']}")

    # Normalized matches must be unique in the same representation used to
    # find them, including mixed straight/curly quote spellings.
    occurrences = (content.count(actual) if actual == inp["old_string"] else
                   _normalize_quotes(content).count(_normalize_quotes(inp["old_string"])))
    if occurrences > 1:
        raise ValueError(
            f"old_string found {occurrences} times in {inp['file_path']}. Must be unique."
        )

    note = " (matched via quote normalization)" if actual != inp["old_string"] else ""
    return content, content.replace(actual, inp["new_string"], 1), note


@dataclass(frozen=True)
class EditProposal:
    """What a write will do, bound to the exact file version it was built from."""

    tool: str
    display_path: str
    path: Path
    before: str
    after: str
    note: str
    base_version: FileVersion | None
    existed: bool

    @property
    def fingerprint(self) -> str:
        parts = [self.display_path, str(self.base_version), self.after]
        return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:10]

    def is_current(self) -> bool:
        """True when the file still holds the version this proposal was built on."""

        try:
            return _file_version(self.path) == self.base_version
        except OSError:
            return False

    def diff(self, limit: int = 120) -> str:
        old = self.before.splitlines(keepends=True)
        new = self.after.splitlines(keepends=True)
        label = self.display_path
        lines = list(difflib.unified_diff(
            old,
            new,
            fromfile=f"a/{label}" if self.existed else "/dev/null",
            tofile=f"b/{label}",
            lineterm="\n",
        ))
        if len(lines) > limit:
            lines = lines[:limit] + [f"... diff truncated ({len(lines) - limit} more lines)\n"]
        return "".join(lines)

    def render(self) -> str:
        header = f"{'edit' if self.tool == 'edit_file' else 'write'} file: {self.display_path}"
        state = "new file" if not self.existed else f"base {self.fingerprint}"
        extra = f"\n{self.note}" if self.note else ""
        return f"{header} ({state}){extra}\n{self.diff()}"


def build_edit_proposal(tool_name: str, inp: ToolInput) -> EditProposal:
    """Compute the exact change before asking the user to approve it."""

    path = _resolve_tool_path(inp["file_path"]).resolve()
    version = _file_version(path)

    if tool_name == "write_file":
        try:
            before = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            after = inp["content"]
            note = ""
        except OSError as exc:
            raise ValueError(f"cannot read {inp['file_path']}: {exc}") from exc
    elif tool_name == "edit_file":
        if version is None:
            raise ValueError(f"cannot edit {inp['file_path']}: the file does not exist")
        try:
            before, after, note = _proposed_edit(path, inp)
        except OSError as exc:
            raise ValueError(f"cannot read {inp['file_path']}: {exc}") from exc
    else:
        raise ValueError(f"{tool_name} does not produce an edit proposal")

    return EditProposal(
        tool=tool_name,
        display_path=str(inp.get("file_path", "")),
        path=path,
        before=before,
        after=after,
        note=note,
        base_version=version,
        existed=version is not None,
    )


def _edit_file(inp: ToolInput, expected_version=_UNTRACKED) -> str:
    try:
        path = _resolve_tool_path(inp["file_path"]).resolve()
        if expected_version is _UNTRACKED:
            expected_version = _file_version(path)

        content, new_content, note = _proposed_edit(path, inp)
        version = _replace_file(path, new_content, expected_version)
        diff = _generate_diff(content, inp["old_string"], inp["new_string"])
        return _FileResult(f"Successfully edited {inp['file_path']}{note}\n\n{diff}", path, version)
    except Exception as e:
        return f"Error editing file: {e}"


def _list_files(inp: ToolInput, path_allowed: Callable[[Path], bool] | None = None) -> str:
    try:
        base = _resolve_tool_path(inp.get("path") or ".")
        if path_allowed is not None and not path_allowed(base):
            return "No files found matching the pattern."
        pattern = inp["pattern"]
        files = []
        for p in base.glob(pattern):
            if p.is_file() and (path_allowed is None or path_allowed(p)):
                rel = str(p.relative_to(base) if base != Path(".") else p)

                if "node_modules" in rel or ".git" in rel.split(os.sep):
                    continue
                files.append(rel)
                if len(files) >= 200:
                    break
        if not files:
            return "No files found matching the pattern."
        result = "\n".join(files[:200])

        if len(files) > 200:
            result += f"\n...and {len(files) - 200} more files are found ..."
        return result
    except Exception as e:
        return f"Error listing files: {e}"


def _grep_search(inp: ToolInput, path_allowed: Callable[[Path], bool] | None = None) -> str:
    # One regex implementation avoids platform-dependent grep syntax and lets
    # us reject denied files before opening them, including glob discoveries.
    return _grep_python(inp["pattern"], str(_resolve_tool_path(inp.get("path") or ".")),
                        inp.get("include"), path_allowed)


def _grep_python(pattern: str, directory: str, include: str | None,
                 path_allowed: Callable[[Path], bool] | None = None) -> str:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Error searching files: invalid regular expression: {exc}"

    matches: list[str] = []

    def search_file(path: Path) -> None:
        if include and not fnmatch.fnmatch(path.name, include):
            return
        if path_allowed is not None and not path_allowed(path):
            return
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, start=1):
                    line = line.rstrip("\n")
                    if regex.search(line):
                        matches.append(f"{path}:{line_number}:{line}")
                        if len(matches) >= 200:
                            break
        except OSError:
            return

    root = Path(directory)
    if path_allowed is not None and not path_allowed(root):
        return "No matches found."
    if root.is_file():
        search_file(root)
    else:
        # Prune excluded directories before visiting them. Directory symlinks
        # are not followed because they may point back to an ancestor.
        for parent, directories, files in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if not name.startswith(".") and name != "node_modules"
                              and (path_allowed is None or path_allowed(Path(parent) / name))]
            for name in files:
                if not name.startswith(".") and name != "node_modules":
                    search_file(Path(parent) / name)
                if len(matches) >= 200:
                    break
            if len(matches) >= 200:
                break
    if not matches:
        return "No matches found."
    output = "\n".join(matches[:100])
    if len(matches) > 100:
        output += f"\n... and {len(matches) - 100} more matches"
    return output


#--------截断过长的工具调用结果

MAX_RESULT_CHARS = 50000


def _truncate_result(result:str) -> str:
    if len(result) <= MAX_RESULT_CHARS:
        return result
    keep_each = (MAX_RESULT_CHARS - 60) // 2
    return (
        result[:keep_each]
        +f"\n\n[... truncated {len(result) - keep_each * 2} chars ...]\n\n"
        +result[-keep_each:]
    )


#----------延迟工具激活---------------------------
_activated_tools: set[str] = set()


def reset_activated_tools() -> None:
    _activated_tools.clear()

def get_active_tool_definitions(all_tools: list[ToolDef] | None = None) -> list[ToolDef]:
    """Return active API schemas without internal deferred-tool metadata."""
    tools = all_tools if all_tools is not None else tool_definitions
    active: list[ToolDef] = []
    for tool in tools:
        if tool.get("deferred") and tool["name"] not in _activated_tools:
            continue
        schema = tool.copy()
        schema.pop("deferred", None)
        active.append(schema)
    return active

def get_deferred_tool_names(all_tools: list[ToolDef] | None = None) -> list[str]:
    tools = all_tools if all_tools is not None else tool_definitions
    return [t["name"] for t in tools if t.get("deferred") and t["name"] not in _activated_tools]

#执行shell命令
async def _run_shell(inp: ToolInput) -> str:
    timeout_ms = inp.get("timeout", 30000)
    timeout_s = timeout_ms / 1000
    if timeout_s <= 0:
        return "Error: timeout must be positive"
    try:
        backend = get_backend()
        result = await backend.run(inp["command"], timeout_s=timeout_s)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return f"Error: {e}"

    if result.timed_out:
        return f"Command timed out after {timeout_ms}ms"
    if result.error:
        return f"Error: {result.error}"

    stdout = result.stdout
    stderr = result.stderr
    if result.returncode != 0:
        stderr = f"\nStderr: {stderr}" if stderr else ""
        stdout = f"\nStdout: {stdout}" if stdout else ""
        return f"Command failed (exit code {result.returncode}){stdout}{stderr}"
    output = stdout or ""
    if stderr:
        output += ("\n" if output else "") + "Stderr:\n" + stderr
    return output or "(no output)"


#危险命令检测模式列表

DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s"),
    re.compile(r"\bgit\s+(push|reset|clean|checkout\s+\.)"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s"),
    re.compile(r">\s*/dev/"),
    re.compile(r"\bkill\b"),
    re.compile(r"\bpkill\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\bdel\s", re.IGNORECASE),
    re.compile(r"\brmdir\s", re.IGNORECASE),
    re.compile(r"\bformat\s", re.IGNORECASE),
    re.compile(r"\btaskkill\s", re.IGNORECASE),
    re.compile(r"\bRemove-Item\s", re.IGNORECASE),
    re.compile(r"\bStop-Process\s", re.IGNORECASE),
]
def is_dangerous(command: str) -> bool:
    return any(p.search(command) for p in DANGEROUS_PATTERNS)

#权限规则
def _parse_rule(rule: str) -> dict:
    m = re.match(r"^([A-Za-z0-9_-]+)\((.+)\)$", rule)
    if m:
        return {"tool": m.group(1), "pattern": m.group(2)}
    return {"tool": rule, "pattern": None}

def _load_settings(file_path: Path) -> dict | None:
    if not file_path.exists():
        return None
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return None

_cached_rules: dict | None = None
_cached_rules_signature = None


def load_permission_rules() -> dict:
    global _cached_rules, _cached_rules_signature
    from .project_trust import is_project_trusted, trust_file
    from .workspace import project_root

    # Rules belong to the project being worked on, not to the process directory:
    # a second workspace in the same process must not inherit the first's rules.
    project = project_root()
    paths = [Path.home() / ".mawoagent" / "settings.json", project / ".mawoagent" / "settings.json",
             project / ".mcp.json", trust_file()]
    def signature(path):
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
    current_signature = (str(project.resolve()), *(signature(path) for path in paths))
    if _cached_rules is not None and _cached_rules_signature == current_signature:
        return _cached_rules

    allow: list[dict] = []
    deny: list[dict] = []

    user_settings = _load_settings(Path.home() / ".mawoagent" / "settings.json")
    project_settings = _load_settings(project / ".mawoagent" / "settings.json")

    for settings, trusted in [(user_settings, True), (project_settings, is_project_trusted())]:
        if not settings or "permissions" not in settings:
            continue
        perms = settings["permissions"]
        if trusted:
            for r in perms.get("allow", []):
                allow.append(_parse_rule(r))
        for r in perms.get("deny", []):
            deny.append(_parse_rule(r))

    _cached_rules = {"allow": allow, "deny": deny}
    _cached_rules_signature = current_signature
    return _cached_rules


def _permission_paths(value: str) -> set[str]:
    lexical = os.path.normcase(os.path.abspath(value))
    paths = {lexical}
    try:
        # Resolve the original path before collapsing '..': alias/../secret
        # follows the symlink first, which can differ from abspath(value).
        path = Path(value)
        if not path.is_absolute():
            path = Path.cwd() / path
        paths.add(os.path.normcase(str(path.resolve())))
        # Preserve intermediate symlink routes: private/link -> public.txt
        # must still match private/* when private itself is reached by an alias.
        for parent in path.parents:
            resolved = parent.resolve()
            if resolved != parent:
                paths.add(os.path.normcase(os.path.abspath(resolved / path.relative_to(parent))))
    except (OSError, RuntimeError):
        pass
    return paths


def _permission_patterns(pattern: str) -> tuple[set[str], bool]:
    prefix = pattern.endswith("*")
    raw = pattern[:-1] if prefix else pattern
    trailing_separator = raw.endswith((os.sep, os.altsep or os.sep))
    paths = _permission_paths(raw)
    if prefix and trailing_separator:
        paths = {path if path.endswith(os.sep) else path + os.sep for path in paths}
    return paths, prefix


def _path_matches_pattern(value: str, pattern: str) -> bool:
    """Compare real paths while retaining the existing trailing-* prefix rules."""
    if pattern == "*":
        return True
    patterns, prefix = _permission_patterns(pattern)
    return any(candidate.startswith(normalized) if prefix else candidate == normalized
               for candidate in _permission_paths(value) for normalized in patterns)


def _discovery_path_filter(tool_name: str, permission_mode: PermissionMode) -> Callable[[Path], bool]:
    # Deny rules still filter results when confirmations are skipped: a denied
    # path must not reappear in a directory listing.
    rules = [rule for rule in load_permission_rules()["deny"]
             if rule["tool"] in {tool_name, "read_file"}]
    if any(rule["pattern"] in {None, "*"} for rule in rules):
        return lambda path: False
    patterns = [_permission_patterns(rule["pattern"]) for rule in rules]
    if not patterns:
        return lambda path: True

    def allowed(path: Path) -> bool:
        # Checking lexical and resolved parents covers a denied directory
        # discovered from an allowed ancestor and symlinks crossing its boundary.
        candidates = {str(parent) for value in _permission_paths(str(path))
                      for parent in (Path(value), *Path(value).parents)}
        return not any(candidate.startswith(pattern) if prefix else candidate == pattern
                       for values, prefix in patterns for pattern in values for candidate in candidates)

    return allowed


def _matches_rule(rule: dict, tool_name: str, inp: dict) -> bool:
    if rule["tool"] != tool_name:
        return False
    if rule["pattern"] is None:
        return True

    value = ""
    if tool_name == "run_shell":
        value = inp.get("command", "")
    elif tool_name == "mcp_start":
        value = inp.get("server_name", "")
    elif tool_name in {"skill_create", "skill_evolve", "skill_restore", "skill_prune"}:
        value = inp.get("skill_name") or inp.get("name", "")
    elif "file_path" in inp:
        return _path_matches_pattern(inp["file_path"], rule["pattern"])
    elif tool_name in {"grep_search", "list_files"}:
        return _path_matches_pattern(inp.get("path") or ".", rule["pattern"])
    elif tool_name.startswith("mcp__") and isinstance(inp.get("command"), str):
        value = inp["command"]
    else:
        return False

    pattern = rule["pattern"]
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return value == pattern


def _check_permission_rules(tool_name: str, inp: dict) -> str | None:
    rules = load_permission_rules()
    for rule in rules["deny"]:
        if _matches_rule(rule, tool_name, inp):
            return "deny"
    for rule in rules["allow"]:
        if _matches_rule(rule, tool_name, inp):
            return "allow"
    return None


PATH_TOOLS = {"read_file", "write_file", "edit_file", "list_files", "grep_search"}
WRITE_PATH_TOOLS = {"write_file", "edit_file"}


def _confirm_outside_scope(mode: PermissionMode, message: str) -> dict:
    """A path outside the granted range needs an explicit decision."""

    if mode == "dontAsk":
        return {"action": "deny", "message": f"Auto-denied (dontAsk mode): {message}"}
    if mode == "plan":
        return {"action": "deny", "message": f"Blocked in plan mode: {message}"}
    return {"action": "confirm", "message": message}


def _workspace_policy_check(
    tool_name: str,
    inp: dict,
    mode: PermissionMode,
    workspace: WorkspaceContext | None,
) -> dict | None:
    """Apply the workspace range before any blanket mode shortcut.

    Only file tools are governed here. ``run_shell`` is untouched: the OS
    reaches anything the user can, so the range below is an approval boundary
    for the built-in file tools, not a sandbox.
    """
    if workspace is None or tool_name not in PATH_TOOLS:
        return None

    raw = inp.get("file_path") or inp.get("path") or "."
    decision = workspace.classify(raw)
    writes = tool_name in WRITE_PATH_TOOLS

    if decision.scope == "outside":
        target = f"{decision.display} (workspace root: {workspace.root})"
        if writes and workspace.workspace_only_writes:
            return _confirm_outside_scope(mode, f"write outside the workspace: {target}")
        if not writes and workspace.workspace_only_reads:
            return _confirm_outside_scope(mode, f"read outside the workspace: {target}")
        return None

    if writes and decision.protected:
        return _confirm_outside_scope(mode, f"write to a policy file: {decision.display}")
    if writes and decision.scope == "runtime_state":
        return _confirm_outside_scope(
            mode, f"write to the runtime state directory: {decision.display}"
        )
    return None


def _policy_write_denied(
    tool_name: str,
    inp: dict,
    workspace: WorkspaceContext | None,
) -> dict | None:
    """Deny edits to policy-defining files even when confirmations are skipped.

    ``bypassPermissions`` means "do not ask", not "allow this session to rewrite
    the rules that constrain it". Without this check the blanket shortcut would
    silently disable the workspace boundary that governs the file tools.
    """

    if tool_name not in WRITE_PATH_TOOLS:
        return None
    active = workspace if workspace is not None else current_workspace()
    if active is None:
        return None
    raw = inp.get("file_path") or inp.get("path") or "."
    decision = active.classify(raw)
    if not decision.protected:
        return None
    return {
        "action": "deny",
        "message": (
            "Policy files are never auto-approved, even with bypassPermissions: "
            f"{decision.display}"
        ),
    }


def check_permission(
    tool_name: str,
    inp: dict,
    mode: PermissionMode = "default",
    plan_file_path: str | None = None,
    workspace: WorkspaceContext | None = None,
) -> dict:
    """Returns {"action": "allow"|"deny"|"confirm", "message": ...}"""
    # Deny rules are a user's explicit statement about what must not happen, and
    # that is orthogonal to "do not ask me". Reading them before the bypass
    # shortcut keeps a yolo session from rewriting the constraints on itself.
    rule_result = _check_permission_rules(tool_name, inp)
    if rule_result == "deny":
        return {"action": "deny", "message": f"Denied by permission rule for {tool_name}"}

    if mode == "bypassPermissions":
        denied = _policy_write_denied(tool_name, inp, workspace)
        if denied is not None:
            return denied
        return {"action": "allow"}

    if mode == "plan":
        if tool_name == "mcp_start" or tool_name.startswith("mcp__"):
            return {"action": "deny", "message": "MCP tools are blocked in plan mode"}
        if tool_name in EDIT_TOOLS:
            file_path = inp.get("file_path") or inp.get("path")
            if (tool_name in {"write_file", "edit_file"} and plan_file_path
                    and _path_matches_pattern(file_path, plan_file_path)):
                return {"action": "allow"}
            return {"action": "deny", "message": f"Blocked in plan mode: {tool_name}"}
        if tool_name == "run_shell":
            return {"action": "deny", "message": "Shell commands blocked in plan mode"}

    if rule_result == "allow":
        # An explicit rule is how a user widens the range for one tool or path.
        return {"action": "allow"}

    scope_result = _workspace_policy_check(
        tool_name, inp, mode, workspace if workspace is not None else current_workspace()
    )
    if scope_result is not None:
        return scope_result

    if tool_name in READ_TOOLS:
        return {"action": "allow"}

    if tool_name in ("enter_plan_mode", "exit_plan_mode"):
        return {"action": "allow"}

    if tool_name.startswith("mcp__"):
        if mode == "dontAsk":
            return {"action": "deny", "message": "Auto-denied (dontAsk mode): MCP tool call"}
        return {"action": "confirm", "message": f"MCP tool call: {tool_name}\nArguments: {json.dumps(inp, ensure_ascii=False, sort_keys=True)}"}

    if mode == "acceptEdits" and tool_name in EDIT_TOOLS:
        return {"action": "allow"}

    needs_confirm = False
    confirm_message = ""

    # A shell command can write files through an interpreter, pipe, or subprocess.
    # A blacklist cannot reliably distinguish read-only commands from writes.
    if tool_name == "run_shell":
        needs_confirm = True
        confirm_message = inp.get("command", "")
    elif tool_name == "mcp_start":
        needs_confirm = True
        confirm_message = f"start MCP server {inp.get('server_name', '')}: {inp.get('command', '')} (cwd: {inp.get('cwd', '')})"
    elif tool_name == "write_file":
        needs_confirm = True
        confirm_message = f"write file: {inp.get('file_path', '')}"
    elif tool_name == "edit_file":
        needs_confirm = True
        confirm_message = f"edit file: {inp.get('file_path', '')}"
    elif tool_name == "skill_evolve":
        needs_confirm = True
        confirm_message = f"evolve skill: {inp.get('skill_name', '')}"
    elif tool_name == "skill_create":
        needs_confirm = True
        confirm_message = f"create skill: {inp.get('name', '')}"
    elif tool_name in {"skill_prune", "skill_restore"}:
        needs_confirm = True
        confirm_message = f"{tool_name}: {inp.get('skill_name', '')}"
    elif tool_name == "state_cleanup":
        needs_confirm = True
        confirm_message = f"delete old disposable artifacts: {json.dumps(inp, ensure_ascii=False)}"

    if needs_confirm:
        if mode == "dontAsk":
            return {"action": "deny", "message": f"Auto-denied (dontAsk mode): {confirm_message}"}
        return {"action": "confirm", "message": confirm_message}

    return {"action": "allow"}


#----------------执行工具调用-----------------------
# 'agent' 和 'skill' 这两个工具在 agent.py 中处理，以避免循环依赖。"

def _tool_search(inp: ToolInput) -> str:
    query = (inp.get("query") or "").lower()
    matches = [
        tool for tool in tool_definitions
        if tool.get("deferred")
        and (query in tool["name"].lower() or query in (tool.get("description") or "").lower())
    ]
    if not matches:
        return "No matching deferred tools found."

    _activated_tools.update(tool["name"] for tool in matches)
    return json.dumps(
        [{"name": tool["name"], "description": tool.get("description", ""), "input_schema": tool["input_schema"]}
         for tool in matches],
        indent=2,
    )


def _skill_evolve(inp: ToolInput) -> str:
    from .skills import evolve_skill

    result = evolve_skill(
        skill_name=inp.get("skill_name", ""),
        lesson=inp.get("lesson", ""),
        rationale=inp.get("rationale", ""),
        target=inp.get("target", "active"),
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


def _skill_create(inp: ToolInput) -> str:
    from .skills import create_skill

    result = create_skill(
        name=inp.get("name", ""),
        description=inp.get("description", ""),
        instructions=inp.get("instructions", ""),
        when_to_use=inp.get("when_to_use", "") or inp.get("when-to-use", ""),
        target=inp.get("target", "project"),
        context=inp.get("context", "inline"),
        user_invocable=bool(inp.get("user_invocable", False)),
        allowed_tools=inp.get("allowed_tools"),
        evidence=inp.get("evidence", ""),
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


def _check_file_write_state(name: str, inp: ToolInput, read_file_state: dict[str, FileVersion]) -> str | None:
    """Require a fresh read before replacing an existing file."""
    abs_path = str(_resolve_tool_path(inp["file_path"]).resolve())
    try:
        version = _file_version(Path(abs_path))
    except OSError as exc:
        return f"Error checking file before {name}: {exc}"

    if version is None:
        return None
    verb = "writing" if name == "write_file" else "editing"
    if abs_path not in read_file_state:
        return f"Error: You must read this file before {verb}. Use read_file first to see its current contents."
    if version != read_file_state[abs_path]:
        return f"Warning: {inp['file_path']} was modified externally since your last read. Please read_file again before {verb}."
    return None


def _memory_save(inp: ToolInput) -> str:
    from .memory import save_memory

    try:
        filename = save_memory(
            inp["name"],
            inp.get("description", ""),
            inp["type"],
            inp["content"],
            source=inp.get("source", "user"),
            scope=inp.get("scope", "project"),
        )
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"
    return f"Saved memory {filename} (source={inp.get('source', 'user')})."


def _memory_search(inp: ToolInput) -> str:
    from .memory import search_memories

    try:
        hits = search_memories(
            inp["query"],
            limit=int(inp.get("limit", 10)),
            include_archived=bool(inp.get("include_archived", False)),
        )
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"
    if not hits:
        return "No matching memories."
    lines = []
    for entry in hits:
        marker = " (archived)" if entry.archived else ""
        lines.append(
            f"{entry.filename} [{entry.type}/{entry.source}, {entry.scope}]{marker}: "
            f"{entry.name} — {entry.description}"
        )
    return "\n".join(lines)


def _memory_archive(inp: ToolInput) -> str:
    from .memory import archive_memory

    try:
        moved = archive_memory(inp["filename"])
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"
    return f"Archived {inp['filename']}." if moved else f"No active memory named {inp['filename']}."


def _memory_restore(inp: ToolInput) -> str:
    from .memory import restore_memory

    try:
        moved = restore_memory(inp["filename"])
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"
    return f"Restored {inp['filename']}." if moved else f"No archived memory named {inp['filename']}."


async def execute_tool(
    name: str, inp: ToolInput, read_file_state: dict[str, FileVersion] | None = None, *,
    truncate_result: bool = True, permission_mode: PermissionMode = "default",
) -> str:
    from .validation import validate_json

    definition = next((tool for tool in tool_definitions if tool["name"] == name), None)
    if definition is not None:
        error = validate_json(inp, definition["input_schema"])
        if error:
            return f"Error: invalid arguments for {name}: {error}"

    if name in {"write_file", "edit_file"} and read_file_state is not None:
        error = _check_file_write_state(name, inp, read_file_state)
        if error:
            return error

    if name == "tool_search":
        # Return the complete schema payload even when ordinary output is capped.
        return _tool_search(inp)

    if name == "run_shell":
        # Shell execution must remain awaitable so cancellation kills descendants.
        result = await _run_shell(inp)
    elif name in {"write_file", "edit_file"} and read_file_state is not None:
        path = str(_resolve_tool_path(inp["file_path"]).resolve())
        handler = _write_file if name == "write_file" else _edit_file
        result = handler(inp, expected_version=read_file_state.get(path))
    elif name in {"list_files", "grep_search"}:
        path_allowed = _discovery_path_filter(name, permission_mode)
        handler = _list_files if name == "list_files" else _grep_search
        result = await asyncio.to_thread(handler, inp, path_allowed)
    else:
        handlers: dict[str, SyncToolHandler] = {
            "read_file": _read_file,
            "write_file": _write_file,
            "edit_file": _edit_file,
            "list_files": _list_files,
            "grep_search": _grep_search,
            "skill_evolve": _skill_evolve,
            "skill_create": _skill_create,
            "memory_save": _memory_save,
            "memory_search": _memory_search,
            "memory_archive": _memory_archive,
            "memory_restore": _memory_restore,
        }
        handler = handlers.get(name)
        if handler is None:
            return f"Unknown tool: {name}"
        if name in CONCURRENCY_SAFE_TOOLS:
            result = await asyncio.to_thread(handler, inp)
        else:
            # Do not leave a file mutation running in a worker after cancellation.
            result = handler(inp)

    if name in {"read_file", "write_file", "edit_file"} and read_file_state is not None:
        if isinstance(result, _FileResult):
            read_file_state[result.path] = result.version
        elif name == "read_file":
            read_file_state.pop(str(_resolve_tool_path(inp["file_path"]).resolve()), None)
    return _truncate_result(result) if truncate_result else result


def reset_permission_cache() -> None:
    global _cached_rules
    _cached_rules = None


