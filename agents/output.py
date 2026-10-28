"""Structured CLI output and exit codes (iteration I7 / T19).

Two things a script needs from an agent CLI: a stable exit code and a stream it
can parse. Both are easy to get wrong in ways that only show up in automation —
a zero exit on a blocked budget, or a progress line in the middle of JSONL.

The rules:

* Exit codes are distinct per outcome: success, error, usage, budget exhausted,
  cancelled. ``2`` is reserved for usage because that is what argparse already
  returns.
* In a structured mode stdout carries only records. Human output moves to
  stderr; suppressing it entirely would make a failed run harder to diagnose.
* ``json`` emits one document, ``jsonl`` emits one object per line. The last
  record is always ``result``, so a reader can stop at the first error without
  guessing whether the stream was truncated.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_BUDGET = 3
EXIT_CANCELLED = 130

FORMATS = ("text", "json", "jsonl")
RESULT_SCHEMA_VERSION = 1


def classify_error(exc: BaseException) -> tuple[str, int]:
    """Map a failure to a status and an exit code."""

    if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
        return "cancelled", EXIT_CANCELLED
    text = str(exc)
    if "Model request blocked" in text:
        return "budget_exceeded", EXIT_BUDGET
    return "error", EXIT_ERROR


class ResultWriter:
    """Collects events and writes them in the configured format."""

    def __init__(self, fmt: str = "text", stream=None):
        if fmt not in FORMATS:
            raise ValueError(f"unknown output format: {fmt}")
        self.format = fmt
        self.stream = stream or sys.stdout
        self.events: list[dict[str, Any]] = []
        self.started_at = time.monotonic()
        self._wrote_result = False

    # -- writing -----------------------------------------------------------
    def _write(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def event(self, kind: str, **fields: Any) -> None:
        """Record a progress event; streamed immediately in jsonl mode."""

        record = {"type": kind, **fields}
        self.events.append(record)
        if self.format == "jsonl":
            self._write(json.dumps(record, ensure_ascii=False))

    # -- lifecycle ---------------------------------------------------------
    def start(self, *, prompt: str, model: str, session_id: str = "") -> None:
        if self.format != "text":
            self.event("start", prompt=prompt, model=model, session_id=session_id)

    def finish(
        self,
        *,
        status: str,
        exit_code: int,
        text: str = "",
        usage: dict[str, Any] | None = None,
        error: str = "",
    ) -> int:
        """Write the terminal record and return the exit code."""

        if self.format == "text":
            return exit_code
        result = {
            "type": "result",
            "status": status,
            "exit_code": exit_code,
            "text": text,
            "usage": usage or {},
            "error": error,
            "duration_ms": int((time.monotonic() - self.started_at) * 1000),
            "schema_version": RESULT_SCHEMA_VERSION,
        }
        if self.format == "jsonl":
            self._write(json.dumps(result, ensure_ascii=False))
        else:
            document = {"schema_version": RESULT_SCHEMA_VERSION, **result}
            document["events"] = self.events
            self._write(json.dumps(document, ensure_ascii=False, indent=2))
        self._wrote_result = True
        return exit_code

    def fail(self, exc: BaseException, *, usage: dict[str, Any] | None = None) -> int:
        status, code = classify_error(exc)
        return self.finish(status=status, exit_code=code, error=f"{type(exc).__name__}: {exc}", usage=usage)
