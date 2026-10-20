"""Acceptance check for t02-config-rename.

Two things are required: no source file may still mention the old key, and the
modules must still import and return the same values under the new name.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

OLD_KEY = "timeout_s"
NEW_KEY = "timeout_seconds"
# A plain substring search would flag "timeout_seconds" as well, because the old
# name is a prefix of the new one. Only a standalone occurrence counts.
OLD_KEY_PATTERN = re.compile(rf"\b{OLD_KEY}\b")


def _import_from(workspace: Path, name: str):
    for cached in ("config", "client", "cli"):
        sys.modules.pop(cached, None)
    sys.path.insert(0, str(workspace))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(workspace))


def check(workspace: Path) -> dict:
    details: list[str] = []
    passed = True

    for path in sorted(workspace.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if OLD_KEY_PATTERN.search(text):
            passed = False
            details.append(f"{path.name} still references {OLD_KEY}")

    try:
        config = _import_from(workspace, "config")
        client = _import_from(workspace, "client")
        cli = _import_from(workspace, "cli")
    except Exception as exc:
        passed = False
        details.append(f"importing the modules failed: {type(exc).__name__}: {exc}")
        return {"passed": False, "details": details}

    if NEW_KEY not in config.DEFAULTS:
        passed = False
        details.append(f"DEFAULTS has no {NEW_KEY} key: {sorted(config.DEFAULTS)}")
    elif config.DEFAULTS[NEW_KEY] != 30:
        passed = False
        details.append(f"DEFAULTS[{NEW_KEY!r}] is {config.DEFAULTS[NEW_KEY]!r}, expected 30")

    if config.DEFAULTS.get("retries") != 3:
        passed = False
        details.append("the unrelated retries key changed value")

    if client.request_timeout() != 30:
        passed = False
        details.append(f"client.request_timeout() returned {client.request_timeout()!r}")

    if client.retries() != 3:
        passed = False
        details.append(f"client.retries() returned {client.retries()!r}")

    if cli.describe() != "timeout=30s retries=3":
        passed = False
        details.append(f"cli.describe() returned {cli.describe()!r}")

    if passed:
        details.append(f"all references use {NEW_KEY} and the modules still import")
    return {"passed": passed, "details": details}
