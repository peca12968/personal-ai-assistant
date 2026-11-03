import copy
import json
import random
import tempfile
import unittest
from pathlib import Path

from assistant.context import clip, make_context
from assistant.engine import Assistant
from assistant.store import Store
from scripts.evaluate_understanding import prepare_case


class RecentBudgetTests(unittest.TestCase):
    def test_spare_capacity_preserves_middle_and_original_roles(self):
        long = '開始\n' + '背景。' * 600 + '\n單價137，運費43。\n' + '背景。' * 600 + '\n結束'
        for role in ('user', 'assistant'):
            history = [{'id': 1, 'role': role, 'content': long}]
            history += [{'id': i, 'role': 'user' if i % 2 else 'assistant', 'content': '收到。'} for i in range(2, 17)]
            original = copy.deepcopy(history)
            data = make_context('接著算9件', history, [])
            rows = data['recent_conversation']
            self.assertEqual(rows[0], {'message_id': 1, 'role': role, 'content': long, 'truncated': False})
            self.assertEqual([r['message_id'] for r in rows], list(range(1, 17)))
            self.assertEqual(history, original)
            self.assertEqual(data['current_request'], '接著算9件')

    def test_fair_capacity_matches_independent_one_character_rounds(self):
        rng = random.Random(771)
        for _ in range(60):
            lengths = [rng.choice([0, 1, 17, 1499, 1500, 1501, 5999, 6000, 9001]) for _ in range(rng.randrange(1, 17))]
            expected = [0] * len(lengths)
            remaining = 24000
            # Independent slow allocator: one character per unsatisfied row,
            # newest first, until all caps or the global cap are reached.
            while remaining:
                active = [i for i in reversed(range(len(lengths))) if expected[i] < min(6000, lengths[i])]
                if not active:
                    break
                for i in active:
                    if remaining == 0:
                        break
                    expected[i] += 1
                    remaining -= 1
            history = [{'id': i, 'role': 'user', 'content': '字' * length} for i, length in enumerate(lengths)]
            rows = make_context('繼續', history, [])['recent_conversation']
            actual = [len(row['content']) for row in rows]
            self.assertEqual(actual, expected, lengths)
            self.assertEqual(sum(actual), min(24000, sum(min(6000, n) for n in lengths)))
            legacy = min(6000, 24000 // len(lengths))
            for length, row in zip(lengths, rows):
                self.assertGreaterEqual(len(row['content']), min(length, legacy))
                self.assertEqual(row['truncated'], len(row['content']) < length)

    def test_saturated_history_keeps_existing_head_tail_and_global_budget(self):
        text = '開頭\n' + '內容🙂\x00' * 2000 + '\n最後限制：不能刪檔'
        history = [{'id': i, 'role': 'user' if i % 2 else 'assistant', 'content': text} for i in range(16)]
        rows = make_context('繼續', history, [])['recent_conversation']
        self.assertEqual(sum(len(row['content']) for row in rows), 24000)
        for row in rows:
            self.assertEqual(row['content'], clip(text, 1500)[0])
            self.assertTrue(row['truncated'])
            self.assertTrue(row['content'].endswith('最後限制：不能刪檔'))
        self.assertEqual(json.loads(json.dumps(rows, ensure_ascii=False)), rows)

    def test_remainder_goes_to_newer_rows_and_caps_stay_bounded(self):
        history = [{'id': i, 'role': 'user', 'content': '字' * n} for i, n in enumerate([1, 9000, 9000, 9000, 9000])]
        rows = make_context('繼續', history, [])['recent_conversation']
        self.assertEqual([len(row['content']) for row in rows], [1, 5999, 6000, 6000, 6000])
        self.assertEqual(make_context('空白', [], [])['recent_conversation'], [])
        empty = make_context('空字串', [{'role': 'assistant', 'content': ''}], [])['recent_conversation']
        self.assertEqual(empty[0]['content'], '')
        self.assertFalse(empty[0]['truncated'])
        outside = [{'id': -1, 'role': 'user', 'content': '不可多帶第17則'}]
        inside = [{'id': i, 'role': 'user', 'content': '🙂' * 10000} for i in range(16)]
        rows = make_context('繼續', outside + inside, [])['recent_conversation']
        self.assertEqual([row['message_id'] for row in rows], list(range(16)))

    def test_actual_answer_snapshot_preserves_source_and_session_boundary(self):
        class Capture:
            def answer(self, prompt, cancel=None):
                self.context = json.loads(prompt.split('以下 JSON 是使用者資料與當前問題：\n')[1])
                return '合成回答'

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3')
            sid = store.new_session()
            other = store.new_session()
            store.add_message(other, 'user', '別的對話，不能帶入')
            long = '前文。' * 600 + '原始條件：單價137，運費43。' + '後文。' * 600
            first = store.add_message(sid, 'user', long)
            for i in range(15):
                store.add_message(sid, 'assistant' if i % 2 == 0 else 'user', '收到背景。')
            engine = Capture()
            app = Assistant(store, engine)
            before = store.messages(sid, 20)
            answer = app.ask(sid, '更正：單價150，免運，買9件。')
            captured = copy.deepcopy(engine.context)
            self.assertEqual(captured['recent_conversation'][0]['message_id'], first)
            self.assertEqual(captured['recent_conversation'][0]['content'], long)
            self.assertNotIn('別的對話，不能帶入', json.dumps(captured, ensure_ascii=False))
            self.assertEqual(captured['current_request'], '更正：單價150，免運，買9件。')
            self.assertEqual(store.messages(sid, 20)[:16], before)
            # The historical answer source must remain immutable when later
            # turns use a different allocation and a different current request.
            with store.connect() as db:
                saved = dict(db.execute('SELECT * FROM answer_contexts WHERE message_id=?', (answer['id'],)).fetchone())
            app.ask(sid, '現在只解釋算式，不要保存記憶。')
            with store.connect() as db:
                self.assertEqual(dict(db.execute('SELECT * FROM answer_contexts WHERE message_id=?', (answer['id'],)).fetchone()), saved)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM memories').fetchone()[0], 0)

    def test_fixed_live_fixtures_retain_conditions_but_not_unseen_middle(self):
        cases = json.loads((Path(__file__).resolve().parents[1] / 'evals/recent_budget.json').read_text('utf-8'))
        contexts = {}
        for case in cases:
            with tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / 'state.sqlite3')
                sid = prepare_case(store, case)
                contexts[case['id']] = Assistant(store, None).prepare_context(sid, case['prompt'])
        for cid, token in [('quote_middle', '137'), ('selected_proposal', '173')]:
            rows = contexts[cid]['recent_conversation']
            self.assertEqual(len(rows), 16)
            self.assertIn(token, '\n'.join(row['content'] for row in rows))
            self.assertTrue(all(not row['truncated'] for row in rows))
        clipped = contexts['still_clipped']['recent_conversation']
        self.assertNotIn('紫砂-682', '\n'.join(row['content'] for row in clipped))
        self.assertEqual(len(clipped[0]['content']), 6000)
        self.assertTrue(clipped[0]['truncated'])


if __name__ == '__main__':
    unittest.main()
