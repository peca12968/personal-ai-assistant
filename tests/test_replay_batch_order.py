import unittest

from scripts.learning import replay_batch_order as study


class BatchOrderTests(unittest.TestCase):
    def test_training_reports_verify_batch_order_and_starting_weights(self):
        parent = dict(gpu='test', environment={'torch': 'pinned'})
        files = {'adapter': 'pinned'}
        schedule = [[3, 4, 5], [0, 1, 2]]
        report = dict(**parent, weights_before='base', weights_after='changed', adapter_files=files,
                      history=[dict(samples=samples, loss=0.5) for samples in schedule])
        study.check_arm(report, schedule, parent, 'base', files)
        with self.assertRaisesRegex(ValueError, '取樣'):
            study.check_arm(report, schedule[::-1], parent, 'base', files)
        with self.assertRaisesRegex(ValueError, '權重'):
            study.check_arm(report, schedule, parent, 'other', files)
        report['history'][0]['loss'] = float('inf')
        with self.assertRaisesRegex(ValueError, '取樣'):
            study.check_arm(report, schedule, parent, 'base', files)

    def test_equal_totals_keep_case_level_losses_and_each_labels_meaning(self):
        cases = [dict(id='fresh-wash_pending', group='fresh', answer='X'),
                 dict(id='contrast-wash-pending-B', group='fresh', answer='R',
                      meanings={'B': 'supported', 'X': 'refuted', 'R': 'undetermined'})]

        def answers(*values):
            return {'rows': [dict(id=c['id'], output=value, tokens=2, ended_with_eos=True)
                             for c, value in zip(cases, values, strict=True)]}

        result = study.compare(cases, {'parent': answers('X', 'X'), 'deadline': answers('B', 'R'),
                                      'deadline-equipment': answers('X', 'X')})
        self.assertEqual(result['deadline']['groups'], result['parent']['groups'])
        self.assertEqual(result['deadline']['lost'], ['fresh-wash_pending'])
        self.assertEqual(result['deadline']['gained'], ['contrast-wash-pending-B'])
        self.assertEqual(result['deadline']['wash']['fresh-wash_pending']['decoded'], 'refuted')
        self.assertEqual(result['deadline']['wash']['contrast-wash-pending-B']['decoded'], 'undetermined')
        self.assertEqual(result['deadline-equipment']['lost'], [])
