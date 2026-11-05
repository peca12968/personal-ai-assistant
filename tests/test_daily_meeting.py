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

    def test_everyday_wording_keeps_intent_and_explains_availability(self):
        q = '小陳 7:05 到 8:00 有空，小蔡 7:40 到 8:10 有空。想開半小時的會，最早什麼時候能開？'
        short = meeting.parse(q)
        self.assertIsNotNone(short)
        self.assertEqual(short, meeting.parse(q.replace('小陳 ', '小陳').replace('小蔡 ', '小蔡')))
        answer, receipt = meeting.resolve(short)
        self.assertIn('沒有可行時段', answer)
        self.assertIn('20 分鐘', answer)
        self.assertEqual(receipt['common'], ['07:40-08:00'])
        answer, _ = meeting.resolve(meeting.parse(q.replace('半小時', '一刻鐘')))
        self.assertIn('07:40-07:55', answer)
        answer, _ = meeting.resolve(meeting.parse(q.replace('7:40 到 8:10', '8:30 到 9:10')))
        self.assertIn('沒有共同空檔', answer)
        for invalid in (q.replace('半小時', '大約半小時'), q.replace('有空', '可能有空'),
                        q + '順便寄通知。', q.replace('有空', '沒空'), q.replace('7:05', '27:05')):
            self.assertIsNone(meeting.parse(invalid))

    def test_natural_availability_binds_attendees_and_cancels_morning_before_overlap(self):
        q = ('安禾今天可談 08:10–09:30 和 13:50–15:30，'
             '佳瑜可碰面的時間原是 08:40–09:25 或 14:05–14:50。'
             '安禾的上午時段已取消，兩人要談 30 分鐘。' + FORMAT)
        plan = meeting.parse(q)
        result, receipt = meeting.resolve(plan)
        self.assertEqual(list(plan['available']), ['安禾', '佳瑜'])
        self.assertEqual(plan['cancelled'], [
            {'name': '安禾', 'interval': [0, 720], 'source': '安禾的上午時段已取消'}])
        self.assertEqual(result, '14:05-14:35')
        self.assertEqual(receipt['common'], ['14:05-14:50'])
        self.assertFalse(receipt['model_called'])
        self.assertEqual(receipt['effective']['佳瑜'], ['08:40-09:25', '14:05-14:50'])

    def test_ambiguous_natural_meeting_clauses_stay_in_chat(self):
        q = ('子清可碰面 09:00–09:30 或 13:20–14:00，亞林可談 09:10–09:55 和 13:25–14:05。'
             '子清取消早上的安排。碰面需要連續 20 分鐘。' + FORMAT)
        self.assertEqual(meeting.resolve(meeting.parse(q))[0], '13:25-13:45')
        for question in (
            q.replace('子清可碰面', '若子清可碰面'), q.replace('亞林可談', '亞林未可談'),
            q.replace('亞林可談', '亞林明天可談'), q.replace('亞林可談', '亞林或珮如可談'),
            q.replace('亞林可談', '對方可談'), q.replace('子清取消', '子清可能取消'),
            q.replace('子清取消', '子菁取消'), q.replace('子清取消', '子清不取消'),
            q.replace('09:00–09:30 或 13:20–14:00', '13:20–13:30 或 13:30–14:00'),
            q.replace('09:00–09:30 或 13:20–14:00', '09:00–09:30 或 13:20–14:00 和 16:10–17:00'),
            q.replace('碰面需要連續', '三人要碰面'), q + '再寄出邀請。',
        ):
            with self.subTest(question=question):
                self.assertIsNone(meeting.parse(question))

    def test_natural_meeting_followup_reuses_exact_participant_name(self):
        q = ('怡君今天可談 09:10-10:00 和 14:20-15:30，冠宇可談 09:25-10:00 和 14:35-15:20。'
             '怡君剛取消早上的安排。兩人想談 20 分鐘。' + FORMAT)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'assistant.sqlite3'
            store = Store(path)
            sid = store.new_session()
            first = DailyAssistant(store, None).ask(sid, q)
            self.assertEqual(first['content'], '14:35-14:55')
            reopened = DailyAssistant(Store(path), None)
            answer = reopened.ask(sid, '上一場會議，怡君的空檔改為 09:00-09:50，重算。')
            self.assertEqual(answer['content'], '09:25-09:45')
            ctx = store.answer_context(answer['id'])
            self.assertEqual(ctx['meeting_source']['answer_id'], first['id'])
            self.assertEqual(ctx['meeting_plan']['cancelled'], [])

    def test_conversation_can_revise_a_prose_answer_after_reopening(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'assistant.sqlite3'
            store = Store(path)
            sid = store.new_session()
            q = '甲 9:20-9:50 有空，乙 9:40-10:20 有空。想開半小時的會，最早幾點能開會？'
            first = DailyAssistant(store, None).ask(sid, q)
            self.assertIn('只有 10 分鐘', first['content'])
            reopened = DailyAssistant(Store(path), None)
            reply = reopened.ask(sid, '上一場會議，甲的空檔改為 9:00-10:20，重算。')
            self.assertIn('09:40-10:10', reply['content'])
            ctx = store.answer_context(reply['id'])
            self.assertEqual(ctx['meeting_source']['answer_id'], first['id'])
            self.assertEqual(ctx['calculation']['result']['exact'], '09:40-10:10')

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

    def test_followups_replace_cancel_and_change_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            app = DailyAssistant(store, None)
            sid = store.new_session()
            first = app.ask(sid, BASE + '甲取消整個上午。乙取消 09:20-09:35。兩人需要連續 20 分鐘。' + FORMAT)
            second = app.ask(sid, '上一場會議，甲的空檔改為 09:00-10:00，請重算。')
            third = app.ask(sid, '上一場會議，乙取消 09:35-09:48，重算。')
            fourth = app.ask(sid, '上一場會議，時長改為 12 分鐘，重算。')
            self.assertEqual([r['content'] for r in (first,second,third,fourth)],
                             ['12:00-12:20','09:35-09:55','沒有可行時段','09:48-10:00'])
            for parent, child in [(first,second),(second,third),(third,fourth)]:
                ctx = store.answer_context(child['id'])
                self.assertEqual(ctx['meeting_source']['answer_id'], parent['id'])
                self.assertFalse(child['calculation']['model_called'])
            self.assertEqual(store.answer_context(first['id'])['meeting_plan']['cancelled'][0]['name'], '甲')
            self.assertEqual(len(store.answer_context(second['id'])['meeting_plan']['cancelled']), 1)

    def test_followup_needs_unambiguous_immediate_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            app = DailyAssistant(store, None)
            sid = store.new_session()
            initial = app.ask(sid, BASE + '兩人需要連續 20 分鐘。' + FORMAT)
            follow = '上一場會議，甲取消整個上午，重算。'
            with self.assertRaisesRegex(ValueError, '純文字'):
                app.ask(sid, follow, image_ids=[123])
            class Raw:
                def answer(self, prompt, cancel=None):
                    return '模型原答'
            raw_sid = store.new_session()
            self.assertEqual(DailyAssistant(store, Raw(), allow_local_calculation=False).ask(raw_sid, follow)['content'],
                             '模型原答')
            for clause in ('丙取消整個上午','他的空檔改為 13:00-15:00','改到明天','乙的空檔改為 23:00-01:00'):
                with self.assertRaises(ValueError):
                    app.ask(sid, '上一場會議，' + clause + '，重算。')
            with self.assertRaisesRegex(ValueError, '沒有可接續'):
                app.ask(store.new_session(), follow)
            self.assertEqual(len(store.messages(sid, 20)), 2)
            with store.connect() as db:
                db.execute('UPDATE messages SET content=? WHERE id=?', ('錯誤時段',initial['id']))
            with self.assertRaisesRegex(ValueError, '沒有可接續'):
                app.ask(sid, follow)
            app.ask(sid, '計算 1 + 1，只回答整數')
            with self.assertRaisesRegex(ValueError, '沒有可接續'):
                app.ask(sid, follow)

    def test_followup_revalidates_tail_during_submission_and_retry(self):
        from assistant import answer_runs
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            app = DailyAssistant(store, None)
            sid = store.new_session()
            app.ask(sid, BASE + '兩人需要連續 20 分鐘。' + FORMAT)
            q = '上一場會議，甲取消整個上午，重算。'
            with patch.object(meeting, 'resolve', side_effect=EngineError('stop', 'cancelled')), self.assertRaises(EngineError):
                app.ask(sid, q)
            self.assertEqual(app.retry(sid)['content'], '12:00-12:20')
            self.assertEqual(len(store.messages(sid, 20)), 4)
            begin = answer_runs.begin
            def raced(*args, **kwargs):
                store.add_message(sid, 'user', '另一個話題')
                return begin(*args, **kwargs)
            with patch.object(answer_runs, 'begin', side_effect=raced), self.assertRaises(ValueError):
                app.ask(sid, q)
            self.assertEqual(answer_runs.runs(store, sid, 1)[0]['status'], 'failed')
            with self.assertRaises(ValueError):
                app.retry(sid)

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
