"""Unified security policy with explicit sources (iteration I1 / T05).

The runtime already reads ``permissions`` from two settings files. This module
adds the file-access policy next to them, in the same files, and keeps track of
*where* each value came from so the UI can show it instead of silently merging.

Resolution order, lowest priority first:

1. built-in defaults
2. ``~/.mawoagent/settings.json`` (user)
3. ``<workspace>/.mawoagent/settings.json`` (project, only when trusted)

A project settings file only wins when its exact content is trusted; otherwise
it is ignored here, matching how ``permissions.allow`` is already treated. The
project file is part of the trust fingerprint in ``project_trust``, so editing
the security block invalidates the existing trust.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .workspace import PROTECTED_RELATIVE_PATHS, WorkspaceContext

DEFAULTS: dict[str, Any] = {
    "workspace_only_reads": False,
    "workspace_only_writes": True,
    "protected_paths": list(PROTECTED_RELATIVE_PATHS),
}

SOURCE_DEFAULT = "default"
SOURCE_USER = "user"
SOURCE_PROJECT = "project"

_SECRET_KEY = re.compile(r"(key|token|secret|password|credential)", re.IGNORECASE)
_TOKEN_VALUE = re.compile(r"\b(sk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_\-]{8,}")


@dataclass(frozen=True)
class Sourced:
    value: Any
    source: str


@dataclass(frozen=True)
class SecurityPolicy:
    settings: dict[str, Sourced]
    warnings: tuple[str, ...] = ()
    origin: str = SOURCE_DEFAULT

    def value(self, key: str) -> Any:
        entry = self.settings.get(key)
        return entry.value if entry else DEFAULTS.get(key)

    def workspace_context(self, root: Path | str | None = None) -> WorkspaceContext:
        base = Path(root) if root else Path.cwd()
        return WorkspaceContext(
            root=base,
            workspace_only_reads=bool(self.value("workspace_only_reads")),
            workspace_only_writes=bool(self.value("workspace_only_writes")),
            protected_paths=tuple(self.value("protected_paths")),
            source=self.origin,
        )

    def describe(self) -> dict[str, Any]:
        """Values plus the source of each one, safe to send to the UI."""

        payload: dict[str, Any] = {
            "root_policy": {key: self.value(key) for key in DEFAULTS},
            "sources": {key: entry.source for key, entry in self.settings.items()},
        }
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        return redact_secrets(payload)


def _settings_file(project: Path) -> Path:
    return project / ".mawoagent" / "settings.json"


def _user_settings_file() -> Path | None:
    """The per-user settings file, or None when no home directory is resolvable.

    ``Path.home()`` raises when HOME/USERPROFILE is absent, which happens in
    stripped-down environments and in tests that clear the environment. The
    user layer is then simply absent; it must not stop the runtime from
    starting.
    """

    try:
        return Path.home() / ".mawoagent" / "settings.json"
    except (RuntimeError, KeyError, OSError):
        return None


def _project_is_trusted(root: Path) -> bool:
    try:
        from .project_trust import is_project_trusted

        return is_project_trusted(root)
    except (RuntimeError, KeyError, OSError, ValueError):
        # Without a trust registry the project layer stays closed.
        return False


def _read_security_block(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("security")
    return block if isinstance(block, dict) else None


def _coerce(key: str, value: Any) -> tuple[bool, Any]:
    if key in {"workspace_only_reads", "workspace_only_writes"}:
        return (isinstance(value, bool), value)
    if key == "protected_paths":
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return True, [item for item in value]
        return False, None
    return False, None


def load_security_policy(project: Path | str | None = None) -> SecurityPolicy:
    """Resolve the policy from defaults, user settings and trusted project settings."""

    root = Path(project).resolve() if project else Path.cwd().resolve()
    settings: dict[str, Sourced] = {
        key: Sourced(value, SOURCE_DEFAULT) for key, value in DEFAULTS.items()
    }
    warnings: list[str] = []

    user_settings = _user_settings_file()
    layers: list[tuple[str, Path]] = []
    if user_settings is not None:
        layers.append((SOURCE_USER, user_settings))
    layers.append((SOURCE_PROJECT, _settings_file(root)))

    origin = SOURCE_DEFAULT
    trusted = _project_is_trusted(root)
    for source, path in layers:
        if source == SOURCE_PROJECT and not trusted:
            # An untrusted project file must not be able to widen its own range.
            if _read_security_block(path) is not None:
                warnings.append(
                    "project security settings ignored: the project configuration is not trusted"
                )
            continue
        block = _read_security_block(path)
        if not block:
            continue
        applied = False
        for key in DEFAULTS:
            if key not in block:
                continue
            ok, coerced = _coerce(key, block[key])
            if ok:
                settings[key] = Sourced(coerced, source)
                applied = True
            else:
                warnings.append(f"{source} setting ignored: invalid value for {key}")
        if applied:
            # The highest layer that contributed anything is the policy origin.
            origin = source

    return SecurityPolicy(settings, tuple(warnings), origin)


def redact_secrets(value: Any) -> Any:
    """Mask credential-looking keys and token-looking strings, recursively."""

    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY.search(key):
                result[key] = "[redacted]"
            else:
                result[key] = redact_secrets(item)
        return result
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _TOKEN_VALUE.sub("[redacted]", value)
    return value


def describe_policy_text(project: Path | str | None = None) -> str:
    """One-line-per-setting summary for the CLI."""

    policy = load_security_policy(project)
    lines = []
    for key in DEFAULTS:
        entry = policy.settings[key]
        lines.append(f"{key} = {entry.value!r} ({entry.source})")
    for warning in policy.warnings:
        lines.append("warning: " + warning)
    return os.linesep.join(lines)
