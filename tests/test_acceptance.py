import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from assistant.acceptance import compare, grade, load_suite, strict_json, validate_suite

ROOT = Path(__file__).resolve().parents[1]


def example():
    return {'version': 1, 'cases': [{'id': 'case1', 'kind': 'communication',
            'question': '請簡短', 'history': [], 'expect': {'length_hint': 'concise', 'hints_only': True}}]}


class AcceptanceTests(unittest.TestCase):
    def test_self_authored_quote_filter_keeps_fixed_cases(self):
        from scripts.observe_acceptance import observe
        suite = load_suite(ROOT / 'evals/improvement/communication_quoted_cues.json')
        self.assertTrue(all(row['passed'] for row in grade(suite, observe(suite))))

    def test_quote_filter_generalizes_to_new_words_delimiters_and_negation(self):
        from assistant.communication import conversation_style
        cases = [('說明「簡潔」的意思。', 'unspecified'), ('翻譯『in detail』。', 'unspecified'),
                 ('「精簡」是什麼？請完整解釋。', 'detailed'), ('分析 ``be brief`` 這個片段。', 'unspecified'),
                 ('讀取以下文字：\n~~~text\nstep-by-step\n~~~', 'unspecified'),
                 ('請用「精簡」的方式回覆。', 'concise'), ('Don\'t be "concise"; explain in detail.', 'detailed')]
        for question, expected in cases:
            with self.subTest(question=question):
                self.assertEqual(conversation_style(question, [])['length_hint'], expected)

    def test_self_improvement_acceptance_remains_a_regression(self):
        from scripts.observe_acceptance import observe
        suite = load_suite(ROOT / 'evals/improvement/communication_negation.json')
        self.assertTrue(all(row['passed'] for row in grade(suite, observe(suite))))

    def test_adjacent_negation_generalizes_beyond_supplied_sentences(self):
        from assistant.communication import conversation_style
        cases = {'別太簡短，請逐步解釋': 'detailed',
                 "don't be brief; explain in detail.": 'detailed',
                 '無需完整解釋，兩句就好': 'concise'}
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(conversation_style(question, [])['length_hint'], expected)

    def test_schema_rejects_ambiguous_or_executable_contracts(self):
        for raw in ('{"version":1,"version":2}', '{"value":NaN}'):
            with self.assertRaises(ValueError):
                strict_json(raw)
        variants = []
        for key, value in [('eval', '1+1'), ('expect', {}), ('kind', 'shell'), ('question', ''), ('id', '../bad')]:
            suite = example()
            suite['cases'][0][key] = value
            variants.append(suite)
        suite = example()
        suite['cases'] *= 2
        variants.append(suite)
        suite = example()
        suite['cases'][0]['expect'] = {'hints_only': 1}
        variants.append(suite)
        for suite in variants:
            with self.subTest(suite=suite), self.assertRaises(ValueError):
                validate_suite(suite)
        with self.assertRaises(ValueError):
            validate_suite(example(), ['assistant/context.py'])

    def test_validation_freezes_input_and_bounds_files(self):
        suite = example()
        frozen = validate_suite(suite)
        suite['cases'][0]['expect']['length_hint'] = 'detailed'
        self.assertEqual(frozen['cases'][0]['expect']['length_hint'], 'concise')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'suite.json'
            path.write_bytes(b' ' * 200001)
            with self.assertRaises(ValueError):
                load_suite(path)

    def test_controller_owns_grades_and_detects_regression(self):
        suite = example()
        second = copy.deepcopy(suite['cases'][0])
        second['id'] = 'case2'
        suite['cases'].append(second)
        good = {'length_hint': 'concise', 'hints_only': True}
        bad = {'length_hint': 'mixed', 'hints_only': True}
        before = [{'id': 'case1', 'actual': bad}, {'id': 'case2', 'actual': good}]
        after = [{'id': 'case1', 'actual': good}, {'id': 'case2', 'actual': bad}]
        report = compare(suite, before, after)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['resolved_ids'], ['case1'])
        self.assertEqual(report['regressed_ids'], ['case2'])
        after[1]['actual'] = good
        self.assertEqual(compare(suite, before, after)['status'], 'verified_improvement')
        self.assertEqual(compare(suite, after, after)['status'], 'already_satisfied')
        after[0]['passed'] = True
        with self.assertRaises(ValueError):
            grade(suite, after)

    def test_wrong_observation_ids_types_and_errors_cannot_pass(self):
        for row in ({'id':'other','actual':{}}, {'id':'case1','actual':{},'error':'oops'}):
            with self.assertRaises(ValueError):
                grade(example(), [row])
        self.assertFalse(grade(example(), [{'id':'case1','error':'RuntimeError'}])[0]['passed'])
        self.assertFalse(grade(example(), [{'id':'case1','actual':{'length_hint':'concise','hints_only':1}}])[0]['passed'])

    def test_fresh_observer_measures_three_real_adapters(self):
        suite = example()
        suite['cases'] += [
            {'id':'followup', 'kind':'followup', 'question':'那接下來呢？',
             'history':[{'role':'user','content':'我要建立備份。'}], 'expect':'我要建立備份。'},
            {'id':'retrieval', 'kind':'retrieval', 'query':'backup', 'limit':1,
             'memories':[{'id':1,'kind':'fact','content':'備份保留三份'},
                         {'id':2,'kind':'fact','content':'喜歡火車旅行'}], 'expect':[1]}]
        validate_suite(suite)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec, output = root / 'spec.json', root / 'output.json'
            spec.write_text(json.dumps(suite, ensure_ascii=False), encoding='utf-8')
            subprocess.run([sys.executable, str(ROOT / 'scripts/observe_acceptance.py'),
                            '--suite', str(spec), '--output', str(output)], check=True, cwd=ROOT, capture_output=True)
            observations = json.loads(output.read_text(encoding='utf-8'))
        self.assertTrue(all(row['passed'] for row in grade(suite, observations)))


if __name__ == '__main__':
    unittest.main()
