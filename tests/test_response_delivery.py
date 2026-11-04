import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from assistant import response_delivery as evidence
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant, EngineError, render_prompt
from assistant.store import Store
from scripts.daily_delivery import DailyLocalEngine, extract


class Raw:
    display_name = 'controlled fixture'

    def __init__(self, text):
        self.text, self.prompts = text, []

    def describe(self):
        return {'kind': 'fixture'}

    def answer(self, prompt, cancel=None):
        self.prompts.append(prompt)
        return self.text


class IdentifierDeliveryTests(unittest.TestCase):
    def test_short_question_and_complete_object_field_match(self):
        for name in ('白盒', '大白盒', 'A盒', '袋'):
            for verb in ('', '請問', '請查'):
                q = f'{verb}{name}代碼？只輸出代碼。'
                raw = f'藍盒的代碼是 K73。{name}上的代碼是 P19。'
                self.assertEqual(extract(q, raw), 'P19')

    def test_preserve_wrong_object_or_field(self):
        for q, raw in [
            ('請問大白盒代碼？只輸出代碼。', '白盒的代碼是 P19。'),
            ('請問白盒代碼？只輸出代碼。', '大白盒的代碼是 P19。'),
            ('請問白盒序號？只輸出代碼。', '白盒的代碼是 P19。'),
            ('請問白盒序號？只輸出代碼。', '白盒的序號是 P19。'),
            ('白盒代碼？只輸出代碼。', '白盒的型號是 P19。'),
            ('白盒代碼？只輸出代碼。', '白盒代碼是 P19。藍盒代碼是 K73。'),
        ]:
            with self.subTest(q=q, raw=raw):
                self.assertEqual(extract(q, raw), raw)

    def test_preserve_conflicting_or_multiple_questions(self):
        for q in ('白盒代碼？只輸出代碼。', '請問白盒代碼？只輸出代碼。'):
            raw = '白盒的代碼是 P19。白盒的代碼是 K73。'
            self.assertEqual(extract(q, raw), raw)
        raw = '白盒的代碼是 P19。'
        for q in ('藍盒代碼？白盒代碼？只輸出代碼。',
                  '白盒或藍盒代碼？只輸出代碼。',
                  '引用「白盒代碼？」。只輸出代碼。',
                  '請說明白盒代碼？只輸出代碼。'):
            self.assertEqual(extract(q, raw), raw)

    def test_only_preserve_conflict_for_same_object_and_field(self):
        q = '白盒代碼？只輸出代碼。'
        raw = '藍盒的代碼是 K73。白盒的型號是 M28。白盒的代碼是 P19。'
        self.assertEqual(extract(q, raw), 'P19')
        self.assertEqual(extract(q, '白盒的代碼是 P19。白盒的代碼是 P19。'), 'P19')


class DeliveryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'original' / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.raw = Raw('藍盒的代碼是 K73。白盒上的代碼是 P19。')
        self.engine = DailyLocalEngine(self.raw)
        self.question = '白盒代碼？只輸出代碼。'

    def answer(self):
        return Assistant(self.store, self.engine).ask(self.sid, self.question)

    def test_atomic_saved_raw_and_visible_answer_without_prompt_change(self):
        answer = self.answer()
        self.assertEqual(answer['content'], 'P19')
        receipt = evidence.read(self.store, answer['id'])
        self.assertEqual(receipt['raw_answer'], self.raw.text)
        self.assertEqual(receipt['delivered_sha256'], evidence.digest('P19'))
        self.assertEqual(len(self.raw.prompts), 1)
        self.assertEqual(render_prompt(self.store.answer_context(answer['id'])), self.raw.prompts[0])
        self.assertIn(self.raw.text, evidence.describe(self.store, answer['id']))
        self.assertTrue(evidence.describe(self.store, answer['id']).endswith('P19'))
        Assistant(self.store, self.engine).ask(self.sid, '你剛剛回了什麼？')
        self.assertNotIn(self.raw.text, self.raw.prompts[-1])

    def test_raw_parameter_comparison_has_no_format_receipt(self):
        answer = Assistant(self.store, self.engine, allow_local_calculation=False).ask(self.sid, self.question)
        self.assertEqual(answer['content'], self.raw.text)
        self.assertIsNone(evidence.read(self.store, answer['id']))

    def test_unchanged_and_local_calculation_do_not_create_model_evidence(self):
        self.raw.text = 'P19'
        self.assertIsNone(evidence.read(self.store, self.answer()['id']))
        answer = Assistant(self.store, self.engine).ask(self.sid, '計算 12+3，只輸出整數')
        self.assertEqual(answer['content'], '15')
        self.assertIsNone(evidence.read(self.store, answer['id']))
        self.assertEqual(len(self.raw.prompts), 1)

    def test_receipt_save_failure_rolls_back_answer_and_success(self):
        with patch.object(evidence, 'save', side_effect=sqlite3.OperationalError('controlled failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.answer()
        self.assertEqual([m['role'] for m in self.store.messages(self.sid)], ['user'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM answer_runs').fetchone()[0], 'failed')
            for table in ('answer_contexts', 'answer_checks', 'answer_deliveries'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def test_cancel_after_raw_does_not_save_delivery(self):
        cancel = threading.Event()
        original = self.raw.answer
        def interrupted(prompt, stop=None):
            result = original(prompt, stop)
            cancel.set()
            return result
        self.raw.answer = interrupted
        with self.assertRaises(EngineError) as raised:
            Assistant(self.store, self.engine).ask(self.sid, self.question, cancel)
        self.assertEqual(raised.exception.code, 'cancelled')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM answer_deliveries').fetchone()[0], 0)

    def test_backup_round_trip_and_legacy_missing_table(self):
        answer = self.answer()
        receipt = evidence.read(self.store, answer['id'])
        backup = create_backup(self.store.path)
        restored = restore_backup(backup['path'], self.root / 'restored')
        copy = Store(Path(restored['data_dir']) / 'assistant.sqlite3')
        self.assertEqual(evidence.read(copy, answer['id']), receipt)
        with self.store.connect() as db:
            db.execute('DROP TABLE answer_deliveries')
        self.assertNotIn('answer_deliveries', validate_database(self.store.path))
        upgraded = Store(self.store.path)
        self.assertIsNone(evidence.read(upgraded, answer['id']))

    def test_reject_changed_raw_or_delivered_or_duplicate_json(self):
        answer = self.answer()
        receipt = evidence.read(self.store, answer['id'])
        for field, value in [('raw_answer', '不同原答'), ('raw_sha256', '0'*64), ('extra', True)]:
            with self.subTest(field=field):
                with self.store.connect() as db:
                    db.execute('UPDATE answer_deliveries SET payload=?',
                               (json.dumps({**receipt, field:value}, ensure_ascii=False),))
                with self.assertRaises(ValueError):
                    validate_database(self.store.path)
        with self.store.connect() as db:
            raw = json.dumps(receipt)[:-1] + ',"schema":"answer_delivery_v1"}'
            db.execute('UPDATE answer_deliveries SET payload=?', (raw,))
        with self.assertRaises(ValueError):
            validate_database(self.store.path)
        with self.store.connect() as db:
            db.execute('UPDATE answer_deliveries SET payload=?', (json.dumps(receipt),))
            db.execute('UPDATE messages SET content=? WHERE id=?', ('K73', answer['id']))
        with self.assertRaises(ValueError):
            validate_database(self.store.path)

    def test_strict_text_limits(self):
        for raw in ('', '\ud800', '甲' * evidence.MAX_TEXT_BYTES):
            with self.assertRaises((ValueError, UnicodeError)):
                evidence.make_receipt(raw, 'P19')

    def test_reference_window_shows_both_answers(self):
        import tkinter as tk
        from tkinter import ttk
        from assistant.context_ui import show_context
        answer = self.answer()
        root = tk.Tk()
        root.withdraw()
        try:
            show_context(root, self.store, answer['id'])
            window = next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
            window.withdraw()
            notebook = next(child for child in window.winfo_children() if isinstance(child, ttk.Notebook))
            self.assertEqual(notebook.tab(0, 'text'), '答案整理')
            frame = notebook.nametowidget(notebook.tabs()[0])
            texts = []
            def collect(widget):
                if isinstance(widget, tk.Text):
                    texts.append(widget.get('1.0', 'end-1c'))
                for child in widget.winfo_children():
                    collect(child)
            collect(frame)
            self.assertEqual(texts, [evidence.describe(self.store, answer['id'])])
        finally:
            root.destroy()


class MemoryFailureTests(unittest.TestCase):
    def test_worker_distinguishes_windows_commit_limit_without_model_answer(self):
        import io
        import sys
        from assistant import local_worker
        from assistant.local_engine import ERRORS
        from assistant.answer_runs import ERROR_LABELS
        self.assertIn('1455', ERRORS['system_memory'])
        self.assertIn('1455', ERROR_LABELS['local_system_memory'])
        with tempfile.TemporaryDirectory() as tmp:
            for number, expected in ((1455, 'system_memory'), (126, 'inference_failed')):
                output = Path(tmp)/f'{number}.json'
                error = OSError('controlled OS failure')
                error.winerror = number
                stdin = io.TextIOWrapper(io.BytesIO(b'{}'), encoding='utf8')
                with patch.object(sys,'argv',['local_worker','--output',str(output)]), \
                     patch.object(sys,'stdin',stdin), patch.object(local_worker,'infer',side_effect=error):
                    self.assertEqual(local_worker.main(),1)
                result = json.loads(output.read_text(encoding='utf8'))
                self.assertEqual(result,dict(schema='local_inference_result_v1',status='failed',code=expected))
