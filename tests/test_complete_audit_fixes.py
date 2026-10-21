from __future__ import annotations

import asyncio
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agents.agent import Agent
from agents.workspace import WorkspaceContext
from agents.main import run_repl
from agents import memory, online_skill_eval as ev, skill_candidates as candidates
from agents import skill_evolution, skills, tools, project_trust, maintenance
from agents.provenance_reader import load_recent_sources
from agents.online_skill_evolution import OnlineSkillCandidate, maintain_online_skill_candidate
from agents.validation import validate_json


def make_agent(**kwargs):
    options = dict(api_base="http://example.test/v1", api_key="fake", custom_system_prompt="test",
                   enable_mcp=False, enable_memory_prefetch=False, enable_online_evolution=False,
                   enable_folding=False, permission_mode="acceptEdits",
                   # These cases exercise other behaviour and write into temporary
                   # directories, so they opt out of the workspace range here.
                   workspace=WorkspaceContext(root=Path.cwd(), workspace_only_writes=False))
    options.update(kwargs)
    agent = Agent(**options)
    agent._auto_save = Mock()
    return agent


def response(protocol, calls=(), finish=None):
    if protocol == "openai":
        return {"choices": [{"finish_reason": finish or ("tool_calls" if calls else "stop"), "message": {
            "role": "assistant", "content": None if calls else "done",
            "tool_calls": [{"type": "function", "id": str(i), "function": {"name": name,
                "arguments": inp if isinstance(inp, str) else json.dumps(inp)}} for i, (name, inp) in enumerate(calls)] or None}}]}
    blocks = [SimpleNamespace(type="tool_use", id=str(i), name=name, input=inp) for i, (name, inp) in enumerate(calls)]
    return SimpleNamespace(stop_reason=finish or ("tool_use" if calls else "end_turn"),
        content=blocks or [SimpleNamespace(type="text", text="done")], usage=SimpleNamespace(input_tokens=1, output_tokens=1))


class CompleteAuditRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def agent(self, **kwargs):
        agent = make_agent(**kwargs)
        self.addAsyncCleanup(agent.aclose)
        return agent

    async def run_calls(self, agent, protocol, calls, finish=None):
        setattr(agent, "_call_" + protocol + "_stream", AsyncMock(side_effect=[response(protocol, calls, finish), response(protocol)]))
        with patch("agents.tools._check_permission_rules", return_value=None), redirect_stdout(io.StringIO()):
            await agent.chat("task")

    async def test_permission_is_checked_after_each_mode_change_for_both_backends(self):
        for protocol in ("openai", "anthropic"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as tmp:
                agent = self.agent(api_protocol=protocol)
                agent._generate_plan_file_path = lambda: str(Path(tmp) / "plan.md")
                target = Path(tmp) / "blocked.txt"
                await self.run_calls(agent, protocol, [("enter_plan_mode", {}),
                    ("write_file", {"file_path": str(target), "content": "must not write"})])
                self.assertEqual(agent.permission_mode, "plan")
                self.assertFalse(target.exists())

    async def test_truncated_responses_skip_tools_and_preserve_finish_reason(self):
        for protocol, finish in (("openai", "length"), ("anthropic", "max_tokens")):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as tmp:
                agent = self.agent(api_protocol=protocol)
                target = Path(tmp) / "blocked.txt"
                await self.run_calls(agent, protocol, [("write_file", {"file_path": str(target), "content": "no"})], finish)
                self.assertFalse(target.exists())
                self.assertEqual(agent.last_finish_reason, finish)
                history = agent._openai_messages if agent.use_openai else agent._anthropic_messages
                self.assertIn("incomplete model response", str(history[-1]))

    async def test_malformed_tool_arguments_are_recoverable_and_valid_next_tool_runs(self):
        for protocol in ("openai", "anthropic"):
            for malformed in ([], None, {"file_path": 123, "content": "bad"}, {"file_path": "missing-content"}):
                with self.subTest(protocol=protocol, malformed=malformed), tempfile.TemporaryDirectory() as tmp:
                    agent = self.agent(api_protocol=protocol)
                    target = Path(tmp) / "valid.txt"
                    await self.run_calls(agent, protocol, [("write_file", malformed),
                        ("write_file", {"file_path": str(target), "content": "valid"})])
                    self.assertEqual(target.read_text(), "valid")
                    history = agent._openai_messages if agent.use_openai else agent._anthropic_messages
                    self.assertIn("invalid arguments", str(history))

    async def test_one_allowed_tool_round_executes_for_both_backends(self):
        for protocol in ("openai", "anthropic"):
            with tempfile.TemporaryDirectory() as tmp:
                agent = self.agent(api_protocol=protocol, max_turns=1)
                target = Path(tmp) / "one.txt"
                await self.run_calls(agent, protocol, [("write_file", {"file_path": str(target), "content": "one"})])
                self.assertEqual(target.read_text(), "one")
                self.assertEqual(agent.current_turns, 1)

    async def test_mcp_confirmations_are_not_reused_and_include_arguments(self):
        agent = self.agent(permission_mode="default", confirm_fn=AsyncMock(side_effect=[True, False]))
        with patch("agents.tools._check_permission_rules", return_value=None), redirect_stdout(io.StringIO()):
            first = tools.check_permission("mcp__demo__exec", {"command": "first"})
            second = tools.check_permission("mcp__demo__exec", {"command": "second"})
            self.assertTrue(await agent._confirm_tool_call("mcp__demo__exec", first))
            self.assertFalse(await agent._confirm_tool_call("mcp__demo__exec", second))
        self.assertEqual(agent.confirm_fn.await_count, 2)
        self.assertIn("second", second["message"])

    async def test_permission_change_during_confirmation_is_respected(self):
        agent = self.agent(permission_mode="default")
        async def confirm(_):
            agent.permission_mode = "plan"
            return True
        agent.confirm_fn = confirm
        with tempfile.TemporaryDirectory() as tmp, patch("agents.tools._check_permission_rules", return_value=None):
            target = Path(tmp) / "blocked.txt"
            result = await agent._run_checked_tool("write_file", {"file_path": str(target), "content": "no"})
            self.assertIn("denied", result)
            self.assertFalse(target.exists())

    async def test_long_result_saves_original_middle_with_bounded_preview(self):
        agent = self.agent()
        with tempfile.TemporaryDirectory() as tmp, patch("pathlib.Path.home", return_value=Path(tmp)):
            # File reads are paginated; artifact storage must preserve complete
            # output from tools that can still produce larger results.
            original = "a" * 40000 + "UNIQUE_MIDDLE" + "b" * 40000
            preview = agent._persist_large_result("read_file", original)
            files = list((Path(tmp) / ".mawoagent" / "tool-results").glob("*.txt"))
            self.assertEqual(len(files), 1)
            self.assertIn("UNIQUE_MIDDLE", files[0].read_text())
            self.assertLess(len(preview), 7000)

    async def test_successful_shell_keeps_stderr(self):
        argv = [sys.executable, "-c", 'import sys; print("IMPORTANT_WARNING",file=sys.stderr)']
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        self.assertIn("IMPORTANT_WARNING", await tools.execute_tool("run_shell", {"command": command}))

    async def test_extract_failure_keeps_window_and_reports_real_status(self):
        agent = self.agent()
        pending = {"messages": [{"role": "user", "content": "preference"}]}
        agent._pending_skill_extraction_window = pending
        result = await agent.extract_now()
        self.assertFalse(result["ok"])
        self.assertIs(agent._pending_skill_extraction_window, pending)
        with patch.object(agent, "_run_online_skill_evolution", new=AsyncMock(return_value={"ok": False, "action": "merge_denied"})):
            self.assertFalse((await agent.extract_now())["ok"])
            self.assertIs(agent._pending_skill_extraction_window, pending)

    async def test_resume_keeps_learning_context_pending_window_and_group(self):
        original = self.agent()
        original._set_pending_skill_extraction_window(original_user_message="first", assistant_text="answer", retrieved_reference=None)
        original._auto_save = Agent._auto_save.__get__(original, Agent)
        with patch("agents.agent.save_session") as save:
            original._auto_save()
        restored = self.agent()
        with redirect_stdout(io.StringIO()):
            restored.restore_session(save.call_args.args[1])
        self.assertEqual(restored._skill_eval_dialog, original._skill_eval_dialog)
        self.assertEqual(restored._pending_skill_extraction_window, original._pending_skill_extraction_window)
        restored._set_pending_skill_extraction_window(original_user_message="extend that", assistant_text="extended", retrieved_reference=None)
        context = restored._pending_skill_extraction_window["replay_context"]
        self.assertEqual(context["context_messages"], original._skill_eval_dialog)
        self.assertEqual(context["session_id"], original._conversation_id)

    async def test_background_concurrency_is_bounded_and_jobs_survive_shutdown(self):
        agent = self.agent(background_skill_concurrency=1, background_skill_queue_limit=2, shutdown_timeout=.01)
        entered = asyncio.Event()
        active = peak = 0
        async def work():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
        with tempfile.TemporaryDirectory() as tmp, patch.object(agent, "_background_jobs_dir", return_value=Path(tmp)), \
                patch.object(agent, "_online_evolution_enabled", return_value=True):
            for i in range(3):
                agent._schedule_background_skill_task(work(), job={"kind": "evolution", "window": {"i": i}})
            await entered.wait()
            self.assertEqual(len(agent._background_skill_tasks), 2)
            self.assertEqual(peak, 1)
            await agent.aclose()
            self.assertEqual(len(list(Path(tmp).glob("*.json"))), 3)
            self.assertTrue(all(json.loads(path.read_text())["status"] == "queued" for path in Path(tmp).glob("*.json")))

    async def test_persisted_background_jobs_resume_without_duplicate_dispatch(self):
        agent = self.agent()
        with tempfile.TemporaryDirectory() as tmp, patch.object(agent, "_background_jobs_dir", return_value=Path(tmp)), \
                patch.object(agent, "_online_evolution_enabled", return_value=True), \
                patch.object(agent, "_run_online_skill_evolution", new=AsyncMock(return_value={"ok": True})) as learn:
            path = Path(tmp) / "saved.json"
            path.write_text(json.dumps({"status": "queued", "attempts": 0, "data": {"kind": "evolution", "window": {"messages": []}}}))
            agent._resume_background_skill_jobs()
            agent._resume_background_skill_jobs()
            await agent.drain_background_skill_tasks()
            learn.assert_awaited_once()
            self.assertFalse(path.exists())

    async def test_context_capacity_override_and_validation(self):
        agent = self.agent(context_window=8000)
        self.assertEqual(agent.effective_window, 6000)
        for kwargs in ({"context_window": 1}, {"max_turns": -1}, {"background_skill_concurrency": 0}):
            with self.assertRaises(ValueError):
                make_agent(**kwargs)

    async def test_corrupt_job_record_does_not_prevent_resource_cleanup(self):
        agent = self.agent()
        agent._openai_client.close = AsyncMock()
        with tempfile.TemporaryDirectory() as tmp, patch.object(agent, "_background_jobs_dir", return_value=Path(tmp)):
            path = Path(tmp) / "bad.json"
            path.write_text('["invalid job"]')
            with self.assertLogs(level="ERROR"):
                await agent.aclose()
            agent._openai_client.close.assert_awaited_once()
            self.assertEqual(path.read_text(), '["invalid job"]')

    async def test_repl_eval_uses_real_formatter_without_model_calls(self):
        agent = self.agent()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ev, "get_evolution_dir", return_value=Path(tmp)), \
                patch.object(ev, "_active_skill_snapshots", return_value={}), \
                patch.object(candidates, "get_evolution_dir", return_value=Path(tmp)), \
                patch.object(agent, "_build_side_query", return_value=None), \
                patch("agents.main._read_repl_input", new=AsyncMock(side_effect=["/skill-eval", "quit"])), \
                patch("agents.main.signal.signal"), redirect_stdout(io.StringIO()):
            await run_repl(agent)
            self.assertTrue((Path(tmp) / "online_eval_report.json").is_file())

    async def test_real_sdks_parse_streams_and_reject_missing_completion(self):
        import anthropic
        import httpx
        import openai
        for protocol, incomplete in (("openai", False), ("anthropic", False), ("openai", True)):
            with self.subTest(protocol=protocol, incomplete=incomplete), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "sdk.txt"
                call_count = 0
                def handler(request):
                    nonlocal call_count
                    call_count += 1
                    body = json.loads(request.content)
                    self.assertTrue(body["stream"])
                    if protocol == "openai":
                        message = ({"role": "assistant", "tool_calls": [{"index": 0, "id": "call", "type": "function", "function": {
                            "name": "write_file", "arguments": json.dumps({"file_path": str(target), "content": "SDK verified"})}}]}
                            if call_count == 1 else {"role": "assistant", "content": "done"})
                        chunk = {"id": "completion", "object": "chat.completion.chunk", "created": 0, "model": "fake",
                                 "choices": [{"index": 0, "delta": message, "finish_reason": None if incomplete else ("tool_calls" if call_count == 1 else "stop")}]}
                        stream = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
                    else:
                        events = [{"type": "message_start", "message": {"id": "msg", "type": "message", "role": "assistant", "model": "fake",
                            "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}}]
                        if call_count == 1:
                            events.extend([
                                {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call", "name": "write_file", "input": {}}},
                                {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps({"file_path": str(target), "content": "SDK verified"})}},
                            ])
                        else:
                            events.extend([
                                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
                            ])
                        events.extend([{"type": "content_block_stop", "index": 0},
                            {"type": "message_delta", "delta": {"stop_reason": "tool_use" if call_count == 1 else "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}},
                            {"type": "message_stop"}])
                        stream = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events)
                    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream.encode())
                agent = self.agent(api_protocol=protocol)
                old_client = agent._openai_client if protocol == "openai" else agent._anthropic_client
                await old_client.close()
                http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                if protocol == "openai":
                    agent._openai_client = openai.AsyncOpenAI(api_key="fake", base_url="http://mock.test/v1", http_client=http_client, max_retries=0)
                else:
                    agent._anthropic_client = anthropic.AsyncAnthropic(api_key="fake", base_url="http://mock.test", http_client=http_client, max_retries=0)
                with patch("agents.tools._check_permission_rules", return_value=None), redirect_stdout(io.StringIO()):
                    await agent.chat("write the test file")
                if incomplete:
                    self.assertFalse(target.exists())
                    self.assertEqual(agent.last_finish_reason, "incomplete")
                else:
                    self.assertEqual(target.read_text(), "SDK verified")
                    self.assertEqual(call_count, 2)


class CompleteAuditDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module in (skill_evolution, candidates, ev, maintenance):
            self.stack.enter_context(patch.object(module, "get_evolution_dir", return_value=self.root / "evolution"))

    def skill(self, body="Always answer in English."):
        path = self.root / "skills" / "demo" / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nname: demo\ndescription: demo\nversion: 0.1.0\n---\n" + body)
        skill = SimpleNamespace(name="demo", skill_dir=str(path.parent), description="demo", prompt_template=body)
        self.stack.enter_context(patch("agents.skills.get_skill_by_name", return_value=skill))
        return path

    async def test_chinese_memory_query_triggers_prefetch(self):
        directory = self.root / "memory"
        directory.mkdir()
        (directory / "one.md").write_text("memory")
        with patch.object(memory, "get_memory_dir", return_value=directory), \
                patch.object(memory, "select_relevant_memories", new=AsyncMock(return_value=[])) as select:
            prefetch = memory.start_memory_prefetch("按照之前的习惯帮我修改代码", AsyncMock(), set(), 0)
            self.assertIsNotNone(prefetch)
            await prefetch.task
            select.assert_awaited_once()

    async def test_memory_names_and_byte_limits_do_not_lose_chinese_data(self):
        directory = self.root / "memory"
        directory.mkdir()
        with patch.object(memory, "get_memory_dir", return_value=directory):
            first = memory.save_memory("代码偏好", "first", "user", "first preference")
            second = memory.save_memory("回答偏好", "second", "user", "second preference")
            self.assertNotEqual(first, second)
            self.assertIn("first preference", (directory / first).read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                memory.delete_memory("../outside.md")
            (directory / "large.md").write_text("中" * 5000, encoding="utf-8")
            header = memory.MemoryHeader("large.md", str(directory / "large.md"), 0, "long", "user")
            with patch.object(memory, "scan_memory_headers", return_value=[header]):
                selected = await memory.select_relevant_memories("query", AsyncMock(return_value='{"selected_memories":["large.md"]}'), set())
                self.assertLessEqual(len(selected[0].content.encode()), memory.MAX_MEMORY_BYTES_PER_FILE)

    def test_skill_arguments_are_literal(self):
        skill = SimpleNamespace(prompt_template="Read $ARGUMENTS and ${ARGUMENTS}", skill_dir="demo")
        value = r"C:\Users\demo\file.py\1"
        self.assertEqual(skills.resolve_skill_prompt(skill, value), "Read " + value + " and " + value)

    async def test_merge_requires_complete_body_and_no_keyword_forced_merge(self):
        candidate = OnlineSkillCandidate("new", "description", instructions="new body")
        with patch("agents.skills.discover_skills", return_value=[]), \
                patch("agents.skills.retrieve_relevant_skills", return_value=[{"name": "unrelated", "score": .99}]), \
                patch("agents.skills.create_skill", return_value={"ok": True}) as create, \
                patch("agents.skills.evolve_skill") as evolve:
            result = await maintain_online_skill_candidate(candidate=candidate, side_query=AsyncMock(return_value='{"action":"add"}'), confirm_write=AsyncMock(return_value=True))
            self.assertEqual(result["action"], "add")
            create.assert_called_once()
            evolve.assert_not_called()
            result = await maintain_online_skill_candidate(candidate=candidate, side_query=AsyncMock(return_value='{"action":"merge","target_skill":"old"}'), confirm_write=AsyncMock(return_value=True))
            self.assertFalse(result["ok"])
            evolve.assert_not_called()

    def test_staging_publish_validation_and_rollback_keep_versions_separate(self):
        path = self.skill()
        original = path.read_text()
        staged = candidates.stage_candidate("merge", {"skill_name": "demo", "lesson": "Prefer Chinese", "instructions": "Always answer in Chinese.", "preserve_existing": True})
        self.assertEqual(path.read_text(), original)
        self.assertFalse(candidates.apply_candidate(staged["candidate_id"])["ok"])
        self.assertTrue(candidates.apply_candidate(staged["candidate_id"], manual_review=True)["ok"])
        self.assertNotIn("Always answer in English.", path.read_text())
        self.assertIn("Always answer in Chinese.", path.read_text())
        self.assertTrue(candidates.rollback_skill("demo")["ok"])
        self.assertEqual(path.read_text(), original)

    def test_stale_candidate_and_changed_approval_are_rejected(self):
        path = self.skill()
        staged = candidates.stage_candidate("merge", {"skill_name": "demo", "lesson": "new", "instructions": "new"})
        path.write_text(path.read_text() + "external change")
        self.assertFalse(candidates.apply_candidate(staged["candidate_id"], manual_review=True)["ok"])
        self.assertIn("changed during approval", candidates.apply_candidate(staged["candidate_id"], manual_review=True, expected_hash="wrong")["error"])

    async def test_failed_required_semantics_cannot_be_compensated_by_total_score(self):
        async def model(system, user):
            if system.startswith("Propose"):
                return '{"candidates":[{"instructions":"CANDIDATE"}]}'
            if system.startswith("Evaluate"):
                improved = json.loads(user)["response"] == "candidate response"
                return json.dumps({"judgments": [{"index": i, "pass": i > 0 and (improved or i > 1), "reason": "test judgment"} for i in range(4)]})
            return "candidate response" if system.startswith("CANDIDATE") else "base response"
        samples = [{"sample_id": split, "group_id": split, "split": split, "context_messages": [], "task_user": "task", "checks": [],
                    "requirements": ["Critical correctness", "Detail 1", "Detail 2", "Detail 3"]} for split in ("mutate_dev", "promotion_test")]
        result = await ev._run_comparison({"name": "demo", "instructions": "BASE"}, samples, ev.QueryBudget(model), {},
            max_candidates=1, min_replay_samples=2, min_promotion_tests=1, min_rule_pass_rate=.8, usage_eligible=True)
        self.assertFalse(result["promoted"])
        self.assertGreater(result["candidate_variants"][0]["dev_summary"]["required_failures"], 0)

    async def test_optional_requirement_remains_a_quality_metric(self):
        async def model(system, user):
            return '{"judgments":[{"index":0,"pass":false,"reason":"style could improve"}]}' if system.startswith("Evaluate") else "answer"
        sample = {"sample_id": "a", "split": "mutate_dev", "task_user": "task", "context_messages": [], "checks": [],
                  "requirements": [{"text": "Use a particular style", "required": False}]}
        summary = await ev._evaluate_variant({"name": "demo", "instructions": ""}, [sample], ev.QueryBudget(model), [], [])
        self.assertEqual(summary["required_failures"], 0)
        self.assertLess(summary["average_score"], 1)

    async def test_separate_judge_has_independent_cache_and_shared_budget(self):
        generator = AsyncMock(return_value="answer")
        judge = AsyncMock(return_value="judgment")
        budget = ev.QueryBudget(generator, judge_query=judge, max_calls=2)
        self.assertEqual(await budget("same", "same"), "answer")
        self.assertEqual(await budget.judge("same", "same"), "judgment")
        self.assertEqual(await budget.judge("same", "same"), "judgment")
        self.assertEqual(budget.calls, 2)
        with self.assertRaises(ev.EvaluationError):
            await budget.judge("another", "input")

    async def test_staged_candidate_passes_evaluation_then_can_publish(self):
        path = self.skill("BASE instructions")
        staged = candidates.stage_candidate("merge", {"skill_name": "demo", "lesson": "correct response", "instructions": "CANDIDATE instructions"})
        snapshot = candidates.candidate_snapshot(candidates.read_candidate(staged["candidate_id"]))
        samples = [{"sample_id": split, "split": split, "task_user": "task", "context_messages": [], "requirements": [],
                    "checks": [{"mode": "contains", "text": "GOOD"}]} for split in ("mutate_dev", "promotion_test")]
        async def model(system, user):
            return "GOOD" if "CANDIDATE instructions" in system else "BAD"
        bundle = await ev._run_comparison({"name": "demo", "instructions": "BASE instructions"}, samples, ev.QueryBudget(model), {},
            max_candidates=1, min_replay_samples=2, min_promotion_tests=1, min_rule_pass_rate=.8, usage_eligible=True, proposed_candidates=[snapshot])
        self.assertTrue(bundle["promoted"])
        ev._persist_eval_artifacts("demo", samples, bundle)
        self.assertTrue(candidates.apply_candidate(staged["candidate_id"])["ok"])
        self.assertIn("CANDIDATE instructions", path.read_text())

    def test_concurrent_champion_change_is_not_overwritten(self):
        root = self.root / "evolution" / "online-eval"
        root.mkdir(parents=True)
        lineage = ev._lineage_id_for_skill("demo")
        current = {"champions": {lineage: {"snapshot": {"name": "demo", "instructions": "NEWER"}}}}
        registry = root / "champions.json"
        registry.write_text(json.dumps(current))
        bundle = {"promoted": True, "status": "healthy", "reason": "test", "previous_champion_snapshot": None,
                  "best_snapshot": {"name": "demo", "instructions": "STALE"}, "best_test": {}, "outputs": [], "judgments": []}
        result = ev._persist_eval_artifacts("demo", [], bundle)
        self.assertFalse(result["promotion"]["promoted"])
        self.assertEqual(json.loads(registry.read_text()), current)

    def test_json_schema_checks_fields_types_and_disallows_remote_references(self):
        check = {"mode": "json_schema", "schema": {"type": "object", "required": ["risks"], "properties": {"risks": {"type": "array", "items": {"type": "string"}}}}}
        self.assertFalse(ev._check_response(check, "null"))
        self.assertFalse(ev._check_response(check, '{"risks":123}'))
        self.assertTrue(ev._check_response(check, '{"risks":["interruption"]}'))
        self.assertFalse(ev._check_response({"mode": "json_parseable"}, "NaN"))
        self.assertIsNotNone(validate_json({}, {"$ref": "https://unavailable.test/schema"}))

    def test_project_allow_rules_require_trust_and_changes_invalidate_it(self):
        home, project = self.root / "home", self.root / "project"
        (project / ".mawoagent").mkdir(parents=True)
        settings = project / ".mawoagent" / "settings.json"
        settings.write_text(json.dumps({"permissions": {"allow": ["mcp_start"], "deny": ["write_file(blocked.txt)"]}}))
        with patch("pathlib.Path.home", return_value=home), patch("pathlib.Path.cwd", return_value=project):
            tools.reset_permission_cache()
            self.assertEqual(tools.check_permission("mcp_start", {"server_name": "demo"}, "dontAsk")["action"], "deny")
            self.assertEqual(tools.check_permission("write_file", {"file_path": "blocked.txt"}, "acceptEdits")["action"], "deny")
            project_trust.trust_project()
            self.assertEqual(tools.check_permission("mcp_start", {"server_name": "demo"}, "dontAsk")["action"], "allow")
            (project / ".mcp.json").write_text('{"demo":{"command":"changed"}}')
            self.assertEqual(tools.check_permission("mcp_start", {"server_name": "demo"}, "dontAsk")["action"], "deny")
        tools.reset_permission_cache()

    def test_trust_invalidates_even_when_mcp_file_metadata_is_preserved(self):
        home, project = self.root / "home", self.root / "project"
        (project / ".mawoagent").mkdir(parents=True)
        (project / ".mawoagent" / "settings.json").write_text('{"permissions":{"allow":["mcp_start"]}}')
        config = project / ".mcp.json"
        config.write_text('{"demo":{"command":"one"}}')
        with patch("pathlib.Path.home", return_value=home), patch("pathlib.Path.cwd", return_value=project):
            tools.reset_permission_cache()
            project_trust.trust_project()
            self.assertEqual(tools.check_permission("mcp_start", {"server_name": "demo"}, "dontAsk")["action"], "allow")
            stat = config.stat()
            config.write_text('{"demo":{"command":"two"}}')
            os.utime(config, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertEqual(tools.check_permission("mcp_start", {"server_name": "demo"}, "dontAsk")["action"], "deny")
        tools.reset_permission_cache()

    def test_invalid_candidate_fields_are_rejected_before_any_write(self):
        path = self.skill()
        original = path.read_text()
        staged = candidates.stage_candidate("merge", {"skill_name": "demo", "lesson": "new", "instructions": "new"})
        proposal_path = Path(staged["file"])
        proposal = json.loads(proposal_path.read_text())
        proposal["write_input"]["unexpected"] = "value"
        proposal_path.write_text(json.dumps(proposal))
        with self.assertRaises(ValueError):
            candidates.apply_candidate(staged["candidate_id"], manual_review=True)
        self.assertEqual(path.read_text(), original)

    async def test_public_evaluation_includes_staged_candidate_and_enables_publish(self):
        path = self.skill("BASE instructions")
        staged = candidates.stage_candidate("merge", {"skill_name": "demo", "lesson": "correctness", "instructions": "CANDIDATE instructions"})
        groups = {}
        for i in range(100):
            name = f"group-{i}"
            split = "mutate_dev" if int(ev._stable_hash(name)[:8], 16) / 0xFFFFFFFF < .75 else "promotion_test"
            groups.setdefault(split, name)
            if len(groups) == 2:
                break
        rows = [{"skill": "demo", "replay_context": {"schema_version": 2, "event_id": group, "session_id": group,
                 "task_user": "task", "context_messages": [], "original_response": "BAD", "feedback": "correction"}}
                for group in groups.values()]
        root = self.root / "evolution"
        root.mkdir(exist_ok=True)
        ev._write_jsonl(root / skill_evolution.ONLINE_PROVENANCE_LOG, rows)
        _, info = ev._build_replay_pool("demo", rows, write_artifacts=True, pool_limit=200, replay_limit=24)
        reviews = ev._read_jsonl(Path(info["review_file"]))
        for review in reviews:
            review.update(approved=True, reviewed_by="test", text_only=True, evaluation_mode="original_task", target_turn_index=0,
                          checks=[{"mode": "contains", "text": "GOOD"}])
        ev._write_jsonl(Path(info["review_file"]), reviews)
        ev._write_json(root / "skill_usage_stats.json", {"demo": {"retrieved": 5, "relevant": 5, "used": 5}})
        async def model(system, user):
            return "GOOD" if "CANDIDATE instructions" in system else "BAD"
        with patch.object(ev, "_active_skill_snapshots", return_value={"demo": {"name": "demo", "instructions": "BASE instructions", "skill_dir": str(path.parent)}}):
            report = await ev.evaluate_online_skill_evolution_async(side_query=model, max_candidates=1)
        self.assertTrue(report["skills"][0]["artifacts"]["promotion"]["promoted"])
        self.assertTrue(candidates.apply_candidate(staged["candidate_id"])["ok"])

    def test_provenance_reader_advances_cursor_bounds_rows_and_recovers_rotation(self):
        log = self.root / "source.jsonl"
        log.write_text("".join(json.dumps({"skill": "demo", "index": i}) + "\n" for i in range(5)))
        self.assertEqual([x["index"] for x in load_recent_sources(log, 2)], [3, 4])
        self.assertEqual([x["index"] for x in load_recent_sources(log, 2)], [3, 4])
        with log.open("a") as stream:
            stream.write(json.dumps({"skill": "demo", "index": 5}) + "\n")
        self.assertEqual([x["index"] for x in load_recent_sources(log, 2)], [4, 5])
        log.write_text('{"skill":"demo","index":9}\n')
        self.assertEqual([x["index"] for x in load_recent_sources(log, 2)], [9])

    def test_cleanup_is_preview_only_and_preserves_champion_run(self):
        old_time = 1
        for name in ("keep", "remove"):
            path = self.root / "evolution" / "online-eval" / "runs" / "demo" / name
            path.mkdir(parents=True)
            os.utime(path, (old_time, old_time))
        registry = self.root / "evolution" / "online-eval" / "champions.json"
        registry.write_text('{"champions":{"demo":{"run_id":"keep"}}}')
        with patch("pathlib.Path.home", return_value=self.root / "home"):
            preview = maintenance.cleanup_state()
            self.assertEqual(preview["count"], 1)
            target = Path(preview["paths"][0])
            self.assertTrue(target.exists())
            maintenance.cleanup_state(apply=True, expected_paths=preview["paths"])
            self.assertFalse(target.exists())
            self.assertTrue(target.parent.joinpath("keep").exists())


if __name__ == "__main__":
    unittest.main()
