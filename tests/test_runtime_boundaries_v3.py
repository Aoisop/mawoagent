from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from agents.agent import Agent
from agents.session import load_session


def call(call_id, name, arguments=None):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments or {})}}


def response(calls=None):
    message = {"role": "assistant", "content": "done"}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": message}]}


def memory():
    return {"episode_memory": {"task_description": "finish task", "key_events": [], "current_progress": "ready"},
            "working_memory": {"immediate_goal": "continue", "current_challenges": "", "next_actions": []},
            "tool_memory": {"tools_used": [], "derived_rules": []}}


class RuntimeBoundariesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("pathlib.Path.home", return_value=self.root))
        self.stack.enter_context(patch("agents.session.SESSION_DIR", self.root / "sessions"))
        self.stack.enter_context(patch("agents.skill_evolution.get_evolution_dir", return_value=self.root / "evolution"))
        self.stack.enter_context(redirect_stdout(io.StringIO()))

    def agent(self, **kwargs):
        options = dict(api_key="fake", api_base="https://example.test/v1", custom_system_prompt="test",
                       enable_mcp=False, enable_memory_prefetch=False, enable_online_evolution=False,
                       enable_folding=False)
        options.update(kwargs)
        agent = Agent(**options)
        self.addAsyncCleanup(agent.aclose)
        return agent

    async def test_tool_compaction_keeps_next_openai_request_valid_and_skips_old_round(self):
        agent = self.agent(enable_folding=True)
        agent._openai_messages.extend([
            {"role": "user", "content": "original task"}, {"role": "assistant", "content": "working"}])
        agent._generate_folded_session_memory = AsyncMock(return_value=memory())
        calls = [call("compact", "compact_context"), call("never-write", "write_file", {
            "file_path": str(self.root / "must-not-exist"), "content": "bad"})]
        count = 0

        async def stream():
            nonlocal count
            count += 1
            if count == 1:
                return response(calls)
            self.assertEqual(agent._fold_count, 1)
            self.assertFalse(any(msg.get("role") == "tool" for msg in agent._openai_messages))
            self.assertIn("folded", json.dumps(agent._openai_messages).lower())
            return response()

        agent._call_openai_stream = stream
        with patch("agents.agent.save_folded_session_memory"):
            await agent._chat_openai("compact and continue")
        self.assertEqual(count, 2)
        self.assertFalse((self.root / "must-not-exist").exists())

    async def test_skipped_results_are_added_only_for_live_calls_and_only_once(self):
        agent = self.agent()
        live, removed = call("live", "read_file"), call("removed", "read_file")
        agent._openai_messages.append({"role": "assistant", "tool_calls": [live]})
        agent._append_openai_skipped_tool_results([live, removed], "skipped")
        agent._append_openai_skipped_tool_results([live, removed], "skipped")
        self.assertEqual([m["tool_call_id"] for m in agent._openai_messages if m.get("role") == "tool"], ["live"])

    async def test_skill_fork_routes_inherited_mcp_without_owning_connection(self):
        parent = self.agent(enable_mcp=True, confirm_fn=AsyncMock(return_value=True))
        connection = NS(call_tool=AsyncMock(return_value="MCP_RESULT"), aclose=AsyncMock())
        parent._mcp_manager._connections["demo"] = connection
        parent._mcp_manager._tools = [{"name": "read", "serverName": "demo", "inputSchema": {"type": "object"}}]
        parent.tools.extend(parent._mcp_manager.get_tool_definitions())
        parent._mcp_initialized = True
        inherited = []

        async def run(child, _prompt):
            inherited.append(child)
            value = await child._run_checked_tool("mcp__demo__read", {})
            return {"text": value, "tokens": {"input": 0, "output": 0}}

        with patch("agents.skills.execute_skill", return_value={"context": "fork", "prompt": "task", "allowed_tools": ["mcp__demo__read"]}), \
                patch.object(Agent, "run_once", run), patch("agents.tools._check_permission_rules", return_value=None):
            self.assertEqual(await parent._execute_skill_tool({"skill_name": "demo"}), "MCP_RESULT")
        connection.call_tool.assert_awaited_once_with("read", {})
        connection.aclose.assert_not_awaited()
        parent.confirm_fn.assert_awaited_once()
        self.assertEqual(await parent._mcp_manager.call_tool("mcp__demo__read", {}), "MCP_RESULT")
        child = inherited[0]
        with self.assertRaisesRegex(ValueError, "not available"):
            await child._mcp_manager.call_tool("mcp__demo__other", {})
        await parent.aclose()
        connection.aclose.assert_awaited_once()

    async def test_borrowed_mcp_still_enforces_child_permissions_and_whitelist(self):
        parent = self.agent()
        definition = {"name": "mcp__demo__read", "description": "read", "input_schema": {"type": "object"}}
        parent._mcp_manager.call_tool = AsyncMock(return_value="should not run")
        child = parent._create_child_agent(system_prompt="test", tools=[definition])
        self.addAsyncCleanup(child.aclose)
        child.permission_mode = "plan"
        with patch("agents.tools._check_permission_rules", return_value=None):
            self.assertIn("denied", await child._run_checked_tool(definition["name"], {}))
            self.assertIn("not available", await child._run_checked_tool("mcp__demo__other", {}))
        parent._mcp_manager.call_tool.assert_not_awaited()

    async def test_large_checkpoint_results_are_written_once_and_recovered_as_previews(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol):
                agent = self.agent(api_protocol=protocol)
                calls = [call(str(i), "run_shell") for i in range(10)]
                if agent.use_openai:
                    agent._openai_messages.append({"role": "assistant", "tool_calls": calls})
                else:
                    agent._anthropic_messages.append({"role": "assistant", "content": [
                        {"type": "tool_use", "id": str(i), "name": "run_shell", "input": {}} for i in range(10)]})
                serializations = []
                save = agent._auto_save
                def measure(*, strict=False):
                    serializations.append(len(json.dumps(agent._tool_checkpoints)))
                    save(strict=strict)
                agent._auto_save = measure
                raw = "BEGIN\n" + "x" * (1024 * 1024) + "\nEND"
                agent._run_checked_tool = AsyncMock(return_value=raw)
                before = set((self.root / ".mawoagent" / "tool-results").glob("*.txt"))
                agent._begin_tool_round((str(i), "run_shell") for i in range(10))
                for i in range(10):
                    preview = await agent._run_tracked_tool(str(i), "run_shell", {})
                    self.assertLess(len(preview), 7000)
                after = set((self.root / ".mawoagent" / "tool-results").glob("*.txt"))
                self.assertEqual(len(after - before), 10)
                self.assertTrue(all(path.read_text() == raw for path in after - before))
                self.assertEqual(len(serializations), 21)
                self.assertLess(sum(serializations), 1024 * 1024)
                saved = load_session(agent.session_id)
                restored = self.agent(api_protocol=protocol)
                restored.restore_session(saved)
                messages = restored._openai_messages if agent.use_openai else restored._anthropic_messages
                self.assertLess(len(json.dumps(messages)), 70000)
                self.assertEqual(set((self.root / ".mawoagent" / "tool-results").glob("*.txt")), after)

    async def test_large_failure_classification_survives_preview_truncation(self):
        agent = self.agent()
        agent._run_checked_tool = AsyncMock(return_value="x" * 40000 + "\nError: failed at end")
        agent._begin_tool_round([("failed", "run_shell")])
        preview = await agent._run_tracked_tool("failed", "run_shell", {})
        self.assertNotIn("failed at end", preview)
        self.assertTrue(agent._tracked_tool_failed("failed", "run_shell", preview))

    async def test_artifact_failure_never_marks_unknown_side_effect_completed(self):
        agent = self.agent()
        agent._run_checked_tool = AsyncMock(return_value="x" * 40000)
        agent._begin_tool_round([("write", "write_file")])
        with patch("agents.result_storage.atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                await agent._run_tracked_tool("write", "write_file", {})
        stored = load_session(agent.session_id)
        self.assertEqual(stored["runtimeState"]["toolCheckpoints"]["write"]["status"], "running")

    async def test_legacy_large_checkpoints_are_bounded_on_recovery(self):
        agent = self.agent()
        agent._openai_messages.append({"role": "assistant", "tool_calls": [call("old", "run_shell")]})
        agent._tool_checkpoints = {"old": {"name": "run_shell", "status": "completed", "result": "legacy" * 10000}}
        messages, journal = deepcopy(agent._openai_messages), deepcopy(agent._tool_checkpoints)
        agent._recover_tool_checkpoints()
        self.assertLess(len(agent._openai_messages[-1]["content"]), 7000)
        self.assertIn("result_file", agent._tool_checkpoints["old"])
        artifact = Path(agent._tool_checkpoints["old"]["result_file"])
        recovered = deepcopy(agent._openai_messages)
        # Restoring the unchanged old session again must not create another
        # full artifact, even if the previous migration was never saved.
        agent._openai_messages, agent._tool_checkpoints = deepcopy(messages), deepcopy(journal)
        with patch("agents.result_storage.atomic_write_text") as write:
            agent._recover_tool_checkpoints()
            write.assert_not_called()
        self.assertEqual(agent._openai_messages, recovered)
        artifact.write_text("corrupt artifact")
        agent._openai_messages, agent._tool_checkpoints = messages, journal
        agent._recover_tool_checkpoints()
        self.assertEqual(artifact.read_text(), "legacy" * 10000)
        self.assertEqual(len(list(artifact.parent.glob("*.txt"))), 1)


if __name__ == "__main__":
    unittest.main()
