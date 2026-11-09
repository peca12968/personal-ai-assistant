import json
import unittest

from scripts.learning import assess_indexed_scope as study


class IndexedScopeTests(unittest.TestCase):
    def test_index_preserves_every_original_character_and_replays_literal_requests(self):
        rows = study.cases()
        for case, previous in zip(rows[:8], study.natural.cases(), strict=True):
            self.assertEqual((case['question'], case['history']), (previous['question'], previous['history']))
        for case in rows:
            if case['group'] == 'control':
                self.assertEqual(case['question'], case['semantic_question'])
                continue
            text = case['text']
            self.assertEqual(''.join(text[row['start']:row['end']] for row in case['source_manifest']), text)
            numbered = ''.join(f'[{row["id"]}] ' + text[row['start']:row['end']] for row in case['source_manifest'])
            self.assertTrue(case['semantic_question'].startswith(numbered))
            self.assertTrue(case['semantic_question'].endswith(study.DIRECTIVE))

    def test_media_gate_keeps_undated_and_current_photo_observations_distinct(self):
        cases = {row['id']: row for row in study.cases()}
        self.assertEqual(cases['undated_photo']['source_manifest'][1]['excluded'], 'undated_media')
        self.assertEqual(cases['cabinet_photo_unknown']['source_manifest'][1]['excluded'], 'undated_media')
        self.assertIsNone(cases['cabinet_photo_current']['source_manifest'][1]['excluded'])
        quoted = '這回檢查「無日期的照片」欄名的拼寫。'
        case = {'text': quoted, 'spans': study.natural.segments(quoted)}
        self.assertIsNone(study.manifest(case)[0]['excluded'])
        self.assertEqual(cases['panel_old_test']['source_manifest'][1]['excluded'], 'earlier')

    def test_time_gate_is_independent_from_model_state_and_source_restoration(self):
        case = next(c for c in study.cases() if c['id'] == 'undated_photo')
        row = dict(ended=True, raw='{"A":{"state":"T","source":2},"B":{"state":"T","source":3}}')
        result = study.grade(case, row)
        self.assertEqual(result['restored'][0], ['T', '桌面上那張無日期的照片看起來封口完整'])
        self.assertEqual(result['rejected'], ['undated_media', None])
        self.assertEqual((result['conclusion'], result['gated_conclusion']), ('可行', '待查'))
        self.assertEqual((result['exact'], result['gated_exact']), (1, 2))

    def test_index_type_missing_and_wrong_object_remain_visible(self):
        case = next(c for c in study.cases() if c['id'] == 'similar_probe')

        def answer(a, b, ended=True):
            raw = json.dumps({'A': {'state': 'T', 'source': a}, 'B': {'state': 'T', 'source': b}})
            return study.grade(case, dict(raw=raw, ended=ended))

        self.assertFalse(answer(True, 5)['valid'])
        self.assertFalse(answer(3, 5, ended=False)['valid'])
        self.assertEqual(answer(20, None)['rejected'], ['out_of_range', 'missing_source'])
        self.assertEqual(answer(1, 2)['rejected'], ['rule', 'earlier'])
        wrong_object = answer(3, 5)
        self.assertEqual(wrong_object['rejected'], [None, None])
        self.assertFalse(wrong_object['gated_correct'])
        self.assertEqual(wrong_object['gated_exact'], 1)

    def test_baseline_binds_exact_original_inputs_and_weights(self):
        cid = 'test'
        case = dict(id=cid, question='題目')
        old = dict(model='model', selection={}, description={}, files={}, reference_protocol='previous',
                   cases=[case], contexts={cid: {'current_request': '題目'}},
                   prompts={cid: {'direct': 'hash'}})
        study.bind_baseline(dict(old), old)
        for replacement in (dict(files={'adapter': 'changed'}),
                            dict(prompts={cid: {'direct': 'changed'}}),
                            dict(contexts={cid: {'current_request': '改寫'}})):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                study.bind_baseline({**old, **replacement}, old)
