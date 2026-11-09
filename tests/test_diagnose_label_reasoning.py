import unittest

from scripts.learning import diagnose_label_reasoning as probe


class LabelReasoningDiagnosticTests(unittest.TestCase):
    def cases(self):
        data = probe.transfer.fixture()
        records = [item for group in data['holdout'] for item in group]
        cases, direct = probe.build_cases(probe.read(probe.transfer.FIXTURE)['holdout'], records)
        return cases, direct, records

    def test_diagnostic_keeps_direct_canaries_and_does_not_leak_the_gold_state(self):
        cases, direct, old = self.cases()
        self.assertEqual(len(cases), 36)
        self.assertEqual(len(direct), 6)
        known = {row['id']: row for row in old}
        for case in cases:
            if case['group'] == 'direct':
                self.assertEqual(case['prompt'], known[case['id']]['prompt'])
            elif case['group'] == 'mapping':
                self.assertIn('已核對的結論', case['prompt'])
                self.assertNotIn('封存扣', case['prompt'])
            else:
                self.assertNotIn('已核對的結論是', case['prompt'])
                for code in known[case['fact'] + '-1']['code_map'].values():
                    self.assertNotIn(code, case['prompt'])

    def test_semantic_output_and_model_mapping_have_separate_scores_and_eos(self):
        cases, _, _ = self.cases()
        rows = [dict(id=c['id'], output=c['answer'], tokens=2, ended_with_eos=True) for c in cases]
        first = next(i for i, c in enumerate(cases) if c['group'] == 'semantic')
        rows[first]['ended_with_eos'] = False
        failed = next(i for i, c in enumerate(cases) if c['group'] == 'mapping')
        rows[failed]['output'] = '說明'
        result = probe.summarize(cases, rows)
        self.assertEqual(result['groups']['semantic'], dict(exact=5, count=6))
        self.assertEqual(result['groups']['mapping'], dict(exact=17, count=18))
        self.assertEqual(result['deterministic_mapping']['semantic'], dict(exact=15, count=18, missing=3))
        self.assertEqual(result['deterministic_mapping']['semantic_rule'], dict(exact=18, count=18, missing=0))
        self.assertIsNone(result['details'][rows[first]['id']]['decoded'])
