from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant.engine import Assistant
from assistant.store import Store
from scripts import daily_chat
from scripts.daily_delivery import DailyLocalEngine, extract


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

