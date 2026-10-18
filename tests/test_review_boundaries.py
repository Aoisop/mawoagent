from __future__ import annotations

import asyncio
from contextlib import contextmanager, ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agents.agent import Agent
from agents.main import run_repl
from agents.mcp_client import McpManager
from agents.online_skill_evolution import OnlineSkillCandidate, maintain_online_skill_candidate
from agents.session_memory import parse_folded_memory
from agents.tools import check_permission, execute_tool


def memory_fixture():
    return {
        "episode_memory": {"task_description": "Fix the parser", "key_events": [], "current_progress": ""},
        "working_memory": {"immediate_goal": "Run tests", "current_challenges": "", "next_actions": []},
        "tool_memory": {"tools_used": [], "derived_rules": []},
    }


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class ReviewBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def agent(self, **kwargs):
        options = dict(api_base="http://example.test/v1", api_key="fake", custom_system_prompt="test",
                       enable_mcp=False, enable_memory_prefetch=False, enable_online_evolution=False)
        options.update(kwargs)
        agent = Agent(**options)
        agent._auto_save = Mock()
        self.addAsyncCleanup(agent.aclose)
        return agent

    async def test_absolute_write_creates_exact_destination_and_tracks_actual_path(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp), \
                patch("agents.tools._auto_update_memory_index"):
            destination = Path(tmp) / "new" / "nested" / "file.txt"
            reads = {}
            result = await execute_tool("write_file", {"file_path": str(destination), "content": "correct"}, reads)
            self.assertFalse(result.startswith("Error"), result)
            self.assertEqual(destination.read_text(), "correct")
            self.assertFalse((Path(tmp) / "file.txt").exists())
            self.assertEqual(set(reads), {str(destination.resolve())})

    async def test_missing_absolute_read_and_edit_do_not_touch_same_named_local_file(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            local = Path(tmp) / "file.txt"
            local.write_text("original")
            missing = Path(tmp) / "missing" / "file.txt"
            for name, extra in (("read_file", {}), ("edit_file", {"old_string": "original", "new_string": "changed"})):
                result = await execute_tool(name, {"file_path": str(missing), **extra})
                self.assertTrue(result.startswith("Error"), result)
                self.assertEqual(local.read_text(), "original")
                self.assertFalse(missing.exists())

    async def test_plan_write_uses_authorized_absolute_file(self):
        agent = self.agent(permission_mode="plan")
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp), \
                patch("agents.tools._check_permission_rules", return_value=None), \
                patch("agents.tools._auto_update_memory_index"):
            plan = Path(tmp) / "plans" / "new-plan.md"
            agent._plan_file_path = str(plan)
            inp = {"file_path": str(plan), "content": "Proposed change"}
            self.assertTrue(await agent.authorize_operation("write_file", inp))
            await agent._execute_tool_call("write_file", inp)
            self.assertEqual(plan.read_text(), "Proposed change")
            self.assertFalse((Path(tmp) / plan.name).exists())

    async def test_invalid_folding_preserves_entire_history_for_both_backends_and_modes(self):
        for protocol in ("openai", "anthropic"):
            for mode in ("single_json", "three_parallel"):
                agent = self.agent(api_protocol=protocol, fold_memory_mode=mode, fold_observer=Mock())
                messages = [
                    {"role": "user", "content": "start" + "x" * 5000 + "CRITICAL MIDDLE" + "y" * 5000 + "end"},
                    {"role": "assistant", "content": "working"},
                    {"role": "user", "content": "continue"},
                    {"role": "assistant", "content": "pending"},
                ]
                attr = "_openai_messages" if agent.use_openai else "_anthropic_messages"
                getattr(agent, attr).extend(messages)
                original = deepcopy(getattr(agent, attr))
                agent.last_input_token_count = 4321
                for response in ("{}", "[]", "not json", RuntimeError("truncated response")):
                    with self.subTest(protocol=protocol, mode=mode, response=str(response)), \
                            patch.object(agent, "_build_side_query") as factory, \
                            patch("agents.agent.save_folded_session_memory") as save:
                        factory.return_value = AsyncMock(
                            **({"side_effect": response} if isinstance(response, Exception) else {"return_value": response}))
                        self.assertFalse(await agent._compact_conversation())
                        self.assertEqual(getattr(agent, attr), original)
                        self.assertEqual(agent.last_input_token_count, 4321)
                        self.assertEqual(agent._fold_count, 0)
                        self.assertEqual(agent._folded_session_memories, [])
                        agent.fold_observer.assert_not_called()
                        save.assert_not_called()
                        self.assertTrue(factory.call_args.kwargs["require_complete"])
                with patch.object(agent, "_build_side_query", return_value=None):
                    self.assertFalse(await agent._compact_conversation())
                    self.assertEqual(getattr(agent, attr), original)

    async def test_valid_folding_still_compacts_both_backends(self):
        for protocol in ("openai", "anthropic"):
            for mode in ("single_json", "three_parallel"):
                with self.subTest(protocol=protocol, mode=mode):
                    agent = self.agent(api_protocol=protocol, fold_memory_mode=mode)
                    attr = "_openai_messages" if agent.use_openai else "_anthropic_messages"
                    getattr(agent, attr).extend([{"role": role, "content": "task"} for role in ("user", "assistant") * 2])
                    memory = memory_fixture()
                    responses = [json.dumps(value) for value in memory.values()] if mode == "three_parallel" else [json.dumps(memory)]
                    with patch.object(agent, "_build_side_query", return_value=AsyncMock(side_effect=responses)), \
                            patch("agents.agent.save_folded_session_memory") as save:
                        self.assertTrue(await agent._compact_conversation())
                        self.assertEqual(agent._fold_count, 1)
                        self.assertEqual(len(getattr(agent, attr)), 2 if agent.use_openai else 1)
                        save.assert_called_once()

    def test_memory_schema_rejects_missing_wrong_types_and_empty_task_state(self):
        malformed = [{}, {"episode_memory": {}}]
        for section, field, value in (("episode_memory", "task_description", 123),
                                      ("working_memory", "next_actions", {}),
                                      ("tool_memory", "derived_rules", [123]),
                                      ("episode_memory", "key_events", [{}])):
            item = memory_fixture()
            item[section][field] = value
            malformed.append(item)
        empty = memory_fixture()
        empty["episode_memory"]["task_description"] = " "
        empty["working_memory"]["immediate_goal"] = ""
        malformed.append(empty)
        for item in malformed:
            with self.subTest(item=item), self.assertRaises(ValueError):
                parse_folded_memory(json.dumps(item))
        self.assertEqual(parse_folded_memory(json.dumps(memory_fixture())), memory_fixture())

    async def test_online_skill_writes_obey_current_mode_and_deny_rules(self):
        agent = self.agent(permission_mode="acceptEdits", confirm_fn=AsyncMock(return_value=True))
        cases = [("acceptEdits", "deny", False), ("plan", "allow", False), ("dontAsk", None, False),
                 ("acceptEdits", None, True), ("default", None, None), ("default", "allow", True)]
        for interactive in (False, True):
            callback = agent._confirm_online_skill_write if interactive else agent._confirm_background_online_skill_write
            for action in ("add", "merge"):
                for mode, rule, allowed in cases:
                    allowed = interactive if allowed is None else allowed
                    with self.subTest(interactive=interactive, action=action, mode=mode, rule=rule), \
                            tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                        root = Path(tmp)
                        skill = root / "skills" / "demo" / "SKILL.md"
                        original = "---\nname: demo\ndescription: Demo skill\nversion: 0.1.0\n---\nOriginal guidance\n"
                        if action == "merge":
                            skill.parent.mkdir(parents=True)
                            skill.write_text(original)
                        stack.enter_context(patch("agents.skills.discover_skills", return_value=[]))
                        stack.enter_context(patch("agents.skills.retrieve_relevant_skills", return_value=[]))
                        stack.enter_context(patch("agents.skills.get_skill_by_name", return_value=None))
                        stack.enter_context(patch("agents.skills.reset_skill_cache"))
                        stack.enter_context(patch("agents.skill_evolution.resolve_skill_file", return_value=skill if action == "merge" else None))
                        stack.enter_context(patch("agents.skill_evolution._skills_root", return_value=root / "skills"))
                        stack.enter_context(patch("agents.skill_evolution.get_evolution_dir", return_value=root / "evolution"))
                        stack.enter_context(patch("agents.tools._check_permission_rules", return_value=rule))
                        agent.permission_mode = mode
                        result = await maintain_online_skill_candidate(
                            candidate=OnlineSkillCandidate(name="demo", description="Reusable lesson", instructions="New guidance"),
                            side_query=AsyncMock(return_value=json.dumps({"action": action, "target_skill": "demo", "merged_instructions": "New guidance"})),
                            confirm_write=callback,
                        )
                        self.assertEqual(result["ok"], allowed, result)
                        if action == "merge":
                            self.assertEqual(skill.read_text() != original, allowed)
                        else:
                            self.assertEqual(skill.exists(), allowed)

    async def test_online_skill_writer_requires_authorizer(self):
        with patch("agents.skills.discover_skills", return_value=[]), \
                patch("agents.skills.retrieve_relevant_skills", return_value=[]), \
                patch("agents.skills.create_skill") as create:
            result = await maintain_online_skill_candidate(
                candidate=OnlineSkillCandidate("demo", "description"),
                side_query=AsyncMock(return_value='{"action":"add"}'))
            self.assertEqual(result["action"], "add_denied")
            create.assert_not_called()

    async def test_repl_skill_commands_apply_rules_and_confirmations(self):
        agent = self.agent()
        for mode, rule, answer, allowed in (("plan", None, None, False), ("dontAsk", None, None, False),
                                          ("acceptEdits", "deny", None, False), ("acceptEdits", None, None, True),
                                          ("default", None, "n", False), ("default", None, "y", True)):
            commands = []
            for command in ("/skill-create demo | description | when | instructions", "/skill-evolve demo durable lesson"):
                commands.append(command)
                if answer:
                    commands.append(answer)
            commands.append("quit")
            agent.permission_mode = mode
            with self.subTest(mode=mode, rule=rule, answer=answer), \
                    patch("agents.tools._check_permission_rules", return_value=rule), \
                    patch("agents.main._read_repl_input", new=AsyncMock(side_effect=commands)), \
                    patch("agents.main.signal.signal"), patch.object(agent, "aclose", new=AsyncMock()), \
                    patch("agents.main.create_skill", return_value={"ok": True}) as create, \
                    patch("agents.main.evolve_skill", return_value={"ok": True}) as evolve, redirect_stdout(io.StringIO()):
                await run_repl(agent)
                self.assertEqual(create.call_count, int(allowed))
                self.assertEqual(evolve.call_count, int(allowed))

    async def test_named_skill_rules_do_not_deny_unrelated_skills(self):
        rules = {"allow": [], "deny": [{"tool": "skill_create", "pattern": "private*"}]}
        with patch("agents.tools.load_permission_rules", return_value=rules):
            self.assertEqual(check_permission("skill_create", {"name": "private-demo"}, "acceptEdits")["action"], "deny")
            self.assertEqual(check_permission("skill_create", {"name": "public-demo"}, "acceptEdits")["action"], "allow")

    async def test_missing_or_invalid_plan_approval_never_exits_plan_mode(self):
        agent = self.agent(permission_mode="plan")
        for response in (None, {}, {"choice": "cancel"}, {"choice": "unknown"}, {"choice": []}):
            agent._aborted = False
            agent._pre_plan_mode = "acceptEdits"
            agent._plan_approval_fn = AsyncMock(return_value=response) if response is not None else None
            with self.subTest(response=response):
                result = await agent._execute_plan_mode_tool("exit_plan_mode")
                self.assertEqual(agent.permission_mode, "plan")
                self.assertEqual(agent._pre_plan_mode, "acceptEdits")
                self.assertTrue(agent._aborted)
                self.assertIn("no implementation authorized", result)

    async def test_repl_eof_during_approval_does_not_allow_following_write(self):
        agent = self.agent(permission_mode="plan")
        agent._pre_plan_mode = "acceptEdits"
        # Use an async backend so the approval itself runs inside the real chat task.
        async def backend(_):
            result = await agent._execute_plan_mode_tool("exit_plan_mode")
            self.assertIn("cancelled", result)
            self.assertFalse(await agent.authorize_operation("write_file", {"file_path": "blocked.txt"}))
        agent._chat_openai = backend
        with patch("agents.main._read_repl_input", new=AsyncMock(side_effect=["task", EOFError(), EOFError()])), \
                patch("agents.main.signal.signal"), patch("agents.tools._check_permission_rules", return_value=None), \
                patch.object(agent, "_augment_user_message_with_skill_context", return_value=("task", None)), \
                redirect_stdout(io.StringIO()):
            await run_repl(agent)
        self.assertTrue(agent._aborted)
        self.assertEqual(agent.permission_mode, "plan")

    async def test_explicit_plan_approval_still_executes(self):
        agent = self.agent(permission_mode="plan")
        for choice, expected in (("execute", "acceptEdits"), ("clear-and-execute", "acceptEdits"), ("manual-execute", "default")):
            agent.permission_mode = "plan"
            agent._pre_plan_mode = "default"
            agent._plan_approval_fn = AsyncMock(return_value={"choice": choice})
            result = await agent._execute_plan_mode_tool("exit_plan_mode")
            self.assertIn("User approved", result)
            self.assertEqual(agent.permission_mode, expected)

    async def test_cancelled_plan_skips_remaining_tools_and_keeps_resumable_history(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as tmp, \
                    patch("agents.tools._check_permission_rules", return_value=None), redirect_stdout(io.StringIO()):
                agent = self.agent(api_protocol=protocol, permission_mode="plan", enable_folding=False)
                plan = Path(tmp) / "plan.md"
                agent._plan_file_path = str(plan)
                agent._plan_approval_fn = AsyncMock(return_value={"choice": "cancel"})
                calls = [("approval", "exit_plan_mode", {}),
                         ("write", "write_file", {"file_path": str(plan), "content": "must not run"})]
                if agent.use_openai:
                    response = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [
                        {"id": id_, "type": "function", "function": {"name": name, "arguments": json.dumps(inp)}}
                        for id_, name, inp in calls]}}]}
                    final = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}]}
                else:
                    response = SimpleNamespace(stop_reason="tool_use", usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                        content=[SimpleNamespace(type="tool_use", id=id_, name=name, input=inp) for id_, name, inp in calls])
                    final = SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                                            content=[SimpleNamespace(type="text", text="done")])
                stream = AsyncMock(side_effect=[response, final])
                setattr(agent, "_call_" + protocol + "_stream", stream)
                with patch.object(agent, "_augment_user_message_with_skill_context", return_value=("task", None)):
                    await agent.chat("task")
                    self.assertFalse(plan.exists())
                    self.assertTrue(agent._aborted)
                    await agent.chat("revise plan")
                self.assertEqual(stream.await_count, 2)
                if agent.use_openai:
                    result_ids = {m.get("tool_call_id") for m in agent._openai_messages if m.get("role") == "tool"}
                else:
                    result_ids = set().union(*(agent._anthropic_tool_result_ids(m) for m in agent._anthropic_messages))
                self.assertEqual(result_ids, {"approval", "write"})

    async def test_mcp_startup_permissions_precede_real_process_launch(self):
        cases = [("plan", None, True, False), ("dontAsk", None, True, False),
                 ("default", None, False, False), ("acceptEdits", "deny", True, False),
                 ("default", None, True, True), ("bypassPermissions", None, False, True)]
        for mode, rule, approval, allowed in cases:
            with self.subTest(mode=mode, rule=rule), tempfile.TemporaryDirectory() as tmp, \
                    patch("agents.tools._check_permission_rules", return_value=rule), redirect_stdout(io.StringIO()):
                marker = Path(tmp) / "started"
                code = (
                    "import sys,json\nfrom pathlib import Path\n"
                    f"Path({str(marker)!r}).touch()\n"
                    "for line in sys.stdin:\n"
                    " m=json.loads(line)\n"
                    " if 'id' in m: print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'tools':[]}}),flush=True)\n"
                )
                agent = self.agent(permission_mode=mode, confirm_fn=AsyncMock(return_value=approval))
                manager = agent._mcp_manager
                with patch.object(manager, "_load_configs", return_value={"demo": {"command": sys.executable, "args": ["-c", code]}}):
                    await asyncio.wait_for(manager.load_and_connect(authorize_start=agent.authorize_operation), 3)
                self.assertEqual(marker.exists(), allowed)
                self.assertEqual(bool(manager._connections), allowed)
                processes = [connection._process for connection in manager._connections.values()]
                await manager.disconnect_all()
                self.assertTrue(all(process.returncode is not None for process in processes))

    async def test_mcp_manager_without_authorizer_never_launches(self):
        manager = McpManager()
        with patch.object(manager, "_load_configs", return_value={"demo": {"command": "never-run"}}), \
                patch("agents.mcp_client.McpConnection.connect", new=AsyncMock()) as connect:
            await manager.load_and_connect()
            connect.assert_not_awaited()

    async def test_plan_chat_defers_mcp_initialization_until_execution_mode(self):
        agent = self.agent(permission_mode="plan", enable_mcp=True)
        agent._chat_openai = AsyncMock()
        with patch.object(agent._mcp_manager, "load_and_connect", new=AsyncMock()) as load, \
                patch.object(agent, "_augment_user_message_with_skill_context", return_value=("task", None)):
            await agent.chat("task")
            load.assert_not_awaited()
            self.assertFalse(agent._mcp_initialized)
            agent.permission_mode = "default"
            await agent.chat("task")
            load.assert_awaited_once_with(authorize_start=agent.authorize_operation)

    async def test_skill_eval_waits_past_shutdown_timeout_without_cancelling_learning(self):
        agent = self.agent(shutdown_timeout=.01)
        completed = asyncio.Event()
        async def learn():
            await asyncio.sleep(.06)
            completed.set()
        task = asyncio.create_task(learn())
        agent._background_skill_tasks.add(task)
        async def evaluate(**kwargs):
            self.assertTrue(completed.is_set())
            self.assertFalse(task.cancelled())
            return "evaluation complete"
        with patch("agents.main._read_repl_input", new=AsyncMock(side_effect=["/skill-eval", "quit"])), \
                patch("agents.main.signal.signal"), patch.object(agent, "_build_side_query", return_value=None), \
                patch("agents.main.format_online_skill_eval_async", side_effect=evaluate) as report, redirect_stdout(io.StringIO()):
            await asyncio.wait_for(run_repl(agent), 2)
            report.assert_awaited_once()

    async def test_cancelling_evaluation_wait_does_not_cancel_background_task(self):
        agent = self.agent(shutdown_timeout=.01)
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        agent._background_skill_tasks.add(task)
        waiter = asyncio.create_task(agent.drain_background_skill_tasks())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(task.cancelled())
        release.set()
        await asyncio.wait_for(task, 1)


if __name__ == "__main__":
    unittest.main()
