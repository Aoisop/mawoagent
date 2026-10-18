from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from agents.agent import Agent
from agents.tools import check_permission


class PermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = patch("agents.tools._check_permission_rules", return_value=None)
        self.rules.start()
        self.addCleanup(self.rules.stop)

    def test_default_requires_confirmation_for_shell_and_file_changes(self) -> None:
        commands = (
            "pwd",
            "python -c 'open(\"important.txt\", \"w\").write(\"x\")'",
            "git status",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(check_permission("run_shell", {"command": command})["action"], "confirm")
        for tool in ("write_file", "edit_file"):
            with self.subTest(tool=tool):
                self.assertEqual(check_permission(tool, {"file_path": "README.md"})["action"], "confirm")

    def test_modes_keep_their_explicit_behavior(self) -> None:
        shell = {"command": "pwd"}
        file = {"file_path": "README.md"}
        self.assertEqual(check_permission("run_shell", shell, "plan")["action"], "deny")
        self.assertEqual(check_permission("run_shell", shell, "dontAsk")["action"], "deny")
        self.assertEqual(check_permission("run_shell", shell, "acceptEdits")["action"], "confirm")
        self.assertEqual(check_permission("write_file", file, "dontAsk")["action"], "deny")
        self.assertEqual(check_permission("write_file", file, "acceptEdits")["action"], "allow")
        self.assertEqual(check_permission("edit_file", file, "acceptEdits")["action"], "allow")
        self.assertEqual(check_permission("run_shell", shell, "bypassPermissions")["action"], "allow")

    def test_shell_and_file_approvals_are_not_cached(self) -> None:
        with patch("agents.agent.build_system_prompt", return_value="test system prompt"):
            agent = Agent(api_base="http://example.test/v1", api_key="test")
        agent._confirm_dangerous = AsyncMock(return_value=True)

        async def approve_twice(tool: str) -> None:
            permission = {"action": "confirm", "message": f"same {tool}"}
            self.assertTrue(await agent._confirm_tool_call(tool, permission))
            self.assertTrue(await agent._confirm_tool_call(tool, permission))

        for tool in ("run_shell", "write_file", "edit_file"):
            with self.subTest(tool=tool):
                asyncio.run(approve_twice(tool))
        self.assertEqual(agent._confirm_dangerous.await_count, 6)


if __name__ == "__main__":
    unittest.main()
