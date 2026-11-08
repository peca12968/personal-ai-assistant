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
from scripts import daily_counts
from scripts.daily_expenses import DailyAssistant


PENS = '三箱原先各裝 8 支筆。開箱後發現其中 5 支損壞，其餘可用。只輸出剩下的可用支數，用阿拉伯數字。'
TOTAL_RING_CASE = next(
    row for row in json.loads((Path(__file__).resolve().parents[1] / 'evals/prerequisite_contrast_assistant_transfer.json')
                              .read_text(encoding='utf-8'))['cases'] if row['id'] == 'explicit_loss_control'
)


class DailyCountTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.engine = Mock(spec=['answer', 'answer_images'])
        self.engine.answer.return_value = '一般模型回答'
        self.engine.answer_images.return_value = '附圖模型回答'
        self.app = DailyAssistant(self.store, self.engine, default_chat=True)

    def test_default_entry_counts_explicit_same_batch_and_delivers_only_the_number(self):
        examples = (
            (PENS, '19'),
            ('四盒原先各裝 6 塊樣片。拆封後發現其中 3 塊不能用，其餘可用。只輸出可用塊數，用阿拉伯數字。', '21'),
            ('兩袋原先各有 9 顆扣環。已從這批取出 4 顆。只回剩下的數量，用阿拉伯數字。', '14'),
            ('三包原先各裝 4 枚扣環。檢查後發現其中 12 枚都不能用。只輸出可用枚數，用阿拉伯數字。', '0'),
            ('十二盒每盒放了二十一件配件。清點後確認當中兩件破損，其餘可用。請問可用件數還有多少？只回阿拉伯數字。', '250'),
            ('999999 箱各有 999999 支筆。從這批取出零支。只回剩下的支數，用阿拉伯數字。', '999998000001'),
            ('原有八籃每籃放 3 個鈕扣。已從這批拿走十二個。只回剩餘個數，用阿拉伯數字。', '12'),
        )
        sid = self.store.new_session()
        self.store.add_message(sid, 'user', '較早的另一批有 10 支不堪使用。')
        for prompt, expected in examples:
            with self.subTest(prompt=prompt):
                result = self.app.ask(sid, prompt)
                self.assertEqual(result['content'], expected)
                receipt = daily_counts.read(self.store, result['id'])
                self.assertEqual(receipt, result[daily_counts.FIELD])
                self.assertIs(receipt['model_called'], False)
                self.assertEqual(receipt['plan']['schema'], daily_counts.SCHEMA)
                self.assertEqual(receipt['plan']['request'], prompt)
                self.assertEqual(self.store.answer_context(result['id'])['recent_conversation'], [])
        self.engine.answer.assert_not_called()

    def test_does_not_supply_counts_for_other_batches_units_or_unclear_requests(self):
        controls = (
            PENS.replace('其中 5 支', '另一批有 5 支'),
            PENS.replace('5 支損壞', '5 箱損壞'),
            PENS.replace('發現其中', '懷疑其中'),
            PENS.replace('5 支損壞', '25 支損壞'),
            PENS.replace('，其餘可用', ''),
            PENS.replace('各裝', '每盒裝'),
            PENS + '再寄給同事。',
            PENS.replace('可用支數', '可用件數'),
            PENS.replace('可用支數', '支數'),
            PENS.replace('支筆。', '支筆損壞。'),
            PENS.replace('支筆。', '支筆和鉛筆。'),
            PENS.replace('三箱', '零箱'),
            PENS.replace('三箱', '1000000箱'),
            PENS.replace('8 支', '約 8 支'),
            '兩袋各有 9 顆扣環。已從這批取出 4 顆。只回數量，用阿拉伯數字。',
            '只根據這一則訊息回答。' + PENS,
        )
        for prompt in controls:
            with self.subTest(prompt=prompt):
                result = self.app.ask(self.store.new_session(), prompt)
                self.assertEqual(result['content'], '一般模型回答')
                self.assertIsNone(daily_counts.read(self.store, result['id']))
        self.assertEqual(self.engine.answer.call_count, len(controls))

    def test_complete_inspection_counts_the_named_item_and_returns_only_digits(self):
        cases = (
            (TOTAL_RING_CASE['question'], TOTAL_RING_CASE['answer']),
            ('已有五盒每盒 4 個瓷杯。盤點完這批共有 3 個破損。請只輸出可用瓷杯的阿拉伯數字總數。', '17'),
            ('原有兩袋各5顆珠子。檢查後這批共有十顆不能用。只回可用珠子的阿拉伯數字總數。', '0'),
        )
        for prompt, expected in cases:
            with self.subTest(prompt=prompt):
                result = self.app.ask(self.store.new_session(), prompt)
                receipt = daily_counts.read(self.store, result['id'])
                self.assertEqual(result['content'], expected)
                self.assertEqual(receipt['plan']['schema'], daily_counts.TOTAL_SCHEMA)
                self.assertIs(receipt['model_called'], False)
        self.engine.answer.assert_not_called()

    def test_named_total_declines_partial_inspection_and_other_scopes(self):
        prompt = TOTAL_RING_CASE['question']
        for control in (
            prompt.replace('後共有', '後另一批共有'), prompt.replace('檢查後共有', '抽查時發現'),
            prompt.replace('共有 4', '至少有 4'), prompt.replace('可用扣環', '可用螺絲'),
            prompt.replace('4 枚不能用', '4 包不能用'), prompt.replace('共有 4', '共有 24'),
            prompt.replace('包各', '包每盒'), prompt + '再寄出清單。',
        ):
            with self.subTest(control=control):
                self.assertIsNone(daily_counts.parse(control))
                result = self.app.ask(self.store.new_session(), control)
                self.assertEqual(result['content'], '一般模型回答')
                self.assertIsNone(daily_counts.read(self.store, result['id']))

    def test_raw_model_and_attachment_routes_bypass_local_counts(self):
        sid = self.store.new_session()
        raw = DailyAssistant(self.store, self.engine, default_chat=True, allow_local_calculation=False)
        baseline = raw.ask(sid, PENS)
        self.assertEqual(baseline['content'], '一般模型回答')
        self.assertEqual(self.store.answer_context(baseline['id'])['current_request'], PENS)
        picture = self.path / 'inventory.png'
        Image.new('RGB', (8, 8)).save(picture)
        selected = image_inputs.attach_paths(self.store, sid, [picture])
        attached = self.app.ask(sid, PENS, image_ids=selected)
        self.assertEqual(attached['content'], '附圖模型回答')
        self.assertIsNone(daily_counts.read(self.store, attached['id']))
        self.assertEqual(self.engine.answer_images.call_args.args[1], (picture.read_bytes(),))
        document = self.path / 'inventory.txt'
        document.write_text('補充庫存', encoding='utf-8')
        self.store.attach_document(sid, document)
        answer = self.app.ask(sid, PENS)
        self.assertEqual(answer['content'], '一般模型回答')
        self.assertIsNone(daily_counts.read(self.store, answer['id']))
        self.assertEqual(self.engine.answer.call_count, 2)

        older_session = self.store.new_session()
        self.engine.answer.side_effect = [EngineError('已停止', 'cancelled'), '一般模型回答']
        with self.assertRaises(EngineError):
            raw.ask(older_session, PENS)
        older_retry = self.app.retry(older_session)
        self.assertEqual(older_retry['content'], '一般模型回答')
        self.assertIsNone(daily_counts.read(self.store, older_retry['id']))

    def test_cancelled_run_retries_the_saved_plan_and_original_user_message(self):
        sid = self.store.new_session()
        with patch.object(daily_counts, 'evaluate', side_effect=EngineError('已停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, PENS)
        result = self.app.retry(sid)
        self.assertEqual(result['content'], '19')
        self.assertEqual(len(self.store.messages(sid, 5)), 2)
        self.assertEqual(daily_counts.read(self.store, result['id']), result[daily_counts.FIELD])
        self.engine.answer.assert_not_called()

    def test_retry_rejects_changed_documents_or_stored_plan(self):
        for change in ('attachment', 'plan', 'question'):
            sid = self.store.new_session()
            with patch.object(daily_counts, 'evaluate', side_effect=EngineError('已停止', 'cancelled')):
                with self.assertRaises(EngineError):
                    self.app.ask(sid, PENS)
            if change == 'attachment':
                document = self.path / 'inventory.txt'
                document.write_text('補充庫存', encoding='utf-8')
                self.store.attach_document(sid, document)
            elif change == 'plan':
                with self.store.connect() as db:
                    raw, run_id = db.execute('SELECT context_json,id FROM answer_runs WHERE session_id=?',
                                             (sid,)).fetchone()
                    context = json.loads(raw)
                    context[daily_counts.FIELD]['deducted'] = '2'
                    db.execute('UPDATE answer_runs SET context_json=? WHERE id=?', (json.dumps(context), run_id))
            else:
                with self.store.connect() as db:
                    db.execute("UPDATE messages SET content=? WHERE session_id=? AND role='user'",
                               ('只根據這一則訊息回答。' + PENS, sid))
            with self.assertRaisesRegex(ValueError, '資料範圍已變更'):
                self.app.retry(sid)
        self.engine.answer.assert_not_called()

    def test_restored_receipt_recomputes_and_rejects_modified_answer_or_source(self):
        answer = self.app.ask(self.store.new_session(), PENS)
        bundle = create_backup(self.store.path, self.path / 'sample.paibackup')
        for change in ('receipt', 'answer', 'question'):
            with self.subTest(change=change):
                restore_backup(bundle['path'], self.path / change)
                restored = Store(self.path / change / 'assistant.sqlite3')
                receipt = daily_counts.read(restored, answer['id'])
                self.assertEqual(receipt, answer[daily_counts.FIELD])
                self.assertIn('3 × 8 − 5 = 19 支', daily_counts.describe(receipt))
                with restored.connect() as db:
                    if change == 'receipt':
                        context = restored.answer_context(answer['id'])
                        context[daily_counts.FIELD]['model_called'] = 0
                        db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                                   (json.dumps(context), answer['id']))
                    else:
                        mid = answer['id'] if change == 'answer' else answer['user_message_id']
                        db.execute('UPDATE messages SET content=? WHERE id=?', ('36', mid))
                with self.assertRaisesRegex(ValueError, '本機'):
                    daily_counts.read(restored, answer['id'])
