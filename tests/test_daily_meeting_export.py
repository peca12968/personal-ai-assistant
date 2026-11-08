from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.store import Store
from scripts import daily_meeting_export as calendar
from scripts.daily_expenses import DailyAssistant
from scripts.publish import validate_path


NAMED_FORMAT = ('只輸出最早可行日期及時段，格式 YYYY-MM-DD HH:MM UTC±HH:MM 至 HH:MM UTC±HH:MM。')
FOLD = ('跨時區會議：基準時區 America/New_York。候選日期：2026-11-01。'
        '甲 America/New_York：2026-11-01 01:50（第一次）-01:10（第二次）。'
        '乙 Etc/UTC：2026-11-01 05:00-07:00。兩人需要連續 20 分鐘。' + NAMED_FORMAT)
SAME_DAY = ('甲可開會：23:20-24:00，乙為 23:30-24:00。兩人需要連續 30 分鐘。'
            '只輸出最早可行時段，格式 HH:MM-HH:MM。')
DATED = ('會議排程：2026-11-09：甲可開會：08:00-09:00，乙為 08:10-09:00。兩人需要連續 30 分鐘。'
         '只輸出最早可行日期及時段，格式 YYYY-MM-DD HH:MM-HH:MM。')


class DailyMeetingExportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.engine = Mock(spec=['answer'])
        self.assistant = DailyAssistant(self.store, self.engine, default_chat=True)

    def ask(self, question):
        return self.assistant.ask(self.store.new_session(), question)['id']

    def properties(self, path):
        raw = path.read_bytes()
        self.assertTrue(raw.endswith(b'\r\n'))
        lines = raw.split(b'\r\n')
        for line in lines:
            self.assertLessEqual(len(line), 75)
            line.decode('utf-8')
        return raw.replace(b'\r\n ', b'').decode('utf-8').splitlines()

    def test_fold_exports_actual_utc_instants_after_restore(self):
        answer_id = self.ask(FOLD)
        bundle = create_backup(self.store.path, self.root / 'meetings.paibackup')
        restore_backup(bundle['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        with patch.object(calendar.zones, 'snapshot', side_effect=AssertionError('unexpected fresh rules')):
            path = calendar.export(restored, answer_id, '跨時區確認', self.root / 'fold.ics')
        lines = self.properties(path)
        self.assertIn('DTSTART:20261101T055000Z', lines)
        self.assertIn('DTEND:20261101T061000Z', lines)
        self.assertIn('SUMMARY:跨時區確認', lines)
        self.assertEqual(lines.count('BEGIN:VEVENT'), 1)
        self.assertEqual({line.split(':', 1)[0] for line in lines},
                         {'BEGIN', 'VERSION', 'PRODID', 'UID', 'DTSTAMP', 'DTSTART', 'DTEND', 'SUMMARY', 'END'})
        self.assertNotIn('America/New_York', path.read_text(encoding='utf-8'))
        self.engine.answer.assert_not_called()

    def test_older_schedules_require_only_their_missing_date_and_zone(self):
        same_day = self.ask(SAME_DAY)
        dated = self.ask(DATED)
        relative = self.ask(DATED.replace('會議排程：2026-11-09：', '會議排程：以 2026-11-08 為今天。明天：'))
        path = self.root / 'day.ics'
        with self.assertRaisesRegex(ValueError, '完整日期與時區'):
            calendar.export(self.store, same_day, '會議', path, day='2026-11-09')
        self.assertFalse(path.exists())
        lines = self.properties(calendar.export(self.store, same_day, '會議', path, day='2026-11-09', zone='UTC+00:00'))
        self.assertIn('DTSTART:20261109T233000Z', lines)
        self.assertIn('DTEND:20261110T000000Z', lines)
        for answer_id, filename in ((dated, 'dated.ics'), (relative, 'relative.ics')):
            with self.assertRaisesRegex(ValueError, '提供這場會議的時區'):
                calendar.export(self.store, answer_id, '會議', self.root / filename)
            with self.assertRaisesRegex(ValueError, '已確認的日期'):
                calendar.export(self.store, answer_id, '會議', self.root / filename, day='2026-11-10', zone='UTC+08:00')
            lines = self.properties(calendar.export(self.store, answer_id, '會議', self.root / filename, zone='Asia/Taipei'))
            self.assertIn('DTSTART:20261109T001000Z', lines)
            self.assertIn('DTEND:20261109T004000Z', lines)
        self.engine.answer.assert_not_called()

    def test_stored_fixed_and_named_zones_preserve_cross_date_and_spring_instants(self):
        fixed = self.ask('跨時區會議：基準時區 UTC+08:00。候選日期：2026-11-09。'
                         '甲 UTC+08:00：2026-11-09 06:00-07:00。乙 UTC-08:00：2026-11-08 14:00-15:00。'
                         '兩人需要連續 30 分鐘。只輸出最早可行日期及時段，格式 YYYY-MM-DD HH:MM-HH:MM UTC±HH:MM。')
        named = self.ask('跨時區會議：基準時區 America/New_York。候選日期：2026-03-08。'
                         '甲 America/New_York：2026-03-08 01:45-03:45。乙 Etc/UTC：2026-03-08 06:40-08:00。'
                         '兩人需要連續 30 分鐘。' + NAMED_FORMAT)
        for answer_id, filename, start, end in (
            (fixed, 'fixed.ics', '20261108T220000Z', '20261108T223000Z'),
            (named, 'spring.ics', '20260308T064500Z', '20260308T071500Z'),
        ):
            with self.assertRaisesRegex(ValueError, '已確認的時區'):
                calendar.export(self.store, answer_id, '會議', self.root / filename, zone='Asia/Taipei')
            lines = self.properties(calendar.export(self.store, answer_id, '會議', self.root / filename))
            self.assertIn('DTSTART:' + start, lines)
            self.assertIn('DTEND:' + end, lines)

    def test_dated_export_rejects_unresolved_or_changed_duration_due_to_zone(self):
        start = self.ask(DATED.replace('2026-11-09', '2026-11-01').replace('08:00-09:00', '01:10-02:30')
                         .replace('08:10-09:00', '01:10-02:30'))
        with self.assertRaisesRegex(ValueError, '重複的當地時間'):
            calendar.export(self.store, start, '會議', self.root / 'ambiguous.ics', zone='America/New_York')
        spring = self.ask(DATED.replace('2026-11-09', '2026-03-08').replace('08:00-09:00', '01:30-03:40')
                          .replace('08:10-09:00', '01:30-03:40').replace('30 分鐘', '120 分鐘'))
        with self.assertRaisesRegex(ValueError, '時長與原排程不同'):
            calendar.export(self.store, spring, '會議', self.root / 'spring.ics', zone='America/New_York')
        self.assertFalse(list(self.root.glob('*.ics')))

    def test_no_overlap_and_modified_records_do_not_export(self):
        no_overlap = self.ask(DATED.replace('08:10-09:00', '10:00-11:00'))
        with self.assertRaisesRegex(ValueError, '沒有可匯出'):
            calendar.export(self.store, no_overlap, '會議', self.root / 'absent.ics', zone='UTC+08:00')
        answer_id = self.ask(FOLD)
        record = self.store.answer_context(answer_id)
        record['calculation']['result']['exact'] = '2026-11-01 01:50 UTC-05:00 至 02:10 UTC-05:00'
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(record, ensure_ascii=False), answer_id))
        with self.assertRaisesRegex(ValueError, '重算結果不符'):
            calendar.export(self.store, answer_id, '會議', self.root / 'modified.ics')
        self.assertFalse(list(self.root.glob('*.ics')))

    def test_escaped_unicode_title_and_exclusive_commit(self):
        answer_id = self.ask(FOLD)
        destination = self.root / 'meeting.ICS'
        title = '跨區同步📅' * 10 + r',會議;A\B'
        for bad in ('會議\nATTENDEE:other', '會議\r', '會議\x00', ' '):
            with self.assertRaisesRegex(ValueError, '單行文字'):
                calendar.export(self.store, answer_id, bad, destination)
        with patch.object(calendar.os, 'fsync', side_effect=OSError('disk write failed')), self.assertRaises(OSError):
            calendar.export(self.store, answer_id, title, destination)
        self.assertFalse(list(self.root.glob('*.ics')))
        self.assertFalse(destination.exists())
        self.properties(calendar.export(self.store, answer_id, title, destination))
        lines = self.properties(destination)
        self.assertIn('SUMMARY:' + '跨區同步📅' * 10 + r'\,會議\;A\\B', lines)
        self.assertTrue(datetime.strptime(next(line.split(':', 1)[1] for line in lines if line.startswith('DTSTAMP:')),
                                          '%Y%m%dT%H%M%SZ'))
        saved = destination.read_bytes()
        with self.assertRaisesRegex(ValueError, '檔案已存在'):
            calendar.export(self.store, answer_id, '新標題', destination)
        self.assertEqual(destination.read_bytes(), saved)
        self.assertFalse(validate_path('docs/meeting.ICS'))

    def test_cli_exports_a_saved_answer_id(self):
        answer_id = self.ask(FOLD)
        destination = self.root / '會議.ics'
        with patch('sys.argv', ['daily_meeting_export', '--data-dir', str(self.root), '--answer-id', str(answer_id),
                               '--title', '會議', '--output', str(destination)]), redirect_stdout(io.StringIO()) as output:
            calendar.main()
        self.assertIn(str(destination.resolve()), output.getvalue())
        self.assertIn('DTSTART:20261101T055000Z', self.properties(destination))
