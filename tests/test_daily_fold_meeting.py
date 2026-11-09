from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_meeting as meeting
from scripts import daily_meeting_zones as zones
from scripts.daily_expenses import DailyAssistant


FORMAT = '只輸出最早可行日期及時段，格式 YYYY-MM-DD HH:MM UTC±HH:MM 至 HH:MM UTC±HH:MM。'
NY = '跨時區會議：基準時區 America/New_York。候選日期：2026-11-01。'
UTC = '乙 Etc/UTC：2026-11-01 05:00-07:00。'
FIRST = NY + '甲 America/New_York：2026-11-01 01:10（第一次）-01:40（第一次）。' + UTC + '兩人需要連續 30 分鐘。' + FORMAT
SECOND = FIRST.replace('（第一次）', '(第二次)')
CROSS = NY + '甲 America/New_York：2026-11-01 01:50（第一次）-01:10（第二次）。' + UTC + '兩人需要連續 20 分鐘。' + FORMAT


class DailyFoldMeetingTests(unittest.TestCase):
    def solve(self, question):
        return meeting.resolve(meeting.bind_named_zones(meeting.parse(question), None, fresh=True))

    def test_explicit_occurrences_choose_distinct_actual_instants(self):
        first, receipt = self.solve(FIRST)
        second, _ = self.solve(SECOND)
        self.assertEqual(receipt['schema'], 'daily_meeting_fold_zone_calculation_v6')
        self.assertEqual(first, '2026-11-01 01:10 UTC-04:00 至 01:40 UTC-04:00')
        self.assertEqual(second, '2026-11-01 01:10 UTC-05:00 至 01:40 UTC-05:00')
        a = datetime.fromisoformat('2026-11-01T01:10-04:00')
        b = datetime.fromisoformat('2026-11-01T01:10-05:00')
        self.assertEqual((b - a).total_seconds(), 3600)

    def test_wall_clock_can_go_back_but_actual_time_must_go_forward(self):
        answer, _ = self.solve(CROSS)
        self.assertEqual(answer, '2026-11-01 01:50 UTC-04:00 至 01:10 UTC-05:00')
        inverted = CROSS.replace('01:50（第一次）-01:10（第二次）', '01:10（第二次）-01:50（第一次）')
        with self.assertRaisesRegex(ValueError, '正向時段'):
            self.solve(inverted)

    def test_half_hour_shift_uses_actual_zone_rules(self):
        template = ('跨時區會議：基準時區 Australia/Lord_Howe。候選日期：2026-04-05。'
                    '甲 Australia/Lord_Howe：2026-04-05 01:35（第一次）-01:50（第一次）。'
                    '乙 Etc/UTC：2026-04-04 14:00-16:00。兩人需要連續 15 分鐘。' + FORMAT)
        self.assertEqual(self.solve(template)[0], '2026-04-05 01:35 UTC+11:00 至 01:50 UTC+11:00')
        second = template.replace('第一次', '第二次')
        self.assertEqual(self.solve(second)[0], '2026-04-05 01:35 UTC+10:30 至 01:50 UTC+10:30')

    def test_missing_repeated_and_single_occurrence_errors_remain_distinct(self):
        for question, detail in (
            (FIRST.replace('（第一次）', ''), '重複的當地時間'),
            (FIRST.replace('2026-11-01', '2026-03-08').replace('01:10', '02:10').replace('01:40', '03:40'),
             '不存在的當地時間'),
            (FIRST.replace('2026-11-01', '2026-11-02'), '只出現一次'),
            (FIRST.replace('甲 America/New_York', '甲 UTC-04:00'), '只出現一次'),
        ):
            with self.subTest(detail=detail), self.assertRaisesRegex(ValueError, detail):
                self.solve(question)
        self.assertIsNone(meeting.parse(FIRST.replace('（第一次）', '（第三次）')))
        self.assertIsNone(meeting.parse(FIRST.replace('（第一次）', '（第一次)')))

    def test_v5_correction_promotes_only_new_plan_and_replays_saved_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            store = Store(folder / 'assistant.sqlite3')
            app = DailyAssistant(store, None, default_chat=True)
            sid = store.new_session()
            original = (NY + '甲 America/New_York：2026-11-01 00:40-03:00。'
                        '乙 Etc/UTC：2026-11-01 05:45-07:00。兩人需要連續 30 分鐘。' + FORMAT)
            first = app.ask(sid, original)
            previous = meeting.read(store, first['id'])
            self.assertEqual(previous['meeting_plan']['schema'], meeting.NAMED_ZONED_PLAN)
            self.assertEqual(first['content'], '2026-11-01 01:45 UTC-04:00 至 01:15 UTC-05:00')
            with patch.object(zones, 'snapshot', side_effect=AssertionError('unexpected current rules')):
                second = app.ask(sid, '上一場會議，甲 2026-11-01 取消 01:00（第二次）-01:30（第二次），重算。')
                self.assertEqual(second['content'], '2026-11-01 01:30 UTC-05:00 至 02:00 UTC-05:00')
                record = meeting.read(store, second['id'])
                self.assertEqual(record['meeting_source']['answer_id'], first['id'])
                self.assertEqual(record['meeting_plan']['schema'], meeting.FOLD_ZONED_PLAN)
                self.assertEqual(record['meeting_plan']['zone_rules'], previous['meeting_plan']['zone_rules'])
                self.assertEqual(meeting.read(store, first['id']), previous)
                backup = create_backup(store.path, folder / 'meeting.paibackup')
                restore_backup(backup['path'], folder / 'restored')
                restored = Store(folder / 'restored' / 'assistant.sqlite3')
                self.assertEqual(meeting.read(restored, second['id']), record)
            record['meeting_plan']['people']['甲']['days']['2026-11-01']['cancelled'][0]['interval']['start_choice'] = 1
            with restored.connect() as db:
                db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                           (json.dumps(record, ensure_ascii=False), second['id']))
            with self.assertRaisesRegex(ValueError, '會議來源與已完成的回答流程不符'):
                meeting.read(restored, second['id'])

    def test_batch_checks_local_endpoints_before_saving_and_reuses_the_frozen_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'assistant.sqlite3')
            app = DailyAssistant(store, None, default_chat=True)
            sid = store.new_session()
            original = (NY + '甲 America/New_York：2026-11-01 00:30-03:00。'
                        '乙 Etc/UTC：2026-11-01 04:45-08:00。兩人需要連續 20 分鐘。' + FORMAT)
            first = app.ask(sid, original)
            previous = meeting.read(store, first['id'])
            valid = ('上一場會議，甲 2026-11-01 取消 01:00（第一次）-01:20（第一次）；'
                     '乙 2026-11-01 取消 04:45-05:05；時長改為 30 分鐘，重算。')
            invalid = valid.replace('04:45-05:05', '04:45（第一次）-05:05')
            with patch.object(zones, 'snapshot', side_effect=AssertionError('unexpected current rules')):
                with self.assertRaisesRegex(ValueError, '只出現一次'):
                    app.ask(sid, invalid)
                self.assertEqual(len(store.messages(sid, 10)), 2)
                self.assertEqual(meeting.read(store, first['id']), previous)
                second = app.ask(sid, valid)
                self.assertEqual(second['content'], '2026-11-01 01:20 UTC-04:00 至 01:50 UTC-04:00')
                current = meeting.read(store, second['id'])
                self.assertEqual(current['meeting_plan']['schema'], meeting.FOLD_ZONED_PLAN)
                self.assertEqual(current['meeting_plan']['zone_rules'], previous['meeting_plan']['zone_rules'])

    def test_retry_preserves_annotations_and_reference_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'assistant.sqlite3')
            sid = store.new_session()
            engine = Mock(spec=['answer'])
            app = DailyAssistant(store, engine, default_chat=True)
            with patch.object(meeting, 'resolve', side_effect=EngineError('停止', 'cancelled')), self.assertRaises(EngineError):
                app.ask(sid, SECOND)
            with patch.object(zones, 'snapshot', side_effect=AssertionError('unexpected current rules')):
                retry = app.retry(sid)
            self.assertEqual(retry['content'], '2026-11-01 01:10 UTC-05:00 至 01:40 UTC-05:00')
            self.assertEqual(len(store.messages(sid, 10)), 2)
            self.assertIn('01:10（第二次）-01:40（第二次）', meeting.describe(meeting.read(store, retry['id'])))
            engine.answer.assert_not_called()
