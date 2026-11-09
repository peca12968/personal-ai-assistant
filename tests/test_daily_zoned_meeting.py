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
from scripts.daily_expenses import DailyAssistant


FORMAT = '只輸出最早可行日期及時段，格式 YYYY-MM-DD HH:MM-HH:MM UTC±HH:MM。'
MIXED = (
    '跨時區會議：基準時區 UTC+08:00。候選日期：2026-11-10、2026-11-09。'
    '甲 UTC+08:00：2026-11-09 09:00-11:00，2026-11-10 14:00-15:00。'
    '乙 UTC-08:00：2026-11-08 17:30-19:00，2026-11-09 22:00-23:00。'
    '丙 UTC+05:30：2026-11-09 07:15-08:15，2026-11-10 11:45-12:30。'
    '三人需要連續 30 分鐘。' + FORMAT
)
MIDNIGHT = (
    '跨時區會議：基準時區 UTC+00:00。候選日期：2026-12-31、2027-01-01。'
    '甲 UTC+08:00：2027-01-01 07:40-08:15 和 17:00-17:45。'
    '乙 UTC-05:00：2026-12-31 18:50-19:20，2027-01-01 04:05-05:00。'
    '兩人需要連續 20 分鐘。' + FORMAT
)


class DailyZonedMeetingTests(unittest.TestCase):
    def test_name_selection_restricts_followup_to_that_persons_local_dates_after_restore(self):
        from assistant import answer_runs
        from scripts import daily_meeting_export as calendar
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root/'assistant.sqlite3')
            sid = store.new_session()
            app = DailyAssistant(store, None, default_chat=True)
            first = app.ask(sid, (
                '跨時區會議：基準時區 UTC+08:00。候選日期：2026-11-09、2026-11-10。'
                '「王小安」 UTC+08:00：2026-11-09 09:00-11:00，2026-11-10 09:00-11:00。'
                '「王小晴」 UTC-08:00：2026-11-08 17:30-19:00，2026-11-09 17:30-19:00。'
                '兩人需要連續 30 分鐘。' + FORMAT))
            asked = app.ask(sid, '上一場會議，王 17:30-18:30 沒空，重算。')
            self.assertIn('哪位參與者', asked['content'])
            with self.assertRaisesRegex(ValueError, '仍待確認姓名'):
                calendar.export(store, asked['id'], '會議', root/'pending.ics')
            stop = Event()
            begin = answer_runs.begin

            def begin_then_stop(*args, **kwargs):
                uid = begin(*args, **kwargs)
                stop.set()
                return uid

            with patch.object(answer_runs, 'begin', side_effect=begin_then_stop), self.assertRaises(EngineError):
                app.ask(sid, '姓名是「王小晴」。', stop)
            selected = app.retry(sid)
            pending = meeting.read(store, selected['id'])
            self.assertEqual(meeting.pending_choice_kind(pending), 'date')
            self.assertEqual(pending[meeting.DATE_EDIT]['target']['choices'], ['2026-11-08','2026-11-09'])
            self.assertEqual(pending['meeting_plan'], meeting.read(store, first['id'])['meeting_plan'])
            bundle = create_backup(store.path, root/'scopes.paibackup')
            restore_backup(bundle['path'], root/'restored')
            restored = Store(root/'restored'/'assistant.sqlite3')
            result = DailyAssistant(restored, None, default_chat=True).ask(sid, '2026-11-08')
            self.assertEqual(result['content'], '2026-11-09 10:30-11:00 UTC+08:00')
            proof = meeting.read(restored, result['id'])
            self.assertEqual(proof['meeting_source']['answer_id'], selected['id'])
            self.assertEqual([item['reply'] for item in proof[meeting.DATE_EDIT]['confirmations']],
                             ['姓名是「王小晴」。', '2026-11-08'])
            cuts = proof['meeting_plan']['people']['王小晴']['days']['2026-11-08']['cancelled']
            self.assertEqual(cuts[0]['source'], '王 17:30-18:30 沒空')

    def test_reference_dates_use_each_persons_local_date_and_offset(self):
        plan = meeting.parse(MIXED)
        self.assertEqual(plan['schema'], meeting.ZONED_PLAN)
        self.assertEqual(plan['people']['乙']['offset'], -480)
        answer, receipt = meeting.resolve(plan)
        self.assertEqual(answer, '2026-11-09 09:45-10:15 UTC+08:00')
        self.assertEqual(receipt['schema'], 'daily_meeting_zoned_calculation_v4')
        self.assertEqual([day['date'] for day in receipt['days']], ['2026-11-09', '2026-11-10'])
        self.assertEqual(receipt['days'][0]['effective']['乙'], ['09:30-11:00'])
        self.assertEqual(receipt['days'][0]['effective']['丙'], ['09:45-10:45'])
        self.assertEqual(receipt['days'][1]['common'], ['14:15-15:00'])

    def test_clips_shared_intervals_at_reference_midnight(self):
        plan = meeting.parse(MIDNIGHT)
        answer, receipt = meeting.resolve(plan)
        self.assertEqual(receipt['days'][0]['common'], ['23:50-24:00'])
        self.assertEqual(receipt['days'][1]['common'], ['00:00-00:15', '09:05-09:45'])
        self.assertEqual(answer, '2027-01-01 09:05-09:25 UTC+00:00')
        meeting.revise(plan, '時長改為 10 分鐘')
        self.assertEqual(meeting.resolve(plan)[0], '2026-12-31 23:50-24:00 UTC+00:00')

    def test_fixed_offset_and_date_syntax_rejects_ambiguous_inputs(self):
        for variant in (
            MIXED.replace('UTC-08:00', 'UTC+14:01'), MIXED.replace('UTC-08:00', 'UTC-00:00'),
            MIXED.replace('UTC-08:00', 'America/Los_Angeles'), MIXED.replace('UTC-08:00', 'UTC+8:00'),
            MIXED.replace('2026-11-08 17:30', '2026-02-29 17:30'), MIXED.replace('2026-11-08 17:30', '明天 17:30'),
            MIXED.replace('2026-11-09 22:00', '2026-11-08 22:00'), MIXED.replace('、2026-11-09', '、2026-11-10'),
            MIXED.replace('丙 UTC+05:30', '乙 UTC+05:30'), MIXED.replace('三人', '兩人'),
            MIXED.replace('17:30-19:00', '23:30-00:30'), MIXED + '請建立行事曆事件。',
        ):
            with self.subTest(variant=variant):
                self.assertIsNone(meeting.parse(variant))

    def test_local_date_edits_replay_through_backup_and_detect_wrong_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            app = DailyAssistant(store, None, default_chat=True)
            first = app.ask(sid, MIXED)
            with self.assertRaisesRegex(ValueError, '原先列出'):
                app.ask(sid, '上一場會議，乙 2026-11-10 取消 17:30-18:00，重算。')
            second = app.ask(sid, '上一場會議，乙 2026-11-08 取消 17:30-18:00，重算。')
            self.assertEqual(second['content'], '2026-11-09 10:00-10:30 UTC+08:00')
            record = meeting.read(store, second['id'])
            self.assertEqual(record['meeting_source']['answer_id'], first['id'])
            self.assertIn('乙 UTC-08:00', meeting.describe(record))
            self.assertIn('換算後每人剩餘時段', meeting.describe(record))
            backup = create_backup(store.path, root / 'meeting.paibackup')
            restore_backup(backup['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(meeting.read(restored, second['id']), record)
            record['calculation']['days'][0]['common'] = ['09:45-10:45']
            with restored.connect() as db:
                db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                           (json.dumps(record, ensure_ascii=False), second['id']))
            with self.assertRaisesRegex(ValueError, '重算結果不符'):
                meeting.read(restored, second['id'])

    def test_retry_preserves_zoned_plan_and_attachment_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            engine = Mock(spec=['answer'])
            engine.answer.return_value = '一般模型回答'
            app = DailyAssistant(store, engine, default_chat=True)
            with patch.object(meeting, 'resolve', side_effect=EngineError('停止', 'cancelled')), self.assertRaises(EngineError):
                app.ask(sid, MIDNIGHT)
            retry = app.retry(sid)
            self.assertEqual(retry['content'], '2027-01-01 09:05-09:25 UTC+00:00')
            self.assertEqual(len(store.messages(sid, 10)), 2)
            engine.answer.assert_not_called()
            source = root / 'meeting.txt'
            source.write_text('另一份會議限制', encoding='utf-8')
            store.attach_document(sid, source)
            self.assertEqual(app.ask(sid, MIDNIGHT)['content'], '一般模型回答')
