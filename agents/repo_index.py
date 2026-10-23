"""Symbol index and repository map (iteration I3 / T08).

The map is produced from a cached index under ``.mawoagent/repo-index.json``.
Parsing is per file and reused while the file's size and mtime stay the same, so
a normal refresh is a walk plus a stat per file; only edited files are re-parsed.

Python is parsed with ``ast``. JavaScript and TypeScript use line patterns and
are reported as approximate: the index says so in its own metadata, so nobody
mistakes a regex match for a resolved symbol.

Boundaries:

* Only files inside the workspace are indexed, and the runtime's own state
  directory is skipped, so the index never describes things a tool could not
  read anyway.
* Nothing here executes project code or imports project modules.
"""

from __future__ import annotations

import ast
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INDEX_VERSION = 2
INDEX_RELATIVE = Path(".mawoagent") / "repo-index.json"

INDEXED_SUFFIXES = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
}

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".mawoagent", "node_modules", "__pycache__",
    ".venv", "venv", "env", "dist", "build", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", "site-packages",
})

MAX_FILE_BYTES = 400_000
MAX_FILES = 3_000
MAX_SYMBOLS_PER_FILE = 200
MAP_BUDGET_DEFAULT = 2_500

_JS_FUNCTION = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(")
_JS_CLASS = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)")
_JS_ARROW = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>")
_JS_METHOD = re.compile(r"^\s{2,}(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{")


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    line: int
    signature: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "line": self.line, "signature": self.signature}

    @classmethod
    def from_dict(cls, data: dict) -> "Symbol":
        return cls(
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "")),
            line=int(data.get("line", 0)),
            signature=str(data.get("signature", "")),
        )


@dataclass
class FileEntry:
    path: str
    mtime_ns: int
    size: int
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "mtime_ns": self.mtime_ns,
            "size": self.size,
            "language": self.language,
            "symbols": [symbol.to_dict() for symbol in self.symbols],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FileEntry":
        return cls(
            path=str(data.get("path", "")),
            mtime_ns=int(data.get("mtime_ns", 0)),
            size=int(data.get("size", 0)),
            language=str(data.get("language", "")),
            symbols=[Symbol.from_dict(item) for item in data.get("symbols", [])],
            error=str(data.get("error", "")),
        )


@dataclass
class RepoIndex:
    root: Path
    generated_at: float
    files: dict[str, FileEntry] = field(default_factory=dict)
    approximate: bool = False

    # -- queries ----------------------------------------------------------
    def search(self, query: str, limit: int = 50) -> list[tuple[str, Symbol]]:
        needle = query.strip().lower()
        if not needle:
            return []
        exact: list[tuple[str, Symbol]] = []
        partial: list[tuple[str, Symbol]] = []
        for path, entry in sorted(self.files.items()):
            for symbol in entry.symbols:
                name = symbol.name.lower()
                if name == needle:
                    exact.append((path, symbol))
                elif needle in name:
                    partial.append((path, symbol))
        return (exact + partial)[:limit]

    def stats(self) -> dict[str, Any]:
        languages: dict[str, int] = {}
        total = 0
        for entry in self.files.values():
            languages[entry.language] = languages.get(entry.language, 0) + 1
            total += len(entry.symbols)
        return {
            "files": len(self.files),
            "symbols": total,
            "languages": languages,
            "generated_at": self.generated_at,
            "approximate": self.approximate,
        }

    def map_text(self, budget: int = MAP_BUDGET_DEFAULT) -> str:
        """A compact outline that fits in *budget* characters.

        Only top-level symbols appear here. Methods are what search is for; a
        map that lists every method stops being a map.
        """

        lines: list[str] = []
        used = 0
        skipped = 0
        ranked: list[tuple[int, str, list[str]]] = []
        for path, entry in self.files.items():
            names = [symbol.name for symbol in entry.symbols if symbol.kind != "method"]
            if not names:
                continue
            # Public names first: a map of private helpers is not an overview.
            public = [name for name in names if not name.rsplit(".", 1)[-1].startswith("_")]
            ordered = public + [name for name in names if name not in public]
            ranked.append((len(public), path, ordered))
        # Files carrying the most public surface come first; with a fixed budget
        # an alphabetical cut would stop inside the first directory.
        ranked.sort(key=lambda item: (-item[0], item[1]))

        for _, path, ordered in ranked:
            shown = ordered[:5]
            line = f"- {path}: {', '.join(shown)}"
            if len(ordered) > len(shown):
                line += f" (+{len(ordered) - len(shown)})"
            if used + len(line) + 1 > budget:
                skipped += 1
                continue
            lines.append(line)
            used += len(line) + 1
        if skipped:
            lines.append(f"- {skipped} more file(s) omitted by the size budget")
        return "\n".join(lines)

    # -- persistence ------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": INDEX_VERSION,
            "root": str(self.root),
            "generated_at": self.generated_at,
            "approximate": self.approximate,
            "files": {path: entry.to_dict() for path, entry in self.files.items()},
        }


# -- parsing ---------------------------------------------------------------
def _python_signature(node: ast.AST, name: str) -> str:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return name
    args = [arg.arg for arg in list(node.args.posonlyargs) + list(node.args.args)]
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    args.extend(arg.arg for arg in node.args.kwonlyargs)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {name}({', '.join(args)})"


def _parse_python(text: str) -> list[Symbol]:
    tree = ast.parse(text)
    symbols: list[Symbol] = []

    def visit(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                kind = "method" if prefix else "function"
                symbols.append(Symbol(name, kind, child.lineno, _python_signature(child, name)))
            elif isinstance(child, ast.ClassDef):
                name = f"{prefix}{child.name}"
                symbols.append(Symbol(name, "class", child.lineno, f"class {name}"))
                visit(child, prefix=f"{name}.")
            elif isinstance(child, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                # Module-level branches can hold imports or definitions; keep
                # looking, but do not descend into function bodies twice.
                visit(child, prefix=prefix)

    visit(tree)
    return symbols[:MAX_SYMBOLS_PER_FILE]


def _parse_script(text: str) -> list[Symbol]:
    symbols: list[Symbol] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if len(symbols) >= MAX_SYMBOLS_PER_FILE:
            break
        for pattern, kind in (
            (_JS_CLASS, "class"),
            (_JS_FUNCTION, "function"),
            (_JS_ARROW, "function"),
            (_JS_METHOD, "method"),
        ):
            match = pattern.match(line)
            if match:
                symbols.append(Symbol(match.group(1), kind, number, line.strip()[:120]))
                break
    return symbols


def _parse_file(path: Path, language: str) -> tuple[list[Symbol], str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], f"unreadable: {exc}"
    try:
        if language == "python":
            return _parse_python(text), ""
        return _parse_script(text), ""
    except SyntaxError as exc:
        return [], f"syntax error: line {exc.lineno}"
    except (ValueError, RecursionError) as exc:
        return [], f"parse failed: {exc}"


# -- building --------------------------------------------------------------
def index_path(root: Path | str) -> Path:
    return Path(root) / INDEX_RELATIVE


def _candidate_files(root: Path) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for current, directories, names in os.walk(root):
        directories[:] = sorted(name for name in directories if name not in SKIP_DIRS)
        for name in sorted(names):
            suffix = Path(name).suffix.lower()
            language = INDEXED_SUFFIXES.get(suffix)
            if language is None:
                continue
            path = Path(current) / name
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            found.append((path, language))
            if len(found) >= MAX_FILES:
                return found
    return found


def load_index(root: Path | str | None = None) -> RepoIndex | None:
    base = Path(root) if root else Path.cwd()
    path = index_path(base)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("version") != INDEX_VERSION:
        return None
    files: dict[str, FileEntry] = {}
    for name, item in data.get("files", {}).items():
        try:
            files[name] = FileEntry.from_dict(item)
        except (TypeError, ValueError):
            continue
    return RepoIndex(
        root=Path(data.get("root", str(base))),
        generated_at=float(data.get("generated_at", 0)),
        files=files,
        approximate=bool(data.get("approximate", False)),
    )


def refresh_index(root: Path | str, previous: RepoIndex | None = None) -> RepoIndex:
    """Walk the workspace and re-parse only the files that changed."""

    base = Path(root).resolve()
    previous = previous if previous is not None else load_index(base)
    cache = previous.files if previous else {}

    files: dict[str, FileEntry] = {}
    changed = previous is None
    approximate = False

    for path, language in _candidate_files(base):
        try:
            stat = path.stat()
        except OSError:
            continue
        relative = path.relative_to(base).as_posix()
        if language in {"javascript", "typescript"}:
            approximate = True

        cached = cache.get(relative)
        if cached and cached.mtime_ns == stat.st_mtime_ns and cached.size == stat.st_size \
                and cached.language == language:
            files[relative] = cached
            continue

        symbols, error = _parse_file(path, language)
        files[relative] = FileEntry(
            path=relative,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            language=language,
            symbols=symbols,
            error=error,
        )
        changed = True

    missing = set(cache) - set(files)
    if missing:
        changed = True

    index = RepoIndex(base, time.time(), files, approximate)
    if changed or not index_path(base).exists():
        _write(index)
    return index


def _write(index: RepoIndex) -> None:
    path = index_path(index.root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(index.to_dict(), ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        # An unwritable state directory must not break the caller.
        pass


# -- presentation ----------------------------------------------------------
def _setting(root: Path, key: str, default: Any) -> Any:
    """Read ``context.<key>`` from the user file, then from a trusted project file."""

    from .project_trust import is_project_trusted

    candidates: list[Path] = []
    try:
        candidates.append(Path.home() / ".mawoagent" / "settings.json")
    except (RuntimeError, KeyError, OSError):
        pass
    if is_project_trusted(root):
        candidates.append(root / ".mawoagent" / "settings.json")

    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        context = data.get("context") if isinstance(data, dict) else None
        if isinstance(context, dict) and key in context:
            return context[key]
    return default


def repo_map_enabled(root: Path | str | None = None) -> bool:
    if os.environ.get("MAWOAGENT_REPO_MAP", "").strip().lower() in {"0", "off", "false", "no"}:
        return False
    base = Path(root) if root else Path.cwd()
    try:
        return bool(_setting(base, "repo_map", True))
    except (OSError, ValueError):
        return True


_MAP_CACHE: dict[str, tuple[float, RepoIndex]] = {}
_MAP_TTL_SECONDS = 30.0


def _cached_index(base: Path) -> RepoIndex:
    """Reuse a recent index for prompt building.

    A new agent is created often; walking the workspace for each one is wasted
    work when nothing has changed in the last half minute. ``/symbols`` always
    calls ``refresh_index`` directly, so an explicit query is never stale.
    """

    key = str(base)
    now = time.time()
    hit = _MAP_CACHE.get(key)
    if hit and now - hit[0] < _MAP_TTL_SECONDS:
        return hit[1]
    index = refresh_index(base, previous=hit[1] if hit else None)
    _MAP_CACHE[key] = (now, index)
    return index


def build_repo_map_section(root: Path | str | None = None, budget: int | None = None) -> str:
    """The block injected into the system prompt, or an empty string when off."""

    base = Path(root) if root else Path.cwd()
    if not repo_map_enabled(base):
        return ""
    if budget is None:
        try:
            configured = _setting(base, "repo_map_budget", MAP_BUDGET_DEFAULT)
            budget = int(configured)
        except (TypeError, ValueError, OSError):
            budget = MAP_BUDGET_DEFAULT
    try:
        index = _cached_index(base)
    except OSError:
        return ""
    body = index.map_text(budget)
    if not body.strip():
        return ""
    listed = sum(1 for line in body.splitlines() if line.startswith("- ") and "omitted" not in line)
    total = len(index.files)
    if listed < total:
        scope = f"{listed} of {total} files"
    elif total == 1:
        scope = "1 file"
    else:
        scope = f"{total} files"
    note = ", JavaScript/TypeScript entries are pattern-based" if index.approximate else ""
    header = f"## Repository map ({scope}{note})"
    return f"{header}\n{body}"


def format_stats(index: RepoIndex) -> str:
    stats = index.stats()
    languages = ", ".join(f"{name}: {count}" for name, count in sorted(stats["languages"].items()))
    return (
        f"{stats['files']} file(s), {stats['symbols']} symbol(s)"
        + (f" — {languages}" if languages else "")
        + (" (approximate for JS/TS)" if stats["approximate"] else "")
    )


def format_search(results: list[tuple[str, Symbol]]) -> str:
    if not results:
        return "No matching symbols."
    lines = []
    for path, symbol in results:
        signature = symbol.signature
        if len(signature) > 100:
            signature = signature[:97] + "..."
        detail = f"  {signature}" if signature else ""
        lines.append(f"{path}:{symbol.line}  {symbol.kind} {symbol.name}{detail}")
    return "\n".join(lines)
