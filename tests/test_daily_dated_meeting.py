import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_meeting as meeting
from scripts import daily_meeting_export as calendar
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
            for invalid in ('2028-03-02 乙取消 12:00-13:00',
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

    def test_missing_date_waits_for_same_session_choice_and_retries_after_restore(self):
        from assistant import answer_runs
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root/'assistant.sqlite3')
            sid = store.new_session()
            engine = Mock(spec=['answer'])
            engine.answer.return_value = '一般回答'
            app = DailyAssistant(store, engine, default_chat=True)
            first = app.ask(sid, DAYS + FORMAT)
            original = meeting.read(store, first['id'])
            change = '上一場會議，乙的空檔改成無空檔，會議改成 35 分鐘，重算。'
            asked = app.ask(sid, change)
            pending = meeting.read(store, asked['id'])
            self.assertTrue(meeting.pending_date(pending))
            self.assertEqual(pending['meeting_plan'], original['meeting_plan'])
            self.assertEqual(pending[meeting.DATE_EDIT]['dates'], ['2028-02-28', '2028-02-29', '2028-03-01'])
            self.assertEqual(pending['meeting_source']['answer_id'], first['id'])
            self.assertIn('整批更正尚待確認日期', meeting.describe(pending))
            destination = root/'unconfirmed.ics'
            with self.assertRaisesRegex(ValueError, '仍待確認日期'):
                calendar.export(store, asked['id'], '會議', destination, zone='Asia/Taipei')
            self.assertFalse(destination.exists())
            self.assertEqual(app.ask(store.new_session(), '2028-02-29')['content'], '一般回答')
            with self.assertRaisesRegex(ValueError, '上一則列出'):
                app.ask(sid, '2028-03-02')
            self.assertEqual(len(store.messages(sid, 10)), 4)
            bundle = create_backup(store.path, root/'choice.paibackup')
            restore_backup(bundle['path'], root/'restored')
            restored = Store(root/'restored/assistant.sqlite3')
            restored_app = DailyAssistant(restored, engine, default_chat=True)
            stop = Event()
            begin = answer_runs.begin

            def begin_then_stop(*args, **kwargs):
                uid = begin(*args, **kwargs)
                stop.set()
                return uid

            with patch.object(answer_runs, 'begin', side_effect=begin_then_stop), self.assertRaises(EngineError):
                restored_app.ask(sid, '日期是 2028-02-29。', stop)
            resolved = restored_app.retry(sid)
            self.assertEqual(resolved['content'], '2028-03-01 08:10-08:45')
            record = meeting.read(restored, resolved['id'])
            self.assertEqual(record['meeting_source']['answer_id'], asked['id'])
            self.assertEqual(record['current_request'], '日期是 2028-02-29。')
            self.assertEqual(record[meeting.DATE_EDIT]['request'], change)
            self.assertEqual(record[meeting.DATE_EDIT]['selected'], '2028-02-29')
            self.assertEqual(meeting.read(restored, asked['id']), pending)
            self.assertIn('依你的回覆採用日期 2028-02-29', meeting.describe(record))
            with restored.connect() as db:
                db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                           (json.dumps(pending, ensure_ascii=False, indent=2), asked['id']))
            self.assertEqual(meeting.read(restored, asked['id']), pending)
            with self.assertRaisesRegex(ValueError, '上一筆來源已變更'):
                meeting.read(restored, resolved['id'])
            engine.answer.assert_called_once()

    def test_unresolved_date_does_not_override_other_edits_or_cross_a_topic_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            sid = store.new_session()
            engine = Mock(spec=['answer'])
            engine.answer.return_value = '一般回答'
            app = DailyAssistant(store, engine, default_chat=True)
            app.ask(sid, DAYS + FORMAT)
            for question, message in (
                ('上一場會議，乙取消 09:00-10:00，2028-02-29 乙的空檔改為無空檔，重算。', '請指定完整日期'),
                ('上一場會議，乙取消 09:00-10:00，乙的空檔改為無空檔，重算。', '寫明各項的完整日期'),
            ):
                with self.assertRaisesRegex(ValueError, message):
                    app.ask(sid, question)
                self.assertEqual(len(store.messages(sid, 10)), 2)
            asked = app.ask(sid, '上一場會議，乙取消 12:00-13:00，重算。')
            self.assertTrue(meeting.pending_date(meeting.read(store, asked['id'])))
            self.assertEqual(app.ask(sid, '換個話題。')['content'], '一般回答')
            self.assertEqual(app.ask(sid, '2028-02-29')['content'], '一般回答')
            app.ask(sid, DAYS + FORMAT)
            app.ask(sid, '上一場會議，乙取消 12:00-13:00，重算。')
            notes = Path(tmp)/'notes.txt'
            notes.write_text('另一份排程', encoding='utf-8')
            store.attach_document(sid, notes)
            self.assertEqual(app.ask(sid, '2028-02-29')['content'], '一般回答')

    def test_multiple_dates_apply_only_after_each_exact_reply_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root/'assistant.sqlite3')
            sid = store.new_session()
            app = DailyAssistant(store, None, default_chat=True)
            first = app.ask(sid, DAYS + FORMAT)
            original = meeting.read(store, first['id'])
            update = '上一場會議，甲取消 12:05-12:45，乙的空檔改成無空檔，會議改 20 分鐘，重算。'
            asked = app.ask(sid, update)
            self.assertIn('第 1 項「甲取消 12:05-12:45」', asked['content'])
            middle = app.ask(sid, '日期是2028-02-29。')
            self.assertIn('第 2 項「乙的空檔改成無空檔」', middle['content'])
            pending = meeting.read(store, middle['id'])
            self.assertEqual(pending['meeting_source']['answer_id'], asked['id'])
            self.assertEqual(pending['meeting_plan'], original['meeting_plan'])
            self.assertEqual([item['selected'] for item in pending[meeting.DATE_EDIT]['items']],
                             ['2028-02-29', None])
            with self.assertRaisesRegex(ValueError, '上一則列出'):
                app.ask(sid, '2028-03-02')
            bundle = create_backup(store.path, root/'batch.paibackup')
            restore_backup(bundle['path'], root/'restored')
            restored = Store(root/'restored'/'assistant.sqlite3')
            resumed = DailyAssistant(restored, None, default_chat=True)
            final = resumed.ask(sid, '2028-02-28')
            self.assertEqual(final['content'], '2028-03-01 08:10-08:30')
            record = meeting.read(restored, final['id'])
            self.assertFalse(meeting.pending_date(record))
            self.assertEqual(record['meeting_source']['answer_id'], middle['id'])
            edit = record[meeting.DATE_EDIT]
            self.assertEqual(edit['request'], update)
            self.assertEqual([item['reply'] for item in edit['items']], ['日期是2028-02-29。', '2028-02-28'])
            self.assertEqual(meeting.read(restored, middle['id']), pending)
            self.assertEqual(meeting.read(restored, first['id']), original)
            changed = resumed.ask(sid, '上一場會議，會議改 15 分鐘，重算。')
            self.assertEqual(changed['content'], '2028-02-29 12:45-13:00')

    def test_excessive_date_combinations_leave_the_plan_intact(self):
        question = '會議排程：' + ''.join(
            f'2028-03-0{day}：甲可開會：09:00-12:00，乙為 09:00-12:00。' for day in range(1, 8)
        ) + '兩人需要連續 20 分鐘。' + FORMAT
        plan = meeting.parse(question)
        expected = meeting.parse(question)
        with self.assertRaisesRegex(ValueError, '日期的組合太多'):
            meeting.prepare_revision(plan, '上一場會議，甲 09:00-09:05 沒空，乙 09:05-09:10 沒空，'
                                           '甲 09:10-09:15 沒空，乙 09:15-09:20 沒空，重算。')
        self.assertEqual(plan, expected)

    def test_older_answer_can_start_date_clarification_but_pending_answer_cannot_be_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            sid = store.new_session()
            app = DailyAssistant(store, None, default_chat=True)
            first = app.ask(sid, DAYS + FORMAT)
            app.ask(sid, '計算 1 + 1，只回答整數')
            update = f'沿用會議回答 #{first["id"]}，乙空檔改成無空檔，重算。'
            asked = app.ask(sid, update)
            pending = meeting.read(store, asked['id'])
            self.assertTrue(meeting.pending_date(pending))
            self.assertEqual(pending['meeting_source']['answer_id'], first['id'])
            self.assertEqual([row['id'] for row in meeting.recent_completed(store, sid)], [first['id']])
            with self.assertRaisesRegex(ValueError, '尚待確認日期'):
                app.ask(sid, f'沿用會議回答 #{asked["id"]}，會議改半小時，重算。')
            answer = app.ask(sid, '2028-02-29')
            proof = meeting.read(store, answer['id'])
            self.assertEqual(answer['content'], '2028-03-01 08:10-08:40')
            self.assertEqual(proof['meeting_source']['answer_id'], asked['id'])
            self.assertEqual(proof[meeting.DATE_EDIT]['request'], update)

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
