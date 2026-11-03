import json
import tempfile
import unittest
from pathlib import Path

from assistant.context import is_explicit_correction,correction_context
from assistant.engine import Assistant
from assistant.store import Store
from scripts.evaluate_understanding import prepare_case,seed_history
from test_natural_corrections import CaptureEngine


class DirectionChangeTests(unittest.TestCase):
    def test_positive_and_data_question_boundaries(self):
        positive=(
            '不用做動畫了，改做清單。','先不要開啟寵物','請不要再開發舊入口了。',
            '麻煩你改用CSV，保留兩位小數。','取消原本的桌面工作，繼續文字功能。',
            '換成純文字。','改回先前方案，還不要執行。',
            'Instead, use a CSV table.','Please switch to text output.',
            'Cancel the previous task; draft a plan only.','Cancel the previous task.',
            '不用做舊版了，之後要不要做新版再討論。',
            'Ｓｗｉｔｃｈ ｔｏ ＣＳＶ.','不要做「過去那個方案」了。')
        negative=(
            '「不用做動畫了，改做清單。」只是範例。','> Instead, use CSV.',
            '```\n不用做動畫了\n```','他說不用做動畫了。','是否不用做動畫了？',
            '不用做動畫了嗎？','改用CSV嗎','不要做動畫了，改做清單嗎？',
            '不要取消原本的工作。','Instead, use CSV is an example.',
            'Instead, use CSV?','Can we switch to CSV?',
            '不用做動畫了這句話是什麼意思','取消原本的說法是範例。',
            '請解釋「改用CSV」','改用',' ' * 160+'不用做動畫了',
            '正常內容'+ '甲'*160+'不用做動畫了',
            'The previous task is cancelled according to a quoted example.')
        for text in positive:
            with self.subTest(text=text):self.assertTrue(is_explicit_correction(text))
        for text in negative:
            with self.subTest(text=text[:60]):self.assertFalse(is_explicit_correction(text))

    def test_frozen_cases_retain_only_original_direction_message(self):
        cases=json.loads((Path(__file__).resolve().parents[1]/'evals/direction_changes.json').read_text(encoding='utf-8'))
        for case in cases:
            with self.subTest(case=case['id']),tempfile.TemporaryDirectory() as tmp:
                store=Store(Path(tmp)/'test.sqlite3');sid=prepare_case(store,case)
                engine=CaptureEngine();app=Assistant(store,engine)
                result=app.ask(sid,case['prompt']);saved=store.answer_context(result['id'])
                self.assertEqual(engine.context,saved)
                rows=saved['earlier_user_corrections']['excerpts']
                expected=[] if case['id']=='quoted_cancel_control' else [case['history'][2]['content']]
                self.assertEqual([r['content'] for r in rows],expected)
                self.assertEqual(saved['current_request'],case['prompt'])
                self.assertEqual(store.retrieve('專案'),[])
                self.assertEqual(Store(store.path).answer_context(result['id']),saved)
                self.assertEqual(app.prepare_context(sid,'換個話題，談音樂')['earlier_user_corrections']['excerpts'],[])

    def test_scope_limits_local_exclusions_and_retry_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'test.sqlite3');sid=store.new_session();other=store.new_session()
            store.add_message(other,'user','不用做另一對話了。')
            store.add_message(sid,'assistant','不用做原任務了。')
            local=store.add_message(sid,'user','不用做本機命令了。')
            with store.connect() as db:db.execute('INSERT INTO local_memory_messages(message_id) VALUES (?)',(local,))
            ids=seed_history(store,sid,(('user',f'改用方案{i}。'+'甲'*1300+'不要執行') for i in range(8)))
            retry=store.add_message(sid,'user','不用做待重試題目了。')
            before=store.add_message(sid,'user','最近訊息邊界')
            store.add_message(sid,'user','不用做最近題目了。')
            rows=store.earlier_correction_messages(sid,before,retry)
            self.assertEqual([r['id'] for r in rows],list(reversed(ids[-7:])))
            picked=correction_context('回顧調整',rows)
            self.assertEqual([r['message_id'] for r in picked['excerpts']],ids[-6:])
            self.assertTrue(picked['selection_limited'])
            self.assertTrue(all(r['truncated'] and len(r['content'])<=1000 and '不要執行' in r['content'] for r in picked['excerpts']))
