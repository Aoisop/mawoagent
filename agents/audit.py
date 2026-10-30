"""Read-only audit trail for tool calls (iteration I6 / T18).

Every tool call emits two records: one when it is about to run and one when it
finishes. The pair matters — a call that started and never ended is exactly the
shape of a crash, and a single "finished" record cannot express it.

Rules that keep this an audit log rather than a plugin system:

* Hooks are read-only. They receive a frozen event and their return value is
  ignored, so an observer cannot change what a tool does.
* Nothing is imported from the workspace. Subscribers are registered by code
  that ships with the runtime; a repository cannot install an audit hook by
  dropping a file in it.
* Arguments are summarised and redacted. An audit log that records a token or a
  full file body becomes a liability of its own.
* Writing never raises. An unwritable log must not turn a working tool call into
  a failure.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .policy import redact_secrets
from .storage import state_lock

REDACTED = "[redacted]"
SECRET_KEYS = ("token", "secret", "password", "passwd", "api_key", "apikey", "authorization", "cookie")
MAX_ARGUMENT_CHARS = 240
MAX_LOG_BYTES = 5 * 1024 * 1024
ROTATED_GENERATIONS = 3


@dataclass(frozen=True)
class AuditEvent:
    call_id: str
    timestamp: str
    phase: str            # "start" | "end"
    session_id: str
    tool: str
    arguments: str
    permission: str = ""  # allow | confirm | deny
    outcome: str = ""     # ok | error | denied | stale | invalid | unavailable | cancelled
    duration_ms: int = 0
    error: str = ""
    tool_call_number: int = 0


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in SECRET_KEYS):
                result[key] = REDACTED
            else:
                result[key] = _redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        # Key names miss the common case: a token inside a shell command or a
        # URL is still a token. policy.redact_secrets matches it by shape.
        return redact_secrets(value)
    return value


def summarize_arguments(arguments: Any, *, limit: int = MAX_ARGUMENT_CHARS) -> str:
    """A bounded, redacted rendering: never the whole file body."""

    redacted = _redact(arguments)
    try:
        text = json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(redacted)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


class AuditLog:
    """Append-only JSONL, rotated by size, with in-process subscribers."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._subscribers: list[Callable[[AuditEvent], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, callback: Callable[[AuditEvent], None]) -> Callable[[], None]:
        """Register a read-only observer; returns an unsubscribe function."""

        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def record(self, event: AuditEvent) -> None:
        self._notify(event)
        if self.path is not None:
            self._append(event)

    def _notify(self, event: AuditEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - an observer cannot break a tool call
                continue

    def _append(self, event: AuditEvent) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(asdict(event), ensure_ascii=False) + "\n"
            # The log is a record of what happened; two processes appending at
            # once must not interleave half a line.
            with state_lock(self.path):
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(line)
        except OSError:
            # An audit log that cannot be written must not fail the call it
            # was describing.
            pass

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.stat().st_size < MAX_LOG_BYTES:
                return
            # Keep a few generations: dropping the only previous file made the
            # history unreachable exactly when it was most interesting.
            for index in range(ROTATED_GENERATIONS - 1, 0, -1):
                older = self.path.with_name(f"{self.path.name}.{index}")
                newer = self.path.with_name(f"{self.path.name}.{index + 1}")
                if older.exists():
                    os.replace(older, newer)
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        except OSError:
            pass


def audit_path(root: Path | str) -> Path:
    return Path(root) / ".mawoagent" / "audit.jsonl"


class CallRecord:
    """Pairs the start and end events for one tool call."""

    def __init__(self, log: AuditLog, *, session_id: str, tool: str, arguments: Any,
                 tool_call_number: int = 0, root: Path | str | None = None):
        self.log = log
        self.call_id = uuid.uuid4().hex[:12]
        self.session_id = session_id
        self.tool = tool
        self.permission = ""
        self.outcome = "unfinished"
        self.error = ""
        self.tool_call_number = tool_call_number
        self._arguments = summarize_arguments(arguments)
        self._started = time.monotonic()
        self._root = root

    def start(self, permission: str = "") -> None:
        self.permission = permission
        self.log.record(AuditEvent(
            call_id=self.call_id, timestamp=_now(), phase="start", session_id=self.session_id,
            tool=self.tool, arguments=self._arguments, permission=permission,
            tool_call_number=self.tool_call_number,
        ))

    def finish(self) -> None:
        self.log.record(AuditEvent(
            call_id=self.call_id, timestamp=_now(), phase="end", session_id=self.session_id,
            tool=self.tool, arguments=self._arguments, permission=self.permission,
            outcome=self.outcome, duration_ms=int((time.monotonic() - self._started) * 1000),
            error=self.error[:400], tool_call_number=self.tool_call_number,
        ))

    def fail(self, outcome: str, error: str = "") -> None:
        """Mark the outcome before returning; the caller still sees the same text."""

        self.outcome = outcome
        if error:
            self.error = error


def read_events(root: Path | str, limit: int = 50) -> list[dict]:
    """The newest events first, walking back through rotated generations."""

    path = audit_path(root)
    events: list[dict] = []
    candidates = [path] + [path.with_name(f"{path.name}.{index}") for index in range(1, ROTATED_GENERATIONS + 1)]
    for candidate in candidates:
        try:
            with candidate.open("r", encoding="utf-8") as stream:
                lines = stream.readlines()[-max(limit * 2, 200):]
        except OSError:
            continue
        for line in reversed(lines):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(events) >= limit:
                return events
    return events
