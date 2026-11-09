import json
import unittest

from scripts.learning import assess_polarity_evidence as study


class PolarityEvidenceTests(unittest.TestCase):
    def test_parser_requires_two_slots_real_observations_and_a_finished_reply(self):
        valid = '{"A":{"state":"T","source":2},"B":{"state":"F","source":3}}'
        self.assertEqual(study.parse_facts(valid, True, 3), [['T', 2], ['F', 3]])
        for raw in (
            valid.replace('"source":2', '"source":1'),
            valid.replace('"source":2', '"source":true'),
            valid.replace('"source":2', '"source":4'),
            valid.replace('"state":"F"', '"state":"已撤回"'),
            valid.replace('"B":', '"A":'),
            '前文' + valid,
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(study.parse_facts(raw, True, 3))
        self.assertIsNone(study.parse_facts(valid, False, 3))

    def test_correct_conclusion_does_not_credit_an_incorrect_source_or_state(self):
        case = next(row for row in study.cases() if row['id'] == 'return_archive')
        direct = dict(id=case['id'], delivered='待查', ended=True)
        extraction = dict(id=case['id'], ended=True, raw=json.dumps(
            {'A': {'state': 'F', 'source': 2}, 'B': {'state': 'U', 'source': 4}}))
        report = study.summarize([case], {'direct': [direct], 'split': [extraction]})
        self.assertEqual(report['groups']['fresh'], dict(count=1, direct=0, solved=1, valid=1, exact_facts=1))
        self.assertEqual(report['details'][0]['facts'], [['F', 2], ['U', 4]])
        self.assertFalse(report['automatic_activation'])

    def test_fixture_keeps_raw_facts_and_control_prompt(self):
        cases = study.cases()
        for case in cases:
            if case['group'] == 'control':
                self.assertEqual(case['question'], case['semantic_question'])
            else:
                for i, source in enumerate(case['clauses'], 1):
                    self.assertIn(source, case['question'])
                    self.assertIn(f'[{i}] {source}', case['semantic_question'])
                self.assertTrue(all(source > 1 for _, source in case['gold']))
