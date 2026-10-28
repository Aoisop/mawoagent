"""Scheduling: validation, mutual exclusion, retry policy, OS entries (I7 / T21)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agents import schedule
from agents.schedule import ScheduleError, ScheduleSpec, validate_cron
from agents.storage import state_lock, try_state_lock


class CronTests(unittest.TestCase):
    def test_common_expressions_are_accepted(self) -> None:
        for expression in ("0 9 * * *", "*/15 * * * *", "0 9 * * 1-5", "30 2 1,15 * *", "0 0 * * 0"):
            self.assertEqual(validate_cron(expression), expression)

    def test_wrong_field_count_is_rejected(self) -> None:
        with self.assertRaises(ScheduleError):
            validate_cron("0 9 * *")

    def test_out_of_range_values_are_rejected(self) -> None:
        for expression in ("60 9 * * *", "0 24 * * *", "0 9 32 * *", "0 9 * 13 *", "0 9 * * 8"):
            with self.assertRaises(ScheduleError):
                validate_cron(expression)

    def test_malformed_fields_are_rejected(self) -> None:
        for expression in ("a b c d e", "0 9 * * 1-", "*/0 * * * *", "0 9 * * 5-2"):
            with self.assertRaises(ScheduleError):
                validate_cron(expression)


class SpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self.tmp.name, "USERPROFILE": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.workspace = Path(self.tmp.name) / "work"
        self.workspace.mkdir()

    def spec(self, **overrides) -> ScheduleSpec:
        values = {"name": "nightly", "prompt": "run the tests", "workspace": self.workspace, "cron": "0 3 * * *"}
        values.update(overrides)
        return ScheduleSpec(**values)

    def test_a_valid_spec_saves_and_loads(self) -> None:
        schedule.save_spec(self.spec())
        loaded = schedule.load_spec("nightly")
        self.assertEqual(loaded.prompt, "run the tests")
        self.assertEqual(loaded.workspace, self.workspace)

    def test_duplicates_need_overwrite(self) -> None:
        schedule.save_spec(self.spec())
        with self.assertRaises(ScheduleError):
            schedule.save_spec(self.spec())
        schedule.save_spec(self.spec(model="other"), overwrite=True)
        self.assertEqual(schedule.load_spec("nightly").model, "other")

    def test_names_are_restricted(self) -> None:
        for name in ("../escape", "a/b", "", "with space"):
            with self.assertRaises(ScheduleError):
                schedule.save_spec(self.spec(name=name))

    def test_policies_and_budgets_are_validated(self) -> None:
        for bad in ({"overlap": "parallel"}, {"retry": "always"}, {"max_turns": 0}, {"max_cost": 0}):
            with self.assertRaises(ScheduleError):
                schedule.save_spec(self.spec(**bad))

    def test_list_and_remove(self) -> None:
        schedule.save_spec(self.spec())
        schedule.save_spec(self.spec(name="weekly", cron="0 4 * * 1"))
        self.assertEqual({spec.name for spec in schedule.list_specs()}, {"nightly", "weekly"})
        self.assertTrue(schedule.remove_spec("nightly"))
        self.assertFalse(schedule.remove_spec("nightly"))

    def test_unknown_schedule_is_an_error(self) -> None:
        with self.assertRaises(ScheduleError):
            schedule.load_spec("missing")


class LockTests(unittest.TestCase):
    def test_try_lock_reports_contention_instead_of_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            holding, release = threading.Event(), threading.Event()

            def holder() -> None:
                with state_lock(path):
                    holding.set()
                    release.wait(5)

            thread = threading.Thread(target=holder, daemon=True)
            thread.start()
            self.assertTrue(holding.wait(5))

            with try_state_lock(path) as acquired:
                self.assertFalse(acquired)
            release.set()
            thread.join(5)

            with try_state_lock(path) as acquired:
                self.assertTrue(acquired)

    def test_the_lock_is_per_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            one, two = Path(tmp) / "a", Path(tmp) / "b"
            self.assertNotEqual(schedule.project_lock(one), schedule.project_lock(two))


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self.tmp.name, "USERPROFILE": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.workspace = Path(self.tmp.name) / "work"
        self.workspace.mkdir()

    def save(self, **overrides) -> None:
        values = {"name": "job", "prompt": "do it", "workspace": self.workspace, "cron": "0 3 * * *"}
        values.update(overrides)
        schedule.save_spec(ScheduleSpec(**values), overwrite=True)

    def test_a_successful_run_is_persisted(self) -> None:
        self.save()
        record = schedule.run_once("job", runner=lambda spec: {"status": "ok", "tool_calls": 2, "turns": 1, "cost_usd": 0.01})
        self.assertEqual(record["status"], "ok")
        runs = schedule.read_runs("job")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["tool_calls"], 2)

    def test_a_failing_runner_is_recorded_not_raised(self) -> None:
        self.save()

        def boom(spec):
            raise RuntimeError("model down")

        record = schedule.run_once("job", runner=boom)
        self.assertEqual(record["status"], "error")
        self.assertIn("model down", record["error"])
        self.assertEqual(len(schedule.read_runs("job")), 1)

    def test_overlap_skips_when_the_project_is_busy(self) -> None:
        self.save()
        holding, release = threading.Event(), threading.Event()

        def holder() -> None:
            with state_lock(schedule.project_lock(self.workspace)):
                holding.set()
                release.wait(5)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(holding.wait(5))
        try:
            record = schedule.run_once("job", runner=lambda spec: {"status": "ok"})
        finally:
            release.set()
            thread.join(5)

        self.assertEqual(record["status"], "skipped_overlap")
        self.assertEqual(record["attempts"], 0)

    def test_overlap_skip_is_recorded(self) -> None:
        self.save()
        with mock.patch.object(schedule, "try_state_lock") as fake:
            class _Ctx:
                def __enter__(self): return False
                def __exit__(self, *exc): return False
            fake.return_value = _Ctx()
            record = schedule.run_once("job", runner=lambda spec: {"status": "ok"})
        self.assertEqual(record["status"], "skipped_overlap")
        self.assertEqual(schedule.read_runs("job")[-1]["status"], "skipped_overlap")

    def test_startup_only_retries_when_nothing_was_attempted(self) -> None:
        self.save(retry="startup_only")
        calls = []

        def failing(spec):
            calls.append(1)
            return {"status": "error", "tool_calls": 0}

        record = schedule.run_once("job", runner=failing, attempts=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(record["attempts"], 2)

    def test_no_retry_after_side_effects(self) -> None:
        self.save(retry="startup_only")
        calls = []

        def failing(spec):
            calls.append(1)
            return {"status": "error", "tool_calls": 1}

        record = schedule.run_once("job", runner=failing, attempts=3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(record["status"], "error")

    def test_never_retries_by_default(self) -> None:
        self.save()
        calls = []

        def failing(spec):
            calls.append(1)
            return {"status": "error", "tool_calls": 0}

        schedule.run_once("job", runner=failing, attempts=3)
        self.assertEqual(len(calls), 1)


class OsEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self.tmp.name, "USERPROFILE": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spec = ScheduleSpec(
            name="nightly", prompt="p", workspace=Path(self.tmp.name), cron="30 2 * * *",
        )

    def test_windows_renders_a_scheduled_task_command(self) -> None:
        entry = schedule.render_os_entry(self.spec, platform="windows")
        self.assertEqual(entry["kind"], "command")
        self.assertIn("schtasks", entry["content"])
        self.assertIn("/ST 02:30", entry["content"])
        self.assertIn("agents.schedule run nightly", entry["content"])

    def test_macos_renders_a_launchd_plist(self) -> None:
        entry = schedule.render_os_entry(self.spec, platform="darwin")
        self.assertEqual(entry["kind"], "file")
        self.assertIn("<key>Hour</key><integer>2</integer>", entry["content"])
        self.assertIn("dev.mawoagent.nightly", entry["path"])

    def test_linux_renders_a_crontab_line(self) -> None:
        entry = schedule.render_os_entry(self.spec, platform="linux")
        self.assertEqual(entry["kind"], "crontab")
        self.assertTrue(entry["content"].startswith("30 2 * * * "))
        self.assertIn("# mawoagent:nightly", entry["content"])

    def test_a_schedule_object_is_recorded_with_its_workspace_and_budget(self) -> None:
        data = self.spec.to_dict()
        self.assertEqual(data["workspace"], str(self.spec.workspace))
        self.assertEqual(data["timezone"], "local")
        self.assertIn("max_cost", data)

    def test_install_without_apply_changes_nothing(self) -> None:
        schedule.save_spec(self.spec)
        entry = schedule.install("nightly", apply=False)
        self.assertFalse(entry["applied"])
        self.assertNotIn(str(self.spec.workspace / ".mawoagent"), entry["content"])

    def test_non_fixed_times_are_refused_where_the_platform_needs_them(self) -> None:
        flexible = ScheduleSpec(name="flex", prompt="p", workspace=Path(self.tmp.name), cron="*/15 * * * *")
        with self.assertRaises(ScheduleError):
            schedule.render_os_entry(flexible, platform="windows")
        # Linux keeps the expression as-is.
        self.assertIn("*/15", schedule.render_os_entry(flexible, platform="linux")["content"])


if __name__ == "__main__":
    unittest.main()
