"""Side-query cost accounting by phase (I6 / T16)."""

from __future__ import annotations

import asyncio
import unittest

from agents.online_skill_eval import EvaluationError, QueryBudget


def budget(**kwargs) -> QueryBudget:
    async def query(system: str, user: str) -> str:
        return "ok"

    return QueryBudget(query, **kwargs)


class PhaseAccountingTests(unittest.TestCase):
    def test_calls_are_counted_per_phase(self) -> None:
        async def run() -> QueryBudget:
            item = budget()
            await item("s", "u1")                       # generation by default
            with item.phase("judge"):
                await item.judge("s", "u2")
            with item.phase("mutation"):
                await item("s", "u3")
            return item

        item = asyncio.run(run())
        self.assertEqual(dict(item.by_phase), {"generation": 1, "judge": 1, "mutation": 1})
        self.assertEqual(item.calls, 3)

    def test_cache_hits_are_not_charged_to_a_phase(self) -> None:
        async def run() -> QueryBudget:
            item = budget()
            await item("s", "same")
            before = dict(item.by_phase)
            await item("s", "same")
            item.after = before  # type: ignore[attr-defined]
            return item

        item = asyncio.run(run())
        self.assertEqual(dict(item.by_phase), item.after)
        self.assertEqual(item.cache_hits, 1)

    def test_the_phase_is_restored_after_a_block(self) -> None:
        async def run() -> QueryBudget:
            item = budget()
            with item.phase("judge"):
                await item.judge("s", "u1")
            await item("s", "u2")
            return item

        item = asyncio.run(run())
        self.assertEqual(dict(item.by_phase), {"judge": 1, "generation": 1})

    def test_the_phase_is_restored_after_an_error(self) -> None:
        async def failing(system: str, user: str) -> str:
            raise RuntimeError("network")

        async def run() -> QueryBudget:
            item = QueryBudget(failing)
            try:
                with item.phase("judge"):
                    await item.judge("s", "u1")
            except EvaluationError:
                pass
            return item

        item = asyncio.run(run())
        self.assertEqual(item._phase, "generation")

    def test_a_budget_exhaustion_stops_further_calls(self) -> None:
        async def run() -> QueryBudget:
            item = budget(max_calls=1)
            await item("s", "u1")
            with self.assertRaises(EvaluationError):
                await item("s", "u2")
            return item

        item = asyncio.run(run())
        self.assertEqual(item.calls, 1)
        self.assertEqual(dict(item.by_phase), {"generation": 1})


class ReportTests(unittest.TestCase):
    def test_format_shows_the_breakdown(self) -> None:
        from agents.online_skill_eval import format_online_skill_eval

        report = {
            "budget": {"model_calls": 3, "max_calls": 80, "cache_hits": 1,
                       "by_phase": {"generation": 2, "judge": 1}},
            "skills": [],
        }
        text = format_online_skill_eval(report)
        self.assertIn("calls by phase: generation=2, judge=1", text)

    def test_report_without_breakdown_still_formats(self) -> None:
        from agents.online_skill_eval import format_online_skill_eval

        text = format_online_skill_eval({"budget": {"model_calls": 0, "max_calls": 1, "cache_hits": 0}, "skills": []})
        self.assertIn("calls=0/1", text)


if __name__ == "__main__":
    unittest.main()
