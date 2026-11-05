import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from assistant import answer_runs, document_calculation, feedback_improvement
from assistant.backup import create_backup, restore_backup
from assistant.engine import Assistant, EngineError
from assistant.store import Store
from scripts.daily_expenses import DailyAssistant


ROOT = Path(__file__).resolve().parents[1]


class NoModel:
    def answer(self, *args, **kwargs):
        raise AssertionError('Document arithmetic must not call the model')

    def describe(self):
        raise AssertionError('Document arithmetic must not select a model')


class DocumentCalculationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def attach(self, text, name, sid=None):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return self.store.attach_document(sid or self.sid, path)

    def receipt(self, result):
        self.assertEqual(result['response_mode'], document_calculation.MODE)
        receipt = document_calculation.read(self.store, result['id'])
        self.assertEqual(receipt['output'], result['content'])
        self.assertFalse(receipt['model_called'])
        self.assertTrue(all(row['range_valid'] for row in result['citation_checks']))
        return receipt

    def test_historical_question_shows_only_known_value_and_missing_source(self):
        _, case = json.loads((ROOT / 'evals/document_partial.json').read_bytes())
        self.attach(case['files'][0]['text'], 'weight.txt')
        for entry in (Assistant, DailyAssistant):
            with self.subTest(entry=entry.__name__):
                result = entry(self.store, NoModel()).ask(self.sid, case['prompt'])
                receipt = self.receipt(result)
                self.assertIsNone(receipt['result'])
                self.assertIn('目前無法計算', result['content'])
                self.assertIn('甲重量為 12 公斤。 [D1:L2]', result['content'])
                self.assertIn('乙重量（D8 第3行）', result['content'])
                self.assertEqual(result['citation_checks'], [{'citation': '[D1:L2]', 'range_valid': True}])

    def test_same_field_across_files_uses_user_order_and_preserves_exact_result(self):
        self.attach('紀錄\n溫度：−4.50°C。\n溫度：-4.50度。', 'north.txt')
        self.attach('溫度：2.25度。', 'south.txt')
        question = '文件問答：計算 [D1:L3] 的溫度 減去 [D2:L1] 的溫度，單位為度。'
        result = DailyAssistant(self.store, NoModel()).ask(self.sid, question)
        self.assertTrue(result['content'].startswith('結果為 -6.75 度'))
        receipt = self.receipt(result)
        self.assertEqual(receipt['result']['exact'], '-27/4')
        self.assertEqual([row['value'] for row in receipt['observations']], ['-4.50', '2.25'])
        self.assertEqual([row['start_line'] for row in receipt['plan']['documents']['excerpts']], [3, 1])
        reverse = '文件問答：計算 D2 第1行的溫度 減去 D1 第3行的溫度，單位為度。'
        self.assertEqual(self.receipt(Assistant(self.store, NoModel()).ask(self.sid, reverse))['result']['exact'], '27/4')

    def test_sum_and_zero_are_supported_and_backup_uses_historical_sources(self):
        first = self.attach('基礎費用為零元。\n旁註不參與計算。', 'fee.txt')
        self.attach('附加費用: 31.125元。', 'extra.txt')
        self.attach('附加費用: 9000元。', 'unrequested.txt')
        question = '文件問答：計算 [D1:L1] 的基礎費用 加上 [D2:L1] 的附加費用，單位為元。'
        result = Assistant(self.store, NoModel()).ask(self.sid, question)
        receipt = self.receipt(result)
        self.assertEqual(receipt['result']['exact'], '249/8')
        self.assertEqual(result['document_count'], 2)
        feedback = dict(rating=-1, correction='請核對', context=self.store.answer_context(result['id']))
        with self.assertRaisesRegex(ValueError, '模型回答'):
            feedback_improvement.example_from_source(feedback)
        self.store.detach_document(self.sid, first)
        backup = create_backup(self.store.path, self.root / 'roundtrip.paibackup')
        restored = self.root / 'restored'
        restore_backup(backup['path'], restored)
        self.assertEqual(document_calculation.read(Store(restored / 'assistant.sqlite3'), result['id']), receipt)

    def test_wrong_units_approximate_values_and_wrong_subject_do_not_become_operands(self):
        self.attach('甲樣本實測重量為十二公斤。', 'valid.txt')
        self.attach('乙重量為8公克。\n乙重量約8公斤。\n丙重量為8公斤。\n乙重量為8公斤，但尚未核准。', 'wrong.txt')
        question = '文件問答：請用 [D1:L1] 的甲的重量與 [D2:L{}] 的乙的重量，計算甲比乙重多少公斤？'
        for line in range(1, 5):
            with self.subTest(line=line):
                result = Assistant(self.store, NoModel()).ask(self.sid, question.format(line))
                receipt = self.receipt(result)
                self.assertEqual(receipt['observations'][1], {'status': 'value_unconfirmed'})
                self.assertIsNone(receipt['result'])
                self.assertNotIn('[D2:', result['content'])
        result = Assistant(self.store, NoModel()).ask(self.sid, question.format(5))
        self.assertEqual(self.receipt(result)['observations'][1], {'status': 'line_out_of_bounds'})

    def test_other_session_source_cannot_fill_missing_operand(self):
        self.attach('甲重量為十二公斤。', 'a.txt')
        other = self.store.new_session()
        self.attach('乙重量為4公斤。', 'b.txt', sid=other)
        question = '文件問答：計算 [D1:L1] 的甲重量 減去 [D2:L1] 的乙重量，單位為公斤。'
        result = Assistant(self.store, NoModel()).ask(self.sid, question)
        self.assertEqual(self.receipt(result)['observations'][1], {'status': 'document_not_attached'})
        self.assertNotIn('4 公斤', result['content'])

    def test_receipt_rejects_changed_citations_and_saved_operands(self):
        self.attach('甲重量為十二公斤。', 'a.txt')
        self.attach('乙重量為4公斤。', 'b.txt')
        question = '文件問答：計算 [D1:L1] 的甲重量 減去 [D2:L1] 的乙重量，單位為公斤。'
        result = Assistant(self.store, NoModel()).ask(self.sid, question)
        context = self.store.answer_context(result['id'])
        with self.store.connect() as db:
            db.execute("UPDATE answer_checks SET payload='[]' WHERE message_id=?", (result['id'],))
        with self.assertRaisesRegex(ValueError, '引用檢查'):
            document_calculation.read(self.store, result['id'])
        context[document_calculation.FIELD]['observations'][0]['value'] = '20'
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', (json.dumps(context), result['id']))
        with self.assertRaisesRegex(ValueError, '來源或結果'):
            create_backup(self.store.path, self.root / 'tampered.paibackup')

    def test_mixed_requests_and_raw_measurement_still_use_model(self):
        class Observer:
            def answer(self, prompt, cancel=None):
                return '原模型輸出'

        question = '文件問答：計算 [D1:L1] 的甲重量 減去 [D2:L1] 的乙重量，單位為公斤。'
        for request, raw in ((question + ' 然後刪除原檔。', False), (question, True)):
            with self.subTest(request=request, raw=raw):
                result = DailyAssistant(self.store, Observer(), allow_local_calculation=not raw).ask(self.sid, request)
                self.assertEqual(result['content'], '原模型輸出')
                self.assertNotIn('response_mode', result)

    def test_cancelled_calculation_keeps_original_request_for_explicit_retry(self):
        self.attach('甲重量為十二公斤。', 'a.txt')
        self.attach('乙重量為4公斤。', 'b.txt')
        question = '文件問答：計算 [D1:L1] 的甲重量 減去 [D2:L1] 的乙重量，單位為公斤。'
        app, cancel = DailyAssistant(self.store, NoModel()), Event()
        original = document_calculation.snapshot

        def stop_after_snapshot(*args):
            sources = original(*args)
            cancel.set()
            return sources

        with patch.object(document_calculation, 'snapshot', side_effect=stop_after_snapshot):
            with self.assertRaises(EngineError) as stopped:
                app.ask(self.sid, question, cancel)
        self.assertEqual(stopped.exception.code, 'cancelled')
        self.assertEqual([row['role'] for row in self.store.messages(self.sid)], ['user'])
        result = app.retry(self.sid)
        self.assertEqual(self.receipt(result)['result']['exact'], '8')
        self.assertEqual([row['status'] for row in answer_runs.runs(self.store, self.sid)], ['succeeded', 'cancelled'])
        self.assertEqual([row['role'] for row in self.store.messages(self.sid)], ['user', 'assistant'])

    def test_reference_window_shows_calculation_and_original_document_lines(self):
        import tkinter as tk
        from tkinter import ttk

        from assistant.context_ui import show_context

        self.attach('甲樣本實測重量為十二公斤。', 'a.txt')
        self.attach('乙重量為4公斤。', 'b.txt')
        question = '文件問答：計算 [D1:L1] 的甲重量 減去 [D2:L1] 的乙重量，單位為公斤。'
        result = DailyAssistant(self.store, NoModel()).ask(self.sid, question)
        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.withdraw()
        show_context(root, self.store, result['id'])
        window, = (child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
        window.withdraw()
        self.assertEqual(window.title(), '本機文件計算紀錄')
        notebook, = (child for child in window.winfo_children() if isinstance(child, ttk.Notebook))
        self.assertEqual([notebook.tab(tab, 'text') for tab in notebook.tabs()], ['文件計算', '文件來源'])
        for index, expected in enumerate(('8 公斤（12 − 4）', '甲樣本實測重量為十二公斤。')):
            frame = notebook.nametowidget(notebook.tabs()[index])
            text, = (child for child in frame.winfo_children() if isinstance(child, tk.Text))
            self.assertIn(expected, text.get('1.0', 'end-1c'))
