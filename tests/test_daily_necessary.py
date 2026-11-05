import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_necessary
from scripts.daily_expenses import DailyAssistant


HEAD = '使用轉運台要有已校驗的水位計和有效的操作證。'
LABELS = '確定能用回「可用」，確定不能用回「停用」，尚不能判定回「待查」。請只輸出結論。'
FALSE_AND_UNKNOWN = HEAD + '水位計尚未校驗，唯一的操作證已確定作廢。' + LABELS


class DailyNecessaryTests(unittest.TestCase):
    def test_required_vs_sufficient_and_unknown_are_distinct(self):
        for rule, facts, expected in (
            (HEAD, '水位計尚未校驗，唯一的操作證已確定作廢。', '停用'),
            (HEAD, '水位計尚未校驗，操作證已確認有效。', '待查'),
            (HEAD, '水位計已校驗，操作證已確認有效。', '待查'),
            (HEAD.replace('要有', '的必要且充分條件是同時具備'),
             '水位計已校驗，操作證已確認有效。', '可用'),
            (HEAD, '水位計已校驗，已確定沒有有效的操作證。', '停用'),
        ):
            with self.subTest(rule=rule, facts=facts):
                plan = daily_necessary.parse(rule + facts + LABELS)
                output, receipt = daily_necessary.evaluate(plan)
                self.assertEqual(output, expected)
                self.assertFalse(receipt['model_called'])
                self.assertEqual(receipt['plan']['request'], rule + facts + LABELS)

    def test_complete_condition_names_keep_their_final_character(self):
        question = ('啟用試驗台的必要且充分條件是同時具備已核准的檢測單與可用的識別碼。'
                    '檢測單已確認核准，識別碼已確認可用。' + LABELS)
        plan = daily_necessary.parse(question)
        self.assertEqual([row['name'] for row in plan['conditions']], ['檢測單', '識別碼'])
        self.assertEqual(daily_necessary.evaluate(plan)[0], '可用')

    def test_ambiguous_or_mixed_requests_keep_their_model_route(self):
        q = FALSE_AND_UNKNOWN
        for prompt in (
            q.replace('唯一的操作證', '操作證'), q.replace('操作證已確定', '舊操作證已確定'),
            q.replace('作廢。', '作廢？'), q.replace('已確定作廢', '可能作廢'),
            q.replace('作廢。', '作廢，操作證已確認有效。'), q.replace('要有', '只要有'),
            q.replace('要有', '僅要有'), q.replace('要有', '通常要有'),
            q.replace('水位計和有效', '水位計或有效'), q.replace('使用轉運台', '使用它'),
            q.replace('「待查」', '「停用」'), q + '也寄出通知。', '請引用「' + q + '」',
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(daily_necessary.parse(prompt))

    def test_daily_receipt_recomputes_after_restore_and_raw_bypasses_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / 'assistant.sqlite3')
            engine = Mock(spec=['answer'])
            engine.answer.return_value = '模型原答'
            answer = DailyAssistant(store, engine).ask(store.new_session(), FALSE_AND_UNKNOWN)
            engine.answer.assert_not_called()
            self.assertEqual(answer['content'], '停用')
            receipt = daily_necessary.read(store, answer['id'])
            self.assertEqual(receipt['analysis']['status'], 'refuted')
            saved = create_backup(store.path, root / 'sample.paibackup')
            restore_backup(saved['path'], root / 'restore')
            other = Store(root / 'restore/assistant.sqlite3')
            self.assertEqual(daily_necessary.read(other, answer['id']), receipt)
            with other.connect() as db:
                context = other.answer_context(answer['id'])
                context[daily_necessary.FIELD]['analysis']['status'] = 'undetermined'
                db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                           (json.dumps(context, ensure_ascii=False), answer['id']))
            with self.assertRaisesRegex(ValueError, '必要條件紀錄'):
                daily_necessary.read(other, answer['id'])
            raw = DailyAssistant(store, engine, allow_local_evidence=False).ask(store.new_session(), FALSE_AND_UNKNOWN)
            self.assertEqual(raw['content'], '模型原答')
            self.assertIsNone(daily_necessary.read(store, raw['id']))

    def test_retry_uses_original_plan_and_attached_sources_block_new_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / 'assistant.sqlite3')
            sid = store.new_session()
            engine = Mock(spec=['answer'])
            engine.answer.return_value = '模型原答'
            app = DailyAssistant(store, engine)
            with patch.object(daily_necessary, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
                with self.assertRaises(EngineError):
                    app.ask(sid, FALSE_AND_UNKNOWN)
            result = app.retry(sid)
            self.assertEqual(result['content'], '停用')
            self.assertEqual(len(store.messages(sid, 5)), 2)
            engine.answer.assert_not_called()
            document = root / 'record.txt'
            document.write_text('另有一張未核實的證件。', encoding='utf-8')
            store.attach_document(sid, document)
            self.assertEqual(app.ask(sid, FALSE_AND_UNKNOWN)['content'], '模型原答')
            engine.answer.assert_called_once()
