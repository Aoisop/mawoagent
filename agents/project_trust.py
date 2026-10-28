"""User-owned trust for an exact project configuration snapshot."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .storage import state_lock, write_json


def trust_file() -> Path:
    return Path.home() / ".mawoagent" / "trusted-projects.json"


def _default_project() -> Path:
    from .workspace import project_root

    return project_root().resolve()


def project_fingerprint(project: Path | None = None) -> str:
    project = (project or _default_project()).resolve()
    digest = hashlib.sha256(str(project).encode())
    for relative in (".mawoagent/settings.json", ".mcp.json"):
        path = project / relative
        digest.update(relative.encode())
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
    return digest.hexdigest()


def is_project_trusted(project: Path | None = None) -> bool:
    project = (project or _default_project()).resolve()
    try:
        records = json.loads(trust_file().read_text(encoding="utf-8"))
        return records.get(str(project)) == project_fingerprint(project)
    except (OSError, ValueError, AttributeError):
        return False


def trust_project(project: Path | None = None) -> None:
    project = (project or _default_project()).resolve()
    path = trust_file()
    with state_lock(path):
        records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(records, dict):
            raise ValueError("Invalid project trust registry")
        records[str(project)] = project_fingerprint(project)
        write_json(path, records)
