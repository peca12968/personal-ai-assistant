import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant.store import Store
from scripts.learning import assess_natural_scope as study


class NaturalScopeEvidenceTests(unittest.TestCase):
    def test_literal_ranges_keep_quoted_punctuation_and_do_not_assign_mixed_time_scope(self):
        text = '只需要編號齊全。前回寫著「現場回報已齊。編號未核。」。原先沒核，剛才已核。眼下在表上。'
        spans = study.segments(text)
        self.assertEqual([row['kind'] for row in spans], ['rule', 'earlier', 'uncertain', 'current'])
        self.assertEqual(''.join(text[row['start']:row['end']] for row in spans), text)
        self.assertIn('【較早線索】前回寫著「現場回報已齊。編號未核。」。', study.render(text, spans))
        self.assertIn('【範圍待辨】原先沒核，剛才已核。', study.render(text, spans))

    def test_unclear_or_malformed_boundaries_cannot_create_time_cues(self):
        for text in ('「剛才核對」是表格欄名。', '前「一頁」次演練。', '前回與本輪狀態混寫。'):
            with self.subTest(text=text):
                self.assertNotEqual(study.segments(text)[0]['kind'], 'current')
        for text in ('外欄「未閉合。剛才核對。', '上次核對」。這次未核。', '範圍。' * 13, '長' * 1601):
            with self.subTest(text=text):
                self.assertEqual(study.segments(text), [])
                self.assertEqual(study.render(text, []), text)

    def test_changing_ground_truth_never_changes_either_model_request(self):
        row = study.read(study.FIXTURE)['cases'][0]
        old = study.compile_case(row)
        new = study.compile_case({**row, 'gold': [['U', None], row['gold'][1]]})
        self.assertNotEqual(old['answer'], new['answer'])
        for key in ('question', 'semantic_question', 'history', 'spans'):
            self.assertEqual(old[key], new[key])
        control = study.cases()[-1]
        self.assertEqual(control['question'], control['semantic_question'])

    def test_conclusion_and_exact_source_are_scored_separately(self):
        case = next(row for row in study.cases() if row['id'] == 'undated_photo')
        quote = case['gold'][1][1]

        def answer(first, second, ended=True):
            raw = json.dumps({'A': {'state': first[0], 'source': first[1]}, 'B': {'state': second[0], 'source': second[1]}},
                             ensure_ascii=False)
            return study.grade(case, dict(raw=raw, ended=ended))

        self.assertEqual(answer(['U', None], ['T', quote])['exact'], 2)
        wrong = answer(['U', '不存在的觀察'], ['T', None])
        self.assertEqual((wrong['correct'], wrong['states'], wrong['exact'], wrong['off_text'], wrong['unsupported']),
                         (True, 2, 0, 1, 1))
        self.assertFalse(answer(['U', None], ['T', 4])['valid'])
        self.assertFalse(answer(['U', None], ['T', quote], ended=False)['valid'])

    def test_prepare_binds_original_question_and_leaves_previous_studies_unchanged(self):
        full = study.full
        original = (full.SCHEMA, full.location, full.cases, full.sources)
        reference = dict(reference='source', model='test-model', models={'parent': {'files': {}}})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sealed = root / 'sealed.json'
            sealed.write_text('{"full_transfer":{"cases":[]}}', encoding='utf-8')
            with (patch.object(study, 'PARENT', root / 'new'),
                  patch.object(study, 'sources', return_value={'fixture': 'abc'}),
                  patch.object(full, 'reference', return_value=(reference, {}, {'research_adapter': True})),
                  patch.object(full.diagnosis.transfer, 'frozen', return_value={'cases': [], 'training': []}),
                  patch.object(full.prior, 'SEALED_FULL', sealed)):
                plan = study.prepare('scope-natural', 'sample-reference')
                folder = study.location('scope-natural')
                self.assertEqual(study.checked(folder), plan)
            case = plan['cases'][0]
            cid = case['id']
            store = Store(folder / 'fixture.sqlite3')
            self.assertEqual([row['content'] for row in store.messages(plan['sessions'][cid], 10)],
                             [row['content'] for row in case['history']])
            self.assertEqual(plan['contexts'][cid]['current_request'], case['question'])
            self.assertNotEqual(plan['prompts'][cid]['direct'], plan['prompts'][cid]['split'])
        self.assertEqual((full.SCHEMA, full.location, full.cases, full.sources), original)
