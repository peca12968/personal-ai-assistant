import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.engine import EngineError
from assistant.store import Store
from scripts.daily_expenses import DailyAssistant, parse


class ExpensesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / 'assistant.sqlite3')
        self.app = DailyAssistant(self.store, None)

    def test_status_currency_same_amount_and_updates_are_independent(self):
        prefix = '請只統計已核准的新台幣支出。紙袋 43 元已核准、包材 43 元待審、貨盒 43 美元已核准。'
        for edit, want, selected in [
            ('', '43', [True, False, False]), ('更正：包材改為已核准。', '86', [True, True, False]),
            ('更正：紙袋改為已駁回。', '0', [False, False, False]),
        ]:
            q = prefix + edit + '只輸出數字。'
            reply = self.app.ask(self.store.new_session(), q)
            ctx = self.store.answer_context(reply['id'])
            self.assertEqual((reply['content'], reply['response_mode']), (want, 'local_calculation'))
            self.assertFalse(ctx['calculation']['model_called'])
            self.assertEqual(ctx['current_request'], q)
            self.assertEqual([r['included'] for r in ctx['expense_ledger']['rows']], selected)
            self.assertEqual(len(ctx['expense_ledger']['changes']), int(bool(edit)))

    def test_cross_turn_recalculation_chains_snapshots_without_overwriting(self):
        sid = self.store.new_session()
        ask = lambda q: self.app.ask(sid, q)
        initial = ask('只統計已核准的新台幣支出。膠帶 39 元已核准、車資 27 元待審、郵資 27 美元待審。只輸出數字。')
        second = ask('把上一筆核銷的車資改為已核准，重新加總')
        third = ask('更正：郵資改為已核准，請重算。')
        fourth = ask('將上一筆核銷的膠帶改為已駁回，重算。')
        self.assertEqual([r['content'] for r in (initial, second, third, fourth)], ['39', '66', '66', '27'])
        for prior, reply in [(initial, second), (second, third), (third, fourth)]:
            ctx = self.store.answer_context(reply['id'])
            self.assertEqual(ctx['expense_source']['answer_id'], prior['id'])
            self.assertEqual(len(ctx['expense_source']['sha256']), 64)
            self.assertFalse(ctx['calculation']['model_called'])
        self.assertEqual(len(self.store.answer_context(initial['id'])['expense_ledger']['changes']), 0)
        self.assertEqual(len(ctx['expense_ledger']['changes']), 3)

    def test_disambiguation_does_not_advance_or_cross_conversation(self):
        sid = self.store.new_session()
        initial = self.app.ask(sid, '封條 16 元待審、封條 16 元待審。核准金額共多少？只回答數字。')
        q = '把上一筆核銷的封條改為已核准，重新加總。'
        with self.assertRaisesRegex(ValueError, '第1項.*第2項'):
            self.app.ask(sid, q)
        with self.assertRaisesRegex(ValueError, '第1項.*第2項'):
            self.app.ask(sid, q.replace('的封條', '的第3項封條'))
        self.assertEqual(len(self.store.messages(sid, 10)), 2)
        with self.assertRaisesRegex(ValueError, '沒有可接續'):
            self.app.ask(self.store.new_session(), q)
        answer = self.app.ask(sid, q.replace('的封條', '的第2項封條'))
        self.assertEqual(answer['content'], '16')
        ctx = self.store.answer_context(answer['id'])
        self.assertEqual(ctx['expense_source']['answer_id'], initial['id'])
        self.assertEqual([r['included'] for r in ctx['expense_ledger']['rows']], [False, True])
        self.app.ask(sid, '計算 7+4，只輸出整數')
        with self.assertRaisesRegex(ValueError, '沒有可接續'):
            self.app.ask(sid, q)

    def test_changed_tail_refused_after_claiming_run(self):
        from assistant import answer_runs
        sid = self.store.new_session()
        self.app.ask(sid, '膠水 17 元待審。核准金額共多少？只回答數字。')
        begin = answer_runs.begin
        def changed(*args, **kwargs):
            self.store.add_message(sid, 'user', '其他問題')
            return begin(*args, **kwargs)
        with patch.object(answer_runs, 'begin', side_effect=changed), self.assertRaises(ValueError):
            self.app.ask(sid, '把上一筆核銷的膠水改為已核准，重算。')
        self.assertEqual(answer_runs.runs(self.store, sid, 1)[0]['status'], 'failed')
        with self.assertRaises(ValueError):
            self.app.retry(sid)

    def test_cancelled_correction_reuses_original_source_on_retry(self):
        sid = self.store.new_session()
        initial = self.app.ask(sid, '束帶 7 元待審。核准金額共多少？只回答數字。')
        with patch('scripts.daily_expenses.expression_math.calculate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, '更正：束帶改為已核准，請重算。')
        result = self.app.retry(sid)
        self.assertEqual(result['content'], '7')
        self.assertEqual(self.store.answer_context(result['id'])['expense_source']['answer_id'], initial['id'])
        self.assertEqual(len(self.store.messages(sid, 10)), 4)

    def test_followup_uses_only_a_verified_prior_answer_and_no_attachments(self):
        sid = self.store.new_session()
        result = self.app.ask(sid, '車資 52 元待審。核准金額共多少？只輸出數字。')
        q = '更正：車資改為已核准，請重新加總。'
        with self.assertRaisesRegex(ValueError, '純文字'):
            self.app.ask(sid, q, image_ids=[99])
        with self.store.connect() as db:
            db.execute('UPDATE messages SET content=? WHERE id=?', ('52', result['id']))
        with self.assertRaisesRegex(ValueError, '沒有可接續'):
            self.app.ask(sid, q)
        self.assertEqual(len(self.store.messages(sid, 10)), 2)

    def test_group_and_fractional_currency(self):
        q = '請款單只核准封條 1.25 元與零件 3.5 元，另有待核准包材 9 元。核准金額共多少，只回答數字。'
        self.assertEqual(self.app.ask(self.store.new_session(), q)['content'], '4.75')
        with self.assertRaisesRegex(EngineError, '不是整數'):
            self.app.ask(self.store.new_session(), q.replace('數字', '整數'))

    def test_incomplete_or_ambiguous_message_does_not_become_a_calculation(self):
        cases = [
            '紙盒 31 元已核准、木盒 8 美元已核准。已核准的款項合計多少？只回答數字。',
            '紙盒 31 元已核准、木盒尚未報價。已核准的款項合計多少？只回答數字。',
            '紙盒 31 元已核准、紙盒 8 元待審。更正：紙盒改為已核准。核准金額共多少？只回答數字。',
            '紙盒 31 元已核准。更正：郵資改為已核准。核准金額共多少？只回答數字。',
            '紙盒每包 31 元已核准。核准金額共多少？只回答數字。',
            '請只統計已核准的新台幣支出。紙盒美金 31 元已核准。只回答數字。',
            '請解釋這個例子「請只統計已核准的新台幣支出。木盒 32 元已核准。只輸出數字。」',
        ]
        for q in cases:
            with self.subTest(q=q):
                self.assertIsNone(parse(q))

    def test_cancelled_run_retries_without_adding_a_second_user_message(self):
        q = '標籤 32 元已核准。核准金額共多少？只回答數字。'
        sid = self.store.new_session()
        with patch('scripts.daily_expenses.expression_math.calculate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, q, threading.Event())
        result = self.app.retry(sid)
        self.assertEqual(result['content'], '32')
        self.assertEqual([r['role'] for r in self.store.messages(sid, 10)], ['user', 'assistant'])
        ctx = self.store.answer_context(result['id'])
        self.assertEqual(ctx['retry_of']['previous_status'], 'cancelled')

    def test_explicit_model_evaluation_and_documents_use_original_engine(self):
        class Raw:
            calls = 0
            def answer(self, prompt, cancel=None):
                self.calls += 1
                return '模型原答'
        raw = Raw()
        q = '耗材 39 元已核准。核准款項合計多少？只輸出數字。'
        sid = self.store.new_session()
        app = DailyAssistant(self.store, raw, allow_local_calculation=False)
        self.assertEqual(app.ask(sid, q)['content'], '模型原答')
        doc = self.store.path.with_name('reference.txt')
        doc.write_text('附件', encoding='utf-8')
        self.store.attach_document(sid, doc)
        self.assertEqual(DailyAssistant(self.store, raw).ask(sid, q)['content'], '模型原答')
        self.assertEqual(raw.calls, 2)
