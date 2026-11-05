import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import answer_runs, document_partial, response_delivery
from assistant.engine import Assistant, EngineError
from assistant.local_engine import LocalEngine
from assistant.store import Store
from scripts.daily_delivery import DailyLocalEngine
from scripts.daily_expenses import DailyAssistant


def documents():
    return {
        'requested_ranges': [
            {'document_id': 1, 'start_line': 999, 'end_line': 999, 'status': 'line_out_of_bounds'},
            {'document_id': 1, 'start_line': 2, 'end_line': 2, 'status': 'provided'},
            {'document_id': 2, 'start_line': 3, 'end_line': 3, 'status': 'provided'},
            {'document_id': 9, 'start_line': 4, 'end_line': 4, 'status': 'document_not_attached'},
        ],
        'excerpts': [
            {'document_id': 1, 'start_line': 2, 'end_line': 2, 'partial_line': False, 'sha256': 'a' * 64},
            {'document_id': 2, 'start_line': 3, 'end_line': 3, 'partial_line': False, 'sha256': 'b' * 64},
        ],
    }


class DocumentPartialTests(unittest.TestCase):
    def test_preserves_request_order_and_uses_only_citations_from_provided_spans(self):
        plan = document_partial.build_plan(documents())
        raw = json.dumps({'points': [
            {'text': 'D2 第3行確認北倉下午四點。', 'sources': ['D2:L3']},
            {'text': '西倉上午九點。', 'sources': ['[D1:L2]']},
        ]}, ensure_ascii=False)
        answer = document_partial.render(raw, plan)
        self.assertEqual(answer.splitlines(), [
            '1. D1 第999行未提供。指定行號超出匯入副本的行數。目前無法確認該範圍的內容。',
            '2. 西倉上午九點。 [D1:L2]',
            '3. D2 第3行確認北倉下午四點。 [D2:L3]',
            '4. D9 第4行未提供。此來源編號不在目前對話的匯入文件中。目前無法確認該範圍的內容。',
        ])
        self.assertEqual(document_partial.render(raw + '"', plan), answer)
        with self.assertRaises(ValueError):
            document_partial.render(raw + '"extra', plan)

    def test_rejects_invented_references_and_keeps_an_empty_model_result_honest(self):
        plan = document_partial.build_plan({**documents(), 'requested_ranges_limited': True})
        for raw in (
                '{"points": [], "points": []}',
                '{"points":[{"text":"不明","sources":["D1:L999"]}]}',
                '{"points":[{"text":"不明","sources":["D1:L2","[D1:L2]"]}]}',
                '{"points":[{"text":"［Ｄ9:Ｌ4］有答案","sources":["D1:L2"]}]}',
                '{"points":[{"text":"D9 顯示九點","sources":["D1:L2"]}]}',
                '{"points":[{"text":"D1 第999行寫九點","sources":["D1:L2"]}]}',
                '{"points":[{"text":"D1 第2行寫九點","sources":["D2:L3"]}]}',
                '{"points":[{"text":"換\\n行","sources":["D1:L2"]}]}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                document_partial.render(raw, plan)
        answer = document_partial.render('{"points":[]}', plan)
        self.assertIn('D1 第2行已提供，這次沒有形成可引用的答案。', answer)
        self.assertIn('其餘尚未處理', answer)
        self.assertNotIn('[D', answer)

    def test_rejects_unbacked_provided_status_and_does_not_change_provided_only(self):
        source = documents()
        self.assertIsNone(document_partial.build_plan({**source, 'excerpts': source['excerpts'][:1]}))
        self.assertIsNone(document_partial.build_plan({**source, 'requested_ranges': source['requested_ranges'][1:3]}))
        self.assertIsNone(document_partial.build_plan({**source, 'requested_ranges': source['requested_ranges'][:1]}))

    def test_retains_bound_point_and_reports_a_separate_rejected_point(self):
        plan = document_partial.build_plan(documents())
        raw = json.dumps({'points': [
            {'text': '西倉上午九點，來源為 [D1:L2]。', 'sources': ['D1:L2']},
            {'text': '東倉由 [D9:L4] 確認是九點。', 'sources': ['D1:L2']},
            {'text': '北倉下午四點。', 'sources': ['D2:L3']},
        ]}, ensure_ascii=False)
        answer = document_partial.render(raw, plan)
        self.assertEqual(answer.count('[D1:L2]'), 1)
        self.assertIn('北倉下午四點。 [D2:L3]', answer)
        self.assertIn('D9 第4行未提供', answer)
        self.assertIn('1 項模型要點的來源無法核對', answer)
        self.assertNotIn('東倉由', answer)
        self.assertNotIn('[D9:', answer)

    def test_product_entries_preserve_raw_and_verify_delivery_against_saved_sources(self):
        raw = '{"points":[{"text":"西站下午兩點。","sources":["D1:L2"]}]}'
        worker = '''
import json, sys
from pathlib import Path
wire = json.loads(sys.stdin.buffer.read())
result = {"schema":"local_inference_result_v1","status":"succeeded","format":"document_partial_v1",
          "answer":sys.argv[2],"selection_fingerprint":wire["selection"]["fingerprint"],
          "input_tokens":130,"output_tokens":22,"peak_gpu_bytes":0}
Path(sys.argv[1]).write_text(json.dumps(result),encoding="utf-8")
'''
        question = '文件問答：核對 D1 第2行與 D9 第3行。'
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            path = directory / '來源.txt'
            path.write_text('西站紀錄\n西站下午兩點。', encoding='utf-8')
            for entry in (Assistant, DailyAssistant):
                with self.subTest(entry=entry.__name__):
                    state = directory / entry.__name__
                    store = Store(state / 'chat.sqlite3')
                    sid = store.new_session()
                    store.attach_document(sid, path)
                    engine = LocalEngine(state, recover_chat_budget=True)
                    app = entry(store, DailyLocalEngine(engine) if entry is DailyAssistant else engine)
                    with patch.object(engine, 'command', side_effect=lambda output: [
                            sys.executable, '-c', worker, str(output), raw]):
                        answer = app.ask(sid, question)
                    self.assertIn('西站下午兩點。 [D1:L2]', answer['content'])
                    self.assertIn('D9 第3行未提供', answer['content'])
                    receipt = response_delivery.read(store, answer['id'])
                    self.assertEqual(receipt['raw_answer'], raw)
                    self.assertEqual(receipt['method'], document_partial.METHOD)
                    self.assertEqual(answer['citation_checks'], [{'citation': '[D1:L2]', 'range_valid': True}])
                    context = store.answer_context(answer['id'])
                    context['documents']['requested_ranges'][1]['status'] = 'line_out_of_bounds'
                    with store.connect() as db:
                        db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?',
                                   (json.dumps(context), answer['id']))
                    with self.assertRaisesRegex(ValueError, '保存的來源'):
                        response_delivery.read(store, answer['id'])

    def test_bad_structured_output_records_failure_without_saving_an_answer(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            path = directory / '來源.txt'
            path.write_text('資料\n有依據的來源。', encoding='utf-8')
            store = Store(directory / 'chat.sqlite3')
            sid = store.new_session()
            store.attach_document(sid, path)
            engine = LocalEngine(directory, recover_chat_budget=True)
            result = {'schema': 'local_inference_result_v1', 'status': 'succeeded',
                      'format': document_partial.METHOD, 'selection_fingerprint': engine.selection['fingerprint'],
                      'answer': '{"points":[{"text":"杜撰","sources":["D9:L3"]}]}',
                      'input_tokens': 50, 'output_tokens': 20, 'peak_gpu_bytes': 0}
            code = 'import sys; from pathlib import Path; Path(sys.argv[1]).write_text(sys.argv[2],encoding="utf-8")'
            with patch.object(engine, 'command', side_effect=lambda output: [
                    sys.executable, '-c', code, str(output), json.dumps(result)]):
                with self.assertRaises(EngineError) as failure:
                    Assistant(store, engine).ask(sid, '文件問答：核對 D1 第2行及 D9 第3行。')
            self.assertEqual(failure.exception.code, 'local_document_partial_invalid')
            run, = answer_runs.runs(store, sid)
            self.assertEqual((run['status'], run['error_code']), ('failed', 'local_document_partial_invalid'))
            self.assertEqual([row['role'] for row in store.messages(sid)], ['user'])
