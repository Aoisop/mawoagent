"""The batch-6 fixes: prompt root, discovery scope, copy mode links, fork depth."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents import prompt, tools
from agents.sandbox import CopyingBackend, LocalBackend
from agents.workspace import WorkspaceContext, workspace_scope


class PromptRootTests(unittest.TestCase):
    def test_the_prompt_describes_the_bound_workspace_not_the_process_cwd(self) -> None:
        from agents.project_trust import trust_project

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            (root / "CLAUDE.md").write_text("WORKSPACE-MARKER\n", encoding="utf-8")
            trust_project(root)

            text = prompt.build_system_prompt(root)

        self.assertIn("WORKSPACE-MARKER", text)
        self.assertIn(str(root), text)


class DiscoveryScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "ws"
        self.outside = base / "secrets"
        self.root.mkdir()
        self.outside.mkdir()
        (self.outside / "key.txt").write_text("sk-live", encoding="utf-8")
        (self.root / "notes.txt").write_text("inside", encoding="utf-8")

    def test_a_parent_glob_is_refused(self) -> None:
        with workspace_scope(WorkspaceContext(root=self.root, workspace_only_reads=True)):
            output = tools._list_files({"path": ".", "pattern": "../secrets/*"})
        self.assertIn("must stay inside", output)

    def test_discovery_does_not_leave_a_confined_workspace(self) -> None:
        with workspace_scope(WorkspaceContext(root=self.root, workspace_only_reads=True)):
            found = tools._list_files({"path": ".", "pattern": "**/*.txt"})
        self.assertIn("notes.txt", found)
        self.assertNotIn("key.txt", found)

    def test_a_junction_or_symlink_out_of_the_workspace_is_not_searched(self) -> None:
        link = self.root / "alias"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")

        with workspace_scope(WorkspaceContext(root=self.root, workspace_only_reads=True)):
            found = tools._list_files({"path": "alias", "pattern": "*.txt"})
        self.assertNotIn("key.txt", found)


class CopyModeLinkTests(unittest.TestCase):
    def test_a_link_out_of_the_workspace_is_not_copied_or_reachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "ws"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
            try:
                (root / "link").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not available here")

            result = asyncio.run(
                CopyingBackend(LocalBackend()).run(
                    'python -c "print(1)"', timeout_s=10, cwd=root
                )
            )

        self.assertEqual(result.error, "")
        self.assertNotIn("TOPSECRET", result.stdout + result.stderr)

    def test_the_copy_reports_the_change_without_touching_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            original = root / "file.txt"
            original.write_text("before\n", encoding="utf-8")

            command = 'python -c "open(\'file.txt\',\'a\').write(\'after\')"'
            result = asyncio.run(CopyingBackend(LocalBackend()).run(command, timeout_s=10, cwd=root))

            self.assertIn("M file.txt", " ".join(result.changed))
            self.assertEqual(original.read_text(encoding="utf-8"), "before\n")


class ForkDepthTests(unittest.TestCase):
    def test_a_child_cannot_be_created_past_the_limit(self) -> None:
        from agents.agent import MAX_AGENT_DEPTH, Agent

        with mock.patch("agents.agent.build_system_prompt", return_value="sys"):
            agent = Agent(api_base="http://example.test/v1", api_key="test")
        agent._agent_depth = MAX_AGENT_DEPTH
        with self.assertRaises(ValueError):
            agent._create_child_agent(system_prompt="child", tools=[])


if __name__ == "__main__":
    unittest.main()
