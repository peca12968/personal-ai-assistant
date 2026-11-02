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
