"""The reusable pipeline must measure actual handoff, including partial failures."""
import copy
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from assistant.engine import EngineError
from scripts import evaluate_goal_handoff as ev
from scripts.evaluation_reports import load_json, validate_report, DRAFT_CONTEXT_MARKER


class GoalHandoffEvaluationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.out = self.root/'run'
        self.suite = self.root/'suite.json'; self.checks = self.root/'checks.json'
        self.case = {'id': 'one', 'category': 'draft', 'prompt': '請整理此工作目標。',
                     'criteria': ['保留工作與限制'],
                     'history': [{'role': 'user', 'content': '以三點整理筆記，不產檔。'}]}
        self.suite.write_text(json.dumps([self.case], ensure_ascii=False), encoding='utf-8')
        self.checks.write_text(json.dumps([{'id': 'one', 'criteria': ['實際交付三點']}]), encoding='utf-8')
        self.calls = []

    def factory(self, *, draft_error=None, goal_error=None, empty=False, hook=None):
        owner = self
        class Engine:
            def __init__(self, path): self.draft = path.name.startswith('draft-engine')
            def answer(self, request, cancel=None):
                owner.calls.append(('draft' if self.draft else 'goal', request))
                if hook: hook(self.draft)
                error = draft_error if self.draft else goal_error
                if error: raise error
                if self.draft:
                    payload = json.loads(request.split(DRAFT_CONTEXT_MARKER, 1)[1])
                    key = next(key for key, value in payload['source_catalog'].items() if '三點' in value)
                    return json.dumps({'items': [] if empty else [{'kind': 'goal', 'text': '以三點整理筆記，不產檔。',
                                                                  'sources': [{'key': key, 'quote': '三點整理筆記'}]}],
                                       'questions': ['請指定工作。'] if empty else []}, ensure_ascii=False)
                return json.dumps({'tool': 'finish', 'args': {'report': '1. 第一點\n2. 第二點\n3. 第三點'},
                                   'note': '合成測試回覆，非實際模型。'}, ensure_ascii=False)
        return Engine

    def run_pipeline(self, **kwargs):
        with patch('sys.stdout', io.StringIO()):
            return ev.run(self.suite, self.checks, self.out, self.factory(**kwargs))

    def count(self, path, table):
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            return db.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def test_actual_two_phase_calls_isolated_database_and_bound_source_links(self):
        hashes = {}; original = ev.clone_state
        def clone(source, destination):
            hashes[source] = ev.sha(source.read_bytes()); original(source, destination)
        with patch.object(ev, 'clone_state', clone): self.assertTrue(self.run_pipeline())
        drafts = load_json(self.out/'drafts.json'); goals = load_json(self.out/'goals.json')
        ev.validate_links(drafts, goals, {'one': ['實際交付三點']})
        self.assertEqual([phase for phase, _ in self.calls], ['draft', 'goal'])
        self.assertEqual(drafts['results'][0]['calls'][0]['request'], self.calls[0][1])
        self.assertEqual(goals['results'][0]['calls'][0]['prompt'], self.calls[1][1])
        self.assertEqual(goals['results'][0]['prompt'], drafts['results'][0]['draft']['objective_text'])
        self.assertEqual(self.count(self.out/'draft-0.sqlite3', 'autonomous_goals'), 0)
        self.assertEqual(self.count(self.out/'goal-0.sqlite3', 'autonomous_goals'), 1)
        for path, digest in hashes.items(): self.assertEqual(ev.sha(path.read_bytes()), digest)
        self.assertEqual(load_json(self.out/'manifest.json')['collection_status'], 'collected')
        self.assertIsNone(goals['results'][0]['human_grade'])
        # Reviewers must not mistake a changed draft/criterion for the same handoff.
        changed = copy.deepcopy(drafts); changed['results'][0]['human_grade'] = 'invented'
        with self.assertRaises(ValueError): ev.validate_links(changed, goals, {'one': ['實際交付三點']})
        with self.assertRaises(ValueError): ev.validate_links(drafts, goals, {'one': ['不同標準']})

    def test_draft_failure_and_questions_only_never_start_a_goal(self):
        for name, kwargs, stage in [('error', {'draft_error': EngineError('草稿引擎錯誤')}, 'draft_failed'),
                                     ('questions', {'empty': True}, 'draft_needs_input')]:
            self.out = self.root/name; self.calls.clear()
            with self.subTest(name=name):
                self.assertFalse(self.run_pipeline(**kwargs))
                manifest = load_json(self.out/'manifest.json')
                self.assertEqual(manifest['cases']['one']['stage'], stage)
                self.assertEqual(manifest['collection_status'], 'incomplete')
                self.assertFalse((self.out/'goals.json').exists())
                self.assertEqual([phase for phase, _ in self.calls], ['draft'])
                validate_report(load_json(self.out/'drafts.json'))

    def test_goal_engine_failure_retains_draft_and_failed_call_without_retry(self):
        self.assertFalse(self.run_pipeline(goal_error=EngineError('工作引擎錯誤')))
        drafts = load_json(self.out/'drafts.json'); goals = load_json(self.out/'goals.json')
        ev.validate_links(drafts, goals, {'one': ['實際交付三點']})
        row = goals['results'][0]
        self.assertEqual(row['error'], '工作引擎錯誤')
        self.assertNotIn('raw', row['calls'][0])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(load_json(self.out/'manifest.json')['cases']['one']['stage'], 'goal_failed')
        self.assertNotIn('error', drafts['results'][0])

    def test_current_source_guard_rejects_changed_clone_before_goal_call(self):
        original = ev.clone_state
        def changed(source, destination):
            original(source, destination)
            with closing(sqlite3.connect(destination)) as db, db:
                db.execute("UPDATE messages SET content='changed'")
        with patch.object(ev, 'clone_state', changed), self.assertRaises(ValueError):
            self.run_pipeline()
        self.assertEqual([phase for phase, _ in self.calls], ['draft'])
        self.assertEqual(self.count(self.out/'goal-0.sqlite3', 'autonomous_goals'), 0)
        self.assertEqual(self.count(self.out/'draft-0.sqlite3', 'autonomous_goals'), 0)
        self.assertEqual(load_json(self.out/'manifest.json')['collection_status'], 'failed')

    def test_changed_rubric_or_first_report_preserves_sidecar_not_mixed_results(self):
        for name in ('rubric', 'draft-report'):
            self.out = self.root/name; self.calls.clear()
            original_checks = self.checks.read_bytes()
            def hook(draft):
                if not draft:
                    target = self.checks if name == 'rubric' else self.out/'drafts.json'
                    target.write_bytes(target.read_bytes()+b'\n')
            with self.subTest(name=name), self.assertRaises(ValueError): self.run_pipeline(hook=hook)
            self.assertEqual(len(self.calls), 2)
            self.assertEqual(load_json(self.out/'goals.json')['results'], [])
            self.assertEqual(load_json(self.out/'goal-0.request.json')['calls'][0]['status'], 'returned')
            self.assertEqual(load_json(self.out/'manifest.json')['collection_status'], 'failed')
            self.checks.write_bytes(original_checks)

    def test_invalid_rubrics_offline_default_and_existing_output_do_not_call_models(self):
        with patch('sys.argv', ['eval', '--suite', str(self.suite), '--checks', str(self.checks)]), \
                patch.object(ev, 'CodexEngine', side_effect=AssertionError('model called')), \
                patch('sys.stdout', io.StringIO()):
            ev.main()
        self.assertFalse(self.out.exists())
        valid = self.checks.read_bytes()
        for value in ([], [{'id': 'unknown', 'criteria': ['x']}], [{'id': 'one', 'criteria': []}]):
            self.checks.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(ValueError): self.run_pipeline()
            self.assertFalse(self.out.exists())
        self.checks.write_bytes(valid)
        self.assertTrue(self.run_pipeline()); before = (self.out/'drafts.json').read_bytes()
        with self.assertRaises(FileExistsError): self.run_pipeline()
        self.assertEqual((self.out/'drafts.json').read_bytes(), before)
        self.assertEqual(len(self.calls), 2)

    def test_one_failed_draft_is_kept_while_only_the_ready_case_is_executed(self):
        second = copy.deepcopy(self.case); second['id'] = 'two'
        self.suite.write_text(json.dumps([self.case, second], ensure_ascii=False), encoding='utf-8')
        self.checks.write_text(json.dumps([{'id': id, 'criteria': ['實際交付三點']} for id in ('one', 'two')]), encoding='utf-8')
        base = self.factory(); owner = self
        class FailFirst(base):
            def __init__(self, path):
                super().__init__(path); self.fail = path.name == 'draft-engine-0'
            def answer(self, request, cancel=None):
                if self.fail:
                    owner.calls.append(('draft', request)); raise EngineError('只有第一題失敗')
                return super().answer(request, cancel)
        with patch('sys.stdout', io.StringIO()):
            self.assertFalse(ev.run(self.suite, self.checks, self.out, FailFirst))
        drafts = load_json(self.out/'drafts.json'); goals = load_json(self.out/'goals.json')
        ev.validate_links(drafts, goals, {'one': ['實際交付三點'], 'two': ['實際交付三點']})
        self.assertEqual(goals['case_ids'], ['two'])
        self.assertEqual(len(drafts['results']), 2)
        self.assertEqual([phase for phase, _ in self.calls], ['draft', 'draft', 'goal'])
        manifest = load_json(self.out/'manifest.json')
        self.assertEqual(manifest['collection_status'], 'incomplete')
        self.assertEqual(manifest['cases']['one']['stage'], 'draft_failed')
        self.assertEqual(manifest['cases']['two']['stage'], 'goal_collected')


if __name__ == '__main__':
    unittest.main()
