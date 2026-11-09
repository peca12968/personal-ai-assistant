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

    def test_hypothesis_keeps_document_values_and_citations_separate_from_the_substitution(self):
        self.attach('甲重量為十二公斤。', 'a.txt')
        self.attach('乙重量為4公斤。', 'b.txt')
        formula = '文件問答：計算 [D1:L1] 的甲重量 減去 [D2:L1] 的乙重量，單位為公斤。'
        question = formula + '假設 D2 第1行的乙重量改用 7 公斤，只做試算。'
        app = DailyAssistant(self.store, NoModel(), default_chat=True)
        result = app.ask(self.sid, question)
        receipt = self.receipt(result)
        self.assertEqual(receipt['schema'], document_calculation.SCENARIO_RECEIPT)
        self.assertEqual(receipt['plan']['base']['request'], formula)
        self.assertEqual([item['value'] for item in receipt['observations']], ['12', '4'])
        self.assertEqual(receipt['result']['exact'], '8')
        self.assertEqual(receipt['scenario']['result']['exact'], '5')
        original, scenario = result['content'].split('\n\n')
        self.assertIn('乙重量為 4 公斤。 [D2:L1]', original)
        self.assertIn('使用者提供的乙重量 7 公斤', scenario)
        self.assertNotIn('[D', scenario)
        self.assertEqual(self.receipt(app.ask(self.sid, formula))['result']['exact'], '8')
        self.assertIn(formula, document_calculation.describe(receipt))
        natural = formula[:-1] + '？如果把 [D2:L1] 的乙重量改成七公斤？'
        self.assertEqual(document_calculation.parse_live_request(natural)['hypothesis'], receipt['plan']['hypothesis'])
        with self.assertRaisesRegex(ValueError, '完整欄位名稱'):
            app.ask(self.sid, question.replace('第1行的乙重量', '第1行的丙重量'))
        self.assertIsNone(document_calculation.parse_live_request(question + '然後刪除原檔。'))

    def test_hypothesis_reuses_exact_unit_conversion_and_requires_the_retained_original(self):
        self.attach('甲時長為1秒。', 'a.txt')
        second = self.attach('乙時長為1分鐘。', 'b.txt')
        formula = '文件問答：計算 [D1:L1] 的甲時長 加上 [D2:L1] 的乙時長，換算成分鐘。'
        question = formula + '假設 [D1:L1] 的甲時長改為 2 秒，只做試算。'
        app = Assistant(self.store, NoModel())
        receipt = self.receipt(app.ask(self.sid, question))
        self.assertEqual(receipt['result']['exact'], '61/60')
        self.assertEqual(receipt['scenario']['quantity']['factor_exact'], '1/60')
        self.assertEqual(receipt['scenario']['result']['exact'], '31/30')
        incompatible = self.receipt(app.ask(self.sid, question.replace('2 秒', '2 公斤')))
        self.assertEqual(incompatible['result']['exact'], '61/60')
        self.assertEqual(incompatible['scenario']['quantity']['status'], 'unit_incompatible')
        self.assertIsNone(incompatible['scenario']['result'])
        self.store.detach_document(self.sid, second)
        replaced_missing = self.receipt(app.ask(self.sid, formula + '假設 [D2:L1] 的乙時長改為 2 秒。'))
        self.assertIsNone(replaced_missing['result'])
        self.assertEqual(replaced_missing['scenario']['result']['exact'], '1/20')
        self.assertNotIn('[D2:', replaced_missing['output'])
        incomplete = self.receipt(app.ask(self.sid, question))
        self.assertEqual(incomplete['scenario']['quantity']['status'], 'source_unavailable')
        self.assertIsNone(incomplete['scenario']['result'])
        self.assertNotIn('[D2:', incomplete['output'])

    def test_hypothesis_retry_pins_sources_and_restore_recomputes_both_results(self):
        self.attach('甲重量為十二公斤。', 'a.txt')
        second = self.attach('乙重量為4公斤。', 'b.txt')
        question = ('文件問答：計算 [D1:L1] 的甲重量 減去 [D2:L1] 的乙重量，單位為公斤。'
                    '假設 [D2:L1] 的乙重量改用 7 公斤。')
        app = Assistant(self.store, NoModel())
        with patch.object(answer_runs, 'finish', side_effect=EngineError('停止', 'cancelled')), self.assertRaises(EngineError):
            app.ask(self.sid, question)
        result = app.retry(self.sid)
        receipt = self.receipt(result)
        backup = create_backup(self.store.path, self.root/'scenario.paibackup')
        restore_backup(backup['path'], self.root/'restored')
        restored = Store(self.root/'restored'/'assistant.sqlite3')
        self.assertEqual(document_calculation.read(restored, result['id']), receipt)
        altered = restored.answer_context(result['id'])
        altered[document_calculation.FIELD]['scenario']['result']['exact'] = '6'
        with restored.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', (json.dumps(altered), result['id']))
        with self.assertRaisesRegex(ValueError, '來源或結果'):
            create_backup(restored.path, self.root/'changed.paibackup')
        with patch.object(answer_runs, 'finish', side_effect=EngineError('停止', 'cancelled')), self.assertRaises(EngineError):
            app.ask(self.sid, question)
        self.store.detach_document(self.sid, second)
        with self.assertRaisesRegex(ValueError, '試算來源已變更'):
            app.retry(self.sid)

    def test_short_original_followup_uses_only_the_adjacent_scenario_and_actual_values(self):
        self.attach('甲重量為十二公斤。', 'a.txt')
        second = self.attach('乙重量為4公斤。', 'b.txt')
        formula = '文件問答：計算 [D1:L1] 的甲重量 減去 [D2:L1] 的乙重量，單位為公斤。'
        question = formula + '假設 [D2:L1] 的乙重量改用 7 公斤。'
        app = DailyAssistant(self.store, NoModel(), default_chat=True)
        scenario = app.ask(self.sid, question)
        self.assertEqual(self.receipt(scenario)['scenario']['result']['exact'], '5')
        self.attach('乙重量為 99 公斤。', 'unrelated.txt')
        result = app.ask(self.sid, '用文件原值重算。')
        receipt = self.receipt(result)
        self.assertEqual(receipt['schema'], document_calculation.ORIGINAL_RECEIPT)
        self.assertEqual(receipt['plan']['scenario_source']['answer_id'], scenario['id'])
        self.assertEqual(receipt['plan']['base']['request'], formula)
        self.assertEqual(receipt['result']['exact'], '8')
        self.assertEqual([value['value'] for value in receipt['observations']], ['12', '4'])
        self.assertEqual(result['document_count'], 2)
        self.assertIn(f'文件試算回答 #{scenario["id"]}', document_calculation.describe(receipt))
        self.store.detach_document(self.sid, second)
        self.assertEqual(document_calculation.read(self.store, result['id']), receipt)

    def test_short_original_followup_requires_the_same_scope_and_live_references(self):
        self.attach('甲重量為十二公斤。', 'a.txt')
        second = self.attach('乙重量為4公斤。', 'b.txt')
        question = ('文件問答：計算 [D1:L1] 的甲重量 減去 [D2:L1] 的乙重量，單位為公斤。'
                    '假設 [D2:L1] 的乙重量改用 7 公斤。')
        app = Assistant(self.store, NoModel())
        app.ask(self.sid, question)
        with self.assertRaisesRegex(ValueError, '緊接在文件試算回答後'):
            app.ask(self.store.new_session(), '用文件原值重算。')
        self.store.detach_document(self.sid, second)
        with self.assertRaisesRegex(ValueError, '引用行已變更'):
            app.ask(self.sid, '用文件原值重算。')
        missing = app.ask(self.sid, question)
        original = self.receipt(app.ask(self.sid, '回到文件原值重算。'))
        self.assertEqual(original['plan']['scenario_source']['answer_id'], missing['id'])
        self.assertIsNone(original['result'])
        self.assertIn('缺少可用的乙重量', original['output'])
        app.ask(self.sid, question)
        self.assertEqual(app.ask(self.sid, '計算 2 + 2，只回答整數。')['content'], '4')
        with self.assertRaisesRegex(ValueError, '緊接在文件試算回答後'):
            app.ask(self.sid, '用文件原值重算。')
        app.ask(self.sid, question)
        begin = answer_runs.begin

        def begin_after_topic_change(*args, **kwargs):
            self.store.add_message(self.sid, 'user', '先談另一件事。')
            return begin(*args, **kwargs)

        with patch.object(answer_runs, 'begin', side_effect=begin_after_topic_change), self.assertRaises(EngineError) as stopped:
            app.ask(self.sid, '用文件原值重算。')
        self.assertEqual(stopped.exception.code, 'document_calculation_invalid')
        with self.assertRaisesRegex(ValueError, '緊接在文件試算回答後'):
            app.retry(self.sid)

    def test_short_original_followup_retry_restore_and_origin_verification(self):
        self.attach('甲時長為1秒。', 'a.txt')
        self.attach('乙時長為1分鐘。', 'b.txt')
        question = ('文件問答：計算 [D1:L1] 的甲時長 加上 [D2:L1] 的乙時長，換算成分鐘。'
                    '假設 [D1:L1] 的甲時長改用 2 秒。')
        app = Assistant(self.store, NoModel())
        app.ask(self.sid, question)
        with patch.object(answer_runs, 'finish', side_effect=EngineError('停止', 'cancelled')), self.assertRaises(EngineError):
            app.ask(self.sid, '請依文件原值重新計算。')
        result = app.retry(self.sid)
        receipt = self.receipt(result)
        self.assertEqual(receipt['result']['exact'], '61/60')
        bundle = create_backup(self.store.path, self.root/'original.paibackup')
        restore_backup(bundle['path'], self.root/'restored')
        restored = Store(self.root/'restored'/'assistant.sqlite3')
        self.assertEqual(document_calculation.read(restored, result['id']), receipt)
        with restored.connect() as db:
            saved = restored.answer_context(result['id'])
            saved[document_calculation.FIELD]['plan']['scenario_source']['sha256'] = '0' * 64
            initial = json.loads(db.execute('SELECT context_json FROM answer_runs WHERE id=?',
                                            (result['run_id'],)).fetchone()[0])
            initial[document_calculation.FIELD] = saved[document_calculation.FIELD]['plan']
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', (json.dumps(saved), result['id']))
            db.execute('UPDATE answer_runs SET context_json=? WHERE id=?', (json.dumps(initial), result['run_id']))
        with self.assertRaisesRegex(ValueError, '上一則試算的算式或來源'):
            document_calculation.read(restored, result['id'])
        with self.assertRaisesRegex(ValueError, '上一則試算的算式或來源'):
            create_backup(restored.path, self.root/'tampered-original.paibackup')

    def test_explicit_original_source_selects_an_earlier_scenario_even_after_a_newer_one(self):
        self.attach('甲重量為十二公斤。', 'a.txt')
        self.attach('乙重量為4公斤。', 'b.txt')
        question = ('文件問答：計算 [D1:L1] 的甲重量 減去 [D2:L1] 的乙重量，單位為公斤。'
                    '假設 [D2:L1] 的乙重量改用 7 公斤。')
        app = DailyAssistant(self.store, NoModel(), default_chat=True)
        earlier = app.ask(self.sid, question)
        later = app.ask(self.sid, question.replace('減去', '加上'))
        self.assertEqual(self.receipt(later)['result']['exact'], '16')
        request = f'用文件試算回答 #{earlier["id"]} 的原值重算。'
        result = self.receipt(app.ask(self.sid, request))
        self.assertEqual(result['schema'], document_calculation.EXPLICIT_ORIGINAL_RECEIPT)
        self.assertEqual(result['plan']['scenario_source']['answer_id'], earlier['id'])
        self.assertEqual(result['plan']['request'], request)
        self.assertEqual(result['result']['exact'], '8')
        self.assertIn('依你指定的', document_calculation.describe(result))
        latest_request = f'依文件試算回答 #{later["id"]} 的文件原值重新計算。'
        self.assertEqual(self.receipt(app.ask(self.sid, latest_request))['result']['exact'], '16')
        self.store.add_message(self.sid, 'assistant', '依文件原值\n尚未核對的文字')
        self.assertEqual(document_calculation.scenario_ids(self.store, self.store.messages(self.sid)),
                         {earlier['id'], later['id']})

    def test_explicit_original_source_rejects_wrong_session_type_and_reference_state(self):
        self.attach('甲重量為十二公斤。', 'a.txt')
        second = self.attach('乙重量為4公斤。', 'b.txt')
        formula = '文件問答：計算 [D1:L1] 的甲重量 減去 [D2:L1] 的乙重量，單位為公斤。'
        app = Assistant(self.store, NoModel())
        normal = app.ask(self.sid, formula)
        scenario = app.ask(self.sid, formula + '假設 [D2:L1] 的乙重量改用 7 公斤。')
        request = f'用文件試算回答 #{scenario["id"]} 的原值重算。'
        foreign = self.store.new_session()
        for sid, query in ((self.sid, request.replace(f'#{scenario["id"]}', f'#{normal["id"]}')),
                           (foreign, request), (self.sid, '用文件試算回答 #9999999999999999999999 的原值重算。')):
            with self.subTest(sid=sid, query=query), self.assertRaisesRegex(ValueError, '可核對的文件試算回答'):
                app.ask(sid, query)
        with self.assertRaisesRegex(ValueError, '可核對的文件試算回答'):
            document_calculation.capture_original(self.store, self.sid, request, before=scenario['id'])
        self.store.detach_document(self.sid, second)
        with self.assertRaisesRegex(ValueError, '引用行已變更'):
            app.ask(self.sid, request)

    def test_explicit_original_source_survives_retry_restore_and_rechecks_parent_delivery(self):
        self.attach('甲時長為1秒。', 'a.txt')
        self.attach('乙時長為1分鐘。', 'b.txt')
        question = ('文件問答：計算 [D1:L1] 的甲時長 加上 [D2:L1] 的乙時長，換算成分鐘。'
                    '假設 [D1:L1] 的甲時長改用 2 秒。')
        app = Assistant(self.store, NoModel())
        scenario = app.ask(self.sid, question)
        app.ask(self.sid, '計算 2 + 2，只回答整數。')
        with patch.object(answer_runs, 'finish', side_effect=EngineError('停止', 'cancelled')), self.assertRaises(EngineError):
            app.ask(self.sid, f'用文件試算回答 #{scenario["id"]} 的原值重算。')
        result = app.retry(self.sid)
        receipt = self.receipt(result)
        self.assertEqual(receipt['result']['exact'], '61/60')
        bundle = create_backup(self.store.path, self.root/'named-source.paibackup')
        restore_backup(bundle['path'], self.root/'restored')
        restored = Store(self.root/'restored'/'assistant.sqlite3')
        self.assertEqual(document_calculation.read(restored, result['id']), receipt)
        with restored.connect() as db:
            db.execute('UPDATE messages SET content=? WHERE id=?', ('已改動的試算', scenario['id']))
        with self.assertRaisesRegex(ValueError, '來源或結果'):
            document_calculation.read(restored, result['id'])

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

    def test_explicit_conversion_computes_exact_mass_length_volume_and_duration(self):
        self.attach('甲重量：2.8kg。\n甲距離為1.25公里。\n甲容量為900ｍＬ。\n甲時長為1秒。', 'a.txt')
        self.attach('乙樣本實測重量為125公克。\n乙距離為1500m。\n乙容量為0.325升。\n乙時長為1分鐘。', 'b.txt')
        for line, label, operation, target, expected, factors in (
                (1, '重量', '減去', '公斤', '107/40', ('1', '1/1000')),
                (2, '距離', '減去', '公尺', '-250', ('1000', '1')),
                (3, '容量', '加上', '毫升', '1225', ('1', '1000')),
                (4, '時長', '加上', '分鐘', '61/60', ('1/60', '1'))):
            with self.subTest(label=label):
                question = (f'文件問答：計算 [D1:L{line}] 的甲{label} {operation} D2 第{line}行的乙{label}'
                            f'，統一換算成 {target}。')
                app = DailyAssistant(self.store, NoModel()) if line % 2 else Assistant(self.store, NoModel())
                response = app.ask(self.sid, question)
                receipt = self.receipt(response)
                self.assertEqual(receipt['schema'], 'document_calculation_receipt_v2')
                self.assertEqual(receipt['plan']['request'], question)
                self.assertEqual(receipt['result']['exact'], expected)
                self.assertEqual(tuple(row['factor_exact'] for row in receipt['observations']), factors)
                self.assertEqual(response['citation_checks'], [
                    {'citation': f'[D1:L{line}]', 'range_valid': True},
                    {'citation': f'[D2:L{line}]', 'range_valid': True},
                ])
                if line == 4:
                    self.assertIn('61/60 分鐘', response['content'])
                    self.assertIn('1 秒 = 1/60 分鐘', document_calculation.describe(receipt))

    def test_conversion_keeps_missing_uncertain_and_incompatible_values_separate(self):
        self.attach('甲重量為2公斤。', 'a.txt')
        self.attach('乙重量為1L。\n乙重量為7Kg。\n乙重量約7公克。\n乙重量為1公斤，但待核准。', 'b.txt')
        question = '文件問答：計算 [D1:L1] 的甲重量 加上 [D{doc}:L{line}] 的乙重量，換算成公斤。'
        for document, line, expected in ((2, 1, 'unit_incompatible'), (2, 2, 'unit_unsupported'),
                                         (2, 3, 'value_unconfirmed'), (2, 4, 'value_unconfirmed'),
                                         (2, 5, 'line_out_of_bounds'), (8, 1, 'document_not_attached')):
            with self.subTest(document=document, line=line):
                response = Assistant(self.store, NoModel()).ask(self.sid, question.format(doc=document, line=line))
                receipt = self.receipt(response)
                self.assertIsNone(receipt['result'])
                self.assertEqual(receipt['observations'][0]['converted']['exact'], '2')
                self.assertEqual(receipt['observations'][1]['status'], expected)
                self.assertNotIn('factor_exact', receipt['observations'][1])
                self.assertTrue(response['content'].startswith('目前無法計算。'))

    def test_conversion_and_v1_receipts_roundtrip_and_factor_tampering_fails(self):
        self.attach('甲重量：1500g。', 'a.txt')
        self.attach('乙重量：1公斤。', 'b.txt')
        base = '文件問答：計算 [D1:L1] 的甲重量 減去 [D2:L1] 的乙重量，'
        app = Assistant(self.store, NoModel())
        before = app.ask(self.sid, base + '單位為公斤。')
        after = app.ask(self.sid, base + '換算成公斤。')
        old, new = self.receipt(before), self.receipt(after)
        self.assertEqual(old['schema'], 'document_calculation_receipt_v1')
        self.assertIsNone(old['result'])
        self.assertEqual(new['result']['exact'], '1/2')
        archive = create_backup(self.store.path, self.root / 'versions.paibackup')
        restore_backup(archive['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(document_calculation.read(restored, before['id']), old)
        self.assertEqual(document_calculation.read(restored, after['id']), new)
        context = self.store.answer_context(after['id'])
        context[document_calculation.FIELD]['observations'][0]['factor_exact'] = '1/100'
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', (json.dumps(context), after['id']))
        with self.assertRaisesRegex(ValueError, '來源或結果'):
            create_backup(self.store.path, self.root / 'tampered-conversion.paibackup')

    def test_conversion_keeps_mixed_requests_and_model_only_evaluation_on_original_path(self):
        class Observer:
            def answer(self, prompt, cancel=None):
                return '原模型輸出'

        stem = '文件問答：計算 [D1:L1] 的甲重量 減去 [D2:L1] 的乙重量，換算成'
        for question, raw in ((stem + '公斤。然後上傳原始文件。', False),
                              (stem + '公斤並刪除原檔。', False),
                              (stem + '公斤。', True)):
            with self.subTest(question=question, raw=raw):
                reply = DailyAssistant(self.store, Observer(), allow_local_calculation=not raw).ask(self.sid, question)
                self.assertEqual(reply['content'], '原模型輸出')
                self.assertNotIn('response_mode', reply)
