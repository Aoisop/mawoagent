from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import io
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agents.agent import Agent
from agents.tool_results import compact_tool_results, SNIP_PLACEHOLDER, OLD_RESULT_PLACEHOLDER


def history(use_openai, calls):
    """Build protocol histories with equivalent tool outcomes and user text."""
    messages = [{"role": "user", "content": "Keep this user instruction"}]
    for index, (name, args, result) in enumerate(calls):
        if use_openai:
            messages.extend([
                {"role": "assistant", "content": "working", "tool_calls": [{"id": str(index), "type": "function",
                    "function": {"name": name, "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": str(index), "content": result},
            ])
        else:
            messages.extend([
                {"role": "assistant", "content": [{"type": "tool_use", "id": str(index), "name": name, "input": args}]},
                {"role": "user", "content": [{"type": "text", "text": "Keep this annotation"},
                    {"type": "tool_result", "tool_use_id": str(index), "content": result}]},
            ])
    return messages


def results(messages, use_openai):
    if use_openai:
        return [message["content"] for message in messages if message.get("role") == "tool"]
    return [block["content"] for message in messages if isinstance(message.get("content"), list)
            for block in message["content"] if block.get("type") == "tool_result"]


class ToolResultCompressionTests(unittest.TestCase):
    def test_large_outputs_keep_both_ends_without_modifying_other_messages(self):
        for protocol in (True, False):
            for utilization, maximum in ((0.5, 30000), (0.8, 15000)):
                with self.subTest(openai=protocol, utilization=utilization):
                    messages = history(protocol, [("read_file", {}, "BEGIN" + "x" * 40000 + "END")])
                    original = deepcopy(messages)
                    compact_tool_results(messages, use_openai=protocol, utilization=utilization)
                    value = results(messages, protocol)[0]
                    self.assertTrue(value.startswith("BEGIN"))
                    self.assertTrue(value.endswith("END"))
                    self.assertLessEqual(len(value), maximum)
                    self.assertEqual(messages[0], original[0])
                    self.assertEqual(messages[1], original[1])
                    if not protocol:
                        self.assertEqual(messages[2]["content"][0], original[2]["content"][0])

    def test_low_pressure_preserves_results_and_non_text_blocks(self):
        for protocol in (True, False):
            messages = history(protocol, [("read_file", {}, "x" * 40000),
                                          ("custom", {}, [{"type": "image", "data": "image"}])])
            original = deepcopy(messages)
            compact_tool_results(messages, use_openai=protocol, utilization=.49, idle_seconds=299)
            self.assertEqual(messages, original)

    def test_stale_results_retain_recent_history_with_protocol_specific_policy(self):
        calls = [("custom", {}, "custom result"),
                 ("read_file", {"file_path": "old.py"}, "old read"),
                 ("read_file", {"file_path": "repeat.py"}, "superseded read"),
                 ("read_file", {"file_path": "repeat.py"}, "latest read"),
                 ("list_files", {}, "listing")]
        openai_messages, anthropic_messages = history(True, calls), history(False, calls)
        compact_tool_results(openai_messages, use_openai=True, utilization=.61)
        compact_tool_results(anthropic_messages, use_openai=False, utilization=.61)
        self.assertEqual(results(openai_messages, True),
                         [SNIP_PLACEHOLDER, SNIP_PLACEHOLDER, "superseded read", "latest read", "listing"])
        self.assertEqual(results(anthropic_messages, False),
                         ["custom result", SNIP_PLACEHOLDER, SNIP_PLACEHOLDER, "latest read", "listing"])

    def test_idle_cleanup_preserves_last_three_and_is_idempotent(self):
        for protocol in (True, False):
            messages = history(protocol, [("custom", {}, f"result {i}") for i in range(5)])
            compact_tool_results(messages, use_openai=protocol, utilization=.1, idle_seconds=300)
            self.assertEqual(results(messages, protocol),
                             [OLD_RESULT_PLACEHOLDER, OLD_RESULT_PLACEHOLDER, "result 2", "result 3", "result 4"])
            once = deepcopy(messages)
            compact_tool_results(messages, use_openai=protocol, utilization=.1, idle_seconds=300)
            self.assertEqual(messages, once)


class ChildLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def agent(self, **kwargs):
        agent = Agent(api_base="https://example.test/v1", api_key="fake", custom_system_prompt="test",
                      enable_mcp=False, enable_memory_prefetch=False, enable_online_evolution=False, **kwargs)
        self.addAsyncCleanup(agent.aclose)
        return agent

    async def test_both_child_entrypoints_preserve_permissions_and_limits(self):
        parent = self.agent(permission_mode="plan", max_cost_usd=2, max_turns=5,
                            max_output_tokens=512, context_window=8000)
        parent.current_turns = 2
        parent.total_input_tokens = 1000
        children = []
        inherited_toolsets = []
        original_factory = parent._create_child_agent

        def factory(**kwargs):
            child = original_factory(**kwargs)
            children.append(child)
            inherited_toolsets.append({tool["name"] for tool in child.tools})
            async def run(_prompt):
                self.assertIs(child._budget_parent, parent)
                self.assertEqual(child.permission_mode, "plan")
                self.assertEqual(child.max_output_tokens, 512)
                self.assertEqual(child.context_window, 8000)
                self.assertEqual(child.max_turns, 3)
                self.assertAlmostEqual(child.max_cost_usd, 1.997)
                return {"text": "done", "tokens": {"input": 0, "output": 0}}
            child.run_once = run
            return child

        with patch.object(parent, "_create_child_agent", side_effect=factory), \
                patch("agents.skills.execute_skill", return_value={"context": "fork", "allowed_tools": [], "prompt": "skill task"}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(await parent._execute_agent_tool({"type": "general", "prompt": "task"}), "done")
            self.assertEqual(await parent._execute_skill_tool({"skill_name": "demo"}), "done")
        self.assertEqual(inherited_toolsets[1], set())
        for toolset in inherited_toolsets:
            self.assertTrue(toolset.isdisjoint({"agent", "enter_plan_mode", "exit_plan_mode"}))
        self.assertEqual(parent._active_children, [])
        self.assertTrue(all(child._budget_parent is None for child in children))

    async def test_child_constructor_failure_closes_ui_lifecycle(self):
        parent = self.agent()
        with patch.object(parent, "_create_child_agent", side_effect=ValueError("invalid child config")), \
                patch("agents.agent.print_sub_agent_start") as started, patch("agents.agent.print_sub_agent_end") as ended:
            result = await parent._execute_agent_tool({"prompt": "task"})
        self.assertIn("invalid child config", result)
        started.assert_called_once()
        ended.assert_called_once()
        self.assertEqual(parent._active_children, [])

    async def test_clear_history_reuses_context_reset_without_clearing_budget(self):
        parent = self.agent()
        parent.total_input_tokens, parent.current_turns = 100, 3
        parent._openai_messages.append({"role": "user", "content": "old task"})
        parent._tool_checkpoints = {"tool": {"status": "completed"}}
        parent._skill_feedback_outbox = [{"feedback": "old"}]
        with redirect_stdout(io.StringIO()):
            parent.clear_history()
        self.assertEqual(parent._openai_messages, [{"role": "system", "content": parent._system_prompt}])
        self.assertEqual((parent.total_input_tokens, parent.current_turns), (100, 3))
        self.assertEqual(parent._tool_checkpoints, {})
        self.assertEqual(parent._skill_feedback_outbox, [])


if __name__ == "__main__":
    unittest.main()
