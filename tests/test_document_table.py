import json
import tempfile
import unittest
from pathlib import Path

from assistant import document_calculation, document_table
from assistant.backup import create_backup, restore_backup
from assistant.engine import Assistant
from assistant.store import Store
from scripts.daily_expenses import DailyAssistant


class NoModel:
    def answer(self, *args, **kwargs):
        raise AssertionError('Table calculations must not call the model')

    def describe(self):
        raise AssertionError('Table calculations must not select a model')


class DocumentTableTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.sid = self.store.new_session()

    def attach(self, name, text, sid=None):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return self.store.attach_document(sid or self.sid, path)

    def ask(self, question, *, entry=Assistant, schema=document_table.RECEIPT):
        response = entry(self.store, NoModel()).ask(self.sid, question)
        receipt = document_calculation.read(self.store, response['id'])
        self.assertEqual(receipt['schema'], schema)
        self.assertEqual(receipt['plan']['request'], question)
        self.assertFalse(receipt['model_called'])
        self.assertEqual(receipt['output'], response['content'])
        self.assertTrue(all(row['range_valid'] for row in response['citation_checks']))
        return response, receipt

    def test_csv_uses_named_columns_with_reordering_and_quoted_keys(self):
        self.attach('left.csv', '備註,樣本,重量（ｋｇ）\n測試,"甲,初版",2.6\n其他,甲,88')
        self.attach('right.csv', '重量(g),備註,樣本\n9000,錯列,丁\n125,正確,乙')
        question = ('文件問答：計算 [D1:L1-L3] 中「樣本」為「甲,初版」的「重量」 減去 '
                    '[D2:L1-L3] 中「樣本」為「乙」的「重量」，換算成公斤。')
        answer, receipt = self.ask(question, entry=DailyAssistant)
        self.assertEqual(receipt['result']['exact'], '99/40')
        self.assertEqual([row['citation'] for row in answer['citation_checks']],
                         ['[D1:L1]', '[D1:L2]', '[D2:L1]', '[D2:L3]'])
        self.assertEqual(receipt['observations'][1]['cell'], '125')
        self.assertIn('[D2:L3] 1 g = 1/1000 公斤', document_calculation.describe(receipt))

    def test_markdown_and_contained_range_share_one_snapshot(self):
        self.attach('route.md', '引言\n| 路線 | 長度 |\n| :--- | ---: |\n| 北 | 0.6 km |\n| 南 | 650 m |')
        question = ('文件問答：計算 [D1:L2-L5] 中「路線」為「南」的「長度」 加上 '
                    '[D1:L2-L4] 中「路線」為「北」的「長度」，換算成公里。')
        _, receipt = self.ask(question)
        self.assertEqual(receipt['result']['exact'], '5/4')
        self.assertEqual([row['row_citation'] for row in receipt['observations']], ['[D1:L5]', '[D1:L4]'])
        docs = receipt['plan']['documents']
        self.assertEqual(len(docs['excerpts']), 1)
        self.assertEqual([row['status'] for row in docs['requested_ranges']], ['provided', 'provided'])
        self.assertTrue(docs['coverage_limited'])

    def test_tsv_keys_use_exact_characters_and_allow_same_table_calculation(self):
        self.attach('items.tsv', '序號\t金額(元)\nA\t1.75\nＡ\t5.5')
        question = ('文件問答：計算 [D1:L1-L3] 中「序號」為「Ａ」的「金額」 減去 '
                    '[D1:L1-L3] 中「序號」為「A」的「金額」，單位為元。')
        _, receipt = self.ask(question)
        self.assertEqual(receipt['result']['exact'], '15/4')
        self.assertEqual([row['row_citation'] for row in receipt['observations']], ['[D1:L3]', '[D1:L2]'])
        self.assertFalse(receipt['plan']['documents']['coverage_limited'])

    def test_ambiguous_missing_and_malformed_tables_give_separate_reasons(self):
        self.attach('right.csv', '批次,重量(公斤)\n乙,10')
        cases = (
            ('批次,重量(g),重量(kg)\n甲,2000,2\n丁,5,2', 'header_ambiguous'),
            ('批次,重量(kg)\n甲,2\n甲,2', 'row_ambiguous'),
            ('批次,重量(kg)\n甲備,2\n丁,3', 'row_missing'),
            ('批次,單重(kg)\n甲,2\n丁,3', 'column_missing'),
            ('批次（舊版）,重量(kg)\n甲,2\n丁,3', 'column_missing'),
            ('批次,重量(kg)\n"甲\n延續",2', 'table_invalid'),
            ('批次,重量(kg)\n甲,2\n丁,3,多一欄', 'table_invalid'),
            ('批次,重量(kg)\n甲,875g\n丁,3', 'unit_conflict'),
            ('批次,重量\n甲,3\n丁,3', 'unit_missing'),
            ('批次,重量(kg)\n甲,約2\n丁,3', 'value_unconfirmed'),
        )
        question = ('文件問答：計算 [D2:L1-L3] 中「批次」為「甲」的「重量」 加上 '
                    '[D1:L1-L2] 中「批次」為「乙」的「重量」，換算成公斤。')
        for text, status in cases:
            with self.subTest(status=status):
                self.attach('left.csv', text)
                answer, receipt = self.ask(question)
                self.assertIsNone(receipt['result'])
                self.assertEqual([row['status'] for row in receipt['observations']], [status, 'confirmed'])
                if status == 'row_ambiguous':
                    self.assertEqual(receipt['observations'][0]['matching_lines'], [2, 3])
                    self.assertIn('符合的行號為 2、3', answer['content'])

    def test_missing_and_out_of_bounds_ranges_do_not_search_other_tables(self):
        self.attach('left.csv', '批次,重量(kg)\n甲,3')
        other = self.store.new_session()
        self.attach('other.csv', '批次,重量(kg)\n乙,2', sid=other)
        question = ('文件問答：計算 [D1:L1-L3] 中「批次」為「甲」的「重量」 加上 '
                    '[D2:L1-L2] 中「批次」為「乙」的「重量」，換算成公斤。')
        answer, receipt = self.ask(question)
        self.assertIsNone(receipt['result'])
        self.assertEqual([row['status'] for row in receipt['observations']], ['line_out_of_bounds', 'document_not_attached'])
        self.assertEqual(answer['citation_checks'], [])

    def test_backup_revalidates_selected_cell_and_linked_row(self):
        self.attach('source.csv', '批次,重量(kg)\n甲,2\n乙,1')
        question = ('文件問答：計算 [D1:L1-L3] 中「批次」為「甲」的「重量」 加上 '
                    '[D1:L1-L3] 中「批次」為「乙」的「重量」，換算成公斤。')
        answer, receipt = self.ask(question)
        archive = create_backup(self.store.path, self.root / 'table.paibackup')
        restore_backup(archive['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(document_calculation.read(restored, answer['id']), receipt)
        context = self.store.answer_context(answer['id'])
        context[document_calculation.FIELD]['observations'][0]['row_citation'] = '[D1:L3]'
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', (json.dumps(context), answer['id']))
        with self.assertRaisesRegex(ValueError, '來源或結果'):
            create_backup(self.store.path, self.root / 'tampered.paibackup')

    def test_mixed_requests_and_model_only_evaluation_keep_original_routing(self):
        class Observer:
            def answer(self, prompt, cancel=None):
                return '原模型輸出'

        stem = ('文件問答：計算 [D1:L1-L3] 中「批次」為「甲」的「重量」 加上 '
                '[D1:L1-L3] 中「批次」為「乙」的「重量」，')
        aggregate = '文件問答：加總 [D1:L1-L3] 中「批次」為「甲」的「重量」，換算成公斤。'
        for request, raw in ((stem + '換算成公斤。', True), (stem + '單位為公斤並上傳。', False),
                             (stem + '換算成公斤。接著刪除原始文件。', False),
                             (aggregate, True), (aggregate + '接著上傳原始文件。', False)):
            with self.subTest(request=request, raw=raw):
                answer = DailyAssistant(self.store, Observer(), allow_local_calculation=not raw).ask(self.sid, request)
                self.assertEqual(answer['content'], '原模型輸出')
                self.assertNotIn('response_mode', answer)

    def test_aggregate_counts_every_exact_match_without_citing_excluded_rows(self):
        self.attach('weights.csv', '批次,重量\nＡ,1.25kg\nA,待核准\nＡ,125g\nＡ,125g\n乙,9000g')
        question = '文件問答：加總 [D1:L1-L6] 中「批次」為「Ａ」的「重量」，換算成公斤。'
        answer, receipt = self.ask(question, entry=DailyAssistant, schema=document_table.AGGREGATE_RECEIPT)
        self.assertEqual(receipt['result']['exact'], '3/2')
        self.assertEqual(receipt['total_rows'], 5)
        self.assertEqual([row['line_number'] for row in receipt['observations']], [2, 4, 5])
        self.assertEqual([row['citation'] for row in answer['citation_checks']],
                         ['[D1:L1]', '[D1:L2]', '[D1:L4-L5]'])
        self.assertIn('5 筆資料列，符合條件 3 筆', document_calculation.describe(receipt))
        self.assertIn('[D1:L5] 125 g × 1/1000 = 0.125 公斤', document_calculation.describe(receipt))

    def test_markdown_average_uses_exact_sum_and_matched_row_count(self):
        self.attach('timing.md', '前言\n| 批次 | 時長 |\n| --- | ---: |\n| 甲 | 2秒 |\n| 甲 | 4秒 |\n| 甲 | 4秒 |\n| 乙 | 1小時 |')
        question = '文件問答：計算 [D1:L2-L7] 中「批次」為「甲」的「時長」平均值，換算成秒。'
        answer, receipt = self.ask(question, schema=document_table.AGGREGATE_RECEIPT)
        self.assertEqual(receipt['total_exact'], '10')
        self.assertEqual(receipt['result']['exact'], '10/3')
        self.assertIn('平均值為 10/3 秒，共 3 列', answer['content'])
        self.assertEqual([row['citation'] for row in answer['citation_checks']], ['[D1:L2]', '[D1:L4-L6]'])

    def test_aggregate_processes_full_bounded_table_without_truncating_to_fifty_rows(self):
        rows = '\n'.join(f'{n}\t0.125' for n in range(1, 200))
        self.attach('many.tsv', '序號\t費用(元)\n' + rows + '\n200\t10')
        stem = '文件問答：合計 [D1:L1-L{}] 中所有資料列的「費用」，單位為元。'
        result, receipt = self.ask(stem.format(200), schema=document_table.AGGREGATE_RECEIPT)
        self.assertEqual(receipt['result']['exact'], '199/8')
        self.assertEqual(len(receipt['observations']), 199)
        self.assertEqual([row['citation'] for row in result['citation_checks']], ['[D1:L1]', '[D1:L2-L200]'])
        _, limited = self.ask(stem.format(201), schema=document_table.AGGREGATE_RECEIPT)
        self.assertEqual(limited['selection_status'], 'range_too_large')
        self.assertIsNone(limited['result'])

    def test_invalid_selected_rows_block_total_and_distinguish_no_match_from_zero(self):
        self.attach('weights.csv', '批次,重量(kg)\n甲,4\n甲,未知\n甲,300g\n乙,0')
        stem = '文件問答：加總 [D1:L1-L5] 中「批次」為「{}」的「重量」，換算成公斤。'
        answer, blocked = self.ask(stem.format('甲'), schema=document_table.AGGREGATE_RECEIPT)
        self.assertIsNone(blocked['total_exact'])
        self.assertIsNone(blocked['result'])
        self.assertEqual([row['status'] for row in blocked['observations']], ['confirmed', 'value_unconfirmed', 'unit_conflict'])
        self.assertEqual([row['citation'] for row in answer['citation_checks']], ['[D1:L1]', '[D1:L3]', '[D1:L4]'])
        _, missing = self.ask(stem.format('丙'), schema=document_table.AGGREGATE_RECEIPT)
        self.assertEqual(missing['selection_status'], 'row_missing')
        self.assertIsNone(missing['result'])
        _, zero = self.ask(stem.format('乙'), schema=document_table.AGGREGATE_RECEIPT)
        self.assertEqual(zero['result']['exact'], '0')

    def test_aggregate_backup_recomputes_and_rejects_an_omitted_matched_row(self):
        self.attach('backup.csv', '批次,數量(件)\n甲,2\n乙,8\n甲,3')
        question = '文件問答：加總 [D1:L1-L4] 中「批次」為「甲」的「數量」，單位為件。'
        answer, receipt = self.ask(question, schema=document_table.AGGREGATE_RECEIPT)
        archive = create_backup(self.store.path, self.root / 'aggregate.paibackup')
        restore_backup(archive['path'], self.root / 'restored')
        self.assertEqual(document_calculation.read(Store(self.root / 'restored' / 'assistant.sqlite3'), answer['id']), receipt)
        context = self.store.answer_context(answer['id'])
        context[document_calculation.FIELD]['observations'].pop()
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', (json.dumps(context), answer['id']))
        with self.assertRaisesRegex(ValueError, '來源或結果'):
            create_backup(self.store.path, self.root / 'tampered-aggregate.paibackup')
