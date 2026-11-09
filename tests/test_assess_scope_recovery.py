import json
import unittest

from scripts.learning import assess_scope_recovery as study


class ScopeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.cases = {row['id']: row for row in study.fresh_cases()}

    def reply(self, cid, state, source, *, ended=True):
        return study.grade(self.cases[cid], dict(raw=json.dumps({'state': state, 'source': source}), ended=ended))

    def test_recovery_prompt_hides_excluded_rows_but_preserves_ids_and_exact_text(self):
        for row in self.cases.values():
            full = row['question']
            filtered = row['semantic_question']
            self.assertEqual(full.splitlines()[0], filtered.splitlines()[0])
            self.assertEqual(full.splitlines()[-1], filtered.splitlines()[-1])
            for item in row['source_manifest']:
                text = row['text'][item['start']:item['end']]
                line = f'[{item["id"]}] {text}'
                self.assertIn(line, full.splitlines())
                self.assertEqual(line in filtered.splitlines(), item['id'] in row['allowed_ids'])
            self.assertEqual(row['initial_guarded'][row['target']], ['U', None])

    def test_recovers_current_veto_without_changing_other_condition(self):
        case = self.cases['silver_veto']
        result = self.reply('silver_veto', 'F', 4)
        self.assertEqual((result['before'], result['conclusion']), ('待查', '不能'))
        self.assertTrue(result['exact_target'])
        self.assertEqual(result['states'][1], case['initial_guarded'][1])
        self.assertEqual(self.reply('silver_pending', 'U', 4)['conclusion'], '待查')

    def test_missing_observation_and_current_positive_are_distinct(self):
        absent = self.reply('strap_missing', 'U', None)
        self.assertEqual((absent['conclusion'], absent['changed'], absent['exact_target']), ('待查', False, True))
        present = self.reply('strap_current', 'T', 3)
        self.assertEqual((present['conclusion'], present['changed'], present['exact_target']), ('可行', True, True))
        for source in (3, 4):
            with self.subTest(source=source):
                other = self.reply('strap_missing', 'T', source)
                self.assertIsNone(other['reason'])
                self.assertFalse(other['correct'])
                self.assertFalse(other['exact_target'])

    def test_rejected_ids_and_invalid_replies_leave_prior_unknown(self):
        for state, source, ended, reason in (
            ('T', 2, True, 'undated_media'), ('F', 1, True, 'rule'), ('T', 10, True, 'out_of_range'),
            ('T', None, True, 'missing_source'), ('T', True, True, 'invalid_reply'),
            ('T', 3, False, 'invalid_reply'),
        ):
            with self.subTest(source=source, ended=ended):
                result = self.reply('strap_current', state, source, ended=ended)
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['conclusion'], '待查')
                self.assertFalse(result['changed'])

    def test_actual_rejection_slots_are_rebuilt_and_literal_control_is_unchanged(self):
        receipt = {'targets': [
            {'id': 'catch_repaired', 'facts': [['T', 2], ['T', 1]]},
            {'id': 'undated_photo', 'facts': [['T', 2], ['T', 3]]},
            {'id': 'cabinet_photo_unknown', 'facts': [['T', 2], ['T', 3]]},
        ]}
        cases = study.cases(receipt)
        self.assertEqual([row['target'] for row in cases[:3]], [1, 0, 0])
        self.assertEqual([row['group'] for row in cases], ['replay'] * 3 + ['fresh'] * 4 + ['control'])
        self.assertEqual(cases[-1]['question'], cases[-1]['semantic_question'])
        with self.assertRaisesRegex(ValueError, '全部'):
            study.cases({'targets': receipt['targets'][:-1]})
