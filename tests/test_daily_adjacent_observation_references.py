import json
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
HEAD = ('判斷「交回墨盒」須同時符合「北側檢測盒」的「封口完整」及「北側檢測盒」的「識別卡已蓋章」，'
        '這些就是全部要求。')
ANCHOR = '本輪確認北側檢測盒的封口完整'
REFERENCE = '它的識別卡已有章'
ASK = HEAD + ANCHOR + '。' + REFERENCE + '。' + CODES


class AdjacentObservationReferenceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '模型替身'
        self.app = DailyAssistant(self.store, self.engine, default_chat=True)

    def test_bare_and_scoped_references_bind_the_same_object_and_keep_both_sources(self):
        for reference in (REFERENCE, '本輪確認' + REFERENCE, '當次確認該物件的識別卡已有章'):
            with self.subTest(reference=reference):
                question = HEAD + ANCHOR + '。' + reference + '。' + CODES
                reply = self.app.ask(self.store.new_session(), question)
                proof = daily.read(self.store, reply['id'])
                self.assertEqual((reply['content'], proof['plan']['schema']), ('R', daily.V11))
                card = proof['plan']['conditions'][1]
                self.assertEqual((card['subject'], card['observation'], card['observation_anchor']),
                                 ('北側檢測盒', reference, ANCHOR))
                self.assertIn('指代原句：' + ANCHOR, daily.describe(proof))
        prior = daily.parse_version(HEAD + ANCHOR + '。本輪確認' + REFERENCE + '。' + CODES, daily.V10)
        self.assertEqual(daily.evaluate(prior)[0], 'X')
        self.engine.answer.assert_not_called()

    def test_one_anchor_supports_consecutive_references_with_separate_polarity(self):
        rule = ('判斷「使用北盒」須同時符合「北側檢測盒」的「封口完整」、「北側檢測盒」的「警示燈已熄滅」'
                '及「北側檢測盒」的「識別卡已蓋章」，這些就是全部要求。')
        question = rule + ANCHOR + '。該物件的警示燈還亮著。同一物件的識別卡已有章。' + CODES
        answer = self.app.ask(self.store.new_session(), question)
        proof = daily.read(self.store, answer['id'])
        conditions = proof['plan']['conditions']
        self.assertEqual(answer['content'], 'B')
        self.assertEqual([row['state'] for row in conditions], ['true', 'false', 'true'])
        self.assertEqual([row['observation_anchor'] for row in conditions[1:]], [ANCHOR, ANCHOR])
        self.engine.answer.assert_not_called()

    def test_earlier_other_object_and_unrelated_referential_field_keep_their_own_scope(self):
        question = (HEAD + '上次確認南側檢測盒的識別卡已有章。' + ANCHOR
                    + '。它的編號已入冊。' + CODES)
        answer = self.app.ask(self.store.new_session(), question)
        proof = daily.read(self.store, answer['id'])
        self.assertEqual(answer['content'], 'X')
        self.assertIsNone(proof['plan']['conditions'][1]['observation'])
        old, different_field = proof['plan']['other_observations']
        self.assertTrue(old['earlier'])
        self.assertEqual(old['subject'], '南側檢測盒')
        self.assertEqual(different_field['subject'], '北側檢測盒')
        self.assertEqual(different_field['observation_anchor'], ANCHOR)
        self.assertNotIn('編號已入冊', proof['canonical'])
        self.engine.answer.assert_not_called()

    def test_ambiguous_or_noncurrent_reference_does_not_fall_through_to_the_legacy_parser(self):
        for reports in (
            '本輪確認它的識別卡已有章。',
            ANCHOR + '。本輪確認南側檢測盒的封口完整。它的識別卡已有章。',
            ANCHOR + '。本輪確認南側檢測盒的封口完整。本輪確認它的識別卡已有章。',
            ANCHOR.replace('本輪', '上次') + '。它的識別卡已有章。',
            ANCHOR + '。上次確認南側檢測盒的封口完整。它的識別卡已有章。',
            ANCHOR + '。上次確認它的識別卡已有章。',
            ANCHOR + '。它的識別卡可能已有章。',
            ANCHOR + '。它的封口不完整。',
            ANCHOR + '。它的識別卡已有章。並交回墨盒。',
        ):
            with self.subTest(reports=reports):
                question = HEAD + reports + CODES
                self.assertIsNone(daily.parse(question))
                self.assertEqual(self.app.ask(self.store.new_session(), question)['content'], '模型替身')

    def test_retry_correction_and_new_round_rebuild_original_reference_after_restore(self):
        sid = self.store.new_session()
        with patch.object(daily, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, ASK)
        first = self.app.retry(sid)
        self.assertEqual(first['content'], 'R')
        with self.assertRaisesRegex(EngineError, '完整條件名稱'):
            self.app.ask(sid, '沿用上一輪條件：本輪確認它的識別卡沒有章。重算。')
        correction = self.app.ask(sid, '沿用上一輪條件：本輪確認北側檢測盒的識別卡沒有章。重算。')
        self.assertEqual(correction['content'], 'B')
        self.assertNotIn('observation_anchor', daily.effective(correction[daily.FIELD]['plan'])['conditions'][1])
        started = self.app.ask(sid, '沿用上一輪規則，開始新一輪核對。' + ANCHOR + '。' + REFERENCE + '。')
        self.assertEqual(started['content'], 'R')
        bundle = create_backup(self.store.path, self.path / 'reference.paibackup')
        restore_backup(bundle['path'], self.path / 'restored')
        restored = Store(self.path / 'restored/assistant.sqlite3')
        self.assertEqual(daily.read(restored, started['id']), started[daily.FIELD])
        context = restored.answer_context(first['id'])
        context[daily.FIELD]['plan']['conditions'][1]['observation_anchor'] = '本輪確認南側檢測盒的封口完整'
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False), first['id']))
        with self.assertRaisesRegex(ValueError, '必要條件規約'):
            daily.read(restored, started['id'])
        self.engine.answer.assert_not_called()

    def test_attachment_and_raw_model_runs_do_not_use_reference_resolution(self):
        sid = self.store.new_session()
        path = self.path / 'notes.txt'
        path.write_text('測試附件', encoding='utf-8')
        self.store.attach_document(sid, path)
        self.assertEqual(self.app.ask(sid, ASK)['content'], '模型替身')
        raw = DailyAssistant(self.store, self.engine, default_chat=True, allow_local_evidence=False)
        self.assertEqual(raw.ask(self.store.new_session(), ASK)['content'], '模型替身')
