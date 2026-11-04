import random
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_meeting as meeting
from scripts.daily_expenses import DailyAssistant

FORMAT = '只輸出最早可行時段，格式 HH:MM-HH:MM。'
BASE = '甲可開會：09:10-10:10 和 11:50-12:50，乙為 09:20-10:00 和 11:55-12:45。'


class DailyMeetingTests(unittest.TestCase):
    def test_cancellations_and_exact_boundary(self):
        for change, duration, expected in [
            ('', 40, '09:20-10:00'), ('甲剛取消整個上午。', 45, '12:00-12:45'),
            ('乙取消 09:35-11:59。', 40, '11:59-12:39'),
            ('甲取消整個上午。乙取消 12:08-12:45。', 10, '沒有可行時段'),
        ]:
            question = BASE + change + f'兩人需要連續 {duration} 分鐘。' + FORMAT
            self.assertEqual(meeting.resolve(meeting.parse(question))[0], expected)
        q = '甲可開會：23:20-23:40、23:40-24:00，乙為 23:20-24:00，丙為 23:30-24:00。三人需要連續 30 分鐘。' + FORMAT
        self.assertEqual(meeting.resolve(meeting.parse(q))[0], '23:30-24:00')

    def test_named_participants_and_touching_only(self):
        q = '「林小晴」可開會：08:00-09:00，「品保組」為 09:00-10:00。需要連續 1 分鐘。' + FORMAT
        self.assertEqual(meeting.resolve(meeting.parse(q))[0], '沒有可行時段')
        self.assertIsNone(meeting.parse(q.replace('「林小晴」', '小晴').replace('「品保組」', '「小晴」')))
        q = q.replace('08:00-09:00', '08:00-09:30').replace('需要連續', '「林小晴」取消 08:00-09:12。需要連續')
        self.assertEqual(meeting.resolve(meeting.parse(q))[0], '09:12-09:13')

    def test_incomplete_or_other_task_stays_in_chat(self):
        valid = BASE + '兩人需要連續 35 分鐘。' + FORMAT
        for question in [valid.replace('兩人', '三人'), valid.replace('09:20-10:00', '23:20-01:00'),
                         valid.replace('09:20-10:00', '25:20-26:00'), valid.replace('乙為', '甲為'),
                         valid.replace('乙為', '乙大概為'), valid.replace('兩人', '丙取消整個上午。兩人'),
                         valid.replace('35 分鐘', '0 分鐘'), valid.replace('35 分鐘', '35 分鐘並預留交通時間'),
                         valid.replace('兩人', '改為明天。兩人'), valid.replace('乙為', '乙或丙為'),
                         valid + '幫我寄出邀請。', '請翻譯「' + valid + '」']:
            with self.subTest(question=question):
                self.assertIsNone(meeting.parse(question))

    def test_compare_against_independent_start_time_search(self):
        rng = random.Random(105)
        for _ in range(120):
            available, cancelled = {}, []
            for name in ('甲', '乙', '丙'):
                available[name] = [sorted(rng.sample(range(75), 2)) for _ in range(3)]
                cancelled.append(dict(name=name, interval=sorted(rng.sample(range(75), 2))))
            duration = rng.randint(1, 12)
            def free(name, t):
                return any(a <= t < b for a,b in available[name]) and not any(
                    c['interval'][0] <= t < c['interval'][1] for c in cancelled if c['name'] == name)
            start = next((t for t in range(75) if all(free(n, i) for n in available for i in range(t, t+duration))), None)
            expected = meeting.display((start, start+duration)) if start is not None else '沒有可行時段'
            answer, _ = meeting.resolve(dict(available=available, cancelled=cancelled, duration=duration))
            self.assertEqual(answer, expected)

    def test_receipt_retry_and_model_only_mode(self):
        class Raw:
            def answer(self, prompt, cancel=None):
                return '模型原答'
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            app = DailyAssistant(store, Raw())
            sid = store.new_session()
            q = BASE + '甲取消整個上午。兩人需要連續 40 分鐘。' + FORMAT
            with patch.object(meeting, 'resolve', side_effect=EngineError('停止', 'cancelled')), self.assertRaises(EngineError):
                app.ask(sid, q)
            result = app.retry(sid)
            self.assertEqual(result['content'], '12:00-12:40')
            ctx = store.answer_context(result['id'])
            self.assertEqual(ctx['calculation']['common'], ['12:00-12:45'])
            self.assertFalse(ctx['calculation']['model_called'])
            self.assertEqual(ctx['retry_of']['previous_status'], 'cancelled')
            self.assertEqual(len(store.messages(sid, 10)), 2)
            self.assertEqual(DailyAssistant(store, Raw(), allow_local_calculation=False).ask(sid, q)['content'], '模型原答')
            doc = Path(tmp)/'extra.txt'
            doc.write_text('另外的時間限制', encoding='utf-8')
            store.attach_document(sid, doc)
            self.assertEqual(app.ask(sid, q)['content'], '模型原答')
