from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agents.agent import Agent
from agents.main import _read_repl_input, run_repl
from agents.online_skill_evolution import judge_retrieved_skill_usage
from agents.skill_evolution import record_skill_usage_judgments
from agents.subagent import get_sub_agent_config


def make_agent(**kwargs):
    with patch('agents.agent.build_system_prompt', return_value='test'):
        return Agent(api_base='http://example.test/v1', api_key='test',
                     enable_mcp=False, enable_memory_prefetch=False, enable_online_evolution=False, **kwargs)


class RuntimeReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_usage_judgments_do_not_become_positive_evidence(self):
        hits = [{'name': 'demo'}]
        for rows in ([{'name': 'demo', 'relevant': 'false', 'used': 'false'}],
                     [{'name': 'demo', 'relevant': False, 'used': True}], [],
                     [{'name': 'wrong', 'relevant': True, 'used': True}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                await judge_retrieved_skill_usage(hits=hits, user_message='task', assistant_text='answer',
                    side_query=AsyncMock(return_value=json.dumps({'judgments': rows})))
        result = await judge_retrieved_skill_usage(hits=hits, user_message='task', assistant_text='demo', side_query=None)
        self.assertEqual(result, [])
        result = await judge_retrieved_skill_usage(hits=hits, user_message='task', assistant_text='answer',
            side_query=AsyncMock(return_value='{"judgments":[{"name":"demo","relevant":false,"used":false}]}'))
        self.assertIs(result[0]['used'], False)

    async def test_usage_writer_rejects_invalid_entire_batch(self):
        with tempfile.TemporaryDirectory() as tmp, patch('agents.skill_evolution.get_evolution_dir', return_value=Path(tmp)):
            with self.assertRaises(ValueError):
                record_skill_usage_judgments([{'name': 'valid', 'relevant': True, 'used': True},
                                              {'name': 'bad', 'relevant': 'false', 'used': 'false'}])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_child_usage_settles_on_success_error_and_cancellation(self):
        for skill_fork in (False, True):
            for error in (None, RuntimeError('failed'), asyncio.CancelledError()):
                with self.subTest(skill_fork=skill_fork, error=type(error).__name__):
                    parent = make_agent(max_turns=10)
                    child = SimpleNamespace(total_input_tokens=100, total_output_tokens=20,
                        side_input_tokens=30, side_output_tokens=5, current_turns=2,
                        aclose=AsyncMock(),
                        run_once=AsyncMock(return_value={'text': 'done', 'tokens': {'input': 100, 'output': 20}}, side_effect=error))
                    result = {'context': 'fork', 'allowed_tools': [], 'prompt': 'task'}
                    with patch('agents.agent.Agent', return_value=child) as ctor, \
                         patch('agents.skills.execute_skill', return_value=result), \
                         patch('agents.agent.print_sub_agent_start'), patch('agents.agent.print_sub_agent_end'):
                        operation = parent._execute_skill_tool({'skill_name': 'demo'}) if skill_fork else parent._execute_agent_tool({'prompt': 'task'})
                        if isinstance(error, asyncio.CancelledError):
                            with self.assertRaises(asyncio.CancelledError):
                                await operation
                        else:
                            await operation
                        if skill_fork:
                            self.assertEqual(ctor.call_args.kwargs['custom_tools'], [])
                    self.assertEqual((parent.total_input_tokens, parent.total_output_tokens,
                                      parent.side_input_tokens, parent.side_output_tokens, parent.current_turns), (100, 20, 30, 5, 2))

    async def test_exhausted_budget_blocks_both_backends_before_api(self):
        for backend in ('openai', 'anthropic'):
            for limits in ({'max_turns': 0}, {'max_cost_usd': 0}):
                parent = make_agent(is_sub_agent=True, **limits)
                parent._emit_text = Mock()
                stream = AsyncMock()
                setattr(parent, '_call_' + backend + '_stream', stream)
                await getattr(parent, '_chat_' + backend)('task')
                stream.assert_not_awaited()
                self.assertEqual(parent.last_finish_reason, 'budget_exceeded')

    async def test_waiting_for_input_allows_background_progress(self):
        finished = threading.Event()
        async def background():
            await asyncio.sleep(.01)
            finished.set()
        def read():
            return 'ready' if finished.wait(1) else 'event loop blocked'
        with patch('builtins.input', side_effect=read):
            task = asyncio.create_task(background())
            self.assertEqual(await _read_repl_input(), 'ready')
            await task
        with patch('builtins.input', side_effect=EOFError):
            with self.assertRaises(EOFError):
                await _read_repl_input()

    async def test_bad_eval_file_does_not_exit_repl(self):
        agent = SimpleNamespace(set_confirm_fn=Mock(), set_plan_approval_fn=Mock(),
                                drain_background_skill_tasks=AsyncMock(), aclose=AsyncMock(),
                                _build_side_query=Mock(return_value=None))
        async def fail(**kwargs):
            agent.drain_background_skill_tasks.assert_awaited_once()
            raise ValueError('invalid review JSON')
        with patch('agents.main._read_repl_input', new=AsyncMock(side_effect=['/skill-eval', 'quit'])) as read, \
             patch('agents.main.signal.signal'), patch('agents.main.print_welcome'), \
             patch('agents.main.print_user_prompt'), patch('agents.main.print_goodbye'), \
             patch('agents.main.print_error') as error, patch('agents.main.format_online_skill_eval_async', side_effect=fail):
            await run_repl(agent)
        self.assertEqual(read.await_count, 2)
        error.assert_called_once()
        self.assertEqual(agent.drain_background_skill_tasks.await_count, 1)
        agent.aclose.assert_awaited_once()

    async def test_usage_tracking_uses_captured_hits(self):
        agent = make_agent()
        agent._online_evolution_enabled = lambda: True
        agent._last_retrieved_skill_hits = [{'name': 'new-turn'}]
        with patch('agents.online_skill_evolution.judge_retrieved_skill_usage', new=AsyncMock(return_value=[])) as judge, \
             patch('agents.skills.record_usage_judgments', return_value={}):
            await agent._run_skill_usage_tracking('old-task', 'old-answer', hits=[{'name': 'old-turn'}])
        self.assertEqual(judge.call_args.kwargs['hits'], [{'name': 'old-turn'}])

    def test_custom_agent_empty_allowlist_and_recursion(self):
        for allowed, expected in (([], []), (['agent', 'read_file'], ['read_file'])):
            config = {'custom': {'allowed_tools': allowed, 'system_prompt': 'test'}}
            with patch('agents.subagent._discover_custom_agents', return_value=config):
                result = get_sub_agent_config('custom')
            self.assertEqual([t['name'] for t in result['tools']], expected)


if __name__ == '__main__':
    unittest.main()
