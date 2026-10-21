"""Instance-level workspace context and file access policy (iteration I1 / T03).

The runtime used to resolve every tool path against ``Path.cwd()`` and to take
permission rules from the current directory. That makes the accessible range an
implicit property of the process rather than of the running session. This module
turns it into an explicit, per-instance value: a workspace root plus the policy
that says what may be read or written outside it.

Two boundaries are deliberate:

* This governs **file tools only**. ``run_shell`` can reach anything the OS
  allows, so the policy must never be described as a sandbox.
* Paths are classified on their **real** form, so ``..``, symbolic links and
  NTFS junctions cannot be used to step outside the root while looking local.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

Scope = Literal["workspace", "runtime_state", "outside"]

# Files that define the permissions themselves, or that are loaded verbatim into
# a later system prompt. Editing any of them through a generic file tool would
# let a session widen its own policy or persist injected instructions across
# every following session, so writes need an explicit confirmation even though
# they sit inside the workspace.
PROTECTED_RELATIVE_PATHS = (
    ".mawoagent/settings.json",
    ".mcp.json",
    # Read into the system prompt, the Skill catalogue, or sub-agent prompts.
    ".mawoagent/rules",
    ".mawoagent/skills",
    ".mawoagent/agents",
)


def _is_unc(value: str) -> bool:
    return value.startswith("\\\\") or value.startswith("//")


def _key(value: str) -> str:
    """Comparison form: absolute and case-normalised for the current platform."""

    return os.path.normcase(os.path.abspath(value))


def _within(child: str, parent: str) -> bool:
    """Containment on comparison keys, safe across drives and UNC shares."""

    if not child or not parent:
        return False
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        # Different drives, or a UNC share compared with a local path.
        return False


def _relative_key(child: str, parent: str) -> str | None:
    try:
        return os.path.relpath(child, parent).replace(os.sep, "/")
    except ValueError:
        return None


@dataclass(frozen=True)
class ResolvedPath:
    """A requested path in the forms the policy and the file tools each need."""

    raw: str
    absolute: str
    lexical: str
    real: str
    is_unc: bool
    relative: str | None


@dataclass(frozen=True)
class AccessDecision:
    scope: Scope
    resolved: ResolvedPath
    reason: str
    protected: bool = False

    @property
    def inside(self) -> bool:
        return self.scope != "outside"

    @property
    def display(self) -> str:
        return self.resolved.absolute


@dataclass(frozen=True)
class WorkspaceContext:
    """The root a session may treat as its project, plus its file policy."""

    root: Path
    workspace_only_reads: bool = False
    workspace_only_writes: bool = True
    protected_paths: tuple[str, ...] = PROTECTED_RELATIVE_PATHS
    source: str = "default"
    state_name: str = ".mawoagent"

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(os.path.abspath(str(self.root))))
        object.__setattr__(
            self,
            "protected_paths",
            tuple(item.replace("\\", "/").strip("/") for item in self.protected_paths),
        )

    @property
    def root_real(self) -> str:
        return _key(os.path.realpath(str(self.root)))

    @property
    def state_dir(self) -> Path:
        return self.root / self.state_name

    # -- resolution -------------------------------------------------------
    def resolve(self, raw: str | os.PathLike[str]) -> ResolvedPath:
        """Resolve *raw* against this workspace, not against the process cwd."""

        text = os.fspath(raw) or "."
        expanded = os.path.expanduser(str(text))

        if _is_unc(expanded) and not _is_unc(str(self.root)):
            # A UNC path is outside a local workspace by construction. Resolving
            # it could touch the network, so it is classified without doing so.
            return ResolvedPath(str(text), expanded, _key(expanded), _key(expanded), True, None)

        if not os.path.isabs(expanded):
            expanded = os.path.join(str(self.root), expanded)

        absolute = os.path.abspath(expanded)
        real = _key(os.path.realpath(expanded))
        return ResolvedPath(
            str(text),
            absolute,
            _key(absolute),
            real,
            _is_unc(absolute),
            _relative_key(real, self.root_real),
        )

    # -- classification ---------------------------------------------------
    def classify(self, raw: str | os.PathLike[str]) -> AccessDecision:
        """Say which range *raw* falls into, judged on its real path."""

        resolved = self.resolve(raw)
        if resolved.is_unc or not _within(resolved.real, self.root_real):
            return AccessDecision(
                "outside",
                resolved,
                f"outside the workspace root {self.root_real}",
            )

        relative = (resolved.relative or "").replace("\\", "/")
        if self._is_protected(relative):
            return AccessDecision(
                "workspace",
                resolved,
                f"protected workspace file {relative}",
                protected=True,
            )
        if relative == self.state_name or relative.startswith(self.state_name + "/"):
            return AccessDecision(
                "runtime_state",
                resolved,
                f"runtime state path {relative}",
            )
        return AccessDecision("workspace", resolved, f"inside the workspace: {relative or '.'}")

    def _is_protected(self, relative: str) -> bool:
        # str.lstrip("./") would eat the leading dot of ".mawoagent", so only a
        # real "./" prefix is removed here. Paths stay "/"-separated: normcase
        # would rewrite them to "\" on Windows and break the directory prefix.
        candidate = relative
        while candidate.startswith("./"):
            candidate = candidate[2:]
        if os.name == "nt":
            candidate = candidate.casefold()
        for item in self.protected_paths:
            key = item.casefold() if os.name == "nt" else item
            if candidate == key or candidate.startswith(key + "/"):
                return True
        return False

    # -- convenience ------------------------------------------------------
    def describe(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "workspace_only_reads": self.workspace_only_reads,
            "workspace_only_writes": self.workspace_only_writes,
            "protected_paths": list(self.protected_paths),
            "source": self.source,
        }


_current_workspace: ContextVar[WorkspaceContext | None] = ContextVar(
    "mawoagent_workspace", default=None
)


def current_workspace() -> WorkspaceContext | None:
    """The workspace bound to the tool call in flight, if any."""

    return _current_workspace.get()


def bind_workspace(context: WorkspaceContext | None) -> Token:
    return _current_workspace.set(context)


def reset_workspace(token: Token) -> None:
    _current_workspace.reset(token)


@contextmanager
def workspace_scope(context: WorkspaceContext | None) -> Iterator[WorkspaceContext | None]:
    token = bind_workspace(context)
    try:
        yield context
    finally:
        reset_workspace(token)


def resolve_tool_path(raw: str, context: WorkspaceContext | None = None) -> Path:
    """Resolve a tool path against the bound workspace.

    Without a bound workspace the requested path is returned unchanged, which
    keeps the historical behaviour for callers that never entered the runtime.
    The returned path keeps its original casing for display and for the OS.
    """

    workspace = context or current_workspace()
    if workspace is None:
        return Path(raw)
    resolved = workspace.resolve(raw)
    if resolved.is_unc:
        return Path(raw)
    return Path(resolved.absolute)
