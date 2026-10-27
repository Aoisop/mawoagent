"""Execution evaluation: evidence thresholds and variant accounting (I6 / T16)."""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from agents.exec_eval import (
    MIN_DELTA,
    MIN_TASKS,
    ExecRecord,
    judge_evidence,
    probe_retrieval,
    run_evaluation,
    summarize,
)


def record(task_id: str, variant: str, passed: bool, **extra) -> ExecRecord:
    return ExecRecord(task_id=task_id, variant=variant, passed=passed, **extra)


class EvidenceTests(unittest.TestCase):
    def test_a_single_variant_is_not_evidence(self) -> None:
        evidence = judge_evidence([record("t1", "forced", True)])
        self.assertEqual(evidence.status, "insufficient_evidence")
        self.assertIn("both variants", evidence.reason)

    def test_different_task_sets_cannot_be_compared(self) -> None:
        records = [record("t1", "baseline", False), record("t2", "forced", True)]
        evidence = judge_evidence(records)
        self.assertEqual(evidence.status, "insufficient_evidence")
        self.assertIn("same tasks", evidence.reason)

    def test_too_few_tasks_is_insufficient(self) -> None:
        records = []
        for index in range(MIN_TASKS - 1):
            records += [record(f"t{index}", "baseline", False), record(f"t{index}", "forced", True)]
        evidence = judge_evidence(records)
        self.assertEqual(evidence.status, "insufficient_evidence")
        self.assertIn("below", evidence.reason)
        self.assertEqual(evidence.tasks, MIN_TASKS - 1)

    def test_a_small_gain_is_not_a_promotion(self) -> None:
        records = []
        for index in range(12):
            records.append(record(f"t{index}", "baseline", index < 4))
            records.append(record(f"t{index}", "forced", index < 5))
        evidence = judge_evidence(records)
        self.assertEqual(evidence.status, "no_improvement")
        self.assertLess(evidence.delta, MIN_DELTA)
        self.assertAlmostEqual(evidence.delta, 0.083, places=3)

    def test_a_clear_gain_promotes(self) -> None:
        records = []
        for index in range(4):
            records.append(record(f"t{index}", "baseline", False))
            records.append(record(f"t{index}", "forced", True))
        evidence = judge_evidence(records)
        self.assertEqual(evidence.status, "promote")
        self.assertEqual(evidence.baseline_rate, 0.0)
        self.assertEqual(evidence.forced_rate, 1.0)
        self.assertGreaterEqual(evidence.delta, MIN_DELTA)

    def test_no_change_is_reported_as_delta_zero(self) -> None:
        records = []
        for index in range(MIN_TASKS):
            records.append(record(f"t{index}", "baseline", True))
            records.append(record(f"t{index}", "forced", True))
        evidence = judge_evidence(records)
        self.assertEqual(evidence.status, "no_improvement")
        self.assertEqual(evidence.delta, 0.0)


class SummaryTests(unittest.TestCase):
    def test_rates_and_averages(self) -> None:
        records = [
            record("t1", "baseline", True, tool_calls=2, input_tokens=10, output_tokens=5, duration_ms=100),
            record("t2", "baseline", False, tool_calls=4, input_tokens=20, output_tokens=5, duration_ms=300),
        ]
        summary = summarize(records)["baseline"]
        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["success_rate"], 0.5)
        self.assertEqual(summary["avg_tool_calls"], 3.0)
        self.assertEqual(summary["total_tokens"], 40)
        self.assertEqual(summary["avg_duration_ms"], 200.0)

    def test_absent_variant_is_omitted(self) -> None:
        self.assertNotIn("forced", summarize([record("t1", "baseline", True)]))


class RunnerTests(unittest.TestCase):
    def tasks(self) -> list[dict]:
        return [{"id": f"t{i}", "prompt": f"do task {i}"} for i in range(3)]

    def test_variants_run_on_every_task(self) -> None:
        seen: list[tuple[str, str]] = []

        async def runner(task, variant, skill_prompt, *, model):
            seen.append((task["id"], variant))
            return ExecRecord(task["id"], variant, passed=variant == "forced")

        with mock.patch("agents.exec_eval.probe_retrieval", return_value=[]):
            report = asyncio.run(run_evaluation(
                self.tasks(), skill_name="demo", skill_prompt="use me", model="m", runner=runner,
            ))
        self.assertEqual(len(seen), 6)
        self.assertEqual(report["evidence"]["status"], "promote")
        self.assertEqual(len(report["records"]), 6)

    def test_records_are_serialised(self) -> None:
        async def runner(task, variant, skill_prompt, *, model):
            return ExecRecord(task["id"], variant, passed=True, tool_calls=1)

        with mock.patch("agents.exec_eval.probe_retrieval", return_value=[]):
            report = asyncio.run(run_evaluation(
                self.tasks(), skill_name="demo", skill_prompt="x", model="m", runner=runner,
            ))
        self.assertEqual(report["records"][0]["variant"], "baseline")
        self.assertEqual(report["records"][0]["tool_calls"], 1)

    def test_a_broken_runner_does_not_silently_pass(self) -> None:
        async def runner(task, variant, skill_prompt, *, model):
            return ExecRecord(task["id"], variant, passed=False, note="boom")

        with mock.patch("agents.exec_eval.probe_retrieval", return_value=[]):
            report = asyncio.run(run_evaluation(
                self.tasks(), skill_name="demo", skill_prompt="x", model="m", runner=runner,
            ))
        self.assertEqual(report["evidence"]["status"], "no_improvement")
        self.assertIn("boom", report["records"][0]["note"])


class RetrievalTests(unittest.TestCase):
    def test_hit_rate_and_rank_are_reported(self) -> None:
        # The real retriever returns dicts with a "name" key.
        def fake_retrieve(query, limit=3):
            if "task 1" in query:
                return [{"name": "other"}, {"name": "demo"}]
            return [{"name": "other"}]

        tasks = [{"id": "t0", "prompt": "do task 0"}, {"id": "t1", "prompt": "do task 1"}]
        with mock.patch("agents.skills.retrieve_relevant_skills", side_effect=fake_retrieve):
            hits = probe_retrieval(tasks, "demo")

        self.assertEqual(len(hits), 2)
        self.assertFalse(hits[0].retrieved)
        self.assertTrue(hits[1].retrieved)
        self.assertEqual(hits[1].rank, 2)

    def test_retrieval_is_separate_from_forced_loading(self) -> None:
        async def runner(task, variant, skill_prompt, *, model):
            return ExecRecord(task["id"], variant, passed=variant == "forced")

        tasks = [{"id": f"t{i}", "prompt": "x"} for i in range(3)]
        with mock.patch("agents.exec_eval.probe_retrieval", return_value=[]):
            report = asyncio.run(run_evaluation(
                tasks, skill_name="demo", skill_prompt="x", model="m", runner=runner,
            ))
        # Forcing made every task pass, and retrieval was never consulted for the score.
        self.assertEqual(report["evidence"]["status"], "promote")
        self.assertEqual(report["retrieval"]["probed"], 0)


if __name__ == "__main__":
    unittest.main()
