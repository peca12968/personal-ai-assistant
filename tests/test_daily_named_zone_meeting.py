import base64
from hashlib import sha256
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


FORMAT = '兩人需要連續 30 分鐘。只輸出最早可行日期及時段，格式 YYYY-MM-DD HH:MM UTC±HH:MM 至 HH:MM UTC±HH:MM。'
SPRING = ('跨時區會議：基準時區 America/New_York。候選日期：2026-03-08。'
          '甲 America/New_York：2026-03-08 01:45-03:45。乙 Etc/UTC：2026-03-08 06:40-08:00。' + FORMAT)
FALL = ('跨時區會議：基準時區 America/New_York。候選日期：2026-11-01。'
        '甲 America/New_York：2026-11-01 00:40-03:00。乙 Etc/UTC：2026-11-01 05:45-07:00。' + FORMAT)


class DailyNamedZoneMeetingTests(unittest.TestCase):
    def test_reference_clock_changes_but_real_duration_is_thirty_minutes(self):
        for question, expected, overlap in (
            (SPRING, '2026-03-08 01:45 UTC-05:00 至 03:15 UTC-04:00',
             '01:45 UTC-05:00 至 03:45 UTC-04:00'),
            (FALL, '2026-11-01 01:45 UTC-04:00 至 01:15 UTC-05:00',
             '01:45 UTC-04:00 至 02:00 UTC-05:00'),
        ):
            with self.subTest(expected=expected):
                plan = meeting.bind_named_zones(meeting.parse(question), None, fresh=True)
                answer, receipt = meeting.resolve(plan)
                self.assertEqual(answer, expected)
                self.assertEqual(receipt['duration'], 30)
                self.assertEqual(receipt['days'][0]['common'], [overlap])
                self.assertEqual(set(plan['zone_rules']['zones']), {'America/New_York', 'Etc/UTC'})

    def test_one_named_participant_uses_distinct_offsets_on_distinct_dates(self):
        question = ('跨時區會議：基準時區 UTC+08:00。候選日期：2026-03-07、2026-03-08。'
                    '甲 America/New_York：2026-03-07 09:00-10:00，2026-03-08 09:00-10:00。'
                    '乙 UTC+08:00：2026-03-07 21:00-22:00，2026-03-08 21:00-22:00。' + FORMAT)
        plan = meeting.bind_named_zones(meeting.parse(question), None, fresh=True)
        answer, receipt = meeting.resolve(plan)
        self.assertEqual(answer, '2026-03-08 21:00 UTC+08:00 至 21:30 UTC+08:00')
        self.assertEqual(receipt['days'][0]['effective']['甲'], ['22:00 UTC+08:00 至 23:00 UTC+08:00'])
        self.assertEqual(receipt['days'][1]['effective']['甲'], ['21:00 UTC+08:00 至 22:00 UTC+08:00'])

    def test_nonexistent_ambiguous_and_unknown_zones_do_not_guess(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'assistant.sqlite3')
            engine = Mock(spec=['answer'])
            app = DailyAssistant(store, engine, default_chat=True)
            for question, detail in (
                (SPRING.replace('01:45-03:45', '02:10-03:45'), '不存在的當地時間'),
                (FALL.replace('00:40-03:00', '01:10-03:00'), '重複的當地時間'),
                (SPRING.replace('America/New_York', 'America/Unknown'), '找不到時區'),
            ):
                with self.subTest(detail=detail), self.assertRaisesRegex(ValueError, detail):
                    app.ask(store.new_session(), question)
            engine.answer.assert_not_called()

    def test_retry_edit_and_backup_use_saved_bytes_even_without_installed_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            app = DailyAssistant(store, None, default_chat=True)
            with patch.object(meeting, 'resolve', side_effect=EngineError('停止', 'cancelled')), self.assertRaises(EngineError):
                app.ask(sid, SPRING)
            with patch.object(zones, 'snapshot', side_effect=AssertionError('unexpected current rules')) as fresh:
                first = app.retry(sid)
                second = app.ask(sid, '上一場會議，乙 2026-03-08 取消 06:40-07:05，重算。')
                self.assertEqual(second['content'], '2026-03-08 03:05 UTC-04:00 至 03:35 UTC-04:00')
                record = meeting.read(store, second['id'])
                self.assertEqual(record['meeting_source']['answer_id'], first['id'])
                self.assertIn('沿用保存的 tzdata', meeting.describe(record))
                backup = create_backup(store.path, root / 'zones.paibackup')
                restore_backup(backup['path'], root / 'restored')
                restored = Store(root / 'restored' / 'assistant.sqlite3')
                self.assertEqual(meeting.read(restored, second['id']), record)
                fresh.assert_not_called()
            initial = restored.answer_context(first['id'])
            initial['meeting_plan']['zone_rules']['zones']['America/New_York']['sha256'] = '0' * 64
            with restored.connect() as db:
                run = json.loads(db.execute('SELECT context_json FROM answer_runs WHERE id=?',
                                            (first['run_id'],)).fetchone()[0])
                run['meeting_plan'] = initial['meeting_plan']
                db.execute('UPDATE answer_runs SET context_json=? WHERE id=?',
                           (json.dumps(run, ensure_ascii=False), first['run_id']))
                db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                           (json.dumps(initial, ensure_ascii=False), first['id']))
            with self.assertRaisesRegex(ValueError, '保存規則無法核對'):
                meeting.read(restored, first['id'])

    def test_corrupt_tzif_counts_are_rejected_before_zone_decoder(self):
        raw = bytearray(44)
        raw[:4] = b'TZif'
        raw[32:36] = (2**31).to_bytes(4, 'big')
        raw[36:40] = (1).to_bytes(4, 'big')
        data = dict(version='2026.6', zones={'Etc/UTC': dict(
            sha256=sha256(raw).hexdigest(), tzif=base64.b64encode(raw).decode('ascii'))})
        with patch.object(zones, 'ZoneInfo') as decoder, self.assertRaisesRegex(ValueError, '保存規則無法核對'):
            zones.load_rules(data, {'Etc/UTC'})
        decoder.from_file.assert_not_called()

    def test_named_attachment_fallback_does_not_load_time_zone_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            store = Store(folder / 'assistant.sqlite3')
            sid = store.new_session()
            source = folder / 'meeting.txt'
            source.write_text('其他會議限制。', encoding='utf-8')
            store.attach_document(sid, source)
            engine = Mock(spec=['answer'])
            engine.answer.return_value = '一般模型回答'
            with patch.object(zones, 'snapshot', side_effect=AssertionError('unexpected local route')):
                self.assertEqual(DailyAssistant(store, engine, default_chat=True).ask(sid, SPRING)['content'], '一般模型回答')
