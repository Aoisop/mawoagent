"""run_shell executes where the agent is bound, not where the process started (C32)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from agents import tools
from agents.workspace import WorkspaceContext, workspace_scope


class ShellWorkingDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()

    def output(self) -> str:
        # Print the process working directory in a way that works on both shells.
        command = f'"{sys.executable}" -c "import os; print(os.getcwd())"'
        return asyncio.run(tools.execute_tool("run_shell", {"command": command}))

    def assert_same_directory(self, reported: str, expected: Path) -> None:
        # Windows reports the 8.3 form (RUNNER~1) where resolve() gives the long
        # name; comparing the strings would fail on the same directory.
        self.assertEqual(
            os.path.normcase(os.path.realpath(reported.strip())),
            os.path.normcase(os.path.realpath(str(expected))),
        )

    def test_the_command_runs_in_the_bound_workspace(self) -> None:
        with workspace_scope(WorkspaceContext(root=self.root)):
            output = self.output()
        self.assert_same_directory(output, self.root)

    def test_without_a_binding_it_falls_back_to_the_process_directory(self) -> None:
        output = self.output()
        self.assert_same_directory(output, Path.cwd())


if __name__ == "__main__":
    unittest.main()
