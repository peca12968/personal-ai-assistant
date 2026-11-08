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


OUTPUT = '兩人需要連續 30 分鐘。只輸出最早可行日期及時段，格式 YYYY-MM-DD HH:MM-HH:MM。'
BEFORE_MIDNIGHT = dict(local_datetime='2028-12-31T23:59:00+08:00', observed_at_utc='2028-12-31T15:59:00+00:00')
AFTER_MIDNIGHT = dict(local_datetime='2029-01-01T00:01:00+08:00', observed_at_utc='2028-12-31T16:01:00+00:00')
QUESTION = ('會議排程：後天：甲可開會：09:00-10:00，乙為 09:15-10:00。'
            '明天：甲可開會：13:00-14:00，乙為 13:20-14:00。' + OUTPUT)


class DailyRelativeMeetingTests(unittest.TestCase):
    def test_explicit_anchor_uses_monday_week_boundaries_and_can_mix_absolute_dates(self):
        question = ('會議排程：以 2028-12-31 為今天。'
                    '2029-01-02：甲可開會：08:00-09:00，乙為 08:15-09:00。'
                    '下星期一：甲可開會：13:00-14:00，乙為 13:20-14:00。'
                    '本週六：甲可開會：11:00-12:00，乙無空檔。' + OUTPUT)
        with patch.object(meeting.document_dates, 'observe_local_clock') as clock:
            plan = meeting.parse(question, use_local_clock=True)
        clock.assert_not_called()
        self.assertEqual(plan['schema'], meeting.RELATIVE_PLAN)
        self.assertEqual(plan['date_anchor'], {'source': 'user', 'date': '2028-12-31'})
        self.assertEqual(plan['date_terms'], {'2029-01-02': '2029-01-02',
                                             '2029-01-01': '下星期一', '2028-12-30': '本週六'})
        self.assertEqual(meeting.resolve(plan)[0], '2029-01-01 13:20-13:50')

    def test_leap_and_ambiguous_or_duplicate_dates(self):
        base = ('以 2028-02-28 為今天。這週三：甲可開會：09:00-10:00，乙為 09:15-10:00。'
                '明天：甲可開會：13:00-14:00，乙為 13:20-14:00。' + OUTPUT)
        self.assertEqual(meeting.resolve(meeting.parse(base))[0], '2028-02-29 13:20-13:50')
        for variant in (base.replace('這週三', '2028-02-29'), base.replace('這週三', '後天').replace('明天', '這週三'),
                        base.replace('這週三', '週三'), base.replace('這週三', '這週'),
                        base.replace('2028-02-28', '2027-02-29'),
                        base.replace('2028-02-28', '9999-12-31'), base.replace('明天', '後天 UTC+8')):
            with self.subTest(variant=variant):
                self.assertIsNone(meeting.parse(variant))

    def test_midnight_retry_keeps_old_observation_but_new_question_reanchors(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'assistant.sqlite3')
            sid = store.new_session()
            app = DailyAssistant(store, None, default_chat=True)
            with patch.object(meeting.document_dates, 'observe_local_clock', return_value=BEFORE_MIDNIGHT) as first_clock, \
                    patch.object(meeting, 'resolve', side_effect=EngineError('停止', 'cancelled')), self.assertRaises(EngineError):
                app.ask(sid, QUESTION)
            first_clock.assert_called_once()
            with patch.object(meeting.document_dates, 'observe_local_clock', return_value=AFTER_MIDNIGHT) as clock:
                answered = app.retry(sid)
                self.assertEqual(answered['content'], '2029-01-01 13:20-13:50')
                record = meeting.read(store, answered['id'])
                self.assertEqual(record['meeting_plan']['date_anchor']['observation'], BEFORE_MIDNIGHT)
                self.assertIn('2029-01-01（原文「明天」）', meeting.describe(record))
                self.assertEqual(len(store.messages(sid, 10)), 2)
                clock.assert_not_called()
                new = app.ask(store.new_session(), QUESTION)
                self.assertEqual(new['content'], '2029-01-02 13:20-13:50')
                clock.assert_called_once()

    def test_restore_replays_relative_root_and_rejects_a_changed_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            app = DailyAssistant(store, None, default_chat=True)
            with patch.object(meeting.document_dates, 'observe_local_clock', return_value=BEFORE_MIDNIGHT):
                first = app.ask(sid, QUESTION)
            with patch.object(meeting.document_dates, 'observe_local_clock') as clock:
                with self.assertRaisesRegex(ValueError, '完整日期'):
                    app.ask(sid, '上一場會議，明天 乙取消整個上午，重算。')
                next_answer = app.ask(sid, '上一場會議，2029-01-01 乙的空檔改為無空檔，重算。')
                self.assertEqual(next_answer['content'], '2029-01-02 09:15-09:45')
                receipt = meeting.read(store, next_answer['id'])
                backup = create_backup(store.path, root / 'relative.paibackup')
                restore_backup(backup['path'], root / 'restored')
                restored = Store(root / 'restored' / 'assistant.sqlite3')
                self.assertEqual(meeting.read(restored, next_answer['id']), receipt)
                clock.assert_not_called()
            context = restored.answer_context(first['id'])
            context['meeting_plan']['date_anchor']['observation']['observed_at_utc'] = '2028-12-31T16:01:00+00:00'
            with restored.connect() as db:
                initial = json.loads(db.execute('SELECT context_json FROM answer_runs WHERE id=?',
                                                (first['run_id'],)).fetchone()[0])
                initial['meeting_plan'] = context['meeting_plan']
                db.execute('UPDATE answer_runs SET context_json=? WHERE id=?',
                           (json.dumps(initial, ensure_ascii=False), first['run_id']))
                db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                           (json.dumps(context, ensure_ascii=False), first['id']))
            with self.assertRaisesRegex(ValueError, '觀察時間不一致'):
                meeting.read(restored, first['id'])

    def test_damaged_retry_refuses_to_read_a_fresh_clock_or_use_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'assistant.sqlite3')
            sid = store.new_session()
            engine = Mock(spec=['answer'])
            app = DailyAssistant(store, engine, default_chat=True)
            with patch.object(meeting.document_dates, 'observe_local_clock', return_value=BEFORE_MIDNIGHT), \
                    patch.object(meeting, 'resolve', side_effect=EngineError('停止', 'cancelled')), self.assertRaises(EngineError):
                app.ask(sid, QUESTION)
            with store.connect() as db:
                row = db.execute('SELECT id,context_json FROM answer_runs WHERE session_id=?', (sid,)).fetchone()
                initial = json.loads(row['context_json'])
                del initial['meeting_plan']['date_anchor']['observation']
                db.execute('UPDATE answer_runs SET context_json=? WHERE id=?',
                           (json.dumps(initial, ensure_ascii=False), row['id']))
            with patch.object(meeting.document_dates, 'observe_local_clock') as clock, \
                    self.assertRaisesRegex(ValueError, '會議來源已變更'):
                app.retry(sid)
            clock.assert_not_called()
            engine.answer.assert_not_called()
            self.assertEqual(len(store.messages(sid, 10)), 1)
