import unittest

from scripts.learning import replay_prefix_probe as probe


class ReplayPrefixProbeTests(unittest.TestCase):
    def cases(self):
        return [dict(id=f'fresh-wash_{case}', group='fresh',
                     prompt=f'{case}。只回 R 代表能、B 代表不能、X 代表未定。', answer=answer)
                for case, answer in zip(('ready', 'blocked', 'pending'), probe.LABELS, strict=True)]

    def test_only_the_mapping_changes_and_answer_follows_its_new_meaning(self):
        originals = self.cases()
        contrasts = probe.relabeled(originals)
        self.assertEqual(len({case['prompt'] for case in contrasts}), 6)
        for case, original in zip(contrasts, originals * 2, strict=True):
            self.assertEqual(case['prompt'].split('。')[0], original['prompt'].split('。')[0])
            self.assertNotEqual(case['answer'], original['answer'])
            self.assertEqual(case['meanings'][case['answer']], probe.STATES[probe.LABELS.index(original['answer'])])

    def test_reports_first_loss_by_case_and_respects_each_cases_mapping(self):
        cases = [*self.cases(), *probe.relabeled(self.cases())]
        def report(replacements=None):
            values = replacements or {}
            return dict(rows=[dict(id=c['id'], output=values.get(c['id'], c['answer']), tokens=2, ended_with_eos=True)
                              for c in cases])
        report0 = report()
        report1 = report({'fresh-wash_pending': 'B', 'contrast-wash-pending-B': 'B'})
        report2 = report({'fresh-wash_pending': 'B'})
        summary = probe.summarize(cases, [report0, report1, report2, report0])
        self.assertEqual(summary['first_lost'], {'fresh-wash_pending': 1, 'contrast-wash-pending-B': 1})
        wash = summary['points']['1']['wash']
        self.assertEqual(wash['fresh-wash_pending']['decoded'], 'refuted')
        self.assertEqual(wash['contrast-wash-pending-B']['decoded'], 'supported')
        self.assertTrue(summary['points']['3']['wash']['fresh-wash_pending']['exact'])
        self.assertFalse(summary['automatic_activation'])
