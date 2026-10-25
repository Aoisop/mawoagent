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
from dataclasses import dataclass, field
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
    changed: list[str] = field(default_factory=list)


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


class DockerBackend(ExecutionBackend):
    """Runs the command in a container with the workspace as the only mount.

    Deliberate defaults: no network, a memory/cpu/pid ceiling, a read-write mount
    of exactly one directory, and ``no-new-privileges``. The container is named
    so a timeout can remove it explicitly — killing the docker client alone would
    leave the container running.

    This backend has not been exercised against a real Docker daemon in the
    project's tests, because CI runners and the development machine do not have
    one. The argument construction, timeout handling and cleanup are covered;
    the isolation itself is not yet verified.
    """

    name = "docker"
    image = "mawoagent/sandbox:0.2"
    workspace_mount = "/work"

    def __init__(
        self,
        *,
        image: str | None = None,
        docker: list[str] | None = None,
        network: str = "none",
        memory: str = "2g",
        cpus: str = "2",
        pids_limit: int = 256,
        user: str | None = None,
    ):
        self.image = image or type(self).image
        # A list so tests can inject a fake client without a shell shim.
        self.docker = list(docker) if docker else ["docker"]
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.user = user

    def docker_args(
        self,
        command: str,
        *,
        container: str,
        workspace: Path,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """The argv for one run. Pure, so it can be asserted without a daemon."""

        args = [
            *self.docker, "run", "--rm", "-i",
            "--name", container,
            "--network", self.network,
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids_limit),
            "--security-opt", "no-new-privileges",
            "-v", f"{workspace}:{self.workspace_mount}",
            "-w", self.workspace_mount,
        ]
        if self.user:
            args += ["--user", self.user]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        args += [self.image, "sh", "-lc", command]
        return args

    async def run(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        import uuid

        started = time.monotonic()
        workspace = Path(cwd or Path.cwd()).resolve()
        if not workspace.is_dir():
            return ExecResult(returncode=None, error=f"not a directory: {workspace}", backend=self.name)

        container = f"mawoagent-{uuid.uuid4().hex[:10]}"
        process = None
        output_task = None
        needs_cleanup = False
        try:
            process = await asyncio.create_subprocess_exec(
                *self.docker_args(command, container=container, workspace=workspace, env=env),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            output_task = asyncio.create_task(process.communicate())
            stdout_bytes, stderr_bytes = await asyncio.wait_for(asyncio.shield(output_task), timeout_s)
            return ExecResult(
                returncode=process.returncode,
                stdout=stdout_bytes.decode(errors="replace"),
                stderr=stderr_bytes.decode(errors="replace"),
                duration_ms=int((time.monotonic() - started) * 1000),
                backend=self.name,
            )
        except asyncio.TimeoutError:
            needs_cleanup = True
            return ExecResult(
                returncode=None,
                timed_out=True,
                duration_ms=int((time.monotonic() - started) * 1000),
                backend=self.name,
            )
        except asyncio.CancelledError:
            needs_cleanup = True
            raise
        except Exception as exc:  # noqa: BLE001 - reported through the result
            return ExecResult(
                returncode=None,
                error=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
                backend=self.name,
            )
        finally:
            try:
                if process is not None and output_task is not None and not output_task.done():
                    await _terminate(process, output_task)
            finally:
                # Killing the client does not stop the container; on timeout or
                # cancellation it has to be removed explicitly.
                if needs_cleanup:
                    await self.remove_container(container)

    async def remove_container(self, container: str) -> None:
        """Remove a container that outlived its client; never raises."""

        try:
            remover = await asyncio.create_subprocess_exec(
                *self.docker, "rm", "-f", container,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(remover.wait(), 10)
        except (OSError, asyncio.TimeoutError, asyncio.CancelledError):
            pass


_COPY_IGNORE = (".git", ".hg", ".mawoagent", "node_modules", "__pycache__", ".venv", "venv", "dist", "build")


def _relative_files(root: Path) -> dict[str, str]:
    """Map each file to a digest, ignoring paths that are never copied."""

    import hashlib

    digests: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file() or any(part in _COPY_IGNORE for part in path.relative_to(root).parts):
            continue
        digests[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changed = [f"A {path}" for path in sorted(set(after) - set(before))]
    changed += [f"D {path}" for path in sorted(set(before) - set(after))]
    changed += [f"M {path}" for path in sorted(set(before) & set(after)) if before[path] != after[path]]
    return changed


class CopyingBackend(ExecutionBackend):
    """Runs the command against a throwaway copy of the workspace.

    The real workspace is never touched; ``changed`` reports what the command
    would have done. This is the "independent copy" mode T11 asks for: it makes
    a command previewable instead of immediately destructive.
    """

    name = "copy"

    def __init__(self, inner: ExecutionBackend):
        self.inner = inner

    async def run(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        import shutil
        import tempfile

        source = Path(cwd or Path.cwd()).resolve()
        if not source.is_dir():
            return ExecResult(returncode=None, error=f"not a directory: {source}", backend=self.name)

        before = _relative_files(source)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "workspace"
            shutil.copytree(source, target, ignore=shutil.ignore_patterns(*_COPY_IGNORE))
            result = await self.inner.run(command, timeout_s=timeout_s, cwd=target, env=env)
            result.changed = _changed_files(before, _relative_files(target))
            result.backend = self.name
            return result


_BACKENDS: dict[str, ExecutionBackend] = {}


def get_backend(kind: str | None = None) -> ExecutionBackend:
    """Return the configured backend.

    ``MAWOAGENT_SANDBOX`` picks local or docker; ``MAWOAGENT_SANDBOX_MODE=copy``
    wraps it so commands run against a copy and only report their effect.
    """

    name = (kind or os.environ.get("MAWOAGENT_SANDBOX") or "local").strip().lower()
    mode = (os.environ.get("MAWOAGENT_SANDBOX_MODE") or "direct").strip().lower()
    if mode not in {"direct", "copy"}:
        raise ValueError(f"unknown sandbox mode: {mode}")

    key = f"{name}:{mode}"
    backend = _BACKENDS.get(key)
    if backend is None:
        if name == "local":
            base: ExecutionBackend = LocalBackend()
        elif name == "docker":
            base = DockerBackend(
                image=os.environ.get("MAWOAGENT_SANDBOX_IMAGE", DockerBackend.image),
                user=os.environ.get("MAWOAGENT_SANDBOX_USER") or None,
            )
        else:
            raise ValueError(f"unknown execution backend: {name}")
        backend = CopyingBackend(base) if mode == "copy" else base
        _BACKENDS[key] = backend
    return backend
