import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_meeting as meeting
from scripts import daily_meeting_export as calendar
from scripts import daily_meeting_zones as zones
from scripts.daily_expenses import DailyAssistant


FORMAT = '只輸出最早可行時段，格式 YYYY-MM-DD HH:MM UTC±HH:MM 至 YYYY-MM-DD HH:MM UTC±HH:MM。'
NEW_YEAR = ('跨時區會議：基準時區 Etc/UTC。候選起始日期：2026-12-31。允許跨基準午夜。'
            '甲 Etc/UTC：2026-12-31 23:40 至 2027-01-01 00:35。'
            '乙 Asia/Taipei：2027-01-01 07:50-08:30。兩人需要連續 30 分鐘。' + FORMAT)
NEW_YEAR_ANSWER = '2026-12-31 23:50 UTC+00:00 至 2027-01-01 00:20 UTC+00:00'
FALL = ('跨時區會議：基準時區 America/New_York。候選起始日期：2026-10-31。允許跨基準午夜。'
        '甲 America/New_York：2026-10-31 23:50 至 2026-11-01 01:30（第二次）。'
        '乙 Etc/UTC：2026-11-01 03:50-06:40。兩人需要連續 150 分鐘。' + FORMAT)


class DailyOvernightMeetingTests(unittest.TestCase):
    def solve(self, question):
        plan = meeting.bind_named_zones(meeting.parse(question), None, fresh=True)
        self.assertEqual(plan['schema'], meeting.OVERNIGHT_ZONED_PLAN)
        return meeting.resolve(plan)

    def test_candidate_dates_restrict_start_only(self):
        answer, receipt = self.solve(NEW_YEAR)
        self.assertEqual(answer, NEW_YEAR_ANSWER)
        self.assertEqual(receipt['days'][0]['common'], [
            '2026-12-31 23:50 UTC+00:00 至 2026-12-31 23:59 UTC+00:00'])
        later = NEW_YEAR.replace('07:50-08:30', '08:05-08:50')
        self.assertEqual(self.solve(later)[0], '沒有可行時段')
        included = later.replace('候選起始日期：2026-12-31', '候選起始日期：2027-01-01、2026-12-31')
        self.assertEqual(self.solve(included)[0], '2027-01-01 00:05 UTC+00:00 至 2027-01-01 00:35 UTC+00:00')

    def test_touching_and_overlapping_alternatives_cannot_be_joined(self):
        for slots in (
            '23:45 至 2027-01-01 00:00 或 2027-01-01 00:00 至 2027-01-01 00:20',
            '23:45 至 2027-01-01 00:04 或 2027-01-01 00:00 至 2027-01-01 00:20',
            '23:45-24:00，2027-01-01 00:00-00:20',
        ):
            with self.subTest(slots=slots):
                self.assertEqual(self.solve(NEW_YEAR.replace('23:40 至 2027-01-01 00:35', slots))[0], '沒有可行時段')

    def test_missing_or_repeated_following_day_endpoints_are_checked(self):
        self.assertEqual(self.solve(FALL)[0], '2026-10-31 23:50 UTC-04:00 至 2026-11-01 01:20 UTC-05:00')
        with self.assertRaisesRegex(ValueError, '重複的當地時間'):
            self.solve(FALL.replace('（第二次）', ''))
        spring = ('跨時區會議：基準時區 America/New_York。候選起始日期：2026-03-07。允許跨基準午夜。'
                  '甲 America/New_York：2026-03-07 23:45 至 2026-03-08 04:15。'
                  '乙 Etc/UTC：2026-03-08 04:50-08:10。兩人需要連續 180 分鐘。' + FORMAT)
        self.assertEqual(self.solve(spring)[0], '2026-03-07 23:50 UTC-05:00 至 2026-03-08 03:50 UTC-04:00')
        with self.assertRaisesRegex(ValueError, '不存在的當地時間'):
            self.solve(spring.replace('04:15', '02:15'))

    def test_explicit_start_policy_and_endpoint_dates_required(self):
        for question in (
            NEW_YEAR.replace('允許跨基準午夜。', ''),
            NEW_YEAR.replace('候選起始日期', '候選日期'),
            NEW_YEAR.replace('23:40 至 2027-01-01 00:35', '23:40-00:35'),
            NEW_YEAR.replace('2027-01-01 00:35', '2027-01-02 00:35'),
            NEW_YEAR.replace('2027-01-01 00:35', '2027-01-01 24:00'),
        ):
            with self.subTest(question=question):
                self.assertIsNone(meeting.parse(question))

    def test_cross_midnight_cancellation_revalidates_after_restore_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            engine = Mock(spec=['answer'])
            app = DailyAssistant(store, engine, default_chat=True)
            first = app.ask(sid, NEW_YEAR)
            with patch.object(zones, 'snapshot', side_effect=AssertionError('unexpected current rules')):
                second = app.ask(sid, '上一場會議，甲 2026-12-31 取消 23:55 至 2027-01-01 00:00，重算。')
                self.assertEqual(second['content'], '沒有可行時段')
                backup = create_backup(store.path, root / 'meeting.paibackup')
                restore_backup(backup['path'], root / 'restored')
                restored = Store(root / 'restored' / 'assistant.sqlite3')
                record = meeting.read(restored, second['id'])
                self.assertEqual(record['meeting_source']['answer_id'], first['id'])
                self.assertIn('共同可開始時間：無', meeting.describe(record))
                original = meeting.read(restored, first['id'])
                self.assertEqual(original['meeting_plan']['zone_rules'], record['meeting_plan']['zone_rules'])
                output = root / 'meeting.ics'
                calendar.export(restored, first['id'], '測試會議', output)
                self.assertIn(b'DTSTART:20261231T235000Z\r\nDTEND:20270101T002000Z\r\n', output.read_bytes())
                with self.assertRaisesRegex(ValueError, '沒有可匯出'):
                    calendar.export(restored, second['id'], '沒有空檔', root / 'none.ics')
            original['calculation']['result']['utc_end_minute'] += 1
            with restored.connect() as db:
                db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                           (json.dumps(original, ensure_ascii=False), first['id']))
            with self.assertRaisesRegex(ValueError, '與重算結果不符'):
                calendar.export(restored, first['id'], '已變更', root / 'changed.ics')
            engine.answer.assert_not_called()

    def test_retry_retains_frozen_endpoint_rules_without_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'assistant.sqlite3')
            engine = Mock(spec=['answer'])
            app = DailyAssistant(store, engine, default_chat=True)
            sid = store.new_session()
            with patch.object(meeting, 'resolve', side_effect=EngineError('停止', 'cancelled')), self.assertRaises(EngineError):
                app.ask(sid, FALL)
            with patch.object(zones, 'snapshot', side_effect=AssertionError('unexpected current rules')):
                result = app.retry(sid)
                self.assertEqual(result['content'], '2026-10-31 23:50 UTC-04:00 至 2026-11-01 01:20 UTC-05:00')
                saved = meeting.read(store, result['id'])
                self.assertIn('2026-11-01 01:30（第二次）', meeting.describe(saved))
            engine.answer.assert_not_called()
