import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from assistant import image_inputs
from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_container_id
from scripts.daily_expenses import DailyAssistant


FIXTURE = Path(__file__).resolve().parents[1] / 'evals/prerequisite_contrast_assistant_transfer.json'
CASE = next(row for row in json.loads(FIXTURE.read_text(encoding='utf-8'))['cases']
            if row['id'] == 'use_current_map_bin')
REQUEST = CASE['question']


class DailyContainerIdTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.engine = Mock(spec=['answer', 'answer_images'])
        self.engine.answer.return_value = '一般模型回答'
        self.engine.answer_images.return_value = '附圖模型回答'
        self.app = DailyAssistant(self.store, self.engine, default_chat=True)

    def history(self):
        sid = self.store.new_session()
        for message in CASE['history']:
            mid = self.store.add_message(sid, message['role'], message['content'])
        return sid, mid

    def test_frozen_case_and_recent_explicit_updates_use_user_evidence(self):
        sid, source = self.history()
        answer = self.app.ask(sid, REQUEST)
        receipt = daily_container_id.read(self.store, answer['id'])
        self.assertEqual(answer['content'], CASE['answer'])
        self.assertIs(receipt['model_called'], False)
        self.assertEqual(receipt['plan']['trail'][-1]['message_id'], str(source))
        self.assertIn('圖紙換到編號 C52 的箱子', daily_container_id.describe(receipt))
        self.store.add_message(sid, 'user', '最新更正，圖紙移到編號 Ｂ_007 的箱子。')
        self.store.add_message(sid, 'user', '器材換到編號 H99 的箱子。')
        self.store.add_message(sid, 'assistant', '我以為圖紙箱是 C41。')
        newer = self.app.ask(sid, REQUEST)
        self.assertEqual(newer['content'], 'Ｂ_007')
        repeated = self.app.ask(sid, REQUEST)
        self.assertEqual(repeated['content'], 'Ｂ_007')
        self.assertEqual(len(daily_container_id.read(self.store, repeated['id'])['plan']['trail']), 3)
        self.assertEqual(daily_container_id.read(self.store, answer['id']), receipt)
        self.engine.answer.assert_not_called()

    def test_ambiguous_or_unverified_newer_statements_block_stale_lookup(self):
        for evidence in (
            '圖紙離開原箱，還沒確認新箱。', '圖紙已移到編號 T5 的櫃子。',
            '圖紙草稿換到編號 T5 的箱子。', '圖紙換到編號 A21 的箱子，圖紙換到編號 B91 的箱子。',
            '圖紙箱照舊。', '範例：「圖紙換到編號 B91 的箱子。」', 'a' * 4097,
        ):
            with self.subTest(evidence=evidence[:30]):
                sid, _ = self.history()
                self.store.add_message(sid, 'user', evidence)
                result = self.app.ask(sid, REQUEST)
                self.assertEqual(result['content'], '一般模型回答')
                self.assertIsNone(daily_container_id.read(self.store, result['id']))
        sid = self.store.new_session()
        self.store.add_message(sid, 'assistant', '圖紙換到編號 X88 的箱子。')
        self.assertEqual(self.app.ask(sid, REQUEST)['content'], '一般模型回答')
        sid, _ = self.history()
        for _ in range(daily_container_id.MAX_USER_TURNS):
            self.store.add_message(sid, 'user', '器材箱照舊。')
        self.assertEqual(self.app.ask(sid, REQUEST)['content'], '一般模型回答')

    def test_attachments_mixed_requests_and_raw_model_assessments_bypass_lookup(self):
        for setting in ('allow_local_evidence', 'allow_local_calculation'):
            raw = DailyAssistant(self.store, self.engine, default_chat=True, **{setting: False})
            sid, _ = self.history()
            self.assertEqual(raw.ask(sid, REQUEST)['content'], '一般模型回答')
        sid, _ = self.history()
        self.assertEqual(self.app.ask(sid, REQUEST + '順便寄出通知。')['content'], '一般模型回答')
        sid, _ = self.history()
        picture = self.root / 'container.png'
        Image.new('RGB', (8, 8)).save(picture)
        images = image_inputs.attach_paths(self.store, sid, [picture])
        self.assertEqual(self.app.ask(sid, REQUEST, image_ids=images)['content'], '附圖模型回答')
        sid, _ = self.history()
        note = self.root / 'container.txt'
        note.write_text('另有圖紙箱資料。', encoding='utf-8')
        self.store.attach_document(sid, note)
        self.assertEqual(self.app.ask(sid, REQUEST)['content'], '一般模型回答')

    def test_retry_backup_and_recheck_bind_every_considered_source(self):
        sid, _ = self.history()
        intervening = self.store.add_message(sid, 'user', '器材換到編號 H99 的箱子。')
        with patch.object(daily_container_id, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, REQUEST)
        result = self.app.retry(sid)
        self.assertEqual(result['content'], CASE['answer'])
        self.assertEqual(sum(row['content'] == REQUEST for row in self.store.messages(sid, 12)), 1)
        bundle = create_backup(self.store.path, self.root / 'sample.paibackup')
        restore_backup(bundle['path'], self.root / 'restored')
        restored = Store(self.root / 'restored/assistant.sqlite3')
        self.assertEqual(daily_container_id.read(restored, result['id']), result[daily_container_id.FIELD])
        with restored.connect() as db:
            original, = db.execute('SELECT context_json FROM answer_runs WHERE id=?', (result['run_id'],)).fetchone()
            amended = json.loads(original)
            amended['engine_selection'] = {'unexpected': True}
            db.execute('UPDATE answer_runs SET context_json=? WHERE id=?', (json.dumps(amended), result['run_id']))
        with self.assertRaisesRegex(ValueError, '原始編號查找的資料範圍'):
            daily_container_id.read(restored, result['id'])
        with restored.connect() as db:
            db.execute('UPDATE answer_runs SET context_json=? WHERE id=?', (original, result['run_id']))
            db.execute('UPDATE messages SET content=? WHERE id=?', ('器材換到編號 H98 的箱子。', intervening))
        with self.assertRaisesRegex(ValueError, '原始來源'):
            daily_container_id.read(restored, result['id'])
        self.engine.answer.assert_not_called()

    def test_changed_source_cannot_be_used_during_submission_or_retry(self):
        sid, source = self.history()
        capture = daily_container_id.capture

        def change_before_commit(store, session, request, before):
            if before is not None:
                with store.connect() as db:
                    db.execute('UPDATE messages SET content=? WHERE id=?', ('圖紙換到編號 X10 的箱子。', source))
            return capture(store, session, request, before)

        with patch.object(daily_container_id, 'capture', side_effect=change_before_commit):
            with self.assertRaisesRegex(EngineError, '提交前'):
                self.app.ask(sid, REQUEST)
        with self.assertRaisesRegex(EngineError, '重試的編號來源已變更'):
            self.app.retry(sid)
        self.engine.answer.assert_not_called()
