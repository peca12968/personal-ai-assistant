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


CODES = '只輸出指定代碼。確定可行回 R，確定不可行回 B，尚無法判定回 X。'
HEAD = ('判斷「開放南櫃」須同時符合「南側物料櫃」的「通氣閥已關閉」及「南側物料櫃」的「核對卡已蓋章」，'
        '這些就是全部要求。')


class NaturalObjectObservationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '模型替身'
        self.app = DailyAssistant(self.store, self.engine, default_chat=True)

    def test_shared_report_preserves_independent_states_for_both_properties(self):
        valve = {'T': '通氣閥關著', 'F': '通氣閥還開著', 'U': '通氣閥尚未核對'}
        card = {'T': '核對卡已有章', 'F': '核對卡沒有章', 'U': '核對卡還沒核對'}
        for a, b in product(valve, card):
            with self.subTest(a=a, b=b):
                report = f'本輪確認「南側物料櫃」{valve[a]}，{card[b]}'
                reply = self.app.ask(self.store.new_session(), HEAD + report + '。' + CODES)
                proof = daily.read(self.store, reply['id'])
                expected = 'B' if 'F' in (a, b) else 'X' if 'U' in (a, b) else 'R'
                self.assertEqual(reply['content'], expected)
                self.assertEqual(proof['plan']['schema'], daily.V10)
                self.assertEqual([row['observation'] for row in proof['plan']['conditions']], [report, report])
                self.assertIn('「南側物料櫃」的「核對卡已蓋章」｜' + report, daily.describe(proof))
        self.engine.answer.assert_not_called()

    def test_near_objects_and_fields_and_earlier_reports_cannot_fill_a_current_property(self):
        request = (HEAD + '上次確認南側物料櫃的通氣閥已關閉。'
                   '本輪確認南側物料櫃的通氣閥已入冊、核對卡已有章。'
                   '本輪確認南側維修櫃的通氣閥已關閉。' + CODES)
        reply = self.app.ask(self.store.new_session(), request)
        proof = daily.read(self.store, reply['id'])
        self.assertEqual(reply['content'], 'X')
        state = proof['plan']['conditions'][0]
        self.assertEqual(state['earlier_observation']['source'], '上次確認南側物料櫃的通氣閥已關閉')
        self.assertIsNone(state['observation'])
        other = proof['plan']['other_observations']
        self.assertEqual([(row['subject'], row['predicate']) for row in other],
                         [('南側物料櫃', '通氣閥已入冊'), ('南側維修櫃', '通氣閥已關閉')])
        self.assertNotIn('南側維修櫃', proof['canonical'])
        self.engine.answer.assert_not_called()

    def test_declared_open_polarity_reverses_same_observation_without_aliasing_conditions(self):
        open_rule = HEAD.replace('「通氣閥已關閉」', '「通氣閥已開啟」')
        reply = self.app.ask(self.store.new_session(),
                             open_rule + '本輪確認南側物料櫃的通氣閥還開著，核對卡已有章。' + CODES)
        self.assertEqual(reply['content'], 'R')
        pair = HEAD.replace('「核對卡已蓋章」', '「通氣閥已開啟」')
        self.assertIsNone(daily.parse(pair + '本輪確認南側物料櫃的通氣閥還開著。' + CODES))
        self.engine.answer.assert_not_called()

    def test_unsupported_uncertain_mixed_and_conflicting_input_does_not_select_the_local_route(self):
        for report in (
            '本輪確認南側物料櫃的通氣閥看起來關著，核對卡已有章。',
            '本輪確認南側物料櫃的通氣閥關著，南側維修櫃的核對卡已有章。',
            '本輪確認南側物料櫃的通氣閥已關閉，通氣閥還開著。',
            '本輪確認南側物料櫃的通氣閥可能已關閉。',
            '確認南側物料櫃的通氣閥關著，核對卡已有章。',
            '本輪確認南側物料櫃的通氣閥關著，核對卡已有章。再打開南櫃。',
        ):
            with self.subTest(report=report):
                question = HEAD + report + CODES
                self.assertIsNone(daily.parse(question))
                self.assertEqual(self.app.ask(self.store.new_session(), question)['content'], '模型替身')
        ambiguous = HEAD.replace('「核對卡已蓋章」', '「通氣閥已入冊」')
        self.assertIsNone(daily.parse(ambiguous + '本輪確認南側物料櫃的通氣閥尚未核對。' + CODES))

    def test_attached_and_raw_assays_keep_their_normal_engine_path(self):
        question = HEAD + '本輪確認南側物料櫃的通氣閥關著，核對卡已有章。' + CODES
        sid = self.store.new_session()
        path = self.path / 'notes.txt'
        path.write_text('本機附件', encoding='utf-8')
        self.store.attach_document(sid, path)
        self.assertEqual(self.app.ask(sid, question)['content'], '模型替身')
        raw = DailyAssistant(self.store, self.engine, default_chat=True, allow_local_evidence=False)
        self.assertEqual(raw.ask(self.store.new_session(), question)['content'], '模型替身')

    def test_retry_and_new_round_reconstruct_word_status_and_sources_after_restore(self):
        question = HEAD + '本輪確認南側物料櫃的核對卡已有章。' + CODES
        sid = self.store.new_session()
        with patch.object(daily, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, question)
        first = self.app.retry(sid)
        self.assertEqual(first['content'], 'X')
        correction = '沿用上一輪條件：本輪確認南側物料櫃的通氣閥確定關閉。重算。'
        with self.assertRaisesRegex(EngineError, '完整條件名稱'):
            self.app.ask(sid, correction.replace('南側物料櫃', '南側維修櫃'))
        fixed = self.app.ask(sid, correction)
        self.assertEqual(fixed['content'], 'R')
        restarted = self.app.ask(sid, '沿用上一輪規則，開始新一輪核對。'
                                      '本輪確認南側物料櫃的通氣閥還開著、核對卡已有章。')
        self.assertEqual(restarted['content'], 'B')
        bundle = create_backup(self.store.path, self.path / 'observations.paibackup')
        restore_backup(bundle['path'], self.path / 'restored')
        restored = Store(self.path / 'restored/assistant.sqlite3')
        self.assertEqual(daily.read(restored, restarted['id']), restarted[daily.FIELD])
        context = restored.answer_context(first['id'])
        context[daily.FIELD]['plan']['conditions'][0]['expected_value'] = 1
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False), first['id']))
        with self.assertRaisesRegex(ValueError, '必要條件規約'):
            daily.read(restored, restarted['id'])
        self.engine.answer.assert_not_called()
