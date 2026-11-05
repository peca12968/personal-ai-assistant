import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from assistant import answer_runs, chat_evidence, feedback_improvement
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant, EngineError
from assistant.store import Store
from scripts.daily_expenses import DailyAssistant
from scripts.evaluate_understanding import collect_case


REQUEST = '條件核對：只有驗證完成，設備才會運轉。已確認驗證完成。能否確定設備運轉？'


class LocalEvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.engine = Mock()
        self.engine.answer.side_effect = AssertionError('local evidence contacted model')
        self.engine.describe.side_effect = AssertionError('local evidence selected model')

    def test_normal_and_daily_routes_use_current_question_without_retrieval(self):
        for cls in (Assistant, DailyAssistant):
            with self.subTest(entry=cls.__name__), patch.object(self.store, 'retrieve', side_effect=AssertionError('memory read')), \
                    patch.object(self.store, 'documents', side_effect=AssertionError('document read')), \
                    patch.object(self.store, 'messages', side_effect=AssertionError('history read')):
                sid = self.store.new_session()
                response = cls(self.store, self.engine).ask(sid, REQUEST)
                receipt = chat_evidence.read(self.store, response['id'])
                self.assertEqual(response['response_mode'], 'local_evidence')
                self.assertEqual(receipt['analysis']['status'], 'undetermined')
                self.assertEqual(receipt['analysis']['compatible_worlds'], 2)
                self.assertIn('資料不足', response['content'])
                with self.store.connect() as db:
                    row = db.execute('SELECT context_json FROM answer_runs WHERE answer_message_id=?', (response['id'],)).fetchone()
                self.assertEqual(json.loads(row[0])['evidence'], receipt['plan'])
        self.engine.answer.assert_not_called()
        self.engine.describe.assert_not_called()

    def test_backup_roundtrip_and_tamper_detection(self):
        response = Assistant(self.store, self.engine).ask(self.store.new_session(), REQUEST)
        receipt = chat_evidence.read(self.store, response['id'])
        archive = create_backup(self.store.path, self.root / 'saved.paibackup')['path']
        folder = self.root / 'restored'
        restore_backup(archive, folder)
        other = Store(folder / 'assistant.sqlite3')
        self.assertEqual(chat_evidence.read(other, response['id']), receipt)
        context = other.answer_context(response['id'])
        context['evidence']['analysis']['compatible_worlds'] = True
        with other.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                       (json.dumps(context, ensure_ascii=False), response['id']))
        with self.assertRaisesRegex(ValueError, '條件核對紀錄與原文或推論不符'):
            validate_database(other.path)

    def test_cancelled_or_mixed_request_cannot_save_a_completed_answer(self):
        app = Assistant(self.store, self.engine)
        sid = self.store.new_session()
        with self.assertRaises(EngineError) as failure:
            app.ask(sid, REQUEST + '請刪除所有對話。')
        self.assertEqual(failure.exception.code, 'evidence_invalid')
        self.assertEqual(answer_runs.runs(self.store, sid)[0]['status'], 'failed')
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(EngineError) as failure:
            app.ask(sid, REQUEST, cancel=cancelled)
        self.assertEqual(failure.exception.code, 'cancelled')
        self.assertFalse(any(row['role'] == 'assistant' for row in self.store.messages(sid)))

    def test_model_only_collection_and_feedback_scope(self):
        model = Mock(spec=['answer'])
        model.answer.return_value = '原始模型錯答'
        case = {'id': 'evidence-control', 'category': 'test', 'prompt': REQUEST, 'criteria': ['固定條件']}
        row = collect_case(self.store, self.store.new_session(), case, model, lambda: None)
        self.assertEqual(row['engine_answer'], '原始模型錯答')
        model.answer.assert_called_once()
        response = Assistant(self.store, self.engine).ask(self.store.new_session(), REQUEST)
        source = {'rating': -1, 'correction': '請更正', 'context': self.store.answer_context(response['id'])}
        with self.assertRaisesRegex(ValueError, '模型回答'):
            feedback_improvement.example_from_source(source)


if __name__ == '__main__':
    unittest.main()
