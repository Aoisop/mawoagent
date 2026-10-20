"""Refresh the pinned initial-state digests in manifest.json.

Run this only after intentionally changing a fixture workspace:

    python tests/fixtures/tasks/regen_manifest.py

Task metadata (allowed tools, budgets, expectations) is owned by
``manifest.json`` and is never rewritten here; only ``initial_state`` is
recomputed. The script prints what changed so an accidental edit is visible in
review instead of being absorbed silently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TASK_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = TASK_ROOT / "manifest.json"

sys.path.insert(0, str(TASK_ROOT.parents[1]))
from tests.fixture_harness import file_digests  # noqa: E402


def main() -> int:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    changed = 0

    for task in manifest["tasks"]:
        workspace = TASK_ROOT / task["directory"] / "workspace"
        if not workspace.is_dir():
            print(f"{task['id']}: workspace directory is missing", file=sys.stderr)
            return 1

        before = task.get("initial_state", {})
        after = file_digests(workspace)
        if before != after:
            changed += 1
            for name in sorted(set(before) | set(after)):
                if before.get(name) != after.get(name):
                    print(f"{task['id']}: {name}: {before.get(name, '<absent>')[:12]} -> {after.get(name, '<absent>')[:12]}")
            task["initial_state"] = after

    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{changed} task(s) updated; manifest written to {MANIFEST_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
