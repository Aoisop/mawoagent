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


class ShellEnvironmentTests(unittest.TestCase):
    """Credentials in the parent environment are not part of the tool contract."""

    def test_secret_variables_are_removed(self) -> None:
        with mock.patch.dict(os.environ, {
            "APIKEY": "a", "OPENAI_API_KEY": "b", "GITHUB_TOKEN": "c",
            "DB_PASSWORD": "d", "PATH": "/usr/bin", "HOME": "/home/x",
        }, clear=True):
            environment = sandbox.shell_environment()
        for name in ("APIKEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD"):
            self.assertNotIn(name, environment)
        self.assertIn("PATH", environment)
        self.assertIn("HOME", environment)

    def test_explicit_values_still_pass_through(self) -> None:
        with mock.patch.dict(os.environ, {"APIKEY": "a"}, clear=True):
            environment = sandbox.shell_environment({"DEPLOY_TOKEN": "explicit"})
        self.assertEqual(environment["DEPLOY_TOKEN"], "explicit")

    def test_a_command_cannot_read_back_the_key(self) -> None:
        with mock.patch.dict(os.environ, {"APIKEY": "sk-hidden"}, clear=False):
            result = run(LocalBackend().run(
                f'"{sys.executable}" -c "import os; print(os.environ.get(\'APIKEY\', \'absent\'))"',
                timeout_s=10,
            ))
        self.assertIn("absent", result.stdout)
        self.assertNotIn("sk-hidden", result.stdout)


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


class CopyModeTests(unittest.TestCase):
    """Copy mode previews a command instead of applying it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "a.txt").write_text("one\n", encoding="utf-8")
        (self.root / "script.py").write_text(
            "from pathlib import Path\n"
            "Path('a.txt').write_text('two\\n')\n"
            "Path('b.txt').write_text('new\\n')\n",
            encoding="utf-8",
        )
        self.backend = sandbox.CopyingBackend(LocalBackend())

    def test_real_workspace_is_untouched(self) -> None:
        asyncio.run(self.backend.run(f'"{sys.executable}" script.py', timeout_s=20, cwd=self.root))
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "one\n")
        self.assertFalse((self.root / "b.txt").exists())

    def test_changes_are_reported(self) -> None:
        result = asyncio.run(self.backend.run(f'"{sys.executable}" script.py', timeout_s=20, cwd=self.root))
        self.assertEqual(result.backend, "copy")
        self.assertIn("M a.txt", result.changed)
        self.assertIn("A b.txt", result.changed)

    def test_deletion_is_reported(self) -> None:
        (self.root / "gone.py").write_text("from pathlib import Path\nPath('a.txt').unlink()\n", encoding="utf-8")
        result = asyncio.run(self.backend.run(f'"{sys.executable}" gone.py', timeout_s=20, cwd=self.root))
        self.assertIn("D a.txt", result.changed)

    def test_runtime_state_is_not_copied(self) -> None:
        (self.root / ".mawoagent").mkdir()
        (self.root / ".mawoagent" / "state.json").write_text("{}", encoding="utf-8")
        result = asyncio.run(self.backend.run(f'"{sys.executable}" -c "pass"', timeout_s=20, cwd=self.root))
        self.assertEqual(result.changed, [])

    def test_mode_selects_the_wrapper(self) -> None:
        with mock.patch.dict(os.environ, {"MAWOAGENT_SANDBOX_MODE": "copy"}):
            self.assertIsInstance(get_backend("local"), sandbox.CopyingBackend)

    def test_unknown_mode_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"MAWOAGENT_SANDBOX_MODE": "magic"}):
            with self.assertRaises(ValueError):
                get_backend("local")


if __name__ == "__main__":
    unittest.main()


class DockerBackendTests(unittest.TestCase):
    """The container backend, exercised through a fake client (no daemon here)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = self.root / "fake_docker.py"
        self.fake.write_text(
            "import sys, time\n"
            "print(' '.join(sys.argv[1:]))\n"
            "if 'sleep' in sys.argv[-1]:\n"
            "    time.sleep(5)\n",
            encoding="utf-8",
        )
        self.backend = sandbox.DockerBackend(docker=[sys.executable, str(self.fake)])

    def args(self, command: str = "echo hi", env=None) -> list[str]:
        return self.backend.docker_args(command, container="c1", workspace=self.root, env=env)

    def test_container_is_network_isolated_and_capped(self) -> None:
        args = self.args()
        self.assertIn("--network", args)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertEqual(args[args.index("--memory") + 1], "2g")
        self.assertEqual(args[args.index("--cpus") + 1], "2")
        self.assertEqual(args[args.index("--pids-limit") + 1], "256")

    def test_capabilities_are_dropped(self) -> None:
        args = self.args()
        self.assertEqual(args[args.index("--cap-drop") + 1], "ALL")
        self.assertIn("no-new-privileges", args[args.index("--security-opt") + 1])

    def test_only_the_workspace_is_mounted(self) -> None:
        mounts = [args for args in self.args() if ":/work" in args]
        self.assertEqual(len(mounts), 1)
        self.assertTrue(mounts[0].startswith(str(self.root)))

    def test_command_runs_through_a_login_shell_in_the_mount(self) -> None:
        args = self.args("echo hi")
        self.assertEqual(args[-3:], ["sh", "-lc", "echo hi"])
        self.assertEqual(args[args.index("-w") + 1], "/work")
        self.assertEqual(args[-4], self.backend.image)

    def test_environment_is_passed_explicitly(self) -> None:
        args = self.args(env={"MAWO_PROBE": "yes"})
        self.assertEqual(args[args.index("-e") + 1], "MAWO_PROBE=yes")
        self.assertLess(args.index("-e"), args.index(self.backend.image))

    def test_run_reports_the_backend_name(self) -> None:
        result = asyncio.run(self.backend.run("echo hi", timeout_s=10, cwd=self.root))
        self.assertEqual(result.backend, "docker")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--network none", result.stdout.replace("\n", " "))

    def test_missing_workspace_is_rejected_before_docker_is_called(self) -> None:
        result = asyncio.run(self.backend.run("echo hi", timeout_s=5, cwd=self.root / "nope"))
        self.assertIsNone(result.returncode)
        self.assertIn("not a directory", result.error)

    def test_timeout_removes_the_container(self) -> None:
        backend = sandbox.DockerBackend(docker=[sys.executable, str(self.fake)])
        backend.remove_container = mock.AsyncMock()
        result = asyncio.run(backend.run("sleep", timeout_s=0.3, cwd=self.root))
        self.assertTrue(result.timed_out)
        backend.remove_container.assert_awaited_once()

    def test_selection_builds_a_docker_backend(self) -> None:
        with mock.patch.dict(os.environ, {"MAWOAGENT_SANDBOX": "docker", "MAWOAGENT_SANDBOX_IMAGE": "img:test"}):
            backend = get_backend()
        self.assertIsInstance(backend, sandbox.DockerBackend)
        self.assertEqual(backend.image, "img:test")
