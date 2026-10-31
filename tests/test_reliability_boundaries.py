"""Failure-injection regressions with real storage, tools and MCP subprocesses."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agents.agent import Agent
from agents.memory import MemoryPrefetch, RelevantMemory
from agents.mcp_client import McpConnection, McpToolError
from agents import session
from agents.workspace import WorkspaceContext


def answer(protocol, text="done", tokens=1):
    if protocol == "openai":
        return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": tokens, "completion_tokens": 1}}
    return NS(content=[NS(type="text", text=text)], stop_reason="end_turn",
              usage=NS(input_tokens=tokens, output_tokens=1))


def folded_memory():
    return {"episode_memory": {"task_description": "remember task", "key_events": [], "current_progress": ""},
            "working_memory": {"immediate_goal": "answer latest question", "current_challenges": "", "next_actions": []},
            "tool_memory": {"tools_used": [], "derived_rules": []}}


class RuntimeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def agent(self, **kwargs):
        options = dict(api_key="fake", api_base="https://example.test/v1", custom_system_prompt="test",
                       enable_mcp=False, enable_online_evolution=False, enable_memory_prefetch=False,
                       enable_folding=False)
        options.update(kwargs)
        agent = Agent(**options)
        agent._auto_save = Mock()
        self.addAsyncCleanup(agent.aclose)
        return agent

    async def test_direct_answers_compact_before_next_request_in_both_protocols(self):
        for protocol in ("openai", "anthropic"):
            for large_new_input in (False, True):
                with self.subTest(protocol=protocol, large_new_input=large_new_input):
                    agent = self.agent(api_protocol=protocol, enable_folding=True, context_window=32000)
                    messages = agent._openai_messages if agent.use_openai else agent._anthropic_messages
                    messages.extend([{"role": "user", "content": "old question"},
                                     {"role": "assistant", "content": "old answer"},
                                     {"role": "user", "content": "previous question"},
                                     {"role": "assistant", "content": "previous answer"}])
                    agent.last_input_token_count = 1 if large_new_input else 30000
                    agent._last_request_bytes = agent._context_request_bytes()
                    agent._generate_folded_session_memory = AsyncMock(return_value=folded_memory())
                    async def stream():
                        self.assertEqual(agent._fold_count, 1, "must fold before requesting the next answer")
                        return answer(protocol)
                    setattr(agent, "_call_" + protocol + "_stream", stream)
                    with patch("agents.agent.save_folded_session_memory"), redirect_stdout(io.StringIO()):
                        await getattr(agent, "_chat_" + protocol)("新内容" * 12000 if large_new_input else "next question")
                    self.assertEqual(agent._fold_count, 1)

    async def test_recall_is_in_first_direct_answer_request_for_both_protocols(self):
        for protocol in ("openai", "anthropic"):
            agent = self.agent(api_protocol=protocol, enable_memory_prefetch=True)
            memory = RelevantMemory("/memory/preference.md", "MEMORY_MARKER", 0, "preference")
            async def recall():
                await asyncio.sleep(0)
                return [memory]
            def prefetch(*_args):
                return MemoryPrefetch(asyncio.create_task(recall()))
            async def stream():
                messages = agent._openai_messages if agent.use_openai else agent._anthropic_messages
                self.assertIn("MEMORY_MARKER", messages[-1]["content"])
                return answer(protocol)
            setattr(agent, "_call_" + protocol + "_stream", stream)
            with patch("agents.context_runtime.start_memory_prefetch", prefetch), redirect_stdout(io.StringIO()):
                await getattr(agent, "_chat_" + protocol)("use my remembered preferences")
            self.assertIn(memory.path, agent._already_surfaced_memories)

    async def test_recall_timeout_and_caller_cancellation_clean_up_task(self):
        for cancel in (False, True):
            agent = self.agent(enable_memory_prefetch=True, memory_recall_timeout=.01 if not cancel else 5)
            started, finished = asyncio.Event(), asyncio.Event()
            async def recall():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    finished.set()
            def prefetch(*_args):
                return MemoryPrefetch(asyncio.create_task(recall()))
            with patch("agents.context_runtime.start_memory_prefetch", prefetch):
                task = asyncio.create_task(agent._recall_before_response("remember my preference"))
                await started.wait()
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    self.assertEqual(await task, "remember my preference")
                self.assertTrue(finished.is_set())
                self.assertFalse(agent._already_surfaced_memories)

    async def test_feedback_survives_error_cancel_and_truncation_on_disk(self):
        for outcome in ("error", "cancel", "length"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(session, "SESSION_DIR", Path(tmp)), redirect_stdout(io.StringIO()):
                agent = self.agent()
                agent._auto_save = Agent._auto_save.__get__(agent)
                agent._pending_skill_extraction_window = {"messages": [{"role": "user", "content": "prior task"}]}
                agent._augment_user_message_with_skill_context = lambda msg: (msg, None)
                async def turn(_msg):
                    if outcome == "error":
                        raise RuntimeError("request failed")
                    if outcome == "cancel":
                        raise asyncio.CancelledError()
                    agent.last_finish_reason = "length"
                agent._chat_openai = turn
                try:
                    await agent.chat("feedback: be concise")
                except (RuntimeError, asyncio.CancelledError):
                    pass
                stored = session.load_session(agent.session_id)
                self.assertEqual(stored["runtimeState"]["skillFeedbackOutbox"][0]["next_user_feedback"], "feedback: be concise")
                restored = self.agent()
                restored.restore_session(stored)
                self.assertEqual(len(restored._skill_feedback_outbox), 1)

    async def test_feedback_is_enqueued_independently_of_next_answer(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            agent = self.agent(enable_online_evolution=True)
            agent._background_jobs_dir = lambda: Path(tmp)
            agent._pending_skill_extraction_window = {"messages": []}
            agent._augment_user_message_with_skill_context = lambda msg: (msg, None)
            agent._chat_openai = AsyncMock(side_effect=RuntimeError("model failure"))
            agent._run_online_skill_evolution = AsyncMock(return_value={"ok": True})
            with patch.dict(os.environ, {"MAWOAGENT_AUTO_SKILL_EVOLUTION": "1"}):
                with self.assertRaises(RuntimeError):
                    await agent.chat("keep this feedback")
                await agent.drain_background_skill_tasks()
            agent._run_online_skill_evolution.assert_awaited_once()
            self.assertEqual(agent._run_online_skill_evolution.call_args.args[0]["next_user_feedback"], "keep this feedback")
            self.assertEqual(agent._skill_feedback_outbox, [])

    async def test_queue_write_failure_keeps_feedback_and_closes_coroutine(self):
        agent = self.agent(enable_online_evolution=True)
        agent._skill_feedback_outbox = [{"messages": [], "next_user_feedback": "keep"}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(agent, "_background_jobs_dir", return_value=Path(tmp)):
            with patch("agents.storage.write_json", side_effect=OSError("disk full")), \
                    patch.dict(os.environ, {"MAWOAGENT_AUTO_SKILL_EVOLUTION": "1"}):
                with self.assertRaisesRegex(OSError, "disk full"):
                    agent._flush_skill_feedback()
            self.assertEqual(len(agent._skill_feedback_outbox), 1)
            self.assertFalse(agent._background_skill_tasks)

    async def test_checkpoint_failure_blocks_actual_file_write(self):
        agent = self.agent(permission_mode="acceptEdits")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "must-not-exist.txt"
            agent._tool_checkpoints = {"write": {"name": "write_file", "status": "not_started"}}
            agent._auto_save = Mock(side_effect=OSError("disk full"))
            with self.assertRaisesRegex(OSError, "disk full"):
                await agent._run_tracked_tool("write", "write_file", {"file_path": str(target), "content": "bad"})
            self.assertFalse(target.exists())

    async def test_stream_retry_keeps_only_successful_answer_for_both_protocols(self):
        class Stream:
            def __init__(self, protocol, fail):
                self.protocol, self.fail = protocol, fail
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): pass
            async def close(self): pass
            async def get_final_message(self): return answer("anthropic", "SUCCESS")
            def __aiter__(self): return self.events()
            async def events(self):
                text = "FAILED_PARTIAL" if self.fail else "SUCCESS"
                if self.protocol == "openai":
                    yield NS(usage=None, choices=[NS(delta=NS(content=text, tool_calls=None), finish_reason=None if self.fail else "stop")])
                else:
                    yield NS(type="content_block_delta", delta=NS(text=text))
                if self.fail:
                    raise RuntimeError("ECONNRESET")
        for protocol in ("openai", "anthropic"):
            agent = self.agent(api_protocol=protocol)
            agent._turn_output_buffer = ["earlier answer\n"]
            agent._output_buffer = ["earlier answer\n"]
            client = agent._openai_client.chat.completions if protocol == "openai" else agent._anthropic_client.messages
            method = "create" if protocol == "openai" else "stream"
            factory = AsyncMock if protocol == "openai" else Mock
            with patch.object(client, method, factory(side_effect=[Stream(protocol, True), Stream(protocol, False)])), \
                    patch("agents.agent.asyncio.sleep", AsyncMock()), redirect_stdout(io.StringIO()):
                await getattr(agent, "_call_" + protocol + "_stream")()
            self.assertEqual("".join(agent._turn_output_buffer), "earlier answer\n\nSUCCESS")
            self.assertEqual(agent._output_buffer, agent._turn_output_buffer)


# Simulates an uncatchable process exit after a real filesystem side effect,
# bypassing finally/auto-save so recovery is verified against durable checkpoints.
CRASH_WORKER = r'''
import asyncio, json, os, sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from agents.agent import Agent
from agents import session
from agents.workspace import WorkspaceContext
root, protocol = Path(sys.argv[1]), sys.argv[2]
session.SESSION_DIR = root / "sessions"
async def main():
    agent = Agent(api_key="fake", api_base="https://example.test/v1", api_protocol=protocol,
                  custom_system_prompt="test", permission_mode="acceptEdits", enable_folding=False,
                  enable_mcp=False, enable_memory_prefetch=False, enable_online_evolution=False,
                  workspace=WorkspaceContext(root=Path.cwd(), workspace_only_writes=False))
    agent.session_id = "crash-case"
    agent._augment_user_message_with_skill_context = lambda text: (text, None)
    calls = [(str(i), "write_file", {"file_path": str(root / (str(i)+".txt")), "content": "written once"}) for i in range(3)]
    async def stream():
        if protocol == "openai":
            return {"choices":[{"finish_reason":"tool_calls","message":{"role":"assistant", "tool_calls":[
                {"id":id_,"type":"function","function":{"name":name,"arguments":json.dumps(inp)}} for id_,name,inp in calls]}}]}
        return NS(stop_reason="tool_use",usage=NS(input_tokens=1,output_tokens=1),
                  content=[NS(type="tool_use",id=id_,name=name,input=inp) for id_,name,inp in calls])
    setattr(agent, "_call_"+protocol+"_stream", stream)
    original = agent._run_checked_tool
    async def checked(name, inp):
        result = await original(name, inp)
        if inp["file_path"].endswith("1.txt"):
            os._exit(17)
        return result
    agent._run_checked_tool = checked
    with patch("agents.tools._check_permission_rules", return_value=None):
        await agent.chat("write three files")
asyncio.run(main())
'''


class CrashRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_file_side_effects_recover_without_automatic_reexecution(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
                result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", CRASH_WORKER, tmp, protocol],
                                                capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 17, result.stderr.decode())
                root = Path(tmp)
                stored = json.loads((root / "sessions" / "crash-case.json").read_text())
                self.assertEqual([stored["runtimeState"]["toolCheckpoints"][str(i)]["status"] for i in range(3)],
                                 ["completed", "running", "not_started"])
                agent = Agent(api_key="fake", api_base="https://example.test/v1", api_protocol=protocol,
                              custom_system_prompt="test", enable_mcp=False, enable_memory_prefetch=False,
                              enable_online_evolution=False, enable_folding=False,
                              workspace=WorkspaceContext(root=Path.cwd(), workspace_only_writes=False))
                try:
                    agent.restore_session(stored)
                    messages = agent._openai_messages if agent.use_openai else agent._anthropic_messages
                    if protocol == "openai":
                        recovered = {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}
                    else:
                        recovered = {b["tool_use_id"]: b["content"] for m in messages if isinstance(m.get("content"), list)
                                     for b in m["content"] if b.get("type") == "tool_result"}
                    self.assertIn("Successfully wrote", recovered["0"])
                    self.assertIn("status unknown", recovered["1"])
                    self.assertIn("not started", recovered["2"])
                    self.assertEqual((root / "0.txt").read_text(), "written once")
                    self.assertEqual((root / "1.txt").read_text(), "written once")
                    self.assertFalse((root / "2.txt").exists())
                    # Resume a normal answer: none of the previous tool calls are rerun.
                    agent._auto_save = Mock()
                    agent._augment_user_message_with_skill_context = lambda text: (text, None)
                    agent._execute_tool_call = AsyncMock(side_effect=AssertionError("must not replay"))
                    setattr(agent, "_call_" + protocol + "_stream", AsyncMock(return_value=answer(protocol)))
                    await agent.chat("what happened?")
                    agent._execute_tool_call.assert_not_awaited()
                finally:
                    await agent.aclose()


class McpBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_response_and_unbroken_stderr_preserve_connection(self):
        code = ('import sys,json\n'
                'for line in sys.stdin:\n'
                ' m=json.loads(line)\n'
                ' sys.stderr.write("e"*100000);sys.stderr.flush()\n'
                ' print(json.dumps({"id":m["id"],"result":{"content":[{"type":"text","text":"x"*100000}]}}),flush=True)\n')
        connection = McpConnection("large", sys.executable, ["-u", "-c", code])
        try:
            await connection.connect()
            for _ in range(2):
                result = await asyncio.wait_for(connection.call_tool("read", {}), timeout=5)
                self.assertEqual(len(result), 100000)
        finally:
            await connection.aclose()

    async def test_oversized_response_fails_fast_on_followup(self):
        code = ('import sys,json\n'
                'for line in sys.stdin:\n'
                ' m=json.loads(line)\n'
                ' print(json.dumps({"id":m["id"],"result":"x"*2000}),flush=True)\n')
        connection = McpConnection("oversize", sys.executable, ["-u", "-c", code])
        try:
            with patch("agents.mcp_client.MCP_MAX_MESSAGE_BYTES", 1024):
                await connection.connect()
                with self.assertRaisesRegex(RuntimeError, "message limit"):
                    await asyncio.wait_for(connection.call_tool("read", {}), timeout=5)
                with self.assertRaisesRegex(RuntimeError, "message limit"):
                    await asyncio.wait_for(connection.call_tool("read", {}), timeout=.1)
                self.assertEqual(connection._pending, {})
        finally:
            await connection.aclose()

    async def test_explicit_error_is_not_misclassified_as_success(self):
        connection = McpConnection("errors", "unused")
        response = {"isError": True, "content": [{"type": "text", "text": "operation refused"}]}
        connection._send_request = AsyncMock(return_value=response)
        with self.assertRaises(McpToolError) as caught:
            await connection.call_tool("write", {})
        self.assertEqual(caught.exception.result, response)

    async def test_structured_only_result_is_not_lost(self):
        connection = McpConnection("structured", "unused")
        connection._send_request = AsyncMock(return_value={"content": [], "structuredContent": {"answer": 42}})
        self.assertEqual(json.loads(await connection.call_tool("read", {})), {"answer": 42})


if __name__ == "__main__":
    unittest.main()
