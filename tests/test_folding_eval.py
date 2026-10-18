from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agents.agent import Agent
from agents.eval_folding import _run_case, extract_answer, load_cases, main_async, score_answer, score_details, summarize


class FoldingSwitchTests(unittest.TestCase):
    def _agent(self, **kwargs) -> Agent:
        with patch("agents.agent.build_system_prompt", return_value="test system prompt"):
            return Agent(api_base="http://example.test/v1", api_key="test", **kwargs)

    def test_disabled_folding_blocks_all_entry_points(self) -> None:
        agent = self._agent(enable_folding=False, auto_compact_threshold=0.01)
        self.assertNotIn("compact_context", [tool["name"] for tool in agent.tools])
        agent.last_input_token_count = agent.effective_window
        with patch.object(agent, "_compact_conversation", new_callable=AsyncMock) as compact:
            asyncio.run(agent._check_and_compact())
            compact.assert_not_awaited()
        self.assertFalse(asyncio.run(agent._compact_conversation(trigger="manual")))
        self.assertIn("disabled", asyncio.run(agent._execute_compact_context_tool({})))
        self.assertIn("not available", asyncio.run(agent._execute_tool_call("compact_context", {})))
        self.assertEqual(agent._fold_count, 0)

    def test_enabled_folding_replaces_history_and_records_trigger(self) -> None:
        fold_events = []
        agent = self._agent(enable_folding=True, fold_observer=fold_events.append)
        agent._openai_messages.extend([
            {"role": "user", "content": "Remember blue."},
            {"role": "assistant", "content": "OK"},
            {"role": "user", "content": "What next?"},
        ])
        memory = {
            "episode_memory": {"task_description": "Remember blue", "key_events": [], "current_progress": ""},
            "working_memory": {"immediate_goal": "Answer", "current_challenges": "", "next_actions": []},
            "tool_memory": {"tools_used": [], "derived_rules": []},
        }
        with patch.object(agent, "_generate_folded_session_memory", new_callable=AsyncMock, return_value=memory), patch("agents.agent.save_folded_session_memory"):
            self.assertTrue(asyncio.run(agent._compact_conversation(trigger="eval_scheduled")))
        self.assertEqual(agent._fold_count, 1)
        self.assertEqual(agent._folded_session_memories[0]["trigger"], "eval_scheduled")
        self.assertIn("Remember blue", fold_events[0]["transcript"])
        self.assertEqual(len(agent._openai_messages), 2)

    def test_fold_limit_blocks_a_second_fold(self) -> None:
        agent = self._agent(max_fold_limit=1)
        agent._openai_messages.extend([
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ])
        memory = {
            "episode_memory": {"task_description": "task", "key_events": [], "current_progress": ""},
            "working_memory": {"immediate_goal": "answer", "current_challenges": "", "next_actions": []},
            "tool_memory": {"tools_used": [], "derived_rules": []},
        }
        with patch.object(agent, "_generate_folded_session_memory", new_callable=AsyncMock, return_value=memory), patch("agents.agent.save_folded_session_memory"):
            self.assertTrue(asyncio.run(agent._compact_conversation()))
            self.assertFalse(asyncio.run(agent._compact_conversation()))
        self.assertIn("limit reached", asyncio.run(agent._execute_compact_context_tool({})))
        self.assertEqual(agent._fold_count, 1)

    def test_three_memories_are_independent_calls(self) -> None:
        agent = self._agent(fold_memory_mode="three_parallel")
        responses = iter([
            '{"task_description":"task","key_events":[],"current_progress":"done"}',
            '{"immediate_goal":"answer","current_challenges":"","next_actions":[]}',
            '{"tools_used":[],"derived_rules":["read first"]}',
        ])
        calls = []
        async def side_query(system, prompt):
            calls.append((system, prompt))
            return next(responses)
        with patch.object(agent, "_build_side_query", return_value=side_query):
            memory = asyncio.run(agent._generate_folded_session_memory("conversation"))
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("conversation" in prompt for _, prompt in calls))
        self.assertEqual(memory["episode_memory"]["task_description"], "task")
        self.assertEqual(memory["working_memory"]["immediate_goal"], "answer")
        self.assertEqual(memory["tool_memory"]["derived_rules"], ["read first"])

    def test_malformed_parallel_memory_rejects_fold(self) -> None:
        agent = self._agent(fold_memory_mode="three_parallel")
        responses = iter(["{}", "{}", "{}"])
        async def side_query(system, prompt):
            return next(responses)
        with patch.object(agent, "_build_side_query", return_value=side_query):
            memory = asyncio.run(agent._generate_folded_session_memory("critical original detail"))
        self.assertIsNone(memory)


class FoldingResultTests(unittest.TestCase):
    def test_case_loading_scoring_and_pair_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cases.jsonl"
            path.write_text(json.dumps({"id": "task/1", "prompts": ["one", "two", "three"], "answer": "BLUE"}) + "\n", encoding="utf-8")
            cases = load_cases(path)
        self.assertEqual(cases[0]["task_id"], "task/1")
        self.assertEqual(extract_answer("reason\nFINAL_ANSWER: Blue\n"), "Blue")
        self.assertTrue(score_answer("Blue", ["BLUE"], mode="normalized"))
        self.assertFalse(score_answer("Blue", ["BLUE"], mode="exact"))
        rows = [
            {"task_id": "task/1", "condition": condition, "status": "completed", "correct": condition == "fold_on", "fold_count": int(condition == "fold_on"),
             "main_input_tokens": 10, "main_output_tokens": 2, "side_input_tokens": 3, "side_output_tokens": 1}
            for condition in ("fold_on", "fold_off")
        ]
        summary = summarize(rows)
        self.assertEqual(summary["paired_outcomes"]["fold_only"], 1)
        self.assertEqual(summary["cases_with_fold"], 1)
        self.assertEqual(summary["valid_pairs"], 1)
        self.assertEqual(summary["accuracy_delta_percentage_points"], 100)

    def test_invalid_pairs_do_not_produce_accuracy(self) -> None:
        rows = [
            {"task_id": "no-fold", "condition": "fold_on", "status": "completed", "correct": True, "fold_count": 0},
            {"task_id": "no-fold", "condition": "fold_off", "status": "completed", "correct": False, "fold_count": 0},
            {"task_id": "timeout", "condition": "fold_on", "status": "timeout", "correct": False, "fold_count": 1},
            {"task_id": "timeout", "condition": "fold_off", "status": "completed", "correct": True, "fold_count": 0},
        ]
        summary = summarize(rows)
        self.assertEqual(summary["cases"], 2)
        self.assertEqual(summary["valid_pairs"], 0)
        self.assertIsNone(summary["accuracy_delta_percentage_points"])
        self.assertIsNone(summary["fold_on"]["accuracy"])
        self.assertEqual(summary["invalid_pairs"], [
            {"task_id": "no-fold", "reasons": ["fold_on:no_fold"]},
            {"task_id": "timeout", "reasons": ["fold_on:timeout"]},
        ])

        verified_rows = [
            {"task_id": "raw-benchmark", "condition": condition, "status": "completed", "correct": True,
             "fold_count": int(condition == "fold_on"), "capabilities_verified": False}
            for condition in ("fold_on", "fold_off")
        ]
        self.assertEqual(summarize(verified_rows)["invalid_pairs"][0]["reasons"], ["case:capabilities_unverified"])

    def test_numeric_scoring_and_review_details(self) -> None:
        self.assertTrue(score_answer("1,000.0", ["1000"], mode="numeric"))
        self.assertFalse(score_answer("1,0", ["10"], mode="numeric"))
        self.assertFalse(score_answer("1000 meters", ["1000"], mode="numeric"))
        self.assertEqual(score_details("Blue", ["BLUE"], mode="normalized")["matched_answer"], "BLUE")
        self.assertTrue(score_details("1000 meters", ["1000"], mode="numeric")["review_required"])

    def test_unsupported_case_capabilities_fail_before_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "cases.jsonl"
            path.write_text(json.dumps({"id": "web", "prompt": "Find this online", "answer": "x", "required_tools": ["web_search"]}) + "\n")
            with self.assertRaisesRegex(ValueError, "unavailable tools: web_search"):
                load_cases(path)
            (root / "files").mkdir()
            (root / "files" / "sheet.xlsx").write_bytes(b"not a text file")
            path.write_text(json.dumps({"id": "file", "prompt": "Read attachment", "answer": "x", "file_name": "sheet.xlsx"}) + "\n")
            with self.assertRaisesRegex(ValueError, "not readable"):
                load_cases(path)
            path.write_text(json.dumps({"id": "benchmark", "prompt": "Answer", "answer": "x", "problem_type": "text"}) + "\n")
            self.assertFalse(load_cases(path)[0]["capabilities_verified"])
            path.write_text(json.dumps({"id": "curated", "prompt": "Answer", "answer": "x", "problem_type": "text", "required_tools": []}) + "\n")
            self.assertTrue(load_cases(path)[0]["capabilities_verified"])

    def test_paired_runner_writes_isolated_traces(self) -> None:
        class FakeAgent:
            tool_sets = []

            def __init__(self, *, enable_folding: bool, **kwargs) -> None:
                self.enable_folding = enable_folding
                self.tool_sets.append((enable_folding, {tool["name"] for tool in kwargs["custom_tools"]}, kwargs["max_output_tokens"]))
                self.session_id = "fake-session"
                self._aborted = False
                self._fold_count = 0
                self._openai_messages = [{"role": "system", "content": "test"}]
                self._anthropic_messages = []
                self._folded_session_memories = []
                self.total_input_tokens = 10
                self.total_output_tokens = 2
                self.side_input_tokens = 0
                self.side_output_tokens = 0
                self.current_turns = 0
                self._mcp_manager = type("Mcp", (), {"disconnect_all": AsyncMock()})()

            async def run_once(self, prompt):
                self._openai_messages.extend([{"role": "user", "content": prompt}, {"role": "assistant", "content": "ACK"}])
                return {"text": "FINAL_ANSWER: BLUE" if "final answer" in prompt else "ACK"}

            async def _compact_conversation(self, *, trigger):
                self._fold_count += 1
                self._folded_session_memories.append({"trigger": trigger})
                return True

            async def drain_background_skill_tasks(self):
                return None

            async def aclose(self):
                await self._mcp_manager.disconnect_all()

            def abort(self):
                self._aborted = True

            def _get_current_cost_usd(self):
                return 0.0

        case = {"task_id": "task/1", "prompts": ["first", "second", "third"], "answers": ["BLUE"]}
        with tempfile.TemporaryDirectory() as temp_dir, patch("agents.eval_folding.Agent", FakeAgent):
            rows = []
            for condition in ("fold_on", "fold_off"):
                rows.append(asyncio.run(_run_case(
                    case, condition, output_dir=Path(temp_dir), model="fake", api_base="http://example.test/v1",
                    api_key="fake", use_openai=True, fold_threshold=0.7, max_turns=10,
                    max_cost=1.0, timeout=10, score_mode="normalized", fold_after_turn=2,
                )))
            self.assertEqual([row["fold_count"] for row in rows], [1, 0])
            self.assertTrue(all(row["correct"] for row in rows))
            self.assertTrue(all(row["scoring"]["reason"] == "matched" for row in rows))
            self.assertTrue(all(row["answer_marker_found"] for row in rows))
            self.assertTrue(all(Path(row["trace_file"]).is_file() for row in rows))
            self.assertTrue(all(Path(row["trace_file"]).is_relative_to(Path(temp_dir)) for row in rows))
            self.assertEqual(FakeAgent.tool_sets[0][1], FakeAgent.tool_sets[1][1])
            self.assertEqual([tools[2] for tools in FakeAgent.tool_sets], [2048, 2048])

    def test_runner_writes_review_queue_and_valid_pair_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cases_path = root / "cases.jsonl"
            cases_path.write_text(json.dumps({"id": "case-1", "prompts": ["one", "two", "three"], "answer": "BLUE"}) + "\n")
            output = root / "results"
            args = SimpleNamespace(
                cases=cases_path, output=output, text_only=False, limit=None, validate_only=False,
                model="fake", api_base=None, fold_threshold=0.7, max_fold_limit=3,
                fold_memory_mode="three_parallel", fold_after_turn=2, max_turns=10,
                max_output_tokens=2048, max_cost=1.0, timeout=10, score_mode="normalized",
            )

            async def fake_run_case(case, condition, **kwargs):
                return {
                    "task_id": case["task_id"], "condition": condition, "status": "completed",
                    "correct": False, "fold_count": int(condition == "fold_on"),
                    "capabilities_verified": True, "scoring": {"review_required": True, "reason": "no_match"},
                    "answer_marker_found": True, "prediction": "RED", "answers": case["answers"],
                    "trace_file": "trace.json", "main_input_tokens": 10, "main_output_tokens": 2,
                    "side_input_tokens": 0, "side_output_tokens": 0,
                }

            with patch("agents.eval_folding._load_env_file"), patch("agents.eval_folding._resolve_api_config", return_value=("http://example.test/v1", "fake", True)), patch("agents.eval_folding._run_case", side_effect=fake_run_case), redirect_stdout(StringIO()):
                asyncio.run(main_async(args))
            summary = json.loads((output / "summary.json").read_text())
            review_rows = (output / "review_queue.jsonl").read_text().splitlines()
            self.assertEqual(summary["valid_pairs"], 1)
            self.assertEqual(len(review_rows), 2)
            self.assertEqual(len((output / "results.jsonl").read_text().splitlines()), 2)

    def test_truncated_final_answer_is_not_scored(self) -> None:
        agent = SimpleNamespace(
            run_once=AsyncMock(return_value={"text": "FINAL_ANSWER: BLUE"}),
            last_finish_reason="length", _aborted=False, _fold_count=0,
            _openai_messages=[], _anthropic_messages=[], _folded_session_memories=[],
            session_id="fake", total_input_tokens=1, total_output_tokens=1,
            side_input_tokens=0, side_output_tokens=0, current_turns=0,
            drain_background_skill_tasks=AsyncMock(),
            aclose=AsyncMock(),
            _mcp_manager=SimpleNamespace(disconnect_all=AsyncMock()),
            _get_current_cost_usd=lambda: 0.0,
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch("agents.eval_folding.Agent", return_value=agent):
            row = asyncio.run(_run_case(
                {"task_id": "truncated", "prompts": ["answer"], "answers": ["BLUE"]},
                "fold_off", output_dir=Path(temp_dir), model="fake", api_base="http://example.test/v1",
                api_key="fake", use_openai=True, fold_threshold=0.7, max_turns=5,
                max_cost=1.0, timeout=10, score_mode="normalized", fold_after_turn=None,
            ))
        self.assertEqual(row["status"], "truncated")
        self.assertFalse(row["correct"])


if __name__ == "__main__":
    unittest.main()
