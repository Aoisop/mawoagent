"""Acceptance check for t01-off-by-one.

The task is to correct the mean calculation. Behaviour is checked directly
instead of re-reading the implementation, so any correct fix passes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_stats(workspace: Path):
    spec = importlib.util.spec_from_file_location("fixture_t01_stats", workspace / "stats.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {workspace / 'stats.py'}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(workspace: Path) -> dict:
    details: list[str] = []
    passed = True

    stats = _load_stats(workspace)

    for values, expected in (([2, 4], 3.0), ([1, 2, 3], 2.0), ([7], 7.0), ([0, 0], 0.0)):
        try:
            actual = stats.mean(values)
        except Exception as exc:
            passed = False
            details.append(f"mean({values!r}) raised {type(exc).__name__}: {exc}")
            continue
        if actual != expected:
            passed = False
            details.append(f"mean({values!r}) returned {actual!r}, expected {expected!r}")

    try:
        stats.mean([])
    except ZeroDivisionError:
        pass
    except Exception as exc:
        passed = False
        details.append(f"mean([]) raised {type(exc).__name__} instead of ZeroDivisionError")
    else:
        passed = False
        details.append("mean([]) returned a value instead of raising ZeroDivisionError")

    if passed:
        details.append("mean returns the arithmetic mean for all sampled inputs")
    return {"passed": passed, "details": details}
