"""Harness for the versioned coding-task fixtures (iteration I0 / T02).

The fixtures live in ``tests/fixtures/tasks``. Each task directory holds three
parts:

``workspace/``
    The fixed initial state handed to the agent. Never edited in place; the
    tests copy it to a temporary directory before running anything.
``check.py``
    A module exposing ``check(workspace: Path) -> dict``. The returned mapping
    must contain ``passed`` (bool) and may contain ``details`` (list of str).
``solution/``
    A reference resolution. Copying it over a pristine workspace must make
    ``check`` pass; that is what proves a fixture is solvable and its checker
    is not trivially false.

``manifest.json`` pins the initial state with a SHA-256 digest per file, so a
silently edited workspace is reported instead of quietly changing the task.
Digests are computed over LF-normalised bytes: a Windows checkout with
``core.autocrlf=true`` rewrites line endings on disk and must not look like a
modified fixture.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TASK_ROOT = Path(__file__).resolve().parent / "fixtures" / "tasks"
MANIFEST_PATH = TASK_ROOT / "manifest.json"
SCHEMA_VERSION = 1

# A run that never produced a verdict still has to be recorded; the plan
# requires failures and timeouts to be counted, not dropped.
RUN_STATUSES = ("passed", "failed", "timeout", "error", "cancelled")

RECORD_REQUIRED_FIELDS = (
    "schema_version",
    "run_id",
    "task_id",
    "status",
    "started_at",
    "duration_ms",
    "model",
    "budget",
    "usage",
    "check",
)

TASK_REQUIRED_FIELDS = (
    "id",
    "title",
    "category",
    "directory",
    "allowed_tools",
    "budget",
    "expected",
    "initial_state",
)


def _digest(path: Path) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def file_digests(root: Path) -> dict[str, str]:
    """Return ``{relative posix path: sha256}`` for every file under *root*.

    Bytecode caches are skipped: they are generated, untracked, and importing a
    checker or a workspace module next to the fixture would otherwise show up as
    an "unexpected extra file" and break the pinned initial state.
    """

    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digests[path.relative_to(root).as_posix()] = _digest(path)
    return digests


def load_manifest() -> dict[str, Any]:
    with MANIFEST_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_task(task: dict[str, Any]) -> dict[str, Any]:
    """Return the manifest entry with ``check`` and ``solution`` paths resolved."""

    directory = TASK_ROOT / task["directory"]
    resolved = dict(task)
    resolved["path"] = directory
    resolved["check_path"] = directory / "check.py"
    resolved["workspace_path"] = directory / "workspace"
    resolved["solution_path"] = directory / "solution"
    return resolved


def verify_initial_state(task: dict[str, Any]) -> list[str]:
    """Report every way the on-disk workspace differs from the pinned state."""

    workspace = (TASK_ROOT / task["directory"]) / "workspace"
    problems: list[str] = []
    if not workspace.is_dir():
        return [f"{task['id']}: workspace directory is missing"]

    actual = file_digests(workspace)
    expected = task["initial_state"]

    for name in sorted(set(expected) - set(actual)):
        problems.append(f"{task['id']}: initial file missing: {name}")
    for name in sorted(set(actual) - set(expected)):
        problems.append(f"{task['id']}: unexpected extra file: {name}")
    for name in sorted(set(expected) & set(actual)):
        if expected[name] != actual[name]:
            problems.append(
                f"{task['id']}: {name} changed since the fixture was pinned "
                f"(expected {expected[name][:12]}..., found {actual[name][:12]}...)"
            )
    return problems


def copy_workspace(task: dict[str, Any], destination: Path) -> Path:
    """Copy the pristine workspace to *destination* and return it."""

    shutil.copytree(TASK_ROOT / task["directory"] / "workspace", destination)
    return destination


def apply_solution(task: dict[str, Any], workspace: Path) -> None:
    """Overlay the reference solution onto an already-copied workspace."""

    solution = TASK_ROOT / task["directory"] / "solution"
    if not solution.is_dir():
        raise FileNotFoundError(f"{task['id']}: no solution directory")
    for path in sorted(solution.rglob("*")):
        if path.is_file():
            target = workspace / path.relative_to(solution)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def _load_check_module(task: dict[str, Any]):
    path = TASK_ROOT / task["directory"] / "check.py"
    module_name = "fixture_check_" + task["directory"].replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Loading a checker must not leave __pycache__ inside the fixture tree.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def run_check(task: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Run a fixture checker against *workspace* and normalise the outcome."""

    started = time.perf_counter()
    try:
        module = _load_check_module(task)
        outcome = module.check(Path(workspace))
    except Exception as exc:  # a broken checker is reported, never swallowed
        return {
            "passed": False,
            "status": "error",
            "details": [f"{type(exc).__name__}: {exc}"],
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    if not isinstance(outcome, dict) or "passed" not in outcome:
        return {
            "passed": False,
            "status": "error",
            "details": [f"check() must return a dict with a 'passed' key, got {outcome!r}"],
            "duration_ms": duration_ms,
        }

    return {
        "passed": bool(outcome["passed"]),
        "status": "passed" if outcome["passed"] else "failed",
        "details": [str(item) for item in outcome.get("details", [])],
        "duration_ms": duration_ms,
    }


def build_run_record(
    *,
    run_id: str,
    task_id: str,
    check: dict[str, Any],
    model: str,
    budget: dict[str, Any],
    usage: dict[str, Any],
    status: str | None = None,
    started_at: str | None = None,
    duration_ms: int | None = None,
    artifacts: dict[str, Any] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Build one run record. Every attempt gets a record, including failures."""

    resolved_status = status or check.get("status", "error")
    if resolved_status not in RUN_STATUSES:
        raise ValueError(f"unknown run status: {resolved_status}")

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "status": resolved_status,
        "started_at": started_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_ms": duration_ms if duration_ms is not None else check.get("duration_ms", 0),
        "model": model,
        "budget": budget,
        "usage": usage,
        "check": check,
        "artifacts": artifacts or {},
        "notes": notes,
    }


def validate_run_record(record: dict[str, Any]) -> list[str]:
    """Return the reasons a run record is unusable; empty means valid."""

    problems: list[str] = []
    for field in RECORD_REQUIRED_FIELDS:
        if field not in record:
            problems.append(f"missing field: {field}")

    if record.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}")
    if record.get("status") not in RUN_STATUSES:
        problems.append(f"status must be one of {RUN_STATUSES}, got {record.get('status')!r}")
    if not isinstance(record.get("budget"), dict):
        problems.append("budget must be an object")
    if not isinstance(record.get("usage"), dict):
        problems.append("usage must be an object")
    if not isinstance(record.get("check"), dict):
        problems.append("check must be an object")
    elif "passed" not in record["check"]:
        problems.append("check must record a passed flag")
    return problems


def append_run_record(record: dict[str, Any], path: Path) -> None:
    """Append one record as a JSONL line, flushing before returning."""

    problems = validate_run_record(record)
    if problems:
        raise ValueError("invalid run record: " + "; ".join(problems))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


if __name__ == "__main__":
    manifest = load_manifest()
    print(json.dumps(
        {task["directory"]: file_digests(TASK_ROOT / task["directory"] / "workspace")
         for task in manifest["tasks"]},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ))
