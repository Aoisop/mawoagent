from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agents.tool_schemas import tool_definitions
from tests.fixture_harness import (
    MANIFEST_PATH,
    RECORD_REQUIRED_FIELDS,
    RUN_STATUSES,
    append_run_record,
    apply_solution,
    build_run_record,
    copy_workspace,
    load_manifest,
    run_check,
    validate_run_record,
    verify_initial_state,
)

KNOWN_TOOLS = {definition["name"] for definition in tool_definitions}


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_manifest()
        self.tasks = self.manifest["tasks"]

    def test_schema_version_is_current(self) -> None:
        self.assertEqual(self.manifest["schema_version"], 1)

    def test_fixture_manifest_is_versioned_next_to_the_tasks(self) -> None:
        self.assertTrue(MANIFEST_PATH.is_file())
        self.assertEqual(MANIFEST_PATH.parent, Path(__file__).resolve().parent / "fixtures" / "tasks")

    def test_several_tasks_cover_different_categories(self) -> None:
        self.assertGreaterEqual(len(self.tasks), 5)
        categories = {task["category"] for task in self.tasks}
        self.assertGreaterEqual(len(categories), 5)

    def test_every_task_declares_the_required_fields(self) -> None:
        required = (
            "id",
            "title",
            "category",
            "directory",
            "allowed_tools",
            "budget",
            "expected",
            "initial_state",
            "check_timeout_s",
        )
        for task in self.tasks:
            with self.subTest(task=task.get("id")):
                for field in required:
                    self.assertIn(field, task)
                self.assertTrue(str(task["expected"]).strip())

    def test_ids_and_directories_are_unique(self) -> None:
        ids = [task["id"] for task in self.tasks]
        directories = [task["directory"] for task in self.tasks]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(directories), len(set(directories)))

    def test_each_task_ships_a_workspace_check_and_solution(self) -> None:
        for task in self.tasks:
            with self.subTest(task=task["id"]):
                task_dir = MANIFEST_PATH.parent / task["directory"]
                self.assertTrue((task_dir / "workspace").is_dir())
                self.assertTrue((task_dir / "check.py").is_file())
                self.assertTrue((task_dir / "solution").is_dir())
                self.assertTrue(task["initial_state"])

    def test_allowed_tools_exist_in_the_runtime(self) -> None:
        for task in self.tasks:
            with self.subTest(task=task["id"]):
                unknown = set(task["allowed_tools"]) - KNOWN_TOOLS
                self.assertEqual(unknown, set(), f"unknown tools: {sorted(unknown)}")

    def test_budgets_and_timeouts_are_positive(self) -> None:
        for task in self.tasks:
            with self.subTest(task=task["id"]):
                self.assertGreater(task["budget"]["max_turns"], 0)
                self.assertGreater(task["budget"]["max_tool_calls"], 0)
                self.assertGreater(task["check_timeout_s"], 0)

    def test_workspace_still_matches_the_pinned_initial_state(self) -> None:
        problems: list[str] = []
        for task in self.tasks:
            problems.extend(verify_initial_state(task))
        self.assertEqual(problems, [], "\n".join(problems))


class FixtureBehaviourTests(unittest.TestCase):
    """A fixture only earns its place if it fails unsolved and passes solved."""

    def setUp(self) -> None:
        self.tasks = load_manifest()["tasks"]

    def test_each_check_rejects_the_pristine_workspace(self) -> None:
        for task in self.tasks:
            with self.subTest(task=task["id"]), tempfile.TemporaryDirectory() as tmp:
                workspace = copy_workspace(task, Path(tmp) / "workspace")
                outcome = run_check(task, workspace)
                self.assertFalse(
                    outcome["passed"],
                    f"{task['id']} already passes without any work: {outcome['details']}",
                )
                self.assertIn(outcome["status"], ("failed", "error"))

    def test_each_check_accepts_the_reference_solution(self) -> None:
        for task in self.tasks:
            with self.subTest(task=task["id"]), tempfile.TemporaryDirectory() as tmp:
                workspace = copy_workspace(task, Path(tmp) / "workspace")
                apply_solution(task, workspace)
                outcome = run_check(task, workspace)
                self.assertTrue(
                    outcome["passed"],
                    f"{task['id']} rejects its own solution: {outcome['details']}",
                )

    def test_checks_do_not_touch_the_versioned_workspace(self) -> None:
        for task in self.tasks:
            with self.subTest(task=task["id"]), tempfile.TemporaryDirectory() as tmp:
                workspace = copy_workspace(task, Path(tmp) / "workspace")
                run_check(task, workspace)
                self.assertEqual(verify_initial_state(task), [])


class RunRecordTests(unittest.TestCase):
    def _record(self, status: str) -> dict:
        return build_run_record(
            run_id="run-0001",
            task_id="t01-off-by-one",
            check={"passed": status == "passed", "status": status, "details": [], "duration_ms": 5},
            model="fixture-model",
            budget={"max_turns": 6, "max_tool_calls": 12},
            usage={"turns": 1, "tool_calls": 2},
            status=status,
        )

    def test_failures_and_timeouts_are_recordable(self) -> None:
        for status in RUN_STATUSES:
            with self.subTest(status=status):
                record = self._record(status)
                self.assertEqual(validate_run_record(record), [])
                self.assertEqual(record["status"], status)

    def test_record_carries_every_required_field(self) -> None:
        record = self._record("passed")
        for field in RECORD_REQUIRED_FIELDS:
            self.assertIn(field, record)

    def test_incomplete_record_is_rejected(self) -> None:
        record = self._record("passed")
        del record["check"]
        self.assertIn("missing field: check", validate_run_record(record))

    def test_unknown_status_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._record("finished")

    def test_records_append_as_jsonl_without_dropping_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs" / "records.jsonl"
            append_run_record(self._record("passed"), path)
            append_run_record(self._record("timeout"), path)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["status"] for row in rows], ["passed", "timeout"])


if __name__ == "__main__":
    unittest.main()
