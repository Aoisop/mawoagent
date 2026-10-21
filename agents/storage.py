"""Crash-safe state files and cooperative locks for local runtime writers."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_held = threading.local()

# Concurrent creators of the same sidecar can briefly see EACCES on Windows.
# Retrying keeps the lock semantics unchanged while avoiding a spurious crash.
_LOCK_OPEN_TIMEOUT_S = 5.0


def _open_lock_file(path: Path):
    target = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + _LOCK_OPEN_TIMEOUT_S
    while True:
        try:
            return target.open("a+b")
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


@contextmanager
def state_lock(path: Path):
    """Lock the entire read/modify/write operation, including across processes.

    Lock files are stable sidecars: never unlink them while another writer may
    hold or wait for the lock. All runtime writers must use the same path.
    """
    path = Path(path).resolve()
    key = str(path)
    with _locks_guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_held, "paths", None)
        if held is None:
            held = _held.paths = set()
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with _open_lock_file(path) as stream:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_write_text(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if path.exists() and hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2,
                                     sort_keys=True, default=str) + "\n")


def append_jsonl(path: Path, value) -> None:
    with state_lock(path):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
