"""Per-turn file snapshots and conflict-safe undo (iteration I2 / T06 and T07).

A snapshot is taken **per user turn**, not per tool call: the first time a file
is about to change during a turn its previous bytes are kept, and at the end of
the turn the resulting bytes are recorded too. That pair is what ``/diff`` shows
and what ``/undo`` restores.

Three boundaries matter:

* Only writes made through the built-in file tools are captured. A command run
  through ``run_shell`` changes files behind the runtime's back and is reported
  as a conflict rather than silently reverted.
* Snapshots never include the runtime's own state, version control internals,
  dependency directories, oversized files or binary blobs.
* Undo is safe by construction: an entry is restored only when the file still
  holds exactly what the turn left there. Anything edited afterwards is left
  alone and reported, because the user's later work outranks the rollback.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

STATE_DIR_NAME = ".mawoagent"
SNAPSHOT_DIR_NAME = "snapshots"

EXCLUDED_PARTS = frozenset(
    {
        STATE_DIR_NAME,
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TURNS_PER_SESSION = 30
MAX_TOTAL_BYTES = 256 * 1024 * 1024

TEXT_SUFFIXES = frozenset(
    {".py", ".md", ".txt", ".json", ".toml", ".yml", ".yaml", ".js", ".mjs", ".cjs",
     ".ts", ".tsx", ".jsx", ".css", ".html", ".htm", ".sh", ".ps1", ".cfg", ".ini",
     ".lock", ".csv", ".sql", ".rs", ".go", ".java", ".c", ".h", ".cpp"}
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshots_root(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / STATE_DIR_NAME / SNAPSHOT_DIR_NAME


def _excluded(relative: Path) -> bool:
    return any(part in EXCLUDED_PARTS for part in relative.parts)


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _looks_textual(path: Path, data: bytes) -> bool:
    if _is_binary(data):
        return False
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    try:
        data[:8192].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


@dataclass
class Entry:
    """One file touched during a turn, with the bytes either side of it."""

    path: str
    existed_before: bool
    before_sha: str | None = None
    after_sha: str | None = None
    size: int = 0
    mode: int | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Turn:
    turn_id: str
    session_id: str
    index: int
    started_at: float
    finished_at: float | None
    status: str
    entries: list[Entry] = field(default_factory=list)
    directory: Path | None = None

    def to_meta(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "index": self.index,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_meta(cls, meta: dict, directory: Path) -> "Turn":
        return cls(
            turn_id=meta["turn_id"],
            session_id=meta["session_id"],
            index=int(meta.get("index", 0)),
            started_at=float(meta.get("started_at", 0)),
            finished_at=meta.get("finished_at"),
            status=meta.get("status", "unknown"),
            entries=[Entry(**item) for item in meta.get("entries", [])],
            directory=directory,
        )


class TurnRecorder:
    """Collects the before-image of every file changed during one turn.

    The turn directory is created lazily: a turn that never changes a file
    costs no disk traffic at all, which matters because most turns are
    read-only and every chat call opens one.
    """

    def __init__(self, root: Path, session_id: str, index: int, directory: Path):
        self.root = Path(root)
        self.session_id = session_id
        self.index = index
        self.directory = directory
        self.turn_id = directory.name
        self.started_at = time.time()
        self.entries: dict[str, Entry] = {}
        self._finished = False
        self._created = False
        self._result: Turn | None = None
        self.blob_dir = directory / "blobs"

    def _ensure_directory(self) -> None:
        if self._created:
            return
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self._created = True
        self._write_meta("open")

    # -- recording --------------------------------------------------------
    def record(self, path: Path | str) -> None:
        """Keep the current bytes of *path* once, before the first change."""

        if self._finished:
            return
        try:
            target = Path(path)
            if not target.is_absolute():
                target = self.root / target
            relative = target.resolve().relative_to(self.root.resolve())
        except (ValueError, OSError):
            return
        if _excluded(relative):
            return

        key = relative.as_posix()
        if key in self.entries:
            return

        entry = Entry(path=key, existed_before=target.exists())
        try:
            if entry.existed_before:
                data = target.read_bytes()
                entry.size = len(data)
                entry.mode = stat_mode(target)
                if len(data) > MAX_FILE_BYTES:
                    entry.note = "skipped: larger than the snapshot limit"
                else:
                    entry.before_sha = _sha(data)
                    self._ensure_directory()
                    self._store_blob(entry.before_sha, data)
            self.entries[key] = entry
        except OSError as exc:
            self.entries[key] = Entry(path=key, existed_before=False, note=f"skipped: {exc}")
            return

        self._ensure_directory()
        self._write_meta("open")

    def _store_blob(self, digest: str, data: bytes) -> None:
        blob = self.blob_dir / digest
        if not blob.exists():
            blob.write_bytes(data)

    # -- finishing --------------------------------------------------------
    def finish(self, status: str = "complete") -> Turn:
        if self._finished:
            return self._result
        self._finished = True

        if not self._created:
            # Nothing was written this turn, so there is nothing to keep.
            self._result = Turn(
                turn_id=self.turn_id,
                session_id=self.session_id,
                index=self.index,
                started_at=self.started_at,
                finished_at=time.time(),
                status="empty",
                entries=[],
                directory=None,
            )
            return self._result

        for entry in self.entries.values():
            if entry.note.startswith("skipped"):
                continue
            target = self.root / entry.path
            try:
                if not target.exists():
                    entry.after_sha = None
                    continue
                data = target.read_bytes()
                entry.size = max(entry.size, len(data))
                entry.after_sha = _sha(data)
                if len(data) <= MAX_FILE_BYTES:
                    self._store_blob(entry.after_sha, data)
            except OSError as exc:
                entry.note = f"unreadable after the turn: {exc}"

        self._write_meta(status, finished_at=time.time())
        prune_turns(self.session_id, self.root)
        self._result = self._turn()
        return self._result

    def _turn(self) -> Turn:
        meta = json.loads((self.directory / "meta.json").read_text(encoding="utf-8"))
        return Turn.from_meta(meta, self.directory)

    def _write_meta(self, status: str, finished_at: float | None = None) -> None:
        turn = Turn(
            turn_id=self.turn_id,
            session_id=self.session_id,
            index=self.index,
            started_at=self.started_at,
            finished_at=finished_at,
            status=status,
            entries=list(self.entries.values()),
            directory=self.directory,
        )
        meta = turn.to_meta()
        temporary = self.directory / "meta.json.tmp"
        temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.directory / "meta.json")


def stat_mode(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return None


_current_recorder: ContextVar[TurnRecorder | None] = ContextVar("mawoagent_snapshot", default=None)


def current_recorder() -> TurnRecorder | None:
    return _current_recorder.get()


def record_before_change(path: Path | str) -> None:
    """Called by the file tools immediately before a write is committed."""

    recorder = _current_recorder.get()
    if recorder is not None:
        recorder.record(path)


@contextmanager
def snapshot_scope(recorder: TurnRecorder | None) -> Iterator[TurnRecorder | None]:
    token: Token = _current_recorder.set(recorder)
    try:
        yield recorder
    finally:
        _current_recorder.reset(token)


def begin_turn(session_id: str, root: Path | str, enabled: bool = True) -> tuple[TurnRecorder | None, Token | None]:
    """Start a turn and bind it for every tool call that follows.

    An existing turn still marked ``open`` belongs to a process that died; it is
    reclassified before a new one starts so the state stays readable.
    """

    if not enabled:
        return None, None
    directory = snapshots_root(root) / session_id
    # No directory is created here on purpose: a read-only turn should leave no
    # trace on disk. mark_interrupted and _next_index both tolerate absence.
    mark_interrupted(session_id, root)

    index = _next_index(directory)
    turn_dir = directory / f"{index:04d}-{int(time.time() * 1000)}"
    recorder = TurnRecorder(Path(root), session_id, index, turn_dir)
    token = _current_recorder.set(recorder)
    return recorder, token


def finish_turn(recorder: TurnRecorder | None, token: Token | None, status: str = "complete") -> Turn | None:
    try:
        if recorder is None:
            return None
        return recorder.finish(status)
    finally:
        # Finishing a turn that was already finished must not try to reset the
        # same context token twice; that raises instead of being a no-op.
        if token is not None and _current_recorder.get() is recorder:
            _current_recorder.reset(token)


def _next_index(directory: Path) -> int:
    highest = 0
    for child in directory.iterdir() if directory.exists() else []:
        if not child.is_dir():
            continue
        try:
            highest = max(highest, int(child.name.split("-", 1)[0]))
        except ValueError:
            continue
    return highest + 1


def mark_interrupted(session_id: str, root: Path | str) -> list[str]:
    """Reclassify turns left ``open`` by a process that is no longer running."""

    repaired: list[str] = []
    for turn in list_turns(session_id, root):
        if turn.status != "open":
            continue
        meta_path = turn.directory / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["status"] = "interrupted"
            meta["finished_at"] = meta.get("finished_at") or time.time()
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            repaired.append(turn.turn_id)
        except (OSError, ValueError):
            continue
    return repaired


def list_turns(session_id: str, root: Path | str) -> list[Turn]:
    directory = snapshots_root(root) / session_id
    if not directory.is_dir():
        return []
    turns: list[Turn] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        try:
            meta = json.loads((child / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        turns.append(Turn.from_meta(meta, child))
    return sorted(turns, key=lambda item: item.index)


def latest_turn(session_id: str, root: Path | str, skip: int = 0) -> Turn | None:
    turns = [turn for turn in list_turns(session_id, root) if turn.status != "open"]
    if skip < 0 or skip >= len(turns):
        return None
    return turns[-1 - skip]


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _blob(turn: Turn, digest: str | None) -> bytes | None:
    if not digest or turn.directory is None:
        return None
    # The digest is read back from meta.json. Anything that is not a sha256 is
    # either corruption or an attempt to walk out of the blob directory.
    if not _DIGEST_RE.fullmatch(digest):
        return None
    path = turn.directory / "blobs" / digest
    try:
        return path.read_bytes()
    except OSError:
        return None


def build_diff(turn: Turn) -> str:
    """Unified diff of the turn, one block per changed file."""

    chunks: list[str] = []
    for entry in turn.entries:
        if entry.note.startswith("skipped"):
            chunks.append(f"# {entry.path}: {entry.note}\n")
            continue
        if entry.before_sha == entry.after_sha:
            continue
        before = _blob(turn, entry.before_sha) or b""
        after = _blob(turn, entry.after_sha) or b""
        target = Path(entry.path)
        if not (_looks_textual(target, before) and _looks_textual(target, after)):
            chunks.append(f"# {entry.path}: binary or non-text content changed\n")
            continue
        old = before.decode("utf-8", errors="replace").splitlines(keepends=True)
        new = after.decode("utf-8", errors="replace").splitlines(keepends=True)
        chunks.extend(
            difflib.unified_diff(
                old,
                new,
                fromfile=f"a/{entry.path}" if entry.existed_before else "/dev/null",
                tofile=f"b/{entry.path}" if entry.after_sha else "/dev/null",
                lineterm="\n",
            )
        )
    return "".join(chunks)


def undo_turn(turn: Turn, root: Path | str) -> dict:
    """Restore the turn's before-images where the files are still untouched."""

    workspace = Path(root)
    workspace_root = workspace.resolve()
    restored: list[str] = []
    removed: list[str] = []
    conflicts: list[str] = []
    skipped: list[str] = []

    for entry in turn.entries:
        if entry.note.startswith("skipped"):
            skipped.append(f"{entry.path}: {entry.note}")
            continue

        # entry.path comes from meta.json on disk. A path that leaves the
        # workspace is not a rollback, it is a write primitive.
        try:
            target = (workspace / entry.path).resolve()
            target.relative_to(workspace_root)
        except (ValueError, OSError):
            skipped.append(f"{entry.path}: outside the workspace")
            continue
        try:
            current = target.read_bytes() if target.exists() else None
        except OSError as exc:
            skipped.append(f"{entry.path}: {exc}")
            continue

        # The user's later edits outrank the rollback: only a file that still
        # holds exactly what the turn left behind is safe to restore.
        if (None if current is None else _sha(current)) != entry.after_sha:
            conflicts.append(entry.path)
            continue

        if entry.existed_before:
            data = _blob(turn, entry.before_sha)
            if data is None:
                skipped.append(f"{entry.path}: the original content is no longer stored")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.undo.tmp")
            temporary.write_bytes(data)
            if entry.mode is not None:
                try:
                    os.chmod(temporary, entry.mode)
                except OSError:
                    pass
            os.replace(temporary, target)
            restored.append(entry.path)
        else:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                skipped.append(f"{entry.path}: {exc}")
                continue
            removed.append(entry.path)

    return {"restored": restored, "removed": removed, "conflicts": conflicts, "skipped": skipped}


def prune_turns(session_id: str, root: Path | str) -> None:
    """Keep the newest turns within both the count and the size budget."""

    turns = list_turns(session_id, root)
    if len(turns) > MAX_TURNS_PER_SESSION:
        for turn in turns[:-MAX_TURNS_PER_SESSION]:
            if turn.directory is not None:
                shutil.rmtree(turn.directory, ignore_errors=True)
    turns = list_turns(session_id, root)

    total = sum(_directory_size(turn.directory) for turn in turns if turn.directory is not None)
    for turn in turns:
        if total <= MAX_TOTAL_BYTES or turn.directory is None:
            break
        total -= _directory_size(turn.directory)
        shutil.rmtree(turn.directory, ignore_errors=True)


def _directory_size(directory: Path | None) -> int:
    if directory is None or not directory.is_dir():
        return 0
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def describe_turn(turn: Turn) -> str:
    changed = [
        entry for entry in turn.entries
        if entry.before_sha != entry.after_sha and not entry.note.startswith("skipped")
    ]
    if not changed:
        return f"turn {turn.index} ({turn.status}): no file changes recorded"
    lines = [f"turn {turn.index} ({turn.status}): {len(changed)} file(s) changed"]
    for entry in changed:
        marker = "+" if not entry.existed_before else ("-" if entry.after_sha is None else "~")
        lines.append(f"  {marker} {entry.path}")
    return "\n".join(lines)
