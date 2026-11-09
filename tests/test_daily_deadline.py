from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_deadline
from scripts.daily_expenses import DailyAssistant


REQUEST = ('這張登記票是唯一的票，今天 08:45 截止，進門的唯一要求是票仍在期限內。'
           '現在同日 08:32。只輸出 M8 表示能進、F4 表示不能、U1 表示未定。')
PENDING = REQUEST.replace('現在同日 08:32。', '08:32 是昨天的紀錄，今天目前時間尚未核對。')
MAP_RULE = ('只輸出指定代碼。尚無法判定回 T4，確定不可行回 B8，確定可行回 W6。'
            '憑手上的唯一導覽票領地圖，期限仍有效便可領取。票上記載本日 12:05 截止，截止那一刻仍有效。')
ARCHIVE_RULE = '當日投件只要求手上的唯一回條仍然有效。回條於今天 18:11 失效。'
ARCHIVE_CODES = '只輸出指定代碼。確定可行回 X2，確定不可行回 C7，尚無法判定回 S6。'


class DailyDeadlineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '一般模型回答'
        self.app = DailyAssistant(self.store, self.engine, default_chat=True)

    def test_same_day_comparison_binds_codes_and_explicit_equality(self):
        for request, expected in (
            (REQUEST, 'M8'),
            (REQUEST.replace('08:32', '08:46'), 'F4'),
            (REQUEST.replace('08:32', '08:45').replace('截止，', '截止（含截止時刻），'), 'M8'),
            (REQUEST.replace('08:32', '08:45').replace('截止，', '截止（不含截止時刻），'), 'F4'),
            ('這張展覽入場券是唯一的券，本日 14:05 截止，入場的唯一要求是展覽入場券仍在期限內。'
             '現在同日 13:58。請只回 2 表示不能入場、3 表示未定、1 表示能入場。', '1'),
        ):
            with self.subTest(request=request):
                reply = self.app.ask(self.store.new_session(), request)
                self.assertEqual(reply['content'], expected)
                receipt = daily_deadline.read(self.store, reply['id'])
                self.assertEqual(receipt['plan']['request'], request)
                self.assertIs(receipt['model_called'], False)
                self.assertIn(receipt['plan']['observation'], daily_deadline.describe(receipt))
        self.engine.answer.assert_not_called()

    def test_ambiguous_or_out_of_scope_requests_keep_the_model_route(self):
        for request in (
            REQUEST.replace('08:32', '08:45'), REQUEST.replace('同日', '隔日'),
            REQUEST.replace('08:32', '大概 08:32'), REQUEST.replace('08:32', '24:00'),
            REQUEST.replace('唯一要求', '其中一項要求'), REQUEST.replace('唯一的票', '其中一張票'),
            REQUEST.replace('是票仍', '是券仍'), REQUEST.replace('能進', '能報到'),
            REQUEST.replace('F4', 'M8'), REQUEST + '再通知管理員。',
            REQUEST.replace('登記票', '已過期的票'),
        ):
            with self.subTest(request=request):
                self.assertIsNone(daily_deadline.parse(request))
                reply = self.app.ask(self.store.new_session(), request)
                self.assertEqual(reply['content'], '一般模型回答')
                self.assertIsNone(daily_deadline.read(self.store, reply['id']))
        for option in ('allow_local_calculation', 'allow_local_evidence'):
            app = DailyAssistant(self.store, self.engine, default_chat=True, **{option: False})
            self.assertEqual(app.ask(self.store.new_session(), REQUEST)['content'], '一般模型回答')
        sid = self.store.new_session()
        source = self.path / 'ticket.txt'
        source.write_text('另一張票的期限。', encoding='utf-8')
        self.store.attach_document(sid, source)
        self.assertEqual(self.app.ask(sid, REQUEST)['content'], '一般模型回答')

    def test_cancelled_retry_and_restored_answer_recompute_original_clocks(self):
        sid = self.store.new_session()
        with patch.object(daily_deadline, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, REQUEST)
        reply = self.app.retry(sid)
        self.assertEqual(reply['content'], 'M8')
        self.assertEqual(len(self.store.messages(sid, 10)), 2)
        backup = create_backup(self.store.path, self.path / 'deadline.paibackup')
        restore_backup(backup['path'], self.path / 'restored')
        restored = Store(self.path / 'restored/assistant.sqlite3')
        self.assertEqual(daily_deadline.read(restored, reply['id']), reply[daily_deadline.FIELD])
        with restored.connect() as db:
            db.execute('UPDATE messages SET content=? WHERE id=?', ('F4', reply['id']))
        with self.assertRaisesRegex(ValueError, '期限回覆與原始時刻'):
            daily_deadline.read(restored, reply['id'])
        self.engine.answer.assert_not_called()

    def test_explicit_today_times_and_older_observations_are_separate(self):
        for request, expected, clock, stale in (
            (PENDING, 'U1', None, '08:32'),
            (PENDING.replace('08:32 是昨天的紀錄，', ''), 'U1', None, None),
            (MAP_RULE + '12:05 來自上一個開放日；本日當下時刻還無紀錄。', 'T4', None, '12:05'),
            (MAP_RULE + '本日已確認當下為 12:05。', 'W6', '12:05', None),
            (MAP_RULE + '本日已確認當下為 12:07。', 'B8', '12:07', None),
            (ARCHIVE_RULE + '18:09 是開門登記時刻；沒有今天現在的時間。' + ARCHIVE_CODES, 'S6', None, '18:09'),
            (ARCHIVE_RULE + '已核對今天當下為 18:11。' + ARCHIVE_CODES, 'C7', '18:11', None),
            (ARCHIVE_RULE + '已核對今天當下為 18:09。' + ARCHIVE_CODES, 'X2', '18:09', None),
        ):
            with self.subTest(request=request):
                reply = self.app.ask(self.store.new_session(), request)
                self.assertEqual(reply['content'], expected)
                receipt = daily_deadline.read(self.store, reply['id'])
                plan = receipt['plan']
                self.assertEqual(plan['schema'], daily_deadline.EVIDENCE_SCHEMA)
                self.assertEqual(plan['current'], clock)
                self.assertEqual(plan['stale']['clock'] if plan['stale'] else None, stale)
                self.assertEqual(receipt['state'] == 'undetermined', clock is None)
                self.assertFalse(receipt['model_called'])
        self.engine.answer.assert_not_called()

    def test_no_conclusions_from_a_conflicting_time_or_wrong_credential(self):
        q = ARCHIVE_RULE + '18:09 是開門登記時刻；沒有今天現在的時間。' + ARCHIVE_CODES
        for request in (
            q.replace('回條於今天', '別人的回條於今天'), q.replace('只要求', '還要求'),
            q.replace('；沒有今天現在的時間', ''), q.replace('沒有今天現在的時間', '昨天時間未核對'),
            q.replace('沒有今天現在的時間', '沒有今天現在的時間，已核對今天當下為 18:08'),
            q.replace('失效。', '失效，截止那一刻仍有效。'), q.replace('回 S6', '回 C7'), q + '再幫我投件。',
            MAP_RULE.replace('領地圖', '領地圖並報到') + '本日已確認當下為 12:05。',
            MAP_RULE.replace('，截止那一刻仍有效', '') + '本日已確認當下為 12:05。',
        ):
            with self.subTest(request=request):
                self.assertIsNone(daily_deadline.parse(request))
                self.assertEqual(self.app.ask(self.store.new_session(), request)['content'], '一般模型回答')
        raw = DailyAssistant(self.store, self.engine, default_chat=True, allow_local_evidence=False)
        self.assertEqual(raw.ask(self.store.new_session(), q)['content'], '一般模型回答')

    def test_pending_retry_and_backup_preserve_the_historical_clock_source(self):
        sid = self.store.new_session()
        with patch.object(daily_deadline, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, PENDING)
        result = self.app.retry(sid)
        self.assertEqual(result['content'], 'U1')
        self.assertEqual(len(self.store.messages(sid, 10)), 2)
        backup = create_backup(self.store.path, self.path / 'pending.paibackup')
        restore_backup(backup['path'], self.path / 'restored-pending')
        restored = Store(self.path / 'restored-pending/assistant.sqlite3')
        receipt = daily_deadline.read(restored, result['id'])
        self.assertEqual(receipt, result[daily_deadline.FIELD])
        text = daily_deadline.describe(receipt)
        self.assertIn('08:32 是昨天的紀錄', text)
        self.assertIn('當日實際時刻尚未核對', text)
        with restored.connect() as db:
            db.execute('UPDATE messages SET content=? WHERE id=?', ('M8', result['id']))
        with self.assertRaisesRegex(ValueError, '期限回覆與原始時刻'):
            daily_deadline.read(restored, result['id'])
        self.engine.answer.assert_not_called()
