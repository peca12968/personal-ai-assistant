import json
import tempfile
import unittest
from pathlib import Path

from assistant.context import is_explicit_correction
from assistant.engine import Assistant
from assistant.store import Store
from scripts.evaluate_understanding import seed_history


class CaptureEngine:
    def answer(self, prompt, cancel=None):
        self.context = json.loads(prompt.split('以下 JSON 是使用者資料與當前問題：\n')[1])
        return '已讀取對話條件。'


class NaturalCorrectionTests(unittest.TestCase):
    def test_frozen_prefix_cases(self):
        path = Path(__file__).resolve().parents[1] / 'evals/correction_prefix_revisions.json'
        cases = json.loads(path.read_text(encoding='utf-8'))
        for case in cases:
            with self.subTest(case=case['id']):
                self.assertEqual(is_explicit_correction(case['text']), case['expected'])

    def test_old_natural_corrections_reach_actual_request_and_saved_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.db')
            engine = CaptureEngine()
            app = Assistant(store, engine)
            sid = store.new_session()
            store.add_message(sid, 'user', '霧杉計畫12人，每人300元。')
            texts = ['  我更正一下，改為14人，每人275元；只是試算，不要付款。  ',
                     'Let me correct that: use 17 boxes, 26 units each. Do not ship.']
            ids = [store.add_message(sid, 'user', text) for text in texts]
            seed_history(store, sid, (('user', f'背景閒聊 {i}') for i in range(230)))
            answer = app.ask(sid, '這次另改15人，每人260元，只按目前條件計算。')
            snapshot = store.answer_context(answer['id'])
            corrections = snapshot['earlier_user_corrections']['excerpts']
            self.assertEqual([row['message_id'] for row in corrections], ids)
            self.assertEqual([row['content'] for row in corrections], texts)
            self.assertEqual(engine.context, snapshot)
            self.assertIn('15人', snapshot['current_request'])
            self.assertEqual(store.retrieve('霧杉'), [])
            self.assertEqual(Store(store.path).answer_context(answer['id']), snapshot)
            reset = app.prepare_context(sid, '換個話題，談音樂')
            self.assertEqual(reset['earlier_user_corrections']['excerpts'], [])

    def test_natural_corrections_keep_scope_latest_six_and_clipping(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.db')
            sid = store.new_session()
            other = store.new_session()
            store.add_message(other, 'user', '更正一下，另一對話內容')
            store.add_message(sid, 'assistant', '更正一下，助理猜測')
            local = store.add_message(sid, 'user', '更正一下，本機命令內容')
            with store.connect() as db:
                db.execute('INSERT INTO local_memory_messages(message_id) VALUES (?)', (local,))
            ids = [store.add_message(sid, 'user', f'Quick correction: item {i} ' + '甲' * 1300 + ' Do not execute.')
                   for i in range(8)]
            excluded = store.add_message(sid, 'user', 'Let me correct myself, retry question')
            before = store.add_message(sid, 'user', 'boundary')
            store.add_message(sid, 'user', '更正一下，最近視窗內容')
            rows = store.earlier_correction_messages(sid, before, excluded)
            self.assertEqual([row['id'] for row in rows], list(reversed(ids[-7:])))
            from assistant.context import correction_context
            selected = correction_context('回顧條件', rows)
            self.assertTrue(selected['selection_limited'])
            self.assertEqual([row['message_id'] for row in selected['excerpts']], ids[-6:])
            self.assertTrue(all(row['truncated'] and len(row['content']) <= 1000
                                and 'Do not execute.' in row['content'] for row in selected['excerpts']))

    def test_normalization_and_bounded_prefix_preserve_qualifiers(self):
        self.assertTrue(is_explicit_correction('Ｑｕｉｃｋ ｃｏｒｒｅｃｔｉｏｎ： use 17'))
        self.assertTrue(is_explicit_correction('更正一下，是不是應該改14人？'))
        self.assertTrue(is_explicit_correction('更正一下，\x00只是條件草稿，不代表確認。'))
        for text in (' ' * 160 + '更正一下，14人', '更正' + ' ' * 10000 + '一下，14人',
                     'I need to correct that document: example', '更正一下子的意思？'):
            with self.subTest(text=text[:40]):
                self.assertFalse(is_explicit_correction(text))


if __name__ == '__main__':
    unittest.main()
