import json
import unittest

from scripts.learning import assess_object_attribute as study


class ObjectAttributeAssayTests(unittest.TestCase):
    def setUp(self):
        self.cases = {case['id']: case for case in study.cases()}

    def grade(self, cid, a, b, *, split=True, ended=True):
        text = json.dumps({'A': a, 'B': b})
        return study.grade(self.cases[cid], {'raw': text, 'ended': ended}, split=split)

    def test_compilation_keeps_assigned_sources_and_ground_truth_out_of_model_text(self):
        row = study.read(study.FIXTURE)['cases'][0]
        old = study.compile_case(row)
        changed_claim = {**row['claims'][0], 'gold': [True, True, 'T']}
        changed = study.compile_case({**row, 'claims': [changed_claim, row['claims'][1]]})
        self.assertNotEqual(old['answer'], changed['answer'])
        for key in ('question', 'semantic_question', 'history', 'records'):
            self.assertEqual(old[key], changed[key])
        for item in self.cases.values():
            if item['group'] != 'control':
                self.assertEqual(item['question'].rsplit('\n', 1)[0], item['semantic_question'].rsplit('\n', 1)[0])
        control = self.cases['literal_control']
        self.assertEqual(control['question'], control['semantic_question'])

    def test_wrong_object_and_wrong_property_are_independent_of_polarity(self):
        b = dict(object=True, property=True, state='T')
        for cid, a in (
            ('similar_cabinet', dict(object=False, property=True, state='T')),
            ('label_only', dict(object=True, property=False, state='F')),
        ):
            with self.subTest(cid=cid):
                result = self.grade(cid, a, b)
                self.assertFalse(result['correct'])
                self.assertTrue(result['guarded_correct'])
                self.assertEqual(result['guarded_states'], ['U', 'T'])
                self.assertEqual((result['object_correct'], result['property_correct']), (2, 2))

    def test_shared_record_can_support_two_positive_or_opposite_states(self):
        for cid, b, expected in (('label_and_seal', 'T', '可行'), ('shared_opposite', 'F', '不能')):
            with self.subTest(cid=cid):
                case = self.cases[cid]
                self.assertEqual(len(case['records']), 1)
                result = self.grade(cid, dict(object=True, property=True, state='T'),
                                    dict(object=True, property=True, state=b))
                self.assertEqual(result['guarded_conclusion'], expected)
                self.assertEqual(result['exact_guarded'], 2)

    def test_unreliable_match_flags_can_lose_a_correct_state(self):
        result = self.grade('same_cabinet_veto', dict(object=True, property=False, state='F'),
                            dict(object=True, property=True, state='T'))
        self.assertTrue(result['correct'])
        self.assertFalse(result['guarded_correct'])
        self.assertEqual(result['guarded_conclusion'], '待查')
        self.assertEqual(result['property_correct'], 1)

    def test_strict_format_checks_types_and_end_marker(self):
        valid = dict(object=True, property=True, state='T')
        for a, ended in ((dict(valid, object=1), True), (dict(valid, property='true'), True),
                         (dict(valid, state='true'), True), (valid, False)):
            with self.subTest(a=a, ended=ended):
                self.assertFalse(self.grade('label_and_seal', a, valid, ended=ended)['valid'])
        self.assertTrue(self.grade('label_and_seal', {'state': 'T'}, {'state': 'T'}, split=False)['correct'])
