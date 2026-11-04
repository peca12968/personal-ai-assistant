from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant.engine import Assistant
from assistant.store import Store
from scripts import daily_chat
from scripts.daily_delivery import DailyLocalEngine, explanation_body, extract


class Raw:
    display_name = 'fixture'

    def __init__(self, draft):
        self.draft = draft
        self.calls = 0

    def describe(self):
        return {'parameter_version': 'fixture'}

    def answer(self, prompt, cancel=None):
        self.calls += 1
        return self.draft


class DeliveryTests(unittest.TestCase):
    def test_final_conclusions_and_literal(self):
        examples = [
            ('本月結帳，只回覆合計的阿拉伯數字。', '兩筆已確認。合計為：900 + 210 = 1,110 元。', '1110'),
            ('空檔在哪？只輸出時段，格式 HH:MM-HH:MM。', '早場已取消。因此，最早可行的時段為 16:35–17:05。', '16:35-17:05'),
            ('問：箱內重量是多少？若無法確定，只輸出 資訊不足。', '無法確定箱內重量。', '資訊不足'),
            ('A 收到、B 缺件。只回覆選項字母。', 'B 缺件', 'B'),
            ('請問灰袋的代碼是什麼？只輸出代碼。', '灰袋上的代碼是 G42。', 'G42'),
            ('請問灰袋的代碼是什麼？只輸出代碼。', '["G42"]', 'G42'),
        ]
        for q, draft, wanted in examples:
            with self.subTest(q=q, draft=draft):
                self.assertEqual(extract(q, draft), wanted)

    def test_only_shorten_equations_with_consistent_numbers(self):
        question = '總額是多少？只回答數字。'
        for text, answer in [('3 - -2 + 7 = 12', '12'), ('0.1 + 0.2 = 0.3', '0.3'),
                             ('-3 × 0.25 = -0.75', '-0.75'), ('7.5 ÷ 3 = 2.5', '2.5'),
                             ('合計為：1,200 - 50 = 1,150 元。', '1150'), ('答案=18', '18')]:
            self.assertEqual(extract(question, text), answer)
        for text in ('7 + 8 = 16', '10 / 0 = 0', '12 × 3 = 35', '12 / 7 = 1.71'):
            visible = extract(question, text)
            self.assertTrue(visible.startswith(text))
            self.assertIn('不成立', visible)
        for text in ('1 + 2 × 3 = 7', '2 × 2 = 5。答案為 19。', '2 + 3 = 5。答案為 6。',
                     '3％ ＝ 0.03。答案是 4。', '1' * 270 + ' + 1 = 3'):
            self.assertEqual(extract(question, text), text)

    def test_mismatched_equation_keeps_workings_in_chat_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            draft = '合計為：94 + 28 = 112 元。'
            engine = DailyLocalEngine(Raw(draft))
            q = '兩筆已核准，合計多少？只回覆數字。'
            sid = store.new_session()
            answer = Assistant(store, engine).ask(sid, q)['content']
            self.assertIn('不成立', answer)
            self.assertEqual(store.messages(sid, 2)[-1]['content'], answer)
            raw = Assistant(store, engine, allow_local_calculation=False)
            self.assertEqual(raw.ask(store.new_session(), q)['content'], draft)

    def test_compare_excluded_items_to_positive_addends_only(self):
        scoped = '只統計已核准的新台幣支出。標籤 70 元已核准，封箱 13 元待核准，運費 5 美元已核准。只輸出數字。'
        for draft, kind in [('70 + 13 = 83', '待核准'), ('已核准總額為：70 + 5 = 75 元。', '美元')]:
            self.assertIn('原題的' + kind, extract(scoped, draft))
        for question, draft in [
            (scoped, '總額為 70 元。'),
            (scoped.replace('只統計', '不要只統計'), '70 + 13 = 83'),
            (scoped.replace('70 元已核准', '13 元已核准'), '13 + 13 = 26'),
            (scoped.replace('封箱 13 元待核准', '封箱 13 元待核准，後來改為已核准'), '70 + 13 = 83'),
            (scoped, '待核准的項目不計入。總額為 70 元。'),
            (scoped, '100 - 13 = 87'),
        ]:
            self.assertNotIn('篩選核對', extract(question, draft))

    def test_number_delivery_matches_the_requested_field(self):
        q = '費用已確認。這趟可報多少？只給數字。'
        self.assertEqual(extract(q,'補助已核准。這趟可報 126 元。'), '126')
        for answer in ('補助已核准。上回可報 126 元。','這趟可報 120 或 126 元。',
                       '可能有折扣。這趟可報 126 元。', '這趟可報約 126 元。'):
            self.assertEqual(extract(q, answer), answer)
        text = '已確認。這趟可報 126.5 元。'
        self.assertEqual(extract(q.replace('數字','整數'), text), text)

    def test_no_guess_no_unrequested_formatting(self):
        risky = [
            ('簡述原因。', '總額為 80 元。'),
            ('請解釋誤差，最後只輸出數字。', '結果為 80。'),
            ('轉述範例「只輸出整數」。', '答案是 12。'),
            ('問：新包重量是多少？若無法得知，只輸出 資料不足。', '無法得知舊包重量。'),
            ('箱內件數？只輸出整數。', '暫定金額。答案是 100。'),
            ('箱內件數？只輸出整數。', '總數不是 15。'),
            ('只輸出整數。', '結果是 1.5。'),
            ('時間？只輸出時段，格式 HH:MM-HH:MM。', '可行時段為 26:00-26:20。'),
            ('紅袋已知。請問藍袋代碼？只輸出代碼。', '紅袋的代碼是 X24。'),
            ('只輸出代碼。', '["Z8", "Z9"]'),
            ('A 退件，B 可用。只回覆選項字母。', 'A 可用'),
        ]
        for q, draft in risky:
            with self.subTest(q=q):
                self.assertEqual(extract(q, draft), draft)

    def test_exact_echo_only_and_keep_requested_wording(self):
        question = '請解釋為什麼冰會浮起來。'
        body = '相同體積的冰比液態水輕。\n\n因此冰會浮起來。'
        ctx = {'current_request': question}
        for separator in ('\n\n', '\r\n\r\n'):
            self.assertEqual(explanation_body(ctx, question + separator + body), body)
        for draft in (question, question + '\n\n ', question + '\n' + body, body,
                      '問題：' + question + '\n\n' + body):
            self.assertEqual(explanation_body(ctx, draft), draft)
        for q in ('先寫出我的問題，再請解釋原因。', '請解釋並逐字保留題目。', '請翻譯這一段。'):
            draft = q + '\n\n' + body
            self.assertEqual(explanation_body({'current_request': q}, draft), draft)
        draft = question + '\n\n' + body
        self.assertEqual(explanation_body({**ctx, 'memories': [{'content': '每次請先重述問題'}]}, draft), draft)

        q = '請解釋浮力，不要重複我的問題。'
        self.assertEqual(explanation_body({'current_request': q, 'memories': ['請先重述問題']},
                                          q + '\n\n' + body), body)
        q = '請先複述題目，再請解釋「不要複述題目」這句話。'
        self.assertEqual(explanation_body({'current_request': q}, q + '\n\n' + body), q + '\n\n' + body)

    def test_saved_explanation_and_requested_restatement(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            for q, keep in [('請解釋為什麼尺上的陰影位置會變。', False),
                            ('請先重述題目，再請解釋為什麼尺上的陰影位置會變。', True)]:
                body = '光源和物體的相對位置改變了。'
                draft = q + '\n\n' + body
                raw = Raw(draft)
                wrapped = DailyLocalEngine(raw)
                sid = store.new_session()
                expected = draft if keep else body
                self.assertEqual(Assistant(store, wrapped).ask(sid, q)['content'], expected)
                self.assertEqual(store.messages(sid, 2)[-1]['content'], expected)
                self.assertEqual(raw.calls, 1)
                self.assertEqual(Assistant(store, wrapped, allow_local_calculation=False).ask(
                    store.new_session(), q)['content'], draft)

    def test_desktop_command_saves_delivered_reply_but_raw_eval_keeps_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Raw('答案是 472。')
            question = '取貨數是多少？只輸出整數。'
            output = io.StringIO()
            with patch.object(daily_chat, 'LocalEngine', return_value=raw), patch('sys.argv', [
                    'daily_chat', '--engine', 'local', '--data-dir', tmp, '--ask', question]), redirect_stdout(output):
                daily_chat.main()
            self.assertEqual(output.getvalue().splitlines()[0], '472')
            self.assertEqual(raw.calls, 1)
            store = Store(Path(tmp)/'assistant.sqlite3')
            sid = output.getvalue().split()[-1]
            self.assertEqual(store.messages(sid, 2)[-1]['content'], '472')
            baseline = Assistant(store, DailyLocalEngine(raw), allow_local_calculation=False)
            self.assertEqual(baseline.ask(store.new_session(), question)['content'], raw.draft)
            self.assertEqual(DailyLocalEngine(raw).answer('其他模型任務'), raw.draft)
