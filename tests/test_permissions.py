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

    def test_missing_confirmation_channel_denies_instead_of_blocking(self) -> None:
        with patch("agents.agent.build_system_prompt", return_value="test system prompt"):
            agent = Agent(api_base="http://example.test/v1", api_key="test")
        with patch("agents.agent.sys.stdin") as stdin:
            stdin.isatty.return_value = False
            allowed = asyncio.run(agent._confirm_dangerous("write file: notes.txt"))
        self.assertFalse(allowed)

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


class DenyRuleTests(unittest.TestCase):
    """A deny rule is a decision about what may happen, not about whether to ask."""

    def test_a_deny_rule_survives_bypass_permissions(self) -> None:
        with patch("agents.tools._check_permission_rules", return_value="deny"):
            for mode in ("default", "acceptEdits", "dontAsk", "bypassPermissions"):
                with self.subTest(mode=mode):
                    verdict = check_permission("run_shell", {"command": "rm -rf /"}, mode)
                    self.assertEqual(verdict["action"], "deny")

    def test_discovery_results_still_honour_deny_under_bypass(self) -> None:
        from pathlib import Path

        from agents.tools import _discovery_path_filter

        rules = {"allow": [], "deny": [{"tool": "read_file", "pattern": "*"}]}
        with patch("agents.tools.load_permission_rules", return_value=rules):
            allowed = _discovery_path_filter("grep_search", "bypassPermissions")
        self.assertFalse(allowed(Path("secrets.txt")))

    def test_a_glob_in_the_middle_of_a_command_rule_matches(self) -> None:
        rules = {"allow": [], "deny": [{"tool": "run_shell", "pattern": "*rm -rf*"}]}
        with patch("agents.tools.load_permission_rules", return_value=rules):
            for command in ("rm -rf /", "cd / && rm -rf /", "true; rm -rf ~"):
                with self.subTest(command=command):
                    verdict = check_permission("run_shell", {"command": command}, "default")
                    self.assertEqual(verdict["action"], "deny")
            self.assertEqual(check_permission("run_shell", {"command": "ls"}, "default")["action"], "confirm")


class DeferredToolScopeTests(unittest.TestCase):
    """Activation belongs to one agent, not to the process."""

    def test_one_agents_search_does_not_widen_another(self) -> None:
        from agents import tools as tools_module

        first: set[str] = set()
        second: set[str] = set()

        tools_module._tool_search({"query": "plan"}, first)

        deferred = {t["name"] for t in tools_module.tool_definitions if t.get("deferred")}
        names_first = {t["name"] for t in tools_module.get_active_tool_definitions(tools_module.tool_definitions, first)}
        names_second = {t["name"] for t in tools_module.get_active_tool_definitions(tools_module.tool_definitions, second)}
        self.assertTrue(deferred & names_first)
        self.assertFalse(deferred & names_second)


if __name__ == "__main__":
    unittest.main()
