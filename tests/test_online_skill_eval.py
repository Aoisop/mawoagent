from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agents import online_skill_eval as ev
from agents.agent import Agent
from agents.skill_evolution import record_online_skill_provenance


def row(event='event', group='session'):
    return {'skill': 'demo', 'time': '2026-10-12', 'replay_context': {
        'schema_version': 2, 'event_id': event, 'session_id': group,
        'task_user': 'U1: original task', 'context_messages': [],
        'original_response': 'A1: bad historical answer', 'feedback': 'U2: correction',
    }}


def approve(sample):
    review = ev._review_template(sample)
    review.update(approved=True, reviewed_by='test-reviewer', text_only=True,
                  evaluation_mode='original_task', target_turn_index=len(sample['context_messages']),
                  checks=[{'mode': 'contains', 'text': 'GOOD'}])
    return review


def case(split, identity=None):
    return {'sample_id': identity or split, 'group_id': split, 'split': split,
            'context_messages': [], 'task_user': split + ' task', 'requirements': [],
            'checks': [{'mode': 'contains', 'text': 'GOOD'}]}


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.object(ev, 'get_evolution_dir', return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_explicit_capture_and_provenance_transport(self):
        agent = Agent.__new__(Agent)
        agent.session_id = 'session'
        agent._skill_eval_dialog = []
        agent._recent_dialog_messages = lambda **kw: []
        agent._set_pending_skill_extraction_window(original_user_message='U1', assistant_text='A1', retrieved_reference=None)
        pending = agent._pop_pending_skill_extraction_window('U2')
        captured = dict(pending['replay_context'], feedback=pending['next_user_feedback'])
        self.assertEqual(captured['task_user'], 'U1')
        self.assertEqual(captured['original_response'], 'A1')
        self.assertEqual(captured['feedback'], 'U2')
        self.assertEqual(captured['context_messages'], [])
        with patch('agents.skill_evolution.get_evolution_dir', return_value=self.root):
            record_online_skill_provenance(action='add', skill_name='demo', messages=[], replay_context=captured)
        saved = ev._read_jsonl(self.root / ev.ONLINE_PROVENANCE_LOG)[0]
        indexed = ev._read_json(self.root / 'online_skill_provenance.json', {})['demo']['sources'][0]
        self.assertEqual(saved['replay_context'], captured)
        self.assertEqual(indexed['replay_context'], captured)
        agent._set_pending_skill_extraction_window(original_user_message='U2', assistant_text='A2', retrieved_reference=None)
        self.assertEqual(agent._pending_skill_extraction_window['replay_context']['context_messages'],
                         [{'role': 'user', 'content': 'U1'}, {'role': 'assistant', 'content': 'A1'}])

    def test_legacy_feedback_never_automatically_approved(self):
        legacy = {'messages': [{'role': 'user', 'content': 'U1'}, {'role': 'assistant', 'content': 'A1'},
                               {'role': 'user', 'content': 'U2'}]}
        sample = ev._source_sample('demo', legacy)
        self.assertEqual((sample['task_user'], sample['feedback']), ('U1', 'U2'))
        selected, info = ev._build_replay_pool('demo', [legacy], write_artifacts=True, pool_limit=200, replay_limit=24)
        self.assertEqual(selected, [])
        self.assertEqual(info['review_required'], 1)
        self.assertFalse(ev._read_jsonl(Path(info['review_file']))[0]['approved'])

    def test_review_validation_and_group_split(self):
        first = ev._source_sample('demo', row())
        second = ev._source_sample('demo', row('event2'))
        r1, r2 = approve(first), approve(second)
        r2['group_id'] = 'attempt-to-resplit'
        a, _ = ev._reviewed_sample(first, r1)
        b, _ = ev._reviewed_sample(second, r2)
        self.assertEqual((a['group_id'], a['split']), (b['group_id'], b['split']))
        self.assertNotIn('original_response', a)
        self.assertNotIn('feedback', a)
        r1['source_hash'] = 'changed'
        self.assertEqual(ev._reviewed_sample(first, r1)[1], 'source_changed')
        r1 = approve(first)
        r1['context_messages'] = [{'role': 'assistant', 'content': first['original_response']}]
        self.assertEqual(ev._reviewed_sample(first, r1)[1], 'context_override_not_allowed')
        first['session_id'] = ''
        r1 = approve(first)
        self.assertEqual(ev._reviewed_sample(first, r1)[1], 'conversation_group_required')

    def nested_sample(self):
        return ev._source_sample('demo', {'session_id': 'session', 'messages': [
            {'role': 'user', 'content': 'U1'}, {'role': 'assistant', 'content': 'A1'},
            {'role': 'user', 'content': 'U2'}, {'role': 'assistant', 'content': 'A2'},
            {'role': 'user', 'content': 'U3'}]})

    def test_nested_windows_bind_original_or_feedback_without_answer_leak(self):
        sample = self.nested_sample()
        review = approve(sample)
        review['target_turn_index'] = 0
        original, reason = ev._reviewed_sample(sample, review)
        self.assertEqual(reason, '')
        self.assertEqual(original['task_user'], 'U1')
        self.assertEqual(original['context_messages'], [])
        review.update(evaluation_mode='feedback_revision', target_turn_index=2, feedback_parent_turn_index=0)
        revision, reason = ev._reviewed_sample(sample, review)
        self.assertEqual(reason, '')
        self.assertEqual(revision['task_user'], 'U2')
        self.assertEqual(revision['context_messages'], ev._source_turns(sample)[:2])
        self.assertEqual(original['split'], revision['split'])
        review['target_turn_index'] = 4
        self.assertEqual(ev._reviewed_sample(sample, review)[1], 'answered_user_turn_required')

    def test_plain_approval_and_task_text_edits_cannot_change_target(self):
        sample = self.nested_sample()
        review = ev._review_template(sample)
        review.update(approved=True, reviewed_by='reviewer', text_only=True)
        self.assertEqual(ev._reviewed_sample(sample, review)[1], 'evaluation_mode_required')
        review = approve(sample)
        review['task_user'] = 'U1'
        self.assertEqual(ev._reviewed_sample(sample, review)[1], 'task_override_not_allowed')
        review.pop('task_user')
        review.update(target_turn_index=0, context_messages=ev._source_turns(sample)[:2])
        self.assertEqual(ev._reviewed_sample(sample, review)[1], 'context_override_not_allowed')
        review.pop('context_messages')
        review.update(evaluation_mode='feedback_revision', target_turn_index=2)
        self.assertEqual(ev._reviewed_sample(sample, review)[1], 'feedback_parent_required')

    def test_nested_same_task_deduplicates_and_conflicting_labels_block(self):
        sample = self.nested_sample()
        short = ev._source_sample('demo', {'session_id': 'session', 'messages': ev._source_turns(sample)[:3]})
        directory = ev._root() / 'datasets' / ev._lineage_id_for_skill('demo')
        ev._write_jsonl(directory / 'replay_pool_v2.jsonl', [sample, short])
        reviews = [dict(approve(s), target_turn_index=0) for s in (sample, short)]
        ev._write_jsonl(directory / 'replay_review.jsonl', reviews)
        selected, info = ev._build_replay_pool('demo', [], write_artifacts=True, pool_limit=200, replay_limit=24)
        self.assertEqual(len(selected), 1)
        self.assertEqual(info['duplicates_removed'], 1)
        self.assertEqual(len(selected[0]['source_sample_ids']), 2)
        reviews[0]['requirements'] = ['A different requirement']
        ev._write_jsonl(directory / 'replay_review.jsonl', reviews)
        selected, info = ev._build_replay_pool('demo', [], write_artifacts=True, pool_limit=200, replay_limit=24)
        self.assertEqual(selected, [])
        self.assertEqual(info['review_required'], 2)

    def test_old_review_migrates_without_reusing_approval(self):
        sample = self.nested_sample()
        directory = ev._root() / 'datasets' / ev._lineage_id_for_skill('demo')
        ev._write_jsonl(directory / 'replay_pool_v2.jsonl', [sample])
        old = approve(sample)
        old.pop('review_schema')
        ev._write_jsonl(directory / 'replay_review.jsonl', [old])
        selected, info = ev._build_replay_pool('demo', [], write_artifacts=True, pool_limit=200, replay_limit=24)
        self.assertEqual(selected, [])
        migrated = ev._read_jsonl(Path(info['review_file']))[0]
        self.assertEqual(migrated['previous_review'], old)
        self.assertFalse(migrated['approved'])

    def test_pool_caps_and_v1_migration(self):
        directory = ev._root() / 'datasets' / ev._lineage_id_for_skill('demo')
        ev._write_jsonl(directory / 'replay_pool.jsonl', [{'messages': [
            {'role': 'user', 'content': 'old task'}, {'role': 'assistant', 'content': 'old reply'}]}])
        _, info = ev._build_replay_pool('demo', [], write_artifacts=True, pool_limit=200, replay_limit=24)
        self.assertEqual(info['review_required'], 1)
        rows = [row(str(i), str(i // 5)) for i in range(250)]
        samples = [ev._source_sample('demo', item) for item in rows]
        ev._write_jsonl(directory / 'replay_review.jsonl', [approve(s) for s in samples])
        selected, info = ev._build_replay_pool('demo', rows, write_artifacts=True, pool_limit=200, replay_limit=24)
        self.assertEqual(info['stored'], 200)
        self.assertEqual(len(selected), 24)
        self.assertLessEqual(max(ev.Counter(x['group_id'] for x in selected).values()), 2)
        self.assertTrue(any(x['split'] == 'promotion_test' for x in selected))

    def test_unreviewed_full_pipeline_never_calls_model(self):
        ev._write_jsonl(self.root / ev.ONLINE_PROVENANCE_LOG, [row()])
        async def query(*args):
            self.fail('Unreviewed evidence must not call the model')
        with patch.object(ev, '_active_skill_snapshots', return_value={'demo': {'name': 'demo', 'instructions': 'JSON'}}):
            report = asyncio.run(ev.evaluate_online_skill_evolution_async(side_query=query))
        self.assertEqual(report['budget']['model_calls'], 0)
        self.assertEqual(report['skills'][0]['status'], 'review_required')
        self.assertFalse((ev._root() / 'champions.json').exists())

    def test_read_only_leaves_no_artifacts(self):
        with patch.object(ev, '_active_skill_snapshots', return_value={'demo': {'name': 'demo', 'instructions': 'base'}}):
            asyncio.run(ev.evaluate_online_skill_evolution_async(write_report=False, write_artifacts=False))
        self.assertEqual(list(self.root.iterdir()), [])


class PairedEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def compare(self, query, champion=None, samples=None, **kwargs):
        return await ev._run_comparison({'name': 'demo', 'instructions': 'BASE'},
            samples or [case('mutate_dev'), case('promotion_test')], query, champion or {},
            max_candidates=kwargs.get('max_candidates', 2), min_replay_samples=2, min_promotion_tests=1,
            min_rule_pass_rate=.8, usage_eligible=True)

    async def test_baseline_and_candidate_regenerate_same_tasks_champion_retested(self):
        calls = []
        async def model(system, user):
            calls.append((system, json.loads(user)))
            if system.startswith('Propose'):
                self.assertNotIn('promotion_test', user)
                return json.dumps({'candidates': [{'instructions': 'CANDIDATE'}]})
            return 'GOOD' if system.startswith('CANDIDATE') else 'BAD'
        query = ev.QueryBudget(model)
        result = await self.compare(query, {'snapshot': {'name': 'demo', 'instructions': 'CHAMP'},
                                            'summary': {'average_score': 99999}})
        self.assertTrue(result['promoted'])
        self.assertEqual(result['champion_test']['average_score'], .5)
        generated = [(s, u) for s, u in calls if not s.startswith('Propose')]
        for split in ('mutate_dev', 'promotion_test'):
            base = next(u for s, u in generated if s.startswith('BASE') and u['task_user'].startswith(split))
            candidate = next(u for s, u in generated if s.startswith('CANDIDATE') and u['task_user'].startswith(split))
            self.assertEqual(base, candidate)
            self.assertNotIn('original_response', base)
            self.assertNotIn('checks', base)
            self.assertNotIn('requirements', base)
        self.assertTrue(all(x['response_source'] == 'regenerated' for x in result['outputs']))

    async def test_dev_improvement_with_test_regression_cannot_promote(self):
        async def model(system, user):
            if system.startswith('Propose'):
                return '{"candidates":[{"instructions":"CANDIDATE"}]}'
            dev = 'mutate_dev' in user
            return 'GOOD' if (system.startswith('CANDIDATE') == dev) else 'BAD'
        result = await self.compare(ev.QueryBudget(model))
        self.assertFalse(result['promoted'])
        self.assertEqual(result['status'], 'watch')

    async def test_original_and_feedback_modes_cannot_substitute_for_each_other(self):
        async def model(*args):
            self.fail('Mismatched evaluation modes should not spend model calls')
        samples = [dict(case('mutate_dev'), evaluation_mode='original_task'),
                   dict(case('promotion_test'), evaluation_mode='feedback_revision')]
        result = await self.compare(ev.QueryBudget(model), samples=samples)
        self.assertFalse(result['promoted'])
        self.assertEqual(result['status'], 'incubating')

    async def test_missing_holdout_does_not_force_split_or_promote(self):
        async def model(*args):
            return 'GOOD'
        result = await self.compare(ev.QueryBudget(model), samples=[case('mutate_dev', 'a'), case('mutate_dev', 'b')])
        self.assertFalse(result['promoted'])
        self.assertEqual(result['status'], 'incubating')

    async def test_budget_and_invalid_judge_fail_closed(self):
        for response, expected in [('', 'empty_model_response'),
                                   ('{"judgments":[{"index":0,"pass":"false","reason":"bad"}]}', 'invalid_judge_response')]:
            async def model(system, user):
                return response if system.startswith('Evaluate') else 'GOOD'
            sample = case('mutate_dev')
            sample['requirements'] = ['A factual requirement']
            result = await self.compare(ev.QueryBudget(model), samples=[sample, case('promotion_test')])
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(result['reason'], expected)
            self.assertFalse(result['promoted'])
        async def model(*args):
            return 'GOOD'
        result = await self.compare(ev.QueryBudget(model, max_calls=1))
        self.assertEqual(result['reason'], 'model_call_budget_exhausted')
        self.assertFalse(result['promoted'])

    async def test_input_limit_timeout_and_cache(self):
        async def model(*args):
            return 'GOOD'
        budget = ev.QueryBudget(model, max_request_chars=5)
        with self.assertRaisesRegex(ev.EvaluationError, 'request_too_large'):
            await budget('123456', '')
        self.assertEqual(budget.calls, 0)
        budget = ev.QueryBudget(model, max_calls=1)
        self.assertEqual(await budget('same', 'task'), await budget('same', 'task'))
        self.assertEqual((budget.calls, budget.cache_hits), (1, 1))
        async def slow(*args):
            await asyncio.sleep(.1)
            return 'GOOD'
        with self.assertRaisesRegex(ev.EvaluationError, 'TimeoutError'):
            await ev.QueryBudget(slow, timeout=.001)('', '')

    async def test_skill_keywords_do_not_create_rules(self):
        async def model(*args):
            return 'GOOD plain text'
        summary = await ev._evaluate_variant({'name': 'demo', 'instructions': 'JSON must cite sources'},
                      [case('mutate_dev')], ev.QueryBudget(model), [], [])
        self.assertEqual(summary['average_score'], 1)
        self.assertEqual(summary['hard_failures'], 0)

    async def test_truncated_provider_response_is_rejected(self):
        for provider in ('openai', 'anthropic'):
            agent = Agent.__new__(Agent)
            agent.model = 'test'
            agent._check_budget = lambda: {'exceeded': False}
            agent.side_input_tokens = agent.side_output_tokens = 0
            agent._anthropic_client = agent._openai_client = None
            if provider == 'openai':
                response = SimpleNamespace(usage=None, choices=[SimpleNamespace(
                    finish_reason='length', message=SimpleNamespace(content='GOOD truncated'))])
                agent._openai_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                    create=AsyncMock(return_value=response))))
            else:
                response = SimpleNamespace(usage=None, stop_reason='max_tokens', content=[])
                agent._anthropic_client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
            with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, 'incomplete_evaluation_response'):
                await agent._build_side_query(require_complete=True)('system', 'task')

    async def test_promoted_artifacts_are_separate_and_recoverable(self):
        async def model(*args):
            return 'GOOD'
        samples = [case('mutate_dev'), case('promotion_test')]
        bundle = await self.compare(ev.QueryBudget(model), samples=samples)
        self.assertTrue(bundle['promoted'])
        with tempfile.TemporaryDirectory() as directory, patch.object(ev, 'get_evolution_dir', return_value=Path(directory)):
            artifact = ev._persist_eval_artifacts('demo', samples, bundle)
            summary = ev._read_json(Path(artifact['run_dir']) / 'summary.json', {})
            self.assertEqual(summary['baseline_snapshot']['instructions'], 'BASE')
            self.assertTrue(ev._read_jsonl(Path(artifact['run_dir']) / 'outputs.jsonl'))
            champion = ev._read_json(ev._root() / 'champions.json', {})['champions'][ev._lineage_id_for_skill('demo')]
            self.assertEqual(champion['snapshot']['instructions'], 'BASE')
            self.assertFalse((Path(directory) / 'SKILL.md').exists())

    def test_comparison_requires_identical_sample_ids(self):
        base = {'complete': True, 'sample_ids': ['a'], 'hard_failures': 0, 'average_score': .5}
        candidate = dict(base, sample_ids=['b'], average_score=1)
        self.assertFalse(ev._better(candidate, base, .05))


if __name__ == '__main__':
    unittest.main()
