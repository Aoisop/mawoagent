"""Preview-first cleanup of disposable artifacts, preserving current champions."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import time

from .skill_evolution import get_evolution_dir


def cleanup_candidates(days: int = 30) -> list[Path]:
    if days < 1:
        raise ValueError("retention days must be positive")
    cutoff = time.time() - days * 86400
    paths = []
    results = Path.home() / ".mawoagent" / "tool-results"
    for path in results.glob("*.txt"):
        if not path.is_symlink() and path.stat().st_mtime < cutoff:
            paths.append(path)
    evaluation = get_evolution_dir() / "online-eval"
    registry = evaluation / "champions.json"
    champions = json.loads(registry.read_text()).get("champions", {}) if registry.exists() else {}
    protected = {record.get("run_id") for record in champions.values()}
    for path in (evaluation / "runs").glob("*/*"):
        if (path.is_dir() and not path.is_symlink() and evaluation.resolve() in path.resolve().parents
                and path.name not in protected and path.stat().st_mtime < cutoff):
            paths.append(path)
    return paths


def cleanup_state(days: int = 30, *, apply: bool = False, expected_paths: list[str] | None = None) -> dict:
    paths = cleanup_candidates(days)
    if apply:
        if expected_paths is not None and set(map(str, paths)) != set(expected_paths):
            raise ValueError("cleanup candidates changed during approval; preview again")
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
    return {"applied": apply, "count": len(paths), "paths": [str(path) for path in paths]}
