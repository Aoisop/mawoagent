"""Execution backends for shell commands (iteration I4 / T11).

Everything that runs a command goes through one interface, so a future isolated
backend can be selected without touching the tools that call it. The local
backend reproduces the previous behaviour exactly: same shell, same session
handling, same process-tree cleanup on timeout or cancellation.

A backend returns data; formatting the human-readable tool result stays in
``tools``. That split is what makes the Docker backend (T12) purely additive.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ExecResult:
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0
    backend: str = "local"
    error: str = ""


class ExecutionBackend:
    """Run one shell command; never raise for ordinary failures."""

    name = "base"

    async def run(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        raise NotImplementedError


class LocalBackend(ExecutionBackend):
    """Runs the command directly on the host, as the runtime always has."""

    name = "local"

    async def run(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        started = time.monotonic()
        process = None
        output_task = None
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env={**os.environ, **env} if env else None,
                start_new_session=os.name != "nt",
            )
            output_task = asyncio.create_task(process.communicate())
            stdout_bytes, stderr_bytes = await asyncio.wait_for(asyncio.shield(output_task), timeout_s)
            return ExecResult(
                returncode=process.returncode,
                stdout=stdout_bytes.decode(errors="replace"),
                stderr=stderr_bytes.decode(errors="replace"),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except asyncio.TimeoutError:
            return ExecResult(
                returncode=None,
                timed_out=True,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported through the result
            return ExecResult(
                returncode=None,
                error=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            if process is not None and output_task is not None and not output_task.done():
                await _terminate(process, output_task)


async def _terminate(process: Any, output_task: asyncio.Task) -> None:
    """Kill the whole process tree and drain the pipes."""

    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    await output_task
    await process.wait()


_BACKENDS: dict[str, ExecutionBackend] = {}


def get_backend(kind: str | None = None) -> ExecutionBackend:
    """Return the configured backend; ``MAWOAGENT_SANDBOX`` selects it."""

    name = (kind or os.environ.get("MAWOAGENT_SANDBOX") or "local").strip().lower()
    backend = _BACKENDS.get(name)
    if backend is None:
        if name == "local":
            backend = LocalBackend()
        else:
            raise ValueError(f"unknown execution backend: {name}")
        _BACKENDS[name] = backend
    return backend
