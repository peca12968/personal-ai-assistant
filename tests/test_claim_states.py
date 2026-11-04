import json
import unittest

from scripts.evaluate_understanding import ROOT, validate_cases


class ClaimStateFixtureTests(unittest.TestCase):
    def setUp(self):
        self.rows = json.loads((ROOT/'evals/claim_states.json').read_text(encoding='utf8'))

    def test_positive_and_negative_claims_use_the_same_evidence_in_fresh_cases(self):
        validate_cases(self.rows)
        self.assertEqual(len(self.rows), 12)
        groups = {}
        for row in self.rows:
            groups.setdefault(row['pair'], []).append(row)
            self.assertIn('命題：'+row['claim']+'證據：'+row['evidence'], row['prompt'])
            self.assertNotIn('history', row)
        self.assertEqual(len(groups), 6)
        for pair in groups.values():
            self.assertEqual(len(pair), 2)
            self.assertEqual({r['negated'] for r in pair}, {True, False})
            self.assertEqual(len({r['evidence'] for r in pair}), 1)
            self.assertEqual(len({r['available'] for r in pair}), 1)
            self.assertNotEqual(pair[0]['claim'], pair[1]['claim'])

    def test_reference_labels_follow_all_compatible_worlds_not_confirmed_status(self):
        counts = dict.fromkeys(('成立', '不成立', '未知'), 0)
        for row in self.rows:
            self.assertTrue(row['available'] is None or type(row['available']) is bool)
            worlds = (False, True) if row['available'] is None else (row['available'],)
            outcomes = {not w if row['negated'] else w for w in worlds}
            expected = '成立' if outcomes == {True} else '不成立' if outcomes == {False} else '未知'
            self.assertEqual(row['expected_label'], expected)
            self.assertIn('「'+expected+'」', row['criteria'][0])
            counts[expected] += 1
        self.assertEqual(counts, {'成立':4, '不成立':4, '未知':4})

    def test_development_failures_are_separate_from_new_objects_and_conditions(self):
        roles = {role:{r['pair'] for r in self.rows if r['study_role']==role}
                 for role in ('development','transfer')}
        self.assertEqual(roles['development'], {'speaker','report'})
        self.assertEqual(roles['transfer'], {'pump','service','lamp','filter'})
        self.assertFalse(roles['development'] & roles['transfer'])
