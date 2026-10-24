"""Execution backends: the local one must behave exactly as before (I4 / T11)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from agents import sandbox
from agents.sandbox import LocalBackend, get_backend
from agents.tools import _run_shell


def run(coro):
    return asyncio.run(coro)


class LocalBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = LocalBackend()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_success_returns_stdout_and_zero(self) -> None:
        result = run(self.backend.run("echo hello", timeout_s=10))
        self.assertEqual(result.returncode, 0)
        self.assertIn("hello", result.stdout)
        self.assertEqual(result.backend, "local")

    def test_nonzero_exit_keeps_both_streams(self) -> None:
        result = run(self.backend.run("echo out && echo err 1>&2 && exit 3", timeout_s=10))
        self.assertEqual(result.returncode, 3)
        self.assertIn("out", result.stdout)
        self.assertIn("err", result.stderr)

    def test_timeout_is_reported_without_raising(self) -> None:
        result = run(self.backend.run(f'"{sys.executable}" -c "import time; time.sleep(5)"', timeout_s=0.3))
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        self.assertLess(result.duration_ms, 4000)

    def test_working_directory_is_respected(self) -> None:
        target = self.root / "sub"
        target.mkdir()
        # `cd` prints the directory only in cmd; ask Python so the check is the
        # same on every platform.
        result = run(self.backend.run(
            f'"{sys.executable}" -c "import os; print(os.getcwd())"',
            timeout_s=10,
            cwd=target,
        ))
        self.assertIn("sub", result.stdout)

    def test_environment_is_merged_not_replaced(self) -> None:
        result = run(self.backend.run(
            f'"{sys.executable}" -c "import os; print(os.environ.get(\'MAWO_PROBE\', \'\')); print(os.environ.get(\'PATH\') is not None)"',
            timeout_s=10,
            env={"MAWO_PROBE": "yes"},
        ))
        self.assertIn("yes", result.stdout)
        self.assertIn("True", result.stdout)
        self.assertIsNone(os.environ.get("MAWO_PROBE"))

    def test_cancellation_propagates(self) -> None:
        async def scenario() -> None:
            task = asyncio.create_task(
                self.backend.run(f'"{sys.executable}" -c "import time; time.sleep(5)"', timeout_s=30)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        run(scenario())


class BackendSelectionTests(unittest.TestCase):
    def test_local_is_the_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAWOAGENT_SANDBOX", None)
            self.assertEqual(get_backend().name, "local")

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            get_backend("firecracker")

    def test_environment_selects_the_backend(self) -> None:
        with mock.patch.dict(os.environ, {"MAWOAGENT_SANDBOX": "local"}):
            self.assertIsInstance(get_backend(), LocalBackend)


class RunShellFormattingTests(unittest.TestCase):
    """The tool result strings are part of the contract; keep them stable."""

    def test_success_text(self) -> None:
        self.assertIn("hello", run(_run_shell({"command": "echo hello"})))

    def test_empty_output_is_named(self) -> None:
        self.assertEqual(run(_run_shell({"command": "exit 0"})), "(no output)")

    def test_failure_shape(self) -> None:
        text = run(_run_shell({"command": "exit 4"}))
        self.assertIn("Command failed (exit code 4)", text)

    def test_timeout_shape(self) -> None:
        text = run(_run_shell({"command": f'"{sys.executable}" -c "import time; time.sleep(5)"', "timeout": 300}))
        self.assertEqual(text, "Command timed out after 300ms")

    def test_non_positive_timeout_is_rejected(self) -> None:
        self.assertEqual(run(_run_shell({"command": "echo x", "timeout": 0})), "Error: timeout must be positive")

    def test_tool_result_does_not_leak_backend_type(self) -> None:
        self.assertIsInstance(run(_run_shell({"command": "echo x"})), str)


if __name__ == "__main__":
    unittest.main()
