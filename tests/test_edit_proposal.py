"""Edit proposals: the approval covers a patch and the version it was built on (T04)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from agents.agent import Agent
from agents.tools import build_edit_proposal
from agents.workspace import WorkspaceContext


class ProposalShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "doc.md"
        self.target.write_text("alpha\nbeta\n", encoding="utf-8")

    def test_write_proposal_shows_the_replacement(self) -> None:
        proposal = build_edit_proposal("write_file", {"file_path": str(self.target), "content": "gamma\n"})
        rendered = proposal.render()

        self.assertTrue(proposal.existed)
        self.assertIn("-alpha", rendered)
        self.assertIn("+gamma", rendered)
        self.assertIn("doc.md", rendered)
        self.assertIn("base ", rendered)

    def test_new_file_proposal_is_against_dev_null(self) -> None:
        new_path = self.root / "created.md"
        proposal = build_edit_proposal("write_file", {"file_path": str(new_path), "content": "hello\n"})

        self.assertFalse(proposal.existed)
        self.assertIsNone(proposal.base_version)
        self.assertIn("/dev/null", proposal.render())
        self.assertIn("new file", proposal.render())

    def test_edit_proposal_contains_only_the_requested_change(self) -> None:
        proposal = build_edit_proposal(
            "edit_file",
            {"file_path": str(self.target), "old_string": "beta", "new_string": "delta"},
        )
        rendered = proposal.render()

        self.assertIn("-beta", rendered)
        self.assertIn("+delta", rendered)
        self.assertNotIn("-alpha", rendered)

    def test_missing_anchor_is_reported_before_approval(self) -> None:
        with self.assertRaises(ValueError) as caught:
            build_edit_proposal(
                "edit_file",
                {"file_path": str(self.target), "old_string": "nope", "new_string": "x"},
            )
        self.assertIn("not found", str(caught.exception))

    def test_ambiguous_anchor_is_rejected(self) -> None:
        self.target.write_text("same\nsame\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            build_edit_proposal(
                "edit_file",
                {"file_path": str(self.target), "old_string": "same", "new_string": "x"},
            )
        self.assertIn("found 2 times", str(caught.exception))

    def test_directory_target_does_not_crash_proposal_building(self) -> None:
        # Preparing the review must not fail; the write itself reports the real
        # OS error later, and nothing is changed before that.
        directory = self.root / "folder"
        directory.mkdir()
        proposal = build_edit_proposal("write_file", {"file_path": str(directory), "content": "x"})
        self.assertIn("folder", proposal.render())

    def test_fingerprint_changes_with_the_base_version(self) -> None:
        first = build_edit_proposal("edit_file", {"file_path": str(self.target), "old_string": "beta", "new_string": "x"})
        self.target.write_text("alpha\nbeta changed\n", encoding="utf-8")
        second = build_edit_proposal("edit_file", {"file_path": str(self.target), "old_string": "beta", "new_string": "x"})

        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_proposal_expires_when_the_file_changes(self) -> None:
        proposal = build_edit_proposal("edit_file", {"file_path": str(self.target), "old_string": "beta", "new_string": "x"})
        self.assertTrue(proposal.is_current())

        self.target.write_text("alpha\nbeta from someone else\n", encoding="utf-8")
        self.assertFalse(proposal.is_current())

    def test_new_file_proposal_expires_when_the_path_appears(self) -> None:
        new_path = self.root / "later.md"
        proposal = build_edit_proposal("write_file", {"file_path": str(new_path), "content": "x\n"})
        self.assertTrue(proposal.is_current())

        new_path.write_text("created elsewhere\n", encoding="utf-8")
        self.assertFalse(proposal.is_current())


class ApprovalFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "doc.md"
        self.target.write_text("alpha\nbeta\n", encoding="utf-8")

    def _agent(self, confirm_fn):
        with patch("agents.agent.build_system_prompt", return_value="test"):
            agent = Agent(
                api_base="https://example.test/v1",
                api_key="fake",
                custom_system_prompt="test",
                enable_mcp=False,
                enable_memory_prefetch=False,
                enable_online_evolution=False,
                enable_folding=False,
                confirm_fn=confirm_fn,
                workspace=WorkspaceContext(root=self.root, workspace_only_writes=False),
            )
        self.addAsyncCleanup(agent.aclose)
        agent._auto_save = Mock()
        return agent

    async def test_confirmation_carries_the_diff(self) -> None:
        seen: list[str] = []

        async def confirm(message: str) -> bool:
            seen.append(message)
            return True

        agent = self._agent(confirm)
        await agent._run_checked_tool("read_file", {"file_path": str(self.target)})
        await agent._run_checked_tool(
            "edit_file",
            {"file_path": str(self.target), "old_string": "beta", "new_string": "delta"},
        )

        self.assertEqual(len(seen), 1)
        self.assertIn("-beta", seen[0])
        self.assertIn("+delta", seen[0])
        self.assertEqual(self.target.read_text(encoding="utf-8"), "alpha\ndelta\n")

    async def test_rejection_writes_nothing(self) -> None:
        agent = self._agent(AsyncMock(return_value=False))
        await agent._run_checked_tool(
            "write_file",
            {"file_path": str(self.target), "content": "rejected\n"},
        )
        self.assertEqual(self.target.read_text(encoding="utf-8"), "alpha\nbeta\n")

    async def test_change_during_approval_aborts_the_write(self) -> None:
        async def confirm(message: str) -> bool:
            # Someone edits the file while the user is looking at the diff.
            self.target.write_text("alpha\nbeta edited elsewhere\n", encoding="utf-8")
            return True

        agent = self._agent(confirm)
        result = await agent._run_checked_tool(
            "edit_file",
            {"file_path": str(self.target), "old_string": "beta", "new_string": "delta"},
        )

        self.assertIn("changed while the edit was waiting", result)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "alpha\nbeta edited elsewhere\n")

    async def test_confirmation_skips_proposals_in_accept_edits_mode(self) -> None:
        agent = self._agent(None)
        agent.permission_mode = "acceptEdits"
        confirm = AsyncMock(return_value=True)
        agent._confirm_tool_call = confirm
        await agent._run_checked_tool("read_file", {"file_path": str(self.target)})

        await agent._run_checked_tool(
            "write_file",
            {"file_path": str(self.target), "content": "auto\n"},
        )

        confirm.assert_not_awaited()
        self.assertEqual(self.target.read_text(encoding="utf-8"), "auto\n")


if __name__ == "__main__":
    unittest.main()
