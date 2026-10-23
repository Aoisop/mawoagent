"""Task-set evaluation runner (iteration I3 / T09).

Runs the fixtures from ``tests/fixtures/tasks`` against a model and writes one
JSONL record per attempt, reusing the record format defined in
``tests/fixture_harness.py`` so a run from the CLI and a run from the tests
produce comparable data.

Two modes exist on purpose:

* ``--mock pass|fail|error`` replaces the model with a scripted driver. It is
  what CI runs: the numbers are meaningless, but the pipeline (workspace copy,
  check, record, aggregation, failures still recorded) is exercised.
* The default mode talks to a real model. It needs credentials and costs money,
  so it is never part of CI.

Ablation is expressed as variants: the same task runs with the repository map
injected and without it, and the summary compares the two.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tests.fixture_harness import (
    append_run_record,
    apply_solution,
    build_run_record,
    copy_workspace,
    load_manifest,
    load_task,
    run_check,
    validate_run_record,
)

VARIANTS = {"on": "1", "off": "0"}


def _task_prompt(task: dict[str, Any]) -> str:
    """The fixtures describe a task, not a chat message; this is the request."""

    return (
        f"{task['title']}\n\n"
        f"Expected behaviour: {task['expected']}\n\n"
        "Work only inside the current directory and verify the change before finishing."
    )


def _prepare(task: dict[str, Any], base: Path) -> Path:
    destination = base / task["id"]
    copy_workspace(task, destination)
    return destination


async def _run_mock(task: dict[str, Any], workspace: Path, scenario: str) -> dict[str, Any]:
    """Scripted stand-in for the model: deterministic and free."""

    if scenario == "error":
        raise RuntimeError("scripted failure")
    if scenario == "pass":
        apply_solution(task, workspace)
    return {"turns": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0}


async def _run_model(task: dict[str, Any], workspace: Path, model: str) -> dict[str, Any]:
    from agents.agent import Agent
    from agents.workspace import WorkspaceContext

    agent = Agent(
        model=model,
        permission_mode="acceptEdits",
        enable_mcp=False,
        enable_memory_prefetch=False,
        enable_online_evolution=False,
        enable_folding=False,
        workspace=WorkspaceContext(root=workspace, workspace_only_writes=False),
    )
    calls = {"count": 0}
    original = agent._run_checked_tool

    async def counting(name, inp):
        calls["count"] += 1
        return await original(name, inp)

    agent._run_checked_tool = counting
    try:
        await agent.chat(_task_prompt(task))
        return {
            "turns": agent.current_turns,
            "tool_calls": calls["count"],
            "input_tokens": agent.total_input_tokens,
            "output_tokens": agent.total_output_tokens,
        }
    finally:
        await agent.aclose()


async def run_one(
    task: dict[str, Any],
    *,
    variant: str,
    repeat: int,
    model: str,
    mock: str | None,
    out_path: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    usage = {"turns": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0}
    status = "error"
    notes = ""

    with tempfile.TemporaryDirectory() as tmp:
        workspace = _prepare(task, Path(tmp))
        os.environ["MAWOAGENT_REPO_MAP"] = VARIANTS[variant]
        try:
            if mock:
                usage = await _run_mock(task, workspace, mock)
            else:
                usage = await _run_model(task, workspace, model)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a record
            notes = f"{type(exc).__name__}: {exc}"
        finally:
            os.environ.pop("MAWOAGENT_REPO_MAP", None)

        duration_ms = int((time.monotonic() - started) * 1000)
        check = {"passed": False, "status": "error", "details": [], "duration_ms": 0}
        if not notes:
            try:
                check = run_check(task, workspace)
            except Exception as exc:  # noqa: BLE001
                notes = f"check failed: {type(exc).__name__}: {exc}"
        status = check.get("status", "error") if not notes else "error"

    record = build_run_record(
        run_id=f"{task['id']}-{variant}-{repeat}-{int(time.time() * 1000)}",
        task_id=task["id"],
        check=check,
        model="mock" if mock else model,
        budget={"variant": variant, "repeat": repeat, "repo_map": variant == "on"},
        usage=usage,
        status=status,
        started_at=started_at,
        duration_ms=duration_ms,
        artifacts={"variant": variant, "repeat": repeat, "mock": mock or ""},
        notes=notes,
    )
    problems = validate_run_record(record)
    if problems:
        raise ValueError(f"invalid run record: {problems}")
    append_run_record(record, out_path)
    return record


def summarize(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate per variant: success rate, tool calls, tokens, duration."""

    summary: dict[str, dict[str, Any]] = {}
    for record in records:
        variant = record.get("budget", {}).get("variant", "default")
        bucket = summary.setdefault(
            variant,
            {"runs": 0, "ok": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0, "duration_ms": 0},
        )
        bucket["runs"] += 1
        bucket["ok"] += 1 if record.get("status") == "passed" else 0
        usage = record.get("usage", {})
        bucket["tool_calls"] += int(usage.get("tool_calls", 0) or 0)
        bucket["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
        bucket["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
        bucket["duration_ms"] += int(record.get("duration_ms", 0) or 0)

    for bucket in summary.values():
        runs = max(bucket["runs"], 1)
        bucket["success_rate"] = round(bucket["ok"] / runs, 3)
        bucket["avg_tool_calls"] = round(bucket["tool_calls"] / runs, 2)
        bucket["avg_tokens"] = round((bucket["input_tokens"] + bucket["output_tokens"]) / runs, 1)
        bucket["avg_duration_ms"] = round(bucket["duration_ms"] / runs, 1)
    return summary


def format_summary(summary: dict[str, dict[str, Any]]) -> str:
    if not summary:
        return "No runs recorded."
    lines = ["variant  runs  ok   rate   tools   tokens   ms"]
    for variant in sorted(summary):
        item = summary[variant]
        lines.append(
            f"{variant:<8} {item['runs']:>4}  {item['ok']:>3}  {item['success_rate']:>5}  "
            f"{item['avg_tool_calls']:>6}  {item['avg_tokens']:>7}  {item['avg_duration_ms']:>6}"
        )
    return "\n".join(lines)


async def run_suite(
    *,
    manifest: Path | None,
    out: Path,
    model: str,
    repeat: int,
    mock: str | None,
    variants: list[str],
    limit: int | None,
) -> list[dict[str, Any]]:
    data = json.loads((manifest or Path("tests/fixtures/tasks/manifest.json")).read_text(encoding="utf-8"))
    tasks = [load_task(task) for task in data["tasks"]]
    if limit:
        tasks = tasks[:limit]

    records: list[dict[str, Any]] = []
    for task in tasks:
        for variant in variants:
            for attempt in range(1, repeat + 1):
                records.append(
                    await run_one(
                        task,
                        variant=variant,
                        repeat=attempt,
                        model=model,
                        mock=mock,
                        out_path=out,
                    )
                )
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the coding-task fixtures and record each attempt.")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("eval-results.jsonl"))
    parser.add_argument("--model", default=os.environ.get("MODEL", "unknown"))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--mock", choices=("pass", "fail", "error"), default=None)
    parser.add_argument("--variants", default="on,off")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    if args.out.exists():
        args.out.unlink()

    variants = [item.strip() for item in args.variants.split(",") if item.strip()]
    records = asyncio.run(
        run_suite(
            manifest=args.manifest,
            out=args.out,
            model=args.model,
            repeat=max(args.repeat, 1),
            mock=args.mock,
            variants=variants,
            limit=args.limit,
        )
    )
    print(format_summary(summarize(records)))
    print(f"\n{len(records)} record(s) written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
