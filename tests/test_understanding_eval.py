import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.engine import Assistant
from assistant.store import Store
from assistant.companion_profile import DEFAULTS
from scripts.evaluate_understanding import SUITE, fingerprint, load_report, mechanical_check, prepare_case, save_report, validate_cases
from scripts.review_understanding import apply_reviews, compare, summarize
from scripts.evaluate_understanding import main as evaluate_main


class UnderstandingEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cases = validate_cases(json.loads(SUITE.read_text(encoding='utf-8')))

    def test_suite_covers_six_dimensions_with_explicit_rubrics(self):
        self.assertEqual(len(self.cases), 36)
        self.assertEqual({row['category'] for row in self.cases},
                         {'constraints', 'clarification', 'corrections', 'context', 'reliability', 'reasoning'})
        for category in {row['category'] for row in self.cases}:
            self.assertEqual(sum(row['category'] == category for row in self.cases),
                             10 if category == 'corrections' else 6 if category == 'reliability' else 5)
        with self.assertRaises(ValueError):
            validate_cases(self.cases + [self.cases[0]])
        with self.assertRaises(ValueError):
            validate_cases([{'id': 'bad', 'category': 'context', 'prompt': 'test', 'criteria': []}])

    def test_companion_fixture_updates_and_resets_real_persisted_preferences(self):
        case = {'id': 'profile', 'category': 'communication', 'prompt': '目前稱呼', 'criteria': ['目前設定正確'],
                'prior_companion_profile': {**DEFAULTS, 'assistant_name': '原名字'},
                'companion_profile': {**DEFAULTS, 'assistant_name': '新名字'},
                'history': [{'role': 'assistant', 'content': '我是原名字。'}]}
        validate_cases([case])
        store = Store(self.root / 'profile.sqlite3')
        sid = prepare_case(store, case)
        context = Assistant(store, None).prepare_context(sid, case['prompt'])
        self.assertEqual(context['companion_profile']['settings']['assistant_name'], '新名字')
        self.assertEqual(context['companion_profile']['revision'], 2)
        self.assertEqual(context['recent_conversation'][0]['content'], '我是原名字。')
        reset = {key: value for key, value in case.items() if key != 'companion_profile'}
        reset['reset_companion_profile'] = True
        sid = prepare_case(store, reset)
        self.assertEqual(Assistant(store, None).prepare_context(sid, reset['prompt'])['companion_profile']['settings'], DEFAULTS)
        with self.assertRaises(ValueError):
            validate_cases([{**case, 'reset_companion_profile': True}])

    def test_long_context_scenarios_actually_exercise_older_excerpts_and_truncation(self):
        for id in ('long_constraint', 'long_correction', 'omitted_middle'):
            case = next(row for row in self.cases if row['id'] == id)
            store = Store(self.root / (id + '.sqlite3'))
            sid = prepare_case(store, case)
            context = Assistant(store, None).prepare_context(sid, case['prompt'])
            if id == 'omitted_middle':
                self.assertTrue(context['recent_conversation'][0]['truncated'])
                self.assertNotIn('不可見-947', json.dumps(context, ensure_ascii=False))
            else:
                self.assertTrue(context['earlier_user_excerpts'])
                self.assertEqual(len(context['recent_conversation']), 16)

    def test_resume_preserves_answers_and_rejects_changed_run(self):
        path = self.root / 'report.json'
        signature = {'suite_sha256': 'suite', 'prompt_sha256': 'prompt', 'source_sha256': 'source'}
        report = load_report(path, signature, ['one', 'two'], False)
        report['results'].append({'id': 'one', 'answer': '已保留的答案', 'human_grade': None})
        save_report(path, report)
        reopened = load_report(path, signature, ['one', 'two'], True)
        self.assertEqual(reopened['results'][0]['answer'], '已保留的答案')
        self.assertIsNone(reopened['results'][0]['human_grade'])
        with self.assertRaises(ValueError):
            load_report(path, signature, ['one', 'two'], False)
        with self.assertRaises(ValueError):
            load_report(path, {**signature, 'prompt_sha256': 'changed'}, ['one', 'two'], True)
        with self.assertRaises(ValueError):
            load_report(path, signature, ['two'], True)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), report)

    def test_invalid_resume_and_duplicate_result_are_rejected(self):
        path = self.root / 'report.json'
        with self.assertRaises(ValueError):
            load_report(path, {}, ['one'], True)
        report = load_report(path, {}, ['one'], False)
        report['results'] = [{'id': 'one'}, {'id': 'one'}]
        save_report(path, report)
        with self.assertRaises(ValueError):
            load_report(path, {}, ['one'], True)

    def test_changed_communication_prompt_prevents_mixing_resumed_answers(self):
        (self.root / 'assistant').mkdir()
        (self.root / 'assistant/engine.py').write_text('original source', encoding='utf-8')
        (self.root / 'prompts').mkdir()
        (self.root / 'prompts/system.txt').write_text('system', encoding='utf-8')
        communication = self.root / 'prompts/communication.txt'
        communication.write_text('只聽，不急著給解法。', encoding='utf-8')
        path = self.root / 'report.json'
        with patch('scripts.evaluate_understanding.ROOT', self.root):
            original = fingerprint()
            report = load_report(path, original, ['one', 'two'], False)
            report['results'].append({'id': 'one', 'answer': '原回答'})
            save_report(path, report)
            before = path.read_bytes()
            communication.write_text('改成直接給步驟。', encoding='utf-8')
            changed = fingerprint()
            self.assertEqual(original['prompt_sha256'], changed['prompt_sha256'])
            self.assertNotEqual(original['communication_prompt_sha256'], changed['communication_prompt_sha256'])
            with self.assertRaises(ValueError):
                load_report(path, changed, ['one', 'two'], True)
            self.assertEqual(path.read_bytes(), before)
    def test_last_answer_is_not_saved_if_prompt_changes_during_generation(self):
        (self.root / 'assistant').mkdir()
        (self.root / 'prompts').mkdir()
        (self.root / 'prompts/system.txt').write_text('system', encoding='utf-8')
        communication = self.root / 'prompts/communication.txt'
        communication.write_text('before', encoding='utf-8')
        suite = self.root / 'custom.json'
        suite.write_text(json.dumps([{'id': 'last', 'category': 'reliability', 'prompt': 'question',
                                     'criteria': ['criterion']}]), encoding='utf-8')
        report = self.root / 'run.json'
        def generate(*args):
            communication.write_text('after', encoding='utf-8')
            return {'content': 'must not be recorded under old fingerprint'}
        with patch('scripts.evaluate_understanding.ROOT', self.root), \
             patch('scripts.evaluate_understanding.CodexEngine'), \
             patch('scripts.evaluate_understanding.Assistant') as app, \
             patch('sys.argv', ['evaluate', '--live', '--suite', str(suite), '--report', str(report)]), \
             patch('sys.stderr', new_callable=io.StringIO):
            app.return_value.prepare_context.return_value = {'current_request': 'question'}
            app.return_value.ask.side_effect = generate
            with self.assertRaises(SystemExit) as raised:
                evaluate_main()
            self.assertEqual(raised.exception.code, 1)
        saved = json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual(saved['case_ids'], ['last'])
        self.assertEqual(saved['results'], [])

    def test_format_check_does_not_imply_semantic_grade(self):
        case = {'check': 'three_bullets'}
        self.assertTrue(mechanical_check(case, '- a\n- b\n- c')['three_bullets'])
        self.assertFalse(mechanical_check(case, '說明\n- a\n- b\n- c')['three_bullets'])
        self.assertFalse(mechanical_check(case, '- a\n- b\n- ')['three_bullets'])
        self.assertEqual(mechanical_check({}, '任意回答'), {})

    def test_review_requires_every_criterion_and_preserves_answer(self):
        report = {'case_ids': ['one'], 'results': [{'id': 'one', 'category': 'test', 'answer': '原始答案', 'criteria': ['a', 'b']}]}
        self.assertEqual(summarize(report)['reviews']['pending'], 1)
        with self.assertRaises(ValueError):
            apply_reviews(report, [{'id': 'one', 'criteria_pass': [True], 'note': '不足'}], 'development_agent')
        self.assertNotIn('reviews', report['results'][0])
        apply_reviews(report, [{'id': 'one', 'criteria_pass': [True, False], 'note': '第二項未符合'}], 'development_agent')
        self.assertEqual(summarize(report)['reviews']['fail'], 1)
        self.assertEqual(report['results'][0]['answer'], '原始答案')
        apply_reviews(report, [{'id': 'one', 'criteria_pass': [True, True], 'note': '使用者逐項核對'}], 'user')
        self.assertEqual(len(report['results'][0]['reviews']), 2)
        self.assertEqual(summarize(report)['latest_reviewers'], ['user'])

    def test_invalid_review_batch_does_not_partially_update(self):
        report = {'case_ids': ['one'], 'results': [{'id': 'one', 'category': 'test', 'answer': '合成答案', 'criteria': ['a']}]}
        with self.assertRaises(ValueError):
            apply_reviews(report, [{'id': 'one', 'criteria_pass': [True], 'note': '核對'},
                                   {'id': 'missing', 'criteria_pass': [True], 'note': '核對'}], 'development_agent')
        self.assertNotIn('reviews', report['results'][0])

    def test_comparison_keeps_unreviewed_cases_pending_and_rejects_other_suite(self):
        report = {'fingerprint': {'suite_sha256': 'a' * 64}, 'case_ids': ['one'],
                  'results': [{'id': 'one', 'category': 'test', 'answer': '合成答案', 'criteria': ['a']}]}
        self.assertEqual(compare(report, report)['pending_ids'], ['one'])
        with self.assertRaises(ValueError):
            compare(report, {**report, 'fingerprint': {'suite_sha256': 'b' * 64}})


if __name__ == '__main__':
    unittest.main()
