import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_explicit_json
from scripts.daily_expenses import DailyAssistant


BOOL = '只輸出一個JSON物件，鍵為ok，值為布林值true，不要空白或程式碼框。'
MULTIPLE = ('只輸出一個緊湊 JSON 物件：鍵為 count，值為整數 -3；鍵為 note，值為字串 "待查，稍後"；'
            '鍵為 ready，值為布林值false；鍵為 reserved，值為空值null。')


class ExplicitJsonTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.engine = Mock(spec=['answer'])
        self.engine.answer.return_value = '一般模型回答'
        self.app = DailyAssistant(self.store, self.engine, default_chat=True)

    def test_default_entry_serializes_typed_values_in_given_order(self):
        for request, expected in (
            (BOOL, '{"ok":true}'),
            (MULTIPLE, '{"count":-3,"note":"待查，稍後","ready":false,"reserved":null}'),
            (r'請只回一個緊湊JSON物件，鍵為"標籤"，值為字串"雙引號\"與換行\n"。',
             r'{"標籤":"雙引號\"與換行\n"}'),
        ):
            with self.subTest(request=request):
                reply = self.app.ask(self.store.new_session(), request)
                self.assertEqual(reply['content'], expected)
                receipt = daily_explicit_json.read(self.store, reply['id'])
                self.assertEqual(receipt['plan']['request'], request)
                self.assertEqual(receipt['result'], expected)
                self.assertIs(receipt['model_called'], False)
        self.engine.answer.assert_not_called()

    def test_missing_values_duplicate_keys_and_invalid_types_keep_normal_route(self):
        for request in (
            BOOL.replace('值為布林值true', '值表示檢查是否通過'),
            BOOL.replace('布林值true', '布林值"true"'),
            BOOL.replace('布林值true', '1.05'),
            BOOL.replace('布林值true', '-0'),
            BOOL.replace('布林值true', '1000000000000'),
            BOOL.replace('，不要空白或程式碼框', ''),
            BOOL + '再寄出通知。',
            '只回緊湊JSON物件，鍵為ok，值為true；鍵為ok，值為false。',
            r'只回緊湊JSON物件，鍵為ok，值為true；鍵為"\u006f\u006b"，值為false。',
            r'只回緊湊JSON物件，鍵為ok，值為"\ud800"。',
        ):
            with self.subTest(request=request):
                self.assertIsNone(daily_explicit_json.parse(request))
                result = self.app.ask(self.store.new_session(), request)
                self.assertEqual(result['content'], '一般模型回答')
                self.assertIsNone(daily_explicit_json.read(self.store, result['id']))
        prefix = '只回緊湊JSON物件：'
        fields = ['鍵為 k' + str(i) + '，值為 true' for i in range(9)]
        plan = daily_explicit_json.parse(prefix + '；'.join(fields[:8]) + '。')
        self.assertEqual(len(plan['fields']), 8)
        self.assertIsNone(daily_explicit_json.parse(prefix + '；'.join(fields) + '。'))

    def test_attachments_and_raw_assays_use_the_existing_engine(self):
        for option in ('allow_local_calculation', 'allow_local_transcription'):
            raw = DailyAssistant(self.store, self.engine, default_chat=True, **{option: False})
            self.assertEqual(raw.ask(self.store.new_session(), BOOL)['content'], '一般模型回答')
        sid = self.store.new_session()
        path = self.path / 'fields.txt'
        path.write_text('另一份欄位資料。', encoding='utf-8')
        self.store.attach_document(sid, path)
        self.assertEqual(self.app.ask(sid, BOOL)['content'], '一般模型回答')

    def test_retry_and_restored_receipt_verify_types_and_saved_answer(self):
        sid = self.store.new_session()
        with patch.object(daily_explicit_json, 'evaluate', side_effect=EngineError('停止', 'cancelled')):
            with self.assertRaises(EngineError):
                self.app.ask(sid, BOOL)
        reply = self.app.retry(sid)
        self.assertEqual(reply['content'], '{"ok":true}')
        self.assertEqual(len(self.store.messages(sid, 10)), 2)
        backup = create_backup(self.store.path, self.path / 'explicit-json.paibackup')
        restore_backup(backup['path'], self.path / 'restored')
        restored = Store(self.path / 'restored/assistant.sqlite3')
        self.assertEqual(daily_explicit_json.read(restored, reply['id']), reply[daily_explicit_json.FIELD])
        with restored.connect() as db:
            db.execute('UPDATE messages SET content=? WHERE id=?', ('{"ok":1}', reply['id']))
        with self.assertRaisesRegex(ValueError, 'JSON 回覆與原始欄位'):
            daily_explicit_json.read(restored, reply['id'])
        self.engine.answer.assert_not_called()
