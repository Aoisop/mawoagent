"""Execution-based skill evaluation (iteration I6 / T16).

The reviewed replay in ``online_skill_eval`` asks a model to answer a task in
text and scores the answer. That measures instruction following; it does not
measure whether the skill lets an agent finish a real task in a real workspace.
This module runs the fixture tasks with the agent, checks the workspace with the
fixture's own checker, and reports what actually happened.

Two things it refuses to do:

* Claim a skill helps when the sample is too small. Promotion needs a minimum
  number of distinct tasks, both variants measured on the same tasks, and a gain
  larger than noise. Anything less is reported as ``insufficient_evidence``.
* Confuse "the skill helps when forced into the prompt" with "the agent would
  have found the skill". Forced loading and natural retrieval are measured
  separately, because a skill nobody retrieves is not a skill the user has.

Text replay is untouched and remains the default in ``online_skill_eval``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

VARIANTS = ("baseline", "forced")
MIN_TASKS = 3
MIN_DELTA = 0.1


@dataclass
class ExecRecord:
    task_id: str
    variant: str
    passed: bool
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    note: str = ""


@dataclass
class RetrievalHit:
    task_id: str
    retrieved: bool
    rank: int = 0


@dataclass
class Evidence:
    status: str
    reason: str
    baseline_rate: float | None = None
    forced_rate: float | None = None
    delta: float | None = None
    tasks: int = 0
    details: dict[str, Any] = field(default_factory=dict)


def _rate(records: list[ExecRecord], variant: str) -> float | None:
    runs = [record for record in records if record.variant == variant]
    if not runs:
        return None
    return sum(1 for record in runs if record.passed) / len(runs)


def judge_evidence(records: list[ExecRecord], *, min_tasks: int = MIN_TASKS,
                   min_delta: float = MIN_DELTA) -> Evidence:
    """Promotion needs a real sample, matched variants, and a visible gain."""

    by_variant = {variant: {r.task_id for r in records if r.variant == variant} for variant in VARIANTS}
    if not by_variant["baseline"] or not by_variant["forced"]:
        return Evidence("insufficient_evidence", "both variants must be measured")
    if by_variant["baseline"] != by_variant["forced"]:
        return Evidence(
            "insufficient_evidence",
            "variants were not measured on the same tasks; the difference is not attributable to the skill",
            tasks=len(by_variant["baseline"]),
        )
    tasks = len(by_variant["baseline"])
    if tasks < min_tasks:
        return Evidence(
            "insufficient_evidence",
            f"{tasks} task(s) is below the {min_tasks} required to distinguish a skill from luck",
            tasks=tasks,
        )

    baseline = _rate(records, "baseline") or 0.0
    forced = _rate(records, "forced") or 0.0
    delta = round(forced - baseline, 3)
    if delta < min_delta:
        return Evidence(
            "no_improvement",
            f"forced loading changed the pass rate by {delta:+.3f}, below the {min_delta:+.3f} threshold",
            baseline_rate=round(baseline, 3), forced_rate=round(forced, 3), delta=delta, tasks=tasks,
        )
    return Evidence(
        "promote",
        f"forced loading raised the pass rate by {delta:+.3f} across {tasks} tasks",
        baseline_rate=round(baseline, 3), forced_rate=round(forced, 3), delta=delta, tasks=tasks,
    )


def summarize(records: list[ExecRecord]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for variant in VARIANTS:
        runs = [record for record in records if record.variant == variant]
        if not runs:
            continue
        summary[variant] = {
            "runs": len(runs),
            "passed": sum(1 for record in runs if record.passed),
            "success_rate": round(sum(1 for record in runs if record.passed) / len(runs), 3),
            "avg_tool_calls": round(sum(record.tool_calls for record in runs) / len(runs), 2),
            "total_tokens": sum(record.input_tokens + record.output_tokens for record in runs),
            "avg_duration_ms": round(sum(record.duration_ms for record in runs) / len(runs), 1),
        }
    return summary


# ─── runners ──────────────────────────────────────────────────────────────

Runner = Callable[[dict[str, Any], str, str], Awaitable[ExecRecord]]


async def run_fixture_task(task: dict[str, Any], variant: str, skill_prompt: str, *,
                           model: str, timeout_s: float = 180) -> ExecRecord:
    """Run one fixture task with the real agent and score it with its checker."""

    from tests.fixture_harness import copy_workspace, run_check  # dev-only dependency

    from .agent import Agent
    from .workspace import WorkspaceContext

    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        workspace = copy_workspace(task, Path(tmp) / task["id"])
        prompt = task["prompt"] if variant == "baseline" else f"{skill_prompt}\n\n{task['prompt']}"
        agent = Agent(
            model=model,
            permission_mode="acceptEdits",
            enable_mcp=False,
            enable_memory_prefetch=False,
            enable_online_evolution=False,
            enable_folding=False,
            workspace=WorkspaceContext(root=workspace, workspace_only_writes=False),
        )
        # Count calls where they are actually made, not where they are logged.
        calls = {"count": 0}
        original = agent._run_checked_tool

        async def counting(name, inp):
            calls["count"] += 1
            return await original(name, inp)

        agent._run_checked_tool = counting
        note = ""
        try:
            await asyncio.wait_for(agent.chat(prompt), timeout_s)
        except Exception as exc:  # noqa: BLE001 - a failed run is a failed run
            note = f"{type(exc).__name__}: {exc}"
        finally:
            tokens = (agent.total_input_tokens, agent.total_output_tokens)
            await agent.aclose()

        check = {"status": "error", "duration_ms": 0}
        if not note:
            try:
                check = run_check(task, workspace)
            except Exception as exc:  # noqa: BLE001
                note = f"check failed: {type(exc).__name__}: {exc}"
        return ExecRecord(
            task_id=task["id"],
            variant=variant,
            passed=check.get("status") == "passed" and not note,
            tool_calls=calls["count"],
            input_tokens=int(tokens[0]),
            output_tokens=int(tokens[1]),
            duration_ms=int((time.monotonic() - started) * 1000),
            note=note,
        )


def probe_retrieval(tasks: list[dict[str, Any]], skill_name: str, *, limit: int = 3) -> list[RetrievalHit]:
    """Would the normal retrieval path surface this skill for these tasks?"""

    from .skills import retrieve_relevant_skills

    hits: list[RetrievalHit] = []
    for task in tasks:
        found = retrieve_relevant_skills(task["prompt"], limit=limit)
        # The retriever reports plain dicts, not skill objects.
        names = [item.get("name", "") if isinstance(item, dict) else getattr(item, "name", "")
                 for item in found]
        hits.append(RetrievalHit(
            task_id=task["id"],
            retrieved=skill_name in names,
            rank=names.index(skill_name) + 1 if skill_name in names else 0,
        ))
    return hits


async def run_evaluation(
    tasks: list[dict[str, Any]],
    *,
    skill_name: str,
    skill_prompt: str,
    model: str,
    runner: Runner = run_fixture_task,
    variants: tuple[str, ...] = VARIANTS,
) -> dict[str, Any]:
    records: list[ExecRecord] = []
    for task in tasks:
        for variant in variants:
            records.append(await runner(task, variant, skill_prompt, model=model))

    hits = probe_retrieval(tasks, skill_name) if tasks else []
    return {
        "schema_version": 1,
        "mode": "execution",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "skill": skill_name,
        "tasks": [task["id"] for task in tasks],
        "records": [asdict(record) for record in records],
        "summary": summarize(records),
        "retrieval": {
            "probed": len(hits),
            "retrieved": sum(1 for hit in hits if hit.retrieved),
            "hit_rate": round(sum(1 for hit in hits if hit.retrieved) / len(hits), 3) if hits else 0.0,
            "hits": [asdict(hit) for hit in hits],
        },
        "evidence": asdict(judge_evidence(records)),
    }


def load_fixture_tasks(manifest: Path | None = None, *, limit: int | None = None) -> list[dict[str, Any]]:
    from tests.fixture_harness import load_task

    path = manifest or Path("tests/fixtures/tasks/manifest.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks = []
    for raw in data["tasks"]:
        task = load_task(raw)
        task["prompt"] = f"{task['title']}\n\nExpected behaviour: {task['expected']}"
        tasks.append(task)
    return tasks[:limit] if limit else tasks


def skill_instructions(skill_name: str) -> str:
    from .skills import get_skill_by_name

    skill = get_skill_by_name(skill_name)
    if skill is None:
        raise ValueError(f"unknown skill: {skill_name}")
    return skill.prompt_template


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run fixture tasks with and without a skill.")
    parser.add_argument("--skill", required=True)
    parser.add_argument("--model", default=os.environ.get("MODEL", "unknown"))
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", type=Path, default=Path("exec-eval.json"))
    args = parser.parse_args(argv)

    tasks = load_fixture_tasks(args.manifest, limit=args.limit)
    report = asyncio.run(run_evaluation(
        tasks, skill_name=args.skill, skill_prompt=skill_instructions(args.skill), model=args.model,
    ))
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"evidence: {report['evidence']['status']} — {report['evidence']['reason']}")
    print(f"retrieval: {report['retrieval']['retrieved']}/{report['retrieval']['probed']}")
    print(f"report: {args.out}")
    return 0 if report["evidence"]["status"] == "promote" else 1


if __name__ == "__main__":
    sys.exit(main())
