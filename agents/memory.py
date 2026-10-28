"""
文件型记忆系统。

核心思路：
1. 每个项目有独立的 memory 目录，目录名由当前工作目录 hash 得到。
2. 每条记忆都是一个 Markdown 文件，文件头部用 YAML frontmatter 保存元信息。
3. MEMORY.md 是自动生成的索引，给 system prompt 快速展示已有记忆。
4. 对话时先轻量扫描记忆文件头，再用 side query 让模型挑出相关记忆。
5. 召回到的记忆会以 <system-reminder> 形式注入当前对话。
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from .frontmatter import as_text, parse_frontmatter, format_frontmatter
from .storage import atomic_write_text, state_lock
# side query 是一个异步函数：输入 system prompt 和 user prompt，返回模型文本。
SideQueryFn = Callable[[str, str], Awaitable[str]]


VALID_TYPES = {"user", "feedback", "project", "reference"}
# Where a memory came from. "inference" is what the model worked out on its own;
# it must stay distinguishable from what the user actually said, otherwise a
# wrong guess gets promoted to a fact by being read back later.
VALID_SOURCES = {"user", "repo", "inference"}
VALID_SCOPES = {"project", "global"}
MAX_INDEX_LINES = 200       # MEMORY.md 注入 system prompt 前最多保留的行数。
MAX_INDEX_BYTES = 25000     # MEMORY.md 注入 system prompt 前最多保留的字节数。
MAX_INDEX_ENTRIES = 40      # 常驻索引的条目上限：再多就不是"常驻"了。
ARCHIVE_DIRNAME = "archive"
_logger = logging.getLogger(__name__)


class MemoryEntry:
    """完整 memory 条目，用于 /memory 列表和 CRUD 操作。"""

    __slots__ = ("name", "description", "type", "filename", "content", "source", "scope", "archived")

    def __init__(self, name: str, description: str, type: str, filename: str, content: str,
                 source: str = "user", scope: str = "project", archived: bool = False):
        self.name = name
        self.description = description
        self.type = type
        self.filename = filename
        self.content = content
        self.source = source
        self.scope = scope
        self.archived = archived




def _project_hash() -> str:
    """Stable hash of the project root, so two workspaces never share memories."""
    from .workspace import project_root

    return hashlib.sha256(str(project_root()).encode()).hexdigest()[:16]


def get_memory_dir() -> Path:
    """返回当前项目的 memory 目录，不存在时自动创建。"""
    d = Path.home() / ".mawoagent" / "projects" / _project_hash() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_index_path() -> Path:
    """MEMORY.md 是当前项目 memory 文件的索引文件。"""
    return get_memory_dir() / "MEMORY.md"




def _slugify(text: str) -> str:
    """把记忆名称转成适合文件名的短 slug。"""
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", text.lower())
    s = s.strip("_")
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    return f"{s[:31]}_{digest}" if s else digest




def archive_dir() -> Path:
    """Archived memories live beside the active ones and stay readable."""

    directory = get_memory_dir() / ARCHIVE_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _parse_entry(path: Path, *, archived: bool) -> MemoryEntry | None:
    result = parse_frontmatter(path.read_text(encoding="utf-8"))
    meta = result.meta
    # 没有 name/type 的文件不算合法 memory。
    if not meta.get("name") or not meta.get("type"):
        return None
    # type 不合法时降级为 project，避免坏文件中断列表。
    raw_type = as_text(meta["type"])
    entry_type = raw_type if raw_type in VALID_TYPES else "project"
    source = as_text(meta.get("source"))
    scope = as_text(meta.get("scope"))
    return MemoryEntry(
        name=as_text(meta["name"]),
        description=as_text(meta.get("description")),
        type=entry_type,
        filename=path.name,
        content=result.body,
        source=source if source in VALID_SOURCES else "user",
        scope=scope if scope in VALID_SCOPES else "project",
        archived=archived,
    )


def list_memories(include_archived: bool = False) -> list[MemoryEntry]:
    """读取当前项目所有 memory 文件，并按修改时间倒序返回。"""
    d = get_memory_dir()
    entries: list[tuple[float, MemoryEntry]] = []
    for f in sorted(d.glob("*.md")):
        # MEMORY.md 是索引，不是一条真实记忆。
        if f.name == "MEMORY.md":
            continue
        try:
            mtime = f.stat().st_mtime
            entry = _parse_entry(f, archived=False)
            if entry is not None:
                entries.append((mtime, entry))
        except (OSError, UnicodeError):
            _logger.warning("Skipping unreadable memory %s", f, exc_info=True)
    if include_archived:
        for f in sorted((d / ARCHIVE_DIRNAME).glob("*.md")) if (d / ARCHIVE_DIRNAME).exists() else []:
            try:
                mtime = f.stat().st_mtime
                entry = _parse_entry(f, archived=True)
                if entry is not None:
                    entries.append((mtime, entry))
            except (OSError, UnicodeError):
                _logger.warning("Skipping unreadable archived memory %s", f, exc_info=True)
    # 最近修改的记忆排在前面，方便 /memory 展示。
    entries.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in entries]


def save_memory(name: str, description: str, type: str, content: str,
                source: str = "user", scope: str = "project") -> str:
    """保存一条 memory，并刷新 MEMORY.md 索引。"""
    if type not in VALID_TYPES:
        raise ValueError("invalid memory type")
    if source not in VALID_SOURCES:
        raise ValueError(f"invalid memory source: {source}")
    if scope not in VALID_SCOPES:
        raise ValueError(f"invalid memory scope: {scope}")
    d = get_memory_dir()
    filename = f"{type}_{_slugify(name)}.md"
    text = format_frontmatter(
        {"name": name, "description": description, "type": type, "source": source, "scope": scope},
        content,
    )
    with state_lock(d / "MEMORY.md"):
        atomic_write_text(d / filename, text)
        _update_memory_index()
    return filename


def _checked_memory_path(filename: str, *, archived: bool = False) -> Path:
    directory = archive_dir() if archived else get_memory_dir()
    filepath = (directory / filename).resolve()
    if filepath.parent != directory.resolve() or filepath.name == "MEMORY.md":
        raise ValueError("memory filename must identify a single memory inside its directory")
    return filepath


def archive_memory(filename: str) -> bool:
    """Move a memory out of the index without losing it."""

    source = _checked_memory_path(filename)
    target = archive_dir() / filename
    if target.exists():
        raise ValueError(f"a memory named {filename} is already archived")
    if not source.exists():
        return False
    with state_lock(get_memory_dir() / "MEMORY.md"):
        source.replace(target)
        _update_memory_index()
    return True


def restore_memory(filename: str) -> bool:
    """Bring an archived memory back into the active set."""

    source = _checked_memory_path(filename, archived=True)
    if not source.exists():
        return False
    target = get_memory_dir() / filename
    if target.exists():
        raise ValueError(f"an active memory named {filename} already exists")
    with state_lock(get_memory_dir() / "MEMORY.md"):
        source.replace(target)
        _update_memory_index()
    return True


def search_memories(query: str, limit: int = 10, include_archived: bool = False) -> list[MemoryEntry]:
    """Case-insensitive substring match over name, description and body."""

    needle = query.strip().lower()
    if not needle:
        return []
    hits: list[MemoryEntry] = []
    for entry in list_memories(include_archived=include_archived):
        haystack = f"{entry.name}\n{entry.description}\n{entry.content}".lower()
        if needle in haystack:
            hits.append(entry)
            if len(hits) >= max(1, limit):
                break
    return hits


def delete_memory(filename: str) -> bool:
    """按文件名删除 memory，删除成功后刷新索引。"""
    directory = get_memory_dir()
    filepath = _checked_memory_path(filename)
    if not filepath.exists():
        return False
    with state_lock(directory / "MEMORY.md"):
        filepath.unlink()
        _update_memory_index()
    return True




def _update_memory_index() -> None:
    """根据当前 memory 文件重新生成 MEMORY.md。"""
    memories = list_memories()
    lines = ["# Memory Index", ""]
    for m in memories[:MAX_INDEX_ENTRIES]:
        # Source and scope stay visible in the index: "the user said this" and
        # "the model inferred this" must not look identical at recall time.
        lines.append(f"- **[{m.name}]({m.filename})** ({m.type}/{m.source}, {m.scope}) — {m.description}")
    if len(memories) > MAX_INDEX_ENTRIES:
        lines.append(f"- ... {len(memories) - MAX_INDEX_ENTRIES} more not listed; search with memory_search")
    atomic_write_text(_get_index_path(), "\n".join(lines))


def load_memory_index() -> str:
    """读取 MEMORY.md，并在注入 system prompt 前做长度保护。"""
    index_path = _get_index_path()
    if not index_path.exists():
        return ""
    content = index_path.read_text(encoding="utf-8")
    lines = content.split("\n")
    if len(lines) > MAX_INDEX_LINES:
        content = "\n".join(lines[:MAX_INDEX_LINES]) + "\n\n[... truncated, too many memory entries ...]"
    if len(content.encode()) > MAX_INDEX_BYTES:
        content = content.encode()[:MAX_INDEX_BYTES].decode(errors="ignore") + "\n\n[... truncated, index too large ...]"
    return content


# ─── Memory Header (lightweight scan) ──────────────────────

class MemoryHeader:
    """轻量 memory 摘要，只包含召回筛选需要的元信息。"""

    __slots__ = ("filename", "file_path", "mtime_ms", "description", "type")

    def __init__(self, filename: str, file_path: str, mtime_ms: float,
                 description: str | None, type: str | None):
        self.filename = filename
        self.file_path = file_path
        self.mtime_ms = mtime_ms
        self.description = description
        self.type = type


MAX_MEMORY_FILES = 200                    # 参与召回筛选的最多 memory 文件数。
MAX_MEMORY_HEADER_BYTES = 16 * 1024       # 防止超长单行或缺失结束符导致无限读取。
MAX_MEMORY_HEADER_LINES = 30
MAX_MEMORY_BYTES_PER_FILE = 4096          # 单个 memory 注入前的最大字节数。
MAX_SESSION_MEMORY_BYTES = 60 * 1024      # 单个会话最多注入的 memory 总量。


def _read_memory_header(path: Path) -> str:
    """Read only a bounded frontmatter prefix, stopping at its closing delimiter."""
    lines: list[bytes] = []
    remaining = MAX_MEMORY_HEADER_BYTES
    with path.open("rb") as stream:
        for index in range(MAX_MEMORY_HEADER_LINES):
            line = stream.readline(remaining)
            if not line:
                break
            lines.append(line)
            remaining -= len(line)
            if (index == 0 and line.strip() != b"---") or (index > 0 and line.strip() == b"---"):
                break
            if not remaining:
                break
    # A prefix may end inside a UTF-8 character; invalid interior bytes still fail.
    return codecs.getincrementaldecoder("utf-8")().decode(b"".join(lines), final=remaining > 0)


def _read_memory_content(path: Path) -> str:
    """Read at most the injection budget plus one byte to detect truncation."""
    with path.open("rb") as stream:
        raw = stream.read(MAX_MEMORY_BYTES_PER_FILE + 1)
    return codecs.getincrementaldecoder("utf-8")().decode(
        raw[:MAX_MEMORY_BYTES_PER_FILE], final=len(raw) <= MAX_MEMORY_BYTES_PER_FILE)


def scan_memory_headers() -> list[MemoryHeader]:
    """快速扫描 memory 文件头，不读取完整正文，用于低成本召回筛选。"""
    d = get_memory_dir()
    files: list[tuple[float, Path]] = []
    for f in d.glob("*.md"):
        if f.name == "MEMORY.md":
            continue
        try:
            files.append((f.stat().st_mtime, f))
        except OSError:
            _logger.warning("Skipping unavailable memory %s", f, exc_info=True)
    files.sort(key=lambda item: item[0], reverse=True)
    headers: list[MemoryHeader] = []
    for mtime, f in files:
        try:
            result = parse_frontmatter(_read_memory_header(f))
            meta = result.meta
            t = as_text(meta.get("type"))
            headers.append(MemoryHeader(
                filename=f.name,
                file_path=str(f),
                mtime_ms=mtime * 1000,
                description=as_text(meta.get("description")) or None,
                type=t if t in VALID_TYPES else None,
            ))
        except (OSError, UnicodeError):
            _logger.warning("Skipping unreadable memory header %s", f, exc_info=True)
            continue
        if len(headers) >= MAX_MEMORY_FILES:
            break
    return headers


def format_memory_manifest(headers: list[MemoryHeader]) -> str:
    """把 memory 摘要列表格式化成给 side query 阅读的 manifest。"""
    lines = []
    for h in headers:
        tag = f"[{h.type}] " if h.type else ""
        ts = datetime.fromtimestamp(h.mtime_ms / 1000, tz=timezone.utc).isoformat()
        if h.description:
            lines.append(f"- {tag}{h.filename} ({ts}): {h.description}")
        else:
            lines.append(f"- {tag}{h.filename} ({ts})")
    return "\n".join(lines)


# ─── Memory Age / Freshness ────────────────────────────────

def memory_age(mtime_ms: float) -> str:
    """把修改时间转换成适合展示的相对时间。"""
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def memory_freshness_warning(mtime_ms: float) -> str:
    """旧记忆可能过期，注入时提醒模型先核对当前代码。"""
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days <= 1:
        return ""
    return (f"This memory is {days} days old. Memories are point-in-time observations, "
            "not live state — claims about code behavior may be outdated. "
            "Verify against current code before asserting as fact.")



SELECT_MEMORIES_PROMPT = """You are selecting memories that will be useful to an AI coding assistant as it processes a user's query. You will be given the user's query and a list of available memory files with their filenames and descriptions.

Return a JSON object with a "selected_memories" array of filenames for the memories that will clearly be useful (up to 5). Only include memories that you are certain will be helpful based on their name and description.
- If you are unsure if a memory will be useful, do not include it.
- If no memories would clearly be useful, return an empty array."""


class RelevantMemory:
    """被召回并准备注入对话的完整 memory。"""

    __slots__ = ("path", "content", "mtime_ms", "header")

    def __init__(self, path: str, content: str, mtime_ms: float, header: str):
        self.path = path
        self.content = content
        self.mtime_ms = mtime_ms
        self.header = header

    @property
    def size(self) -> int:
        """当前 memory 内容占用的字节数，供 Agent 统计本会话注入预算。"""
        return len(self.content.encode())


async def select_relevant_memories(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
) -> list[RelevantMemory]:
    """
    从 memory 目录中选择和当前 query 最相关的记忆。

    流程：
    1. 扫描 memory 头信息，避免一开始就读取所有正文。
    2. 排除本 session 已经注入过的 memory，避免重复污染上下文。
    3. 把候选摘要交给 side query，让模型选择最多 5 个文件名。
    4. 读取被选中的 memory 正文，截断过大的文件。
    5. 包装成 RelevantMemory，交给后续注入逻辑。
    """

    # 扫描所有 memory 文件头信息。
    headers = scan_memory_headers()

    if not headers:
        return []

    # 排除已经展示过的 memory。
    candidates = [h for h in headers if h.file_path not in already_surfaced]
    if not candidates:
        return []

    # manifest 是给 side query 看的候选摘要列表。
    manifest = format_memory_manifest(candidates)

    # 调用 side_query，让模型根据文件名和描述挑选相关 memory。
    try:
        text = await side_query(
            SELECT_MEMORIES_PROMPT,
            f"Query: {query}\n\nAvailable memories:\n{manifest}",
        )

        # side query 可能返回解释文本，这里只提取其中的 JSON 对象。
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return []

        # 解析 JSON，拿到被选中的 memory 文件名。
        parsed = json.loads(match.group(0))
        selected_filenames = set(parsed.get("selected_memories", []))
        # 根据文件名筛选候选 memory，最多取 5 个。
        selected = [h for h in candidates if h.filename in selected_filenames][:5]

        result: list[RelevantMemory] = []
        for h in selected:
            try:
                content = _read_memory_content(Path(h.file_path))
            except (OSError, UnicodeError):
                # Files may disappear or change while the side query is running.
                _logger.warning("Skipping unreadable recalled memory %s", h.file_path, exc_info=True)
                continue

            # 根据 memory 修改时间生成提示头；旧记忆会附带 freshness warning。
            freshness = memory_freshness_warning(h.mtime_ms)
            header_text = (
                f"{freshness}\n\nMemory: {h.file_path}:" if freshness
                else f"Memory (saved {memory_age(h.mtime_ms)}): {h.file_path}:"
            )
            # 返回 RelevantMemory 列表，后续会被格式化成 <system-reminder>。
            result.append(RelevantMemory(
                path=h.file_path, content=content,
                mtime_ms=h.mtime_ms, header=header_text,
            ))
        return result
    except Exception as e:
        # 召回失败不应该影响主对话；取消类错误直接静默。
        if "cancel" in str(e).lower():
            return []
        print(f"[memory] semantic recall failed: {e}")
        return []


class MemoryPrefetch:
    """封装 memory 召回异步任务，供 Agent 主循环轮询。"""

    def __init__(self, task: asyncio.Task):
        self.task = task
        # consumed 表示结果是否已经注入过，避免同一个任务结果重复使用。
        self.consumed = False

    @property
    def settled(self) -> bool:
        """任务是否已经完成。"""
        return self.task.done()


def start_memory_prefetch(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
    session_memory_bytes: int,
) -> MemoryPrefetch | None:
    """
    在主模型回复前，提前异步启动 memory 召回。

    返回值不是 memory 内容，而是 MemoryPrefetch 句柄。
    Agent 主循环后续会检查任务是否完成，完成后再把 memory 注入当前消息。
    """

    # 只有多词输入才触发 memory 预取，避免每个短命令都消耗一次 side query。
    if not re.search(r"\s", query.strip()) and len(re.findall(r"[\u4e00-\u9fff]", query)) < 6:
        return None

    # 当前 session 的 memory 使用量不能超过预算。
    if session_memory_bytes >= MAX_SESSION_MEMORY_BYTES:
        return None

    # memory 目录里必须真的有 memory 文件。
    d = get_memory_dir()
    has_memories = any(f.suffix == ".md" and f.name != "MEMORY.md" for f in d.iterdir())
    if not has_memories:
        return None

    # 条件通过后创建异步任务，让召回和主模型请求并行推进。
    task = asyncio.create_task(
        select_relevant_memories(query, side_query, already_surfaced)
    )
    return MemoryPrefetch(task)


def format_memories_for_injection(memories: list[RelevantMemory]) -> str:
    """把召回的 memory 包成 system-reminder，便于注入到用户消息。"""
    parts = []
    for m in memories:
        parts.append(f"<system-reminder>\n{m.header}\n\n{m.content}\n</system-reminder>")
    return "\n\n".join(parts)


def build_memory_prompt_section() -> str:
    """
    生成注入 system prompt 的 Memory System 说明。

    这段说明告诉模型：
    - memory 文件存放在哪里；
    - 有哪些 memory 类型；
    - 如何通过 write_file 保存 memory；
    - 哪些内容不应该保存；
    - 当前 MEMORY.md 索引里有哪些记忆。
    """
    index = load_memory_index()
    memory_dir = str(get_memory_dir())

    return f"""# Memory System

You have a persistent, file-based memory system at `{memory_dir}`.

## Memory Types
- **user**: User's role, preferences, knowledge level
- **feedback**: Corrections and guidance from the user (include Why + How to apply)
- **project**: Ongoing work, goals, deadlines, decisions
- **reference**: Pointers to external resources (URLs, tools, dashboards)

## How to Save Memories
Use the `memory_save` tool. Do not write memory files with `write_file`: the tool
records the metadata the index depends on.

```
memory_save(name, description, type, content, source, scope)
```

- `type`: user | feedback | project | reference
- `source`: **user** when the user said it, **repo** when it is verifiable in the
  code or docs, **inference** when you concluded it yourself. This is not
  decoration: an inference is read back later as if it were a fact, so a wrong
  guess becomes a durable one.
- `scope`: project (default) or global

Search with `memory_search`, retire a memory with `memory_archive` (recoverable),
and bring it back with `memory_restore`. The MEMORY.md index is regenerated
automatically — do NOT update it manually.

## What NOT to Save
- Code patterns or architecture (read the code instead)
- Git history (use git log)
- Anything already in CLAUDE.md
- Ephemeral task details

## When to Recall
When the user asks you to remember or recall, or when prior context seems relevant.
{chr(10) + "## Current Memory Index" + chr(10) + index if index else chr(10) + "(No memories saved yet.)"}"""
