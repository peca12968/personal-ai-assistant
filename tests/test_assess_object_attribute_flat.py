import json
import unittest

from scripts.learning import assess_object_attribute_flat as flat


class ObjectAttributeFlatTests(unittest.TestCase):
    def test_only_format_changes_in_the_frozen_inputs(self):
        for previous, current in zip(flat.first.cases(), flat.cases(), strict=True):
            self.assertEqual(previous['history'], current['history'])
            self.assertEqual(previous.get('gold'), current.get('gold'))
            if current['group'] == 'control':
                self.assertEqual(previous, current)
            else:
                self.assertEqual(previous['question'].rsplit('\n', 1)[0], current['question'].rsplit('\n', 1)[0])
                self.assertTrue(current['question'].endswith(flat.DIRECT))
                self.assertTrue(current['semantic_question'].endswith(flat.SPLIT))

    def test_normalization_requires_the_complete_flat_schema(self):
        direct = dict(A_state='T', B_state='F')
        split = dict(direct, A_object=True, A_property=True, B_object=True, B_property=True)
        for raw, mode in ((direct, True), (split, False), (dict(split, A_object=1), True),
                          (dict(direct, A_state='X'), False), ({'A': {'state': 'T'}, 'B': {'state': 'F'}}, False)):
            with self.subTest(raw=raw, mode=mode):
                self.assertEqual(flat.normalize(dict(raw=json.dumps(raw), ended=True), split=mode), '')
        self.assertEqual(flat.normalize(dict(raw=json.dumps(direct), ended=False), split=False), '')
        valid = json.loads(flat.normalize(dict(raw=json.dumps(split), ended=True), split=True))
        self.assertEqual(valid['B'], dict(state='F', object=True, property=True))

    def test_flat_states_and_flags_are_scored_independently(self):
        case = next(row for row in flat.cases() if row['id'] == 'shared_opposite')
        direct = dict(A_state='T', B_state='F')
        split = dict(direct, A_object=True, A_property=True, B_object=True, B_property=False)
        self.assertTrue(flat.grade(case, dict(raw=json.dumps(direct), ended=True), split=False)['correct'])
        verdict = flat.grade(case, dict(raw=json.dumps(split), ended=True), split=True)
        self.assertTrue(verdict['correct'])
        self.assertFalse(verdict['guarded_correct'])
        self.assertEqual(verdict['guarded_conclusion'], '待查')
