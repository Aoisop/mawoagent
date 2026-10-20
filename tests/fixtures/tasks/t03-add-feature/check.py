"""Acceptance check for t03-add-feature.

The behaviour is sampled beyond the shipped test file so that an implementation
which only special-cases the visible assertions does not pass.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_parsing(workspace: Path):
    spec = importlib.util.spec_from_file_location("fixture_t03_parsing", workspace / "parsing.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {workspace / 'parsing.py'}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(workspace: Path) -> dict:
    details: list[str] = []
    passed = True

    parsing = _load_parsing(workspace)

    accepted = (("45s", 45), ("3m", 180), ("0s", 0), ("10m", 600))
    for text, expected in accepted:
        try:
            actual = parsing.parse_duration(text)
        except Exception as exc:
            passed = False
            details.append(f"parse_duration({text!r}) raised {type(exc).__name__}: {exc}")
            continue
        if actual != expected:
            passed = False
            details.append(f"parse_duration({text!r}) returned {actual!r}, expected {expected!r}")

    for text in ("5h", "abc", ""):
        try:
            parsing.parse_duration(text)
        except ValueError:
            continue
        except Exception as exc:
            passed = False
            details.append(
                f"parse_duration({text!r}) raised {type(exc).__name__} instead of ValueError"
            )
        else:
            passed = False
            details.append(f"parse_duration({text!r}) accepted an unsupported input")

    if passed:
        details.append("parse_duration handles supported and unsupported inputs")
    return {"passed": passed, "details": details}
