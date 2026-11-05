import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_meeting as meeting
from scripts.daily_expenses import DailyAssistant


FORMAT = '只輸出最早可行日期及時段，格式 YYYY-MM-DD HH:MM-HH:MM。'
DAYS = (
    '會議排程：\n'
    '2028-03-01：甲可開會：08:00-09:00，乙為 08:10-09:00。\n'
    '2028-02-28：甲可開會：08:00-11:00，乙無空檔。\n'
    '2028-02-29：甲可開會：11:40-13:10，乙為 11:30-13:00，甲取消整個上午。\n'
    '兩人需要連續 30 分鐘。\n'
)


class DailyDatedMeetingTests(unittest.TestCase):
    def test_calendar_order_exact_output_and_no_availability(self):
        question = DAYS + FORMAT
        plan = meeting.parse(question)
        self.assertEqual(plan['schema'], meeting.DATED_PLAN)
        self.assertEqual(plan, meeting.parse(question.replace('\n', '')))
        answer, receipt = meeting.resolve(plan)
        self.assertEqual(answer, '2028-02-29 12:00-12:30')
        self.assertEqual([d['date'] for d in receipt['days']], ['2028-02-28', '2028-02-29', '2028-03-01'])
        self.assertEqual(receipt['days'][0]['effective']['乙'], [])
        self.assertEqual(receipt['days'][1]['common'], ['12:00-13:00'])
        self.assertEqual(receipt['days'][2]['first'], '08:10-08:40')
        self.assertFalse(receipt['model_called'])
        natural, calc = meeting.resolve(meeting.parse(DAYS + '最早哪一天的哪個時段可以開會？'))
        self.assertEqual(calc, receipt)
        self.assertIn('2028-02-29 12:00-12:30', natural)

    def test_invalid_dates_participants_and_mixed_requests_fall_back(self):
        question = DAYS + FORMAT
        for other in (
            question.replace('2028-02-29', '2027-02-29'),
            question.replace('2028-02-28', '2028-02-29'),
            question.replace('2028-02-29', '2028-2-29'),
            question.replace('2028-02-28', '明天'),
            question.replace('2028-02-28：', '2028-02-28 UTC：'),
            question.replace('08:00-11:00，乙無空檔', '08:00-11:00'),
            question.replace('乙無空檔', '丙無空檔'),
            question.replace('乙無空檔', '乙今天無空檔'),
            question.replace('兩人', '三人'),
            question.replace('11:40-13:10', '23:40-01:10'),
            question + '幫我寄出邀請。',
        ):
            with self.subTest(other=other):
                self.assertIsNone(meeting.parse(other))

    def test_adjacent_midnight_windows_remain_separate(self):
        question = ('2026-12-31：甲可開會：23:45-24:00，乙為 23:40-24:00。\n'
                    '2027-01-01：甲可開會：00:00-00:20，乙為 00:00-00:30。\n'
                    '兩人需要連續 30 分鐘。\n')
        answer, receipt = meeting.resolve(meeting.parse(question + FORMAT))
        self.assertEqual(answer, '沒有可行時段')
        self.assertEqual([d['common'] for d in receipt['days']], [['23:45-24:00'], ['00:00-00:20']])
        answer, _ = meeting.resolve(meeting.parse(question + '最早哪一天可以開會？'))
        self.assertIn('每一天都沒有連續 30 分鐘', answer)

    def test_date_specific_edit_and_backup_preserve_other_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            app = DailyAssistant(store, None, default_chat=True)
            first = app.ask(sid, DAYS + FORMAT)
            for invalid in ('乙取消 12:00-13:00', '2028-03-02 乙取消 12:00-13:00',
                            '2028-02-29 丙取消 12:00-13:00'):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    app.ask(sid, f'上一場會議，{invalid}，重算。')
            second = app.ask(sid, '上一場會議，2028-02-29 乙的空檔改為無空檔，重算。')
            self.assertEqual(second['content'], '2028-03-01 08:10-08:40')
            updated = meeting.read(store, second['id'])
            self.assertEqual(updated['meeting_source']['answer_id'], first['id'])
            original = meeting.read(store, first['id'])['meeting_plan']
            self.assertEqual(updated['meeting_plan']['days']['2028-03-01'], original['days']['2028-03-01'])
            self.assertEqual(updated['meeting_plan']['days']['2028-02-29']['cancelled'],
                             original['days']['2028-02-29']['cancelled'])
            self.assertIn('當日最早：08:10-08:40', meeting.describe(updated))
            third = app.ask(sid, '上一場會議，時長改為 60 分鐘，重算。')
            self.assertEqual(third['content'], '沒有可行時段')
            expected = meeting.read(store, third['id'])
            backup = create_backup(store.path, root / 'meeting.paibackup')
            restore_backup(backup['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(meeting.read(restored, third['id']), expected)
            expected['calculation']['days'][1]['common'] = ['12:00-13:00']
            with restored.connect() as db:
                db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                           (json.dumps(expected, ensure_ascii=False), third['id']))
            with self.assertRaisesRegex(ValueError, '重算結果不符'):
                meeting.read(restored, third['id'])

    def test_default_retry_and_ordinary_attachment_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            store = Store(folder / 'assistant.sqlite3')
            sid = store.new_session()
            engine = Mock(spec=['answer'])
            engine.answer.return_value = '一般模型回答'
            app = DailyAssistant(store, engine, default_chat=True)
            with patch.object(meeting, 'resolve', side_effect=EngineError('停止', 'cancelled')), self.assertRaises(EngineError):
                app.ask(sid, DAYS + FORMAT)
            reply = app.retry(sid)
            self.assertEqual(reply['content'], '2028-02-29 12:00-12:30')
            self.assertEqual(len(store.messages(sid, 10)), 2)
            engine.answer.assert_not_called()
            doc = folder / 'meeting.txt'
            doc.write_text('另一份會議限制', encoding='utf-8')
            store.attach_document(sid, doc)
            self.assertEqual(app.ask(sid, DAYS + FORMAT)['content'], '一般模型回答')
            self.assertEqual(DailyAssistant(store, engine, allow_local_calculation=False).ask(
                store.new_session(), DAYS + FORMAT)['content'], '一般模型回答')
