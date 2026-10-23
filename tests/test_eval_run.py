"""The task-set runner: records, failures and aggregation (I3 / T09)."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from agents import eval_run
from agents.eval_run import format_summary, run_suite, summarize
from tests.fixture_harness import validate_run_record


class RunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "results.jsonl"

    def run_mock(self, scenario: str, *, variants: list[str] | None = None, repeat: int = 1, limit: int = 1):
        return asyncio.run(
            run_suite(
                manifest=None,
                out=self.out,
                model="unused",
                repeat=repeat,
                mock=scenario,
                variants=variants or ["on"],
                limit=limit,
            )
        )

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.out.read_text(encoding="utf-8").splitlines() if line.strip()]


class RecordTests(RunnerTestCase):
    def test_passing_scenario_is_recorded_as_passed(self) -> None:
        records = self.run_mock("pass")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "passed")
        self.assertEqual(self.records()[0]["task_id"], "t01-off-by-one")

    def test_failing_scenario_is_recorded_as_failed(self) -> None:
        records = self.run_mock("fail")
        self.assertEqual(records[0]["status"], "failed")
        self.assertTrue(records[0]["check"]["details"])

    def test_exceptions_become_error_records_instead_of_losing_the_run(self) -> None:
        records = self.run_mock("error")
        self.assertEqual(records[0]["status"], "error")
        self.assertIn("scripted failure", records[0]["notes"])

    def test_every_record_matches_the_shared_schema(self) -> None:
        self.run_mock("pass", variants=["on", "off"])
        for record in self.records():
            with self.subTest(variant=record["budget"]["variant"]):
                self.assertEqual(validate_run_record(record), [])

    def test_repeat_writes_one_record_per_attempt(self) -> None:
        self.run_mock("pass", repeat=3)
        self.assertEqual(len(self.records()), 3)
        self.assertEqual([item["budget"]["repeat"] for item in self.records()], [1, 2, 3])

    def test_variants_are_flagged_in_the_record(self) -> None:
        records = self.run_mock("pass", variants=["on", "off"])
        flags = {record["budget"]["variant"]: record["budget"]["repo_map"] for record in records}
        self.assertEqual(flags, {"on": True, "off": False})

    def test_repository_map_environment_is_restored(self) -> None:
        os.environ.pop("MAWOAGENT_REPO_MAP", None)
        self.run_mock("pass")
        self.assertNotIn("MAWOAGENT_REPO_MAP", os.environ)


class SummaryTests(unittest.TestCase):
    @staticmethod
    def record(variant: str, status: str, *, tools: int = 0, tokens: int = 0, duration: int = 10) -> dict:
        return {
            "status": status,
            "budget": {"variant": variant},
            "usage": {"tool_calls": tools, "input_tokens": tokens, "output_tokens": 0},
            "duration_ms": duration,
        }

    def test_success_rate_and_averages(self) -> None:
        summary = summarize([
            self.record("on", "passed", tools=2, tokens=100, duration=20),
            self.record("on", "failed", tools=4, tokens=200, duration=40),
        ])["on"]

        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["success_rate"], 0.5)
        self.assertEqual(summary["avg_tool_calls"], 3.0)
        self.assertEqual(summary["avg_tokens"], 150.0)
        self.assertEqual(summary["avg_duration_ms"], 30.0)

    def test_variants_are_compared_side_by_side(self) -> None:
        summary = summarize([
            self.record("on", "passed", tools=1),
            self.record("off", "failed", tools=9),
        ])
        self.assertEqual(set(summary), {"on", "off"})
        text = format_summary(summary)
        self.assertIn("variant", text)
        self.assertIn("off", text)

    def test_empty_input_has_a_readable_message(self) -> None:
        self.assertEqual(format_summary({}), "No runs recorded.")


class PromptTests(unittest.TestCase):
    def test_prompt_carries_both_the_title_and_the_expectation(self) -> None:
        prompt = eval_run._task_prompt({
            "title": "Fix the mean",
            "expected": "mean returns the arithmetic mean",
        })
        self.assertIn("Fix the mean", prompt)
        self.assertIn("mean returns the arithmetic mean", prompt)
        self.assertIn("current directory", prompt)


if __name__ == "__main__":
    unittest.main()
