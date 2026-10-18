from __future__ import annotations

import asyncio
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agents.agent import Agent
from agents.main import _read_repl_input, _resolve_api_config, run_repl, run_one_shot
from agents import session, skill_evolution
from agents.mcp_client import McpConnection, McpManager
from agents.storage import atomic_write_text
from agents.tools import _edit_file, execute_tool


def make_agent(**kwargs):
    options = dict(api_base='http://example.test/v1', api_key='fake', custom_system_prompt='test',
                   enable_mcp=False, enable_memory_prefetch=False, enable_online_evolution=False)
    options.update(kwargs)
    agent = Agent(**options)
    agent._auto_save = Mock()
    return agent


class RuntimeFixTests(unittest.IsolatedAsyncioTestCase):
    def agent(self, **kwargs):
        agent = make_agent(**kwargs)
        self.addAsyncCleanup(agent.aclose)
        return agent

    async def test_native_and_proxy_configuration_select_correct_client(self):
        cases = [
            ({'OPENAI_API_KEY': 'fake'}, None, True),
            ({'ANTHROPIC_API_KEY': 'fake'}, None, False),
            ({'ANTHROPIC_API_KEY': 'fake', 'ANTHROPIC_BASE_URL': 'https://api.anthropic.com'}, None, False),
            ({'ANTHROPIC_API_KEY': 'fake', 'ANTHROPIC_BASE_URL': 'http://proxy.test/custom'}, None, False),
            ({'OPENAI_API_KEY': 'fake', 'OPENAI_BASE_URL': 'http://proxy.test/v1'}, None, True),
            ({'APIKEY': 'fake', 'API': 'http://proxy.test/anthropic'}, None, False),
            ({'APIKEY': 'fake', 'API': 'http://proxy.test/anthropic'}, 'http://override.test/v1', True),
        ]
        for env, override, expected in cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                base, key, is_openai = _resolve_api_config(override)
                a = self.agent(api_base=base if is_openai else None,
                               anthropic_base_url=None if is_openai else base,
                               api_key=key, api_protocol='openai' if is_openai else 'anthropic')
                self.assertEqual(a.use_openai, expected)
                self.assertEqual(a._openai_client is not None, expected)

    async def test_auxiliary_requests_stop_at_zero_budget_for_both_protocols(self):
        for protocol in ('openai', 'anthropic'):
            a = self.agent(api_protocol=protocol, max_cost_usd=0)
            client = a._openai_client.chat.completions if protocol == 'openai' else a._anthropic_client.messages
            with patch.object(client, 'create', new=AsyncMock()) as create:
                with self.assertRaisesRegex(RuntimeError, 'Cost limit reached'):
                    await a._build_side_query()('test', 'task')
                create.assert_not_awaited()

    async def test_parent_and_child_share_budget_and_clear_does_not_reset_spend(self):
        parent = self.agent(max_cost_usd=1, input_price_per_million=1, output_price_per_million=2)
        child = self.agent(input_price_per_million=1, output_price_per_million=2)
        parent._register_child(child)
        child.side_input_tokens = 1_000_000
        self.assertEqual(parent._get_current_cost_usd(), 1)
        with self.assertRaisesRegex(RuntimeError, 'Cost limit reached'):
            await child._build_side_query()('test', 'task')
        parent._account_child_usage(child)
        with redirect_stdout(io.StringIO()):
            parent.clear_history()
        self.assertEqual(parent._get_current_cost_usd(), 1)
        self.assertTrue(parent._check_budget()['exceeded'])

    async def test_repl_sigint_aborts_normal_chat_and_closes_resources(self):
        a = self.agent()
        handler = {}
        cancelled = asyncio.Event()
        async def backend(_):
            asyncio.get_running_loop().call_soon(handler['fn'], signal.SIGINT, None)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        a._chat_openai = backend
        a.aclose = AsyncMock()
        with patch('agents.main.signal.signal', side_effect=lambda sig, fn: handler.update(fn=fn)), \
             patch('agents.main._read_repl_input', new=AsyncMock(side_effect=['task', 'quit'])), \
             patch.object(a, '_augment_user_message_with_skill_context', return_value=('task', None)), \
             redirect_stdout(io.StringIO()):
            await asyncio.wait_for(run_repl(a), 2)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(a._aborted)
        a.aclose.assert_awaited_once()

    async def test_external_cancellation_propagates_and_repairs_tool_history(self):
        for protocol in ('openai', 'anthropic'):
            a = self.agent(api_protocol=protocol)
            entered = asyncio.Event()
            async def backend(_):
                if a.use_openai:
                    a._openai_messages.append({'role': 'assistant', 'tool_calls': [
                        {'id': 'pending', 'type': 'function', 'function': {'name': 'run_shell', 'arguments': '{}'}}]})
                else:
                    a._anthropic_messages.append({'role': 'assistant', 'content': [
                        {'type': 'tool_use', 'id': 'pending', 'name': 'run_shell', 'input': {}}]})
                entered.set()
                await asyncio.Event().wait()
            setattr(a, '_chat_' + protocol, backend)
            with patch.object(a, '_augment_user_message_with_skill_context', return_value=('task', None)):
                task = asyncio.create_task(a.chat('task'))
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            if a.use_openai:
                self.assertEqual(a._openai_messages[-1]['tool_call_id'], 'pending')
            else:
                self.assertEqual(a._anthropic_messages[-1]['content'][0]['tool_use_id'], 'pending')

    async def test_abort_during_mcp_initialization_cleans_up_and_can_retry(self):
        a = self.agent(enable_mcp=True)
        entered = asyncio.Event()
        async def initialize(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        a._mcp_manager.load_and_connect = AsyncMock(side_effect=initialize)
        a._mcp_manager.disconnect_all = AsyncMock()
        task = asyncio.create_task(a.chat('task'))
        await entered.wait()
        self.assertTrue(a.is_processing)
        a.abort()
        await task
        self.assertFalse(a._mcp_initialized)
        a._mcp_manager.disconnect_all.assert_awaited_once()
        a._mcp_manager.load_and_connect = AsyncMock()
        a._chat_openai = AsyncMock()
        with patch.object(a, '_augment_user_message_with_skill_context', return_value=('retry', None)), redirect_stdout(io.StringIO()):
            await a.chat('retry')
        a._mcp_manager.load_and_connect.assert_awaited_once()
        self.assertTrue(a._mcp_initialized)

    async def test_openai_stream_closes_on_cancellation(self):
        entered = asyncio.Event()
        class Stream:
            def __init__(self):
                self.close = AsyncMock()
            def __aiter__(self):
                return self
            async def __anext__(self):
                entered.set()
                await asyncio.Event().wait()
        stream = Stream()
        a = self.agent()
        with patch.object(a._openai_client.chat.completions, 'create', new=AsyncMock(return_value=stream)):
            task = asyncio.create_task(a._call_openai_stream())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        stream.close.assert_awaited_once()

    async def test_cancelled_input_reuses_single_reader_without_losing_line(self):
        ready, release = threading.Event(), threading.Event()
        def read():
            ready.set()
            release.wait(2)
            return 'next command'
        with patch('builtins.input', side_effect=read) as input_fn:
            first = asyncio.create_task(_read_repl_input())
            await asyncio.to_thread(ready.wait, 1)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            await asyncio.sleep(.02)
            self.assertEqual(await _read_repl_input(), 'next command')
            input_fn.assert_called_once()

    async def test_background_shutdown_is_bounded_and_one_shot_closes_on_error(self):
        a = self.agent(shutdown_timeout=.02)
        finished = asyncio.Event()
        async def background():
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        a._schedule_background_skill_task(background())
        await asyncio.wait_for(a.aclose(), 1)
        self.assertTrue(finished.is_set())
        fake = SimpleNamespace(permission_mode='default', chat=AsyncMock(side_effect=RuntimeError('failure')),
                               aclose=AsyncMock())
        with self.assertRaises(RuntimeError):
            await run_one_shot(fake, 'task')
        fake.aclose.assert_awaited_once()

    async def test_pruning_requires_permission_and_archives_can_be_restored(self):
        for mode, rule, pruned in [('default', None, False), ('dontAsk', None, False),
                                   ('acceptEdits', None, True), ('acceptEdits', 'deny', False)]:
            with self.subTest(mode=mode, rule=rule), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                skill_dir = root / 'skills' / 'demo'
                skill_dir.mkdir(parents=True)
                (skill_dir / 'SKILL.md').write_text('original skill')
                stack.enter_context(patch('agents.skill_evolution.get_evolution_dir', return_value=root / 'evolution'))
                stack.enter_context(patch('agents.skill_evolution._skills_root', return_value=root / 'skills'))
                stack.enter_context(patch('agents.tools._check_permission_rules', return_value=rule))
                stack.enter_context(patch.dict(os.environ, {'MAWOAGENT_SKILL_USAGE_PRUNE_MIN_RETRIEVED': '1'}))
                stack.enter_context(patch('agents.online_skill_evolution.judge_retrieved_skill_usage', new=AsyncMock(
                    return_value=[{'name': 'demo', 'source': 'user', 'skill_dir': str(skill_dir),
                                   'relevant': False, 'used': False}])))
                a = self.agent(permission_mode=mode)
                a._online_evolution_enabled = lambda: True
                a._refresh_runtime_system_prompt = Mock()
                await a._run_skill_usage_tracking('task', 'answer', hits=[{'name': 'demo'}])
                self.assertEqual(skill_dir.exists(), not pruned)
                if pruned:
                    result = skill_evolution.restore_pruned_skill('demo')
                    self.assertTrue(result['ok'])
                    self.assertEqual((skill_dir / 'SKILL.md').read_text(), 'original skill')
                    stats = json.loads((root / 'evolution' / 'skill_usage_stats.json').read_text())
                    self.assertFalse(stats['demo']['pruned'])
                    self.assertEqual(stats['demo']['retrieved'], 0)

    @unittest.skipIf(os.name == 'nt', 'POSIX process group integration')
    async def test_shell_timeout_and_cancellation_stop_descendant_writes(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as tmp:
                ready, marker = Path(tmp) / 'ready', Path(tmp) / 'late-write'
                code = ('from pathlib import Path; import time; '
                        f'Path({str(ready)!r}).touch(); time.sleep(.5); Path({str(marker)!r}).touch()')
                command = shlex.quote(sys.executable) + ' -c ' + shlex.quote(code) + ' & wait'
                task = asyncio.create_task(execute_tool('run_shell', {'command': command, 'timeout': 200 if not cancel else 5000}))
                async def wait_ready():
                    while not ready.exists():
                        await asyncio.sleep(.005)
                await asyncio.wait_for(wait_ready(), 2)
                # Reaching this point proves the loop can run during the shell.
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    self.assertIn('timed out', await task)
                await asyncio.sleep(.55)
                self.assertFalse(marker.exists())

    async def test_mcp_shutdown_waits_for_child_and_reader_tasks(self):
        conn = McpConnection('test', sys.executable, ['-c', 'import time; time.sleep(30)'])
        await conn.connect()
        process, reader = conn._process, conn._reader_task
        manager = McpManager()
        manager._connections['test'] = conn
        await manager.disconnect_all()
        self.assertIsNotNone(process.returncode)
        self.assertTrue(reader.done())
        self.assertFalse(manager._connections)


class StateFixTests(unittest.TestCase):
    def test_resume_filters_project_and_orders_by_last_update(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(session, 'SESSION_DIR', Path(tmp)):
            cwd = str(Path.cwd())
            with patch('agents.session.time.strftime', side_effect=['2026-01-01', '2026-02-01', '2026-03-01']):
                session.save_session('a', {'metadata': {'id': 'a', 'cwd': cwd, 'startTime': '2025-01-01'}})
                session.save_session('b', {'metadata': {'id': 'b', 'cwd': cwd, 'startTime': '2024-01-01'}})
                session.save_session('other', {'metadata': {'id': 'other', 'cwd': '/different/project'}})
            self.assertEqual(session.get_latest_session_id(), 'b')
            self.assertIsNone(session.get_latest_session_id(Path('/empty/project')))
            self.assertIsNone(session.load_session('../escape'))

    def test_restore_rejects_other_project_and_protocol_and_keeps_current_prompt(self):
        a = make_agent()
        data = {'metadata': {'cwd': '/other', 'apiProtocol': 'openai'},
                'openaiMessages': [{'role': 'system', 'content': 'old prompt'}, {'role': 'user', 'content': 'task'}],
                'foldedSessionMemories': [{'working_memory': 'saved'}]}
        with self.assertRaisesRegex(ValueError, 'another project'):
            a.restore_session(data)
        data['metadata'] = {'cwd': str(Path.cwd()), 'apiProtocol': 'anthropic'}
        with self.assertRaisesRegex(ValueError, 'protocol'):
            a.restore_session(data)
        data['metadata']['apiProtocol'] = 'openai'
        with redirect_stdout(io.StringIO()):
            a.restore_session(data)
        self.assertEqual(a._openai_messages[0]['content'], 'test')
        self.assertEqual(a._fold_count, 1)
        asyncio.run(a.aclose())

    def test_quote_normalization_rejects_ambiguous_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'example.txt'
            for text in ['a=“same”\nb=“same”\n', 'a=“same”\nb=″same″\n']:
                path.write_text(text)
                result = _edit_file({'file_path': str(path), 'old_string': '"same"', 'new_string': '"changed"'})
                self.assertIn('Must be unique', result)
                self.assertEqual(path.read_text(), text)
            path.write_text('a=“same”\n')
            self.assertIn('Successfully', _edit_file({'file_path': str(path), 'old_string': '"same"', 'new_string': '"changed"'}))
            self.assertEqual(path.read_text(), 'a="changed"\n')

    def test_failed_atomic_replace_preserves_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            path.write_text('{"original":true}')
            with patch('agents.storage.os.replace', side_effect=OSError('injected failure')):
                with self.assertRaises(OSError):
                    atomic_write_text(path, '{"new":true}')
            self.assertEqual(path.read_text(), '{"original":true}')
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_corrupt_usage_stats_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, patch('agents.skill_evolution.get_evolution_dir', return_value=Path(tmp)):
            path = Path(tmp) / 'skill_usage_stats.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                skill_evolution.record_skill_usage_judgments([{'name': 'demo', 'used': False, 'relevant': False}])
            self.assertEqual(path.read_text(), '{broken')

    def test_multiprocess_usage_updates_do_not_lose_counts(self):
        script = '''import sys
from pathlib import Path
from agents import skill_evolution as se
se.get_evolution_dir = lambda: Path(sys.argv[1])
for _ in range(15):
    se.record_skill_usage_judgments([{"name":"demo", "relevant":True, "used":True}])
'''
        with tempfile.TemporaryDirectory() as tmp:
            processes = [subprocess.Popen([sys.executable, '-c', script, tmp], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                         for _ in range(4)]
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr.decode())
            stats = json.loads((Path(tmp) / 'skill_usage_stats.json').read_text())
            self.assertEqual(stats['demo']['retrieved'], 60)
            self.assertEqual(stats['demo']['used'], 60)


if __name__ == '__main__':
    unittest.main()
