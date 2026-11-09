import json
from itertools import product
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_necessary as daily
from scripts.daily_expenses import DailyAssistant


HEAD = '判斷「送回甲號箱」須同時符合「甲號箱」的「束帶已扣緊」及「甲號箱」的「核對卡已蓋章」，這些就是全部要求。'
CODES = '只輸出指定代碼。確定可行回 R，確定不可行回 B，尚無法判定回 X。'
REPORT = '本輪核對「甲號箱」的「束帶已扣緊」成立、「核對卡已蓋章」成立'
CORRECTION = '沿用上一輪條件：本輪核對「甲號箱」的「束帶已扣緊」成立。重算。'
ROUND = '沿用上一輪規則，開始新一輪核對。'


class DailyObjectPropertyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '模型替身'
        self.app = DailyAssistant(self.store, self.engine, default_chat=True)

    def test_one_report_can_independently_support_two_conditions(self):
        statuses = {'成立': 'T', '不成立': 'F', '尚未核對': 'U'}
        for first, second in product(statuses, repeat=2):
            with self.subTest(first=first, second=second):
                report = f'本輪核對「甲號箱」的「束帶已扣緊」{first}、「核對卡已蓋章」{second}'
                output = self.app.ask(self.store.new_session(), HEAD + report + '。' + CODES)
                receipt = daily.read(self.store, output['id'])
                states = (statuses[first], statuses[second])
                expected = 'B' if 'F' in states else 'X' if 'U' in states else 'R'
                self.assertEqual(output['content'], expected)
                self.assertEqual(receipt['plan']['schema'], daily.V9)
                self.assertEqual([c['observation'] for c in receipt['plan']['conditions']], [report, report])
                self.assertIn('「甲號箱」的「核對卡已蓋章」｜' + report, daily.describe(receipt))
        self.engine.answer.assert_not_called()

    def test_near_names_other_fields_and_old_report_leave_missing_condition_unknown(self):
        question = (HEAD + REPORT.replace('本輪', '上一次') + '。'
                    '本輪核對「甲號箱東側」的「束帶已扣緊」成立。'
                    '本輪核對「甲號箱」的「束帶已登錄」成立、「核對卡已蓋章」成立。' + CODES)
        reply = self.app.ask(self.store.new_session(), question)
        receipt = daily.read(self.store, reply['id'])
        missing, card = receipt['plan']['conditions']
        self.assertEqual(reply['content'], 'X')
        self.assertIsNone(missing['observation'])
        self.assertEqual(missing['state'], 'unknown')
        self.assertEqual(card['state'], 'true')
        self.assertEqual(missing['earlier_observation']['state'], 'true')
        self.assertEqual([(r['subject'], r['predicate']) for r in receipt['plan']['other_observations']],
                         [('甲號箱東側', '束帶已扣緊'), ('甲號箱', '束帶已登錄')])
        self.assertIn('原問中其他物件或屬性的觀察', daily.describe(receipt))
        self.assertNotIn('甲號箱東側', receipt['canonical'])
        self.engine.answer.assert_not_called()

    def test_ambiguous_names_duplicates_mixed_tasks_and_attachments_keep_normal_route(self):
        good = HEAD + REPORT + '。' + CODES
        for question in (good.replace('本輪', ''), good + '順便送出箱子。',
                         good.replace(REPORT, REPORT + '。本輪核對「甲號箱」的「核對卡已蓋章」不成立'),
                         good.replace('「甲號箱」', '「甲 的 號箱」'),
                         good.replace('「束帶已扣緊」', '「束帶已扣緊 」')):
            with self.subTest(question=question):
                self.assertIsNone(daily.parse(question))
                self.assertEqual(self.app.ask(self.store.new_session(), question)['content'], '模型替身')
        sid = self.store.new_session()
        path = self.path / 'photo-notes.txt'
        path.write_text('本機附件', encoding='utf-8')
        self.store.attach_document(sid, path)
        self.assertEqual(self.app.ask(sid, good)['content'], '模型替身')
        raw = DailyAssistant(self.store, self.engine, default_chat=True, allow_local_evidence=False)
        self.assertEqual(raw.ask(self.store.new_session(), good)['content'], '模型替身')

    def test_maximum_length_bound_names_work_in_backup_groups_and_check_order(self):
        fields = [f'P{i}-' + '欄' * 21 for i in range(6)]
        subject = '箱' * 24
        terms = [f'「{subject}」的「{field}」' for field in fields]
        grouped = '及'.join('（' + '或'.join(group) + '）' for group in (terms[:3], terms[3:]))
        rule = f'判斷「上架」須同時符合{grouped}，這些就是全部要求。'
        observations = f'本輪核對「{subject}」的' + '、'.join(f'「{field}」尚未核對' for field in fields)
        sid = self.store.new_session()
        first = self.app.ask(sid, rule + observations + '。' + CODES)
        self.assertEqual(first['content'], 'X')
        request = '沿用上一輪條件：檢查時間：' + '，'.join(f'{term}1分鐘' for term in terms) + '。先查哪項最省時間？'
        self.assertGreater(len(request), 300)
        order = self.app.ask(sid, request)
        receipt = daily.read(self.store, order['id'])
        self.assertIn('check_order', receipt)
        self.assertEqual(receipt['plan']['basis']['schema'], daily.V9)
        self.engine.answer.assert_not_called()

    def test_retry_correction_and_new_round_revalidate_exact_property_after_restore(self):
        question = HEAD + '本輪核對「甲號箱」的「核對卡已蓋章」成立。' + CODES
        sid = self.store.new_session()
        with patch.object(daily, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, question)
        first = self.app.retry(sid)
        self.assertEqual(first['content'], 'X')
        with self.assertRaisesRegex(EngineError, '完整條件名稱'):
            self.app.ask(sid, CORRECTION.replace('「甲號箱」', '「甲號箱東側」'))
        fixed = self.app.ask(sid, CORRECTION)
        self.assertEqual(fixed['content'], 'R')
        round_answer = self.app.ask(sid, ROUND + REPORT.replace('束帶已扣緊」成立', '束帶已扣緊」不成立') + '。')
        self.assertEqual(round_answer['content'], 'B')
        saved = create_backup(self.store.path, self.path / 'typed.paibackup')
        restore_backup(saved['path'], self.path / 'restored')
        restored = Store(self.path / 'restored/assistant.sqlite3')
        proof = daily.read(restored, round_answer['id'])
        self.assertEqual(proof, round_answer[daily.FIELD])
        context = restored.answer_context(first['id'])
        context[daily.FIELD]['plan']['conditions'][0]['predicate'] = '束帶已登錄'
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False), first['id']))
        with self.assertRaisesRegex(ValueError, '必要條件規約'):
            daily.read(restored, round_answer['id'])
        self.engine.answer.assert_not_called()
