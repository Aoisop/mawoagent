"""Acceptance check for t05-shell-verify.

The failure only shows up when the test suite is executed, so the check runs it
the same way a developer would. The shipped assertions could be weakened during
the fix, so the behaviour is sampled independently as well.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path


def check(workspace: Path) -> dict:
    details: list[str] = []
    passed = True

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(workspace) + (os.pathsep + existing if existing else "")

    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", ".", "-q"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    if result.returncode != 0:
        passed = False
        tail = (result.stderr or result.stdout).strip().splitlines()[-6:]
        details.append(f"the workspace test suite failed (exit {result.returncode})")
        details.extend(f"  {line}" for line in tail)

    for cached in ("report", "utils", "util"):
        sys.modules.pop(cached, None)
    sys.path.insert(0, str(workspace))
    try:
        report = importlib.import_module("report")
    except Exception as exc:
        passed = False
        details.append(f"importing report failed: {type(exc).__name__}: {exc}")
    else:
        if report.headline("Hello World") != "# hello-world":
            passed = False
            details.append(f"headline('Hello World') returned {report.headline('Hello World')!r}")
        if report.headline("A B") != "# a-b":
            passed = False
            details.append(f"headline('A B') returned {report.headline('A B')!r}")
    finally:
        sys.path.remove(str(workspace))

    if passed:
        details.append("the workspace suite passes and headline still slugifies titles")
    return {"passed": passed, "details": details}
