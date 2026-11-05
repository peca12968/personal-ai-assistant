import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import document_calculation, document_dates, document_table
from assistant.backup import create_backup, restore_backup
from assistant.engine import Assistant, EngineError
from assistant.store import Store
from scripts.daily_expenses import DailyAssistant


LEAP_CLOCK = {'local_datetime': '2024-03-01T00:15:00+08:00',
              'observed_at_utc': '2024-02-29T16:15:00+00:00'}
MONDAY_CLOCK = {'local_datetime': '2026-01-05T00:15:00+08:00',
                'observed_at_utc': '2026-01-04T16:15:00+00:00'}


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
        multi = '文件問答：加總 [D1:L1-L3]、[D2:L1-L3] 的「重量」，換算成公斤。'
        filtered = '文件問答：加總 [D1:L1-L3] 中「批次」為「甲」且「狀態」為「完成」的「重量」，換算成公斤。'
        numeric = '文件問答：加總 [D1:L1-L3] 中「重量」至少 2 公斤的「重量」，換算成公斤。'
        logical = filtered.replace('且', '或')
        dated = '文件問答：加總 [D1:L1-L3] 中「日期」晚於 2026-11-05 的「重量」，換算成公斤。'
        relative = dated.replace('晚於 2026-11-05 ', '在上週')
        for request, raw in ((stem + '換算成公斤。', True), (stem + '單位為公斤並上傳。', False),
                             (stem + '換算成公斤。接著刪除原始文件。', False),
                             (aggregate, True), (aggregate + '接著上傳原始文件。', False),
                             (multi, True), (multi + '接著上傳原始文件。', False),
                             (filtered, True), (filtered + '接著上傳原始文件。', False),
                             (filtered.replace('且', '且或'), False), (numeric, True), (logical, True),
                             (numeric.replace('2 公斤', '2'), False),
                             (numeric + '接著上傳原始文件。', False), (logical + '接著上傳原始文件。', False),
                             (dated, True), (dated + '接著上傳原始文件。', False),
                             (dated.replace('2026-11-05', '11/05'), False),
                             (relative, True), (relative + '接著上傳原始文件。', False)):
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

    def test_cross_table_mean_weights_rows_and_converts_before_calculation(self):
        self.attach('left.csv', '批次,時長(秒)\n甲,1\n乙,2\n丙,2')
        self.attach('right.md', '| 批次 | 時長 |\n| --- | --- |\n| 丁 | 1 min |')
        question = '文件問答：計算 [D1:L1-L4] 和 [D2:L1-L3] 的「時長」平均，換算成秒。'
        answer, receipt = self.ask(question, entry=DailyAssistant, schema=document_table.MULTI_AGGREGATE_RECEIPT)
        self.assertEqual(receipt['result']['exact'], '65/4')
        self.assertEqual(receipt['total_exact'], '65')
        self.assertEqual([len(group['observations']) for group in receipt['groups']], [3, 1])
        self.assertEqual([row['citation'] for row in answer['citation_checks']],
                         ['[D1:L1]', '[D1:L2-L4]', '[D2:L1]', '[D2:L3]'])
        self.assertIn('精確總和 65 秒 ÷ 4', document_calculation.describe(receipt))
        self.assertIn('[D2:L3] 1 min × 60 = 60 秒', document_calculation.describe(receipt))

    def test_cross_table_allows_an_empty_filter_and_distinguishes_global_no_match(self):
        self.attach('left.csv', '倉別,重量(kg)\n北,0\n北,2\nＡ,11')
        self.attach('right.tsv', '重量(kg)\t倉別\n100\tＡ\n900\t南')
        stem = '文件問答：加總 [D1:L1-L4]、[D2:L1-L3] 中「倉別」為「{}」的「重量」，換算成公斤。'
        _, receipt = self.ask(stem.format('北'), schema=document_table.MULTI_AGGREGATE_RECEIPT)
        self.assertEqual(receipt['result']['exact'], '2')
        self.assertEqual([group['selection_status'] for group in receipt['groups']], ['selected', 'row_missing'])
        _, missing = self.ask(stem.format('A'), schema=document_table.MULTI_AGGREGATE_RECEIPT)
        self.assertEqual(missing['status'], 'no_match')
        self.assertIsNone(missing['result'])

    def test_cross_table_invalid_or_missing_sources_never_yield_partial_totals(self):
        self.attach('left.csv', '倉別,重量(kg)\n北,3')
        stem = '文件問答：合計 [D1:L1-L2]、[D2:L1-L2] 的「重量」，換算成公斤。'
        answer, missing = self.ask(stem, schema=document_table.MULTI_AGGREGATE_RECEIPT)
        self.assertEqual(missing['groups'][1]['selection_status'], 'document_not_attached')
        self.assertEqual(missing['status'], 'source_blocked')
        self.assertIsNone(missing['total_exact'])
        self.assertNotIn('[D2:', answer['content'])
        self.attach('right.csv', '倉別,重量(kg)\n北,375g')
        answer, conflict = self.ask(stem, schema=document_table.MULTI_AGGREGATE_RECEIPT)
        self.assertEqual(conflict['groups'][1]['observations'][0]['status'], 'unit_conflict')
        self.assertIsNone(conflict['result'])
        self.assertIn('[D2:L2]', answer['content'])

    def test_cross_table_rejects_overlapping_physical_rows_including_identical_copies(self):
        table = '樣本,金額(元)\n甲,2\n乙,3\n樣本,金額(元)\n丙,7'
        self.attach('a.csv', table)
        self.attach('copy.csv', table)
        stem = '文件問答：加總 [D1:L1-L3]、{} 的「金額」，單位為元。'
        for citation in ('[D1:L1-L2]', '[D1:L1-L3]', '[D2:L1-L3]'):
            with self.subTest(citation=citation):
                _, receipt = self.ask(stem.format(citation), schema=document_table.MULTI_AGGREGATE_RECEIPT)
                self.assertEqual(receipt['status'], 'range_overlap')
                self.assertEqual(receipt['overlaps'], [[0, 1]])
                self.assertIsNone(receipt['result'])
        _, disjoint = self.ask(stem.format('[D1:L4-L5]'), schema=document_table.MULTI_AGGREGATE_RECEIPT)
        self.assertEqual(disjoint['result']['exact'], '12')

    def test_cross_table_verifies_four_ranges_and_rejects_dropped_group_in_backup(self):
        for number in range(1, 5):
            self.attach(f'{number}.csv', f'項目,費用(元)\n第{number}份,{number}.5')
        question = '文件問答：合計 [D4:L1-L2]、[D2:L1-L2] 與 [D1:L1-L2] 和 [D3:L1-L2] 的「費用」，單位為元。'
        answer, receipt = self.ask(question, schema=document_table.MULTI_AGGREGATE_RECEIPT)
        self.assertEqual(receipt['result']['exact'], '12')
        self.assertEqual([group['observations'][0]['row_citation'] for group in receipt['groups']],
                         ['[D4:L2]', '[D2:L2]', '[D1:L2]', '[D3:L2]'])
        self.assertIsNone(document_calculation.parse_live_request(question.replace('[D3:L1-L2]', '[D3:L1-L2]、[D5:L1-L2]')))
        archive = create_backup(self.store.path, self.root / 'multi.paibackup')
        restore_backup(archive['path'], self.root / 'restored')
        self.assertEqual(document_calculation.read(Store(self.root / 'restored' / 'assistant.sqlite3'), answer['id']), receipt)
        context = self.store.answer_context(answer['id'])
        context[document_calculation.FIELD]['groups'].pop()
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', (json.dumps(context), answer['id']))
        with self.assertRaisesRegex(ValueError, '來源或結果'):
            create_backup(self.store.path, self.root / 'tampered-multi.paibackup')

    def test_multi_column_filters_use_exact_unicode_and_all_conditions(self):
        self.attach('weights.csv', '倉別,狀態,區域,品項,重量\nＡ,已完成,東,木且紙,3kg\nA,已完成,東,木且紙,99kg\nＡ,取消,東,木且紙,未知')
        question = ('文件問答：加總 [D1:L1-L4] 中「倉別」為「Ａ」且「狀態」不為「取消」'
                    '且「區域」為「東」且「品項」為「木且紙」的「重量」，換算成公斤。')
        answer, receipt = self.ask(question, entry=DailyAssistant, schema=document_table.FILTER_AGGREGATE_RECEIPT)
        self.assertEqual(receipt['result']['exact'], '3')
        self.assertEqual(len(receipt['plan']['filters']), 4)
        group, = receipt['groups']
        self.assertEqual([row['status'] for row in group['decisions']], ['matched', 'excluded', 'excluded'])
        self.assertEqual([test['status'] for test in group['decisions'][2]['tests']], ['true', 'false', 'true', 'true'])
        self.assertEqual([row['citation'] for row in answer['citation_checks']], ['[D1:L1]', '[D1:L2]'])
        self.assertIn('第 3 行 · 排除', document_calculation.describe(receipt))

    def test_false_condition_excludes_blank_rows_but_potential_matches_block_result(self):
        self.attach('shipment.csv', '倉別,狀態,箱數(箱)\n北,完成,5\n南,,未知\n北,,9')
        stem = '文件問答：合計 [D1:L1-L{}] 中「狀態」不為「取消」且「倉別」為「北」的「箱數」，單位為箱。'
        _, ready = self.ask(stem.format(3), schema=document_table.FILTER_AGGREGATE_RECEIPT)
        self.assertEqual(ready['result']['exact'], '5')
        self.assertEqual(ready['groups'][0]['decisions'][1]['status'], 'excluded')
        answer, blocked = self.ask(stem.format(4), schema=document_table.FILTER_AGGREGATE_RECEIPT)
        self.assertIsNone(blocked['total_exact'])
        group, = blocked['groups']
        self.assertEqual(group['selection_status'], 'filter_unknown')
        self.assertEqual([row['status'] for row in group['decisions']], ['matched', 'excluded', 'unknown'])
        self.assertIn('[D1:L4] 「狀態」空白', answer['content'])
        self.assertEqual(answer['citation_checks'], [{'citation': '[D1:L4]', 'range_valid': True}])

    def test_multi_filter_cross_table_mean_preserves_each_source_and_denominator(self):
        self.attach('first.csv', '班別,狀態,時長(秒)\n早,完成,1\n早,完成,3\n早,取消,100')
        self.attach('second.tsv', '時長\t狀態\t班別\n1 min\t完成\t早\n10秒\t完成\t晚')
        question = ('文件問答：計算 [D1:L1-L4] 與 [D2:L1-L3] 中「班別」為「早」'
                    '且「狀態」不為「取消」的「時長」平均，換算成秒。')
        answer, receipt = self.ask(question, schema=document_table.FILTER_AGGREGATE_RECEIPT)
        self.assertEqual(receipt['result']['exact'], '64/3')
        self.assertEqual([len(group['observations']) for group in receipt['groups']], [2, 1])
        self.assertEqual([row['citation'] for row in answer['citation_checks']],
                         ['[D1:L1]', '[D1:L2-L3]', '[D2:L1]', '[D2:L2]'])
        self.assertIn('精確總和 64 秒 ÷ 3', document_calculation.describe(receipt))

    def test_missing_condition_column_blocks_cross_table_result(self):
        self.attach('first.csv', '倉別,狀態,箱數(箱)\n北,完成,5')
        self.attach('second.csv', '倉別,箱數(箱)\n南,99')
        question = ('文件問答：合計 [D1:L1-L2]、[D2:L1-L2] 中「倉別」為「北」'
                    '且「狀態」為「完成」的「箱數」，單位為箱。')
        _, receipt = self.ask(question, schema=document_table.FILTER_AGGREGATE_RECEIPT)
        self.assertEqual(receipt['status'], 'source_blocked')
        self.assertEqual(receipt['groups'][1]['selection_status'], 'column_missing')
        self.assertEqual(receipt['groups'][1]['decisions'], [])
        self.assertIsNone(receipt['result'])

    def test_filter_backup_rechecks_excluded_rows_and_raw_evidence(self):
        self.attach('table.csv', '倉別,狀態,箱數(箱)\n北,完成,5\n南,完成,99')
        question = ('文件問答：合計 [D1:L1-L3] 中「倉別」為「北」'
                    '且「狀態」為「完成」的「箱數」，單位為箱。')
        answer, receipt = self.ask(question, schema=document_table.FILTER_AGGREGATE_RECEIPT)
        archive = create_backup(self.store.path, self.root / 'filtered.paibackup')
        restore_backup(archive['path'], self.root / 'restored')
        self.assertEqual(document_calculation.read(Store(self.root / 'restored' / 'assistant.sqlite3'), answer['id']), receipt)
        context = self.store.answer_context(answer['id'])
        context[document_calculation.FIELD]['groups'][0]['decisions'][1]['tests'][0]['value'] = '北'
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', (json.dumps(context), answer['id']))
        with self.assertRaisesRegex(ValueError, '來源或結果'):
            create_backup(self.store.path, self.root / 'tampered-filter.paibackup')

    def test_numeric_bounds_on_one_column_use_exact_units_and_operator_boundaries(self):
        self.attach('weights.csv', '重量,狀態,箱數(箱)\n1000g,完成,2\n1.5kg,完成,3\n1501g,完成,98\n999g,完成,99\n1200g,取消,100')
        stem = ('文件問答：加總 [D1:L1-L6] 中「重量」{} １ 公斤且「重量」至多 1.5 公斤'
                '且「狀態」為「完成」的「箱數」，單位為箱。')
        answer, inclusive = self.ask(stem.format('至少'), entry=DailyAssistant, schema=document_table.NUMERIC_AGGREGATE_RECEIPT)
        self.assertEqual(inclusive['result']['exact'], '5')
        self.assertEqual([row['citation'] for row in answer['citation_checks']], ['[D1:L1]', '[D1:L2-L3]'])
        tests = inclusive['groups'][0]['decisions'][0]['tests']
        self.assertEqual([test['status'] for test in tests], ['true', 'true', 'true'])
        self.assertEqual(tests[0]['factor_exact'], '1/1000')
        self.assertEqual(tests[0]['normalized_exact'], '1')
        _, strict = self.ask(stem.format('>'), schema=document_table.NUMERIC_AGGREGATE_RECEIPT)
        self.assertEqual(strict['result']['exact'], '3')

    def test_numeric_equality_and_cross_table_mean_use_exact_row_values(self):
        self.attach('left.csv', '重量,時長(秒)\n300g,2\n0.30000001kg,100')
        self.attach('right.tsv', '時長(秒)\t重量(公克)\n3\t300\n100\t301')
        question = '文件問答：計算 [D1:L1-L3] 與 [D2:L1-L3] 中「重量」等於 0.3 公斤的「時長」平均，換算成秒。'
        answer, receipt = self.ask(question, schema=document_table.NUMERIC_AGGREGATE_RECEIPT)
        self.assertEqual(receipt['result']['exact'], '5/2')
        self.assertEqual([row['citation'] for row in answer['citation_checks']],
                         ['[D1:L1]', '[D1:L2]', '[D2:L1]', '[D2:L2]'])
        self.assertEqual(receipt['groups'][1]['decisions'][0]['tests'][0]['header_unit'], '公克')
        self.assertIn('比較值 3/10 公斤', document_calculation.describe(receipt))

    def test_unknown_numeric_units_have_separate_reasons_and_do_not_guess(self):
        self.attach('weights.csv', '重量,箱數(箱)\n2kg,3\n2L,100\n4,100\n,100\n約2kg,100')
        question = '文件問答：加總 [D1:L1-L6] 中「重量」至少 2 公斤的「箱數」，單位為箱。'
        answer, receipt = self.ask(question, schema=document_table.NUMERIC_AGGREGATE_RECEIPT)
        self.assertIsNone(receipt['result'])
        decisions = receipt['groups'][0]['decisions']
        self.assertEqual([row['status'] for row in decisions], ['matched', 'unknown', 'unknown', 'unknown', 'unknown'])
        self.assertEqual([row['tests'][0]['reason'] for row in decisions[1:]],
                         ['filter_unit_incompatible', 'unit_missing', 'filter_blank', 'value_unconfirmed'])
        self.assertIn('[D1:L3] 「重量」來源單位與比較門檻的單位不相容', answer['content'])
        self.assertIn('[D1:L5] 「重量」欄位空白', answer['content'])

    def test_nonconverted_count_threshold_and_known_false_exclude_unconfirmed_data(self):
        self.attach('stock.csv', '狀態,箱數(箱),金額(元)\n完成,5,12.5\n取消,5件,未知\n,2,未知')
        question = ('文件問答：合計 [D1:L1-L4] 中「箱數」大於 2 箱且「狀態」為「完成」的「金額」，單位為元。')
        _, receipt = self.ask(question, schema=document_table.NUMERIC_AGGREGATE_RECEIPT)
        self.assertEqual(receipt['result']['exact'], '25/2')
        decisions = receipt['groups'][0]['decisions']
        self.assertEqual([row['status'] for row in decisions], ['matched', 'excluded', 'excluded'])
        self.assertEqual(decisions[1]['tests'][0]['reason'], 'unit_conflict')
        self.assertEqual(decisions[2]['tests'][1]['status'], 'unknown')

    def test_numeric_threshold_backup_rechecks_converted_predicate_evidence(self):
        self.attach('stock.csv', '重量(g),數量(件)\n200,7\n150,9')
        question = '文件問答：合計 [D1:L1-L3] 中「重量」≥ 0.2 公斤的「數量」，單位為件。'
        answer, receipt = self.ask(question, schema=document_table.NUMERIC_AGGREGATE_RECEIPT)
        archive = create_backup(self.store.path, self.root / 'numeric.paibackup')
        restore_backup(archive['path'], self.root / 'restored')
        self.assertEqual(document_calculation.read(Store(self.root / 'restored' / 'assistant.sqlite3'), answer['id']), receipt)
        context = self.store.answer_context(answer['id'])
        context[document_calculation.FIELD]['groups'][0]['decisions'][1]['tests'][0]['normalized_exact'] = '1/5'
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', (json.dumps(context), answer['id']))
        with self.assertRaisesRegex(ValueError, '來源或結果'):
            create_backup(self.store.path, self.root / 'tampered-threshold.paibackup')

    def test_or_includes_each_row_once_when_either_branch_is_known_true(self):
        self.attach('deliveries.csv', '倉別,急件,箱數(箱)\n北,是,4\n北,,3\n,是,2\n南,否,99')
        question = '文件問答：合計 [D1:L1-L5] 中「倉別」為「北」或「急件」為「是」的「箱數」，單位為箱。'
        answer, receipt = self.ask(question, entry=DailyAssistant, schema=document_table.LOGIC_AGGREGATE_RECEIPT)
        self.assertEqual(receipt['result']['exact'], '9')
        group, = receipt['groups']
        self.assertEqual([row['status'] for row in group['decisions']], ['matched', 'matched', 'matched', 'excluded'])
        self.assertEqual([row['unresolved'] for row in group['decisions']], [[], [], [], []])
        self.assertEqual([row['citation'] for row in answer['citation_checks']], ['[D1:L1]', '[D1:L2-L4]'])

    def test_parentheses_override_and_precedence_without_splitting_quoted_values(self):
        self.attach('deliveries.csv', '倉別,品項,急件,箱數(箱)\n北,木或紙,否,4\n北,其他,是,5\n南,木或紙,是,9')
        stem = '文件問答：合計 [D1:L1-L4] 中{}的「箱數」，單位為箱。'
        _, default = self.ask(stem.format('「倉別」為「北」且「品項」為「木或紙」或「急件」為「是」'),
                              schema=document_table.LOGIC_AGGREGATE_RECEIPT)
        _, grouped = self.ask(stem.format('「倉別」為「北」且（「品項」為「木或紙」或「急件」為「是」）'),
                              schema=document_table.LOGIC_AGGREGATE_RECEIPT)
        self.assertEqual(default['result']['exact'], '18')
        self.assertEqual(grouped['result']['exact'], '9')
        self.assertIn('條件結構 （（條件 1 且 條件 2） 或 條件 3）', document_calculation.describe(default))
        self.assertEqual(default['plan']['filters'][1]['value'], '木或紙')

    def test_or_with_numeric_comparison_keeps_operator_and_unknown_unit_separate(self):
        self.attach('deliveries.csv', '重量,急件,數量(件)\n2000g,否,3\n2L,是,4\n1.999kg,否,99')
        question = '文件問答：合計 [D1:L1-L4] 中「重量」大於或等於 2 公斤或「急件」為「是」的「數量」，單位為件。'
        _, receipt = self.ask(question, schema=document_table.LOGIC_AGGREGATE_RECEIPT)
        self.assertEqual(receipt['result']['exact'], '7')
        self.assertEqual(receipt['plan']['filters'][0]['operator'], 'ge')
        decisions = receipt['groups'][0]['decisions']
        self.assertEqual(decisions[1]['tests'][0]['reason'], 'filter_unit_incompatible')
        self.assertEqual(decisions[1]['status'], 'matched')

    def test_unresolved_or_reports_only_relevant_leaves_and_backup_rechecks_them(self):
        self.attach('deliveries.csv', '倉別,狀態,急件,箱數(箱)\n北,核准,否,2\n南,,,99')
        question = ('文件問答：合計 [D1:L1-L3] 中（「倉別」為「北」且「狀態」為「核准」）'
                    '或「急件」為「是」的「箱數」，單位為箱。')
        answer, receipt = self.ask(question, schema=document_table.LOGIC_AGGREGATE_RECEIPT)
        self.assertIsNone(receipt['result'])
        row = receipt['groups'][0]['decisions'][1]
        self.assertEqual([test['status'] for test in row['tests']], ['false', 'unknown', 'unknown'])
        self.assertEqual(row['unresolved'], [2])
        self.assertIn('[D1:L3] 「急件」空白', answer['content'])
        self.assertNotIn('「狀態」', answer['content'])
        archive = create_backup(self.store.path, self.root / 'logic.paibackup')
        restore_backup(archive['path'], self.root / 'restored')
        self.assertEqual(document_calculation.read(Store(self.root / 'restored' / 'assistant.sqlite3'), answer['id']), receipt)
        context = self.store.answer_context(answer['id'])
        context[document_calculation.FIELD]['groups'][0]['decisions'][1]['unresolved'] = [1, 2]
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', (json.dumps(context), answer['id']))
        with self.assertRaisesRegex(ValueError, '來源或結果'):
            create_backup(self.store.path, self.root / 'tampered-logic.paibackup')

    def test_boolean_parser_enforces_expression_limits_and_whole_message_scope(self):
        atom = '「倉別」為「北」'
        stem = '文件問答：加總 [D1:L1-L3] 中{}的「箱數」，單位為箱。'
        self.assertEqual(document_calculation.parse_live_request(stem.format('（' * 4 + atom + '）' * 4))['schema'],
                         document_table.LOGIC_AGGREGATE_PLAN)
        self.assertEqual(len(document_calculation.parse_live_request(stem.format('或'.join([atom] * 8)))['filters']), 8)
        for expression in ('（' * 5 + atom + '）' * 5, '或'.join([atom] * 9), atom + '或',
                           f'（{atom}或{atom}', f'{atom}或執行命令'):
            with self.subTest(expression=expression):
                self.assertIsNone(document_calculation.parse_live_request(stem.format(expression)))

    def test_date_interval_includes_both_boundaries_and_normalizes_source_dates(self):
        self.attach('dates.csv', '日期,箱數(箱)\n2024-02-28,99\n2024-02-29,3\n2024/3/1,4\n2024年3月2日,99')
        question = '文件問答：合計 [D1:L1-L5] 中「日期」從 2024/2/29 到 2024年3月1日 的「箱數」，單位為箱。'
        answer, receipt = self.ask(question, entry=DailyAssistant, schema=document_table.DATE_AGGREGATE_RECEIPT)
        self.assertEqual(receipt['result']['exact'], '7')
        self.assertEqual([row['citation'] for row in answer['citation_checks']], ['[D1:L1]', '[D1:L3-L4]'])
        self.assertEqual(receipt['plan']['filters'][0], dict(column='日期', operator='between', date='2024-02-29', end_date='2024-03-01'))
        self.assertEqual(receipt['groups'][0]['decisions'][2]['tests'][0]['normalized_date'], '2024-03-01')

    def test_date_comparisons_distinguish_strict_and_inclusive_at_cutoff(self):
        self.attach('dates.csv', '日期,箱數(箱)\n2026-11-04,2\n2026/11/5,3\n2026-11-06,4')
        stem = '文件問答：合計 [D1:L1-L4] 中「日期」{} 2026-11-05 的「箱數」，單位為箱。'
        for operator, exact in (('早於', '2'), ('不晚於', '5'), ('晚於', '4'), ('不早於', '7'), ('等於', '3'), ('不等於', '6')):
            with self.subTest(operator=operator):
                _, receipt = self.ask(stem.format(operator), schema=document_table.DATE_AGGREGATE_RECEIPT)
                self.assertEqual(receipt['result']['exact'], exact)

    def test_cross_table_date_logic_combines_text_and_numeric_conditions(self):
        self.attach('morning.csv', '日期,重量(kg),急件,時長(秒)\n2026-11-04,2,否,6\n2026-11-05,2,否,99')
        self.attach('evening.tsv', '時長(秒)\t急件\t日期\t重量\n10\t是\t未定\t未知\n99\t否\t2026/11/06\t2000g')
        question = ('文件問答：計算 [D1:L1-L3] 與 [D2:L1-L3] 中（「日期」早於 2026-11-05'
                    '且「重量」至少 2 公斤）或「急件」為「是」的「時長」平均，換算成秒。')
        answer, receipt = self.ask(question, schema=document_table.DATE_AGGREGATE_RECEIPT)
        self.assertEqual(receipt['result']['exact'], '8')
        self.assertEqual([row['citation'] for row in answer['citation_checks']], ['[D1:L1]', '[D1:L2]', '[D2:L1]', '[D2:L2]'])
        self.assertEqual(receipt['groups'][1]['decisions'][0]['tests'][0]['reason'], 'filter_date_invalid')
        self.assertEqual(receipt['groups'][1]['decisions'][0]['unresolved'], [])

    def test_unknown_source_dates_preserve_reason_and_do_not_guess(self):
        self.attach('dates.csv', '日期,箱數(箱)\n2025-03-01,5\n2025-02-29,99\n03/01,99\n2025-03-01T01:00,99\n,99')
        question = '文件問答：合計 [D1:L1-L6] 中「日期」不早於 2025-02-28 的「箱數」，單位為箱。'
        answer, receipt = self.ask(question, schema=document_table.DATE_AGGREGATE_RECEIPT)
        self.assertIsNone(receipt['result'])
        decisions = receipt['groups'][0]['decisions']
        self.assertEqual([row['tests'][0]['reason'] for row in decisions[1:]], ['filter_date_invalid'] * 3 + ['filter_blank'])
        self.assertIn('[D1:L3] 「日期」不是可確認的完整公曆日期', answer['content'])
        self.assertIn('[D1:L6] 「日期」欄位空白', answer['content'])

    def test_invalid_requested_dates_or_reversed_bounds_stop_before_filtering(self):
        self.attach('dates.csv', '日期,箱數(箱)\n2025-03-01,5')
        stem = '文件問答：合計 [D1:L1-L2] 中「日期」{} 的「箱數」，單位為箱。'
        for condition, diagnostic in (('等於 2025-02-29', '2025-02-29 不是有效的公曆日期'),
                                      ('自 2025-03-05 至 2025-03-01', '起日 2025-03-05 晚於迄日 2025-03-01')):
            with self.subTest(condition=condition):
                answer, receipt = self.ask(stem.format(condition), schema=document_table.DATE_AGGREGATE_RECEIPT)
                self.assertEqual(receipt['status'], 'invalid_date_condition')
                self.assertEqual(receipt['groups'], [])
                self.assertIsNone(receipt['result'])
                self.assertIn(diagnostic, answer['content'])

    def test_date_backup_recomputes_normalized_dates(self):
        self.attach('dates.csv', '日期,箱數(箱)\n2024年2月29日,3\n2024-03-01,4')
        question = '文件問答：合計 [D1:L1-L3] 中「日期」等於 2024-02-29 的「箱數」，單位為箱。'
        answer, receipt = self.ask(question, schema=document_table.DATE_AGGREGATE_RECEIPT)
        self.assertIn('比較日期 2024-02-29', document_calculation.describe(receipt))
        archive = create_backup(self.store.path, self.root / 'date-filter.paibackup')
        restore_backup(archive['path'], self.root / 'restored')
        self.assertEqual(document_calculation.read(Store(self.root / 'restored' / 'assistant.sqlite3'), answer['id']), receipt)
        context = self.store.answer_context(answer['id'])
        context[document_calculation.FIELD]['groups'][0]['decisions'][1]['tests'][0]['normalized_date'] = '2024-02-29'
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', (json.dumps(context), answer['id']))
        with self.assertRaisesRegex(ValueError, '來源或結果'):
            create_backup(self.store.path, self.root / 'tampered-date-filter.paibackup')

    def test_relative_range_and_strict_cutoff_share_one_local_observation(self):
        self.attach('dates.csv', '日期,箱數(箱)\n2024-02-27,99\n2024年2月28日,2\n2024-02-29,3\n2024/3/1,4\n2024-03-02,99')
        question = ('文件問答：合計 [D1:L1-L6] 中「日期」從前天到今天且「日期」早於明天'
                    '的「箱數」，單位為箱。')
        with patch.object(document_dates, 'observe_local_clock', return_value=LEAP_CLOCK) as clock:
            answer, receipt = self.ask(question, entry=DailyAssistant, schema=document_table.RELATIVE_DATE_AGGREGATE_RECEIPT)
        clock.assert_called_once_with()
        self.assertEqual(receipt['result']['exact'], '9')
        self.assertEqual(receipt['plan']['date_observation'], LEAP_CLOCK)
        first, second = receipt['plan']['filters']
        self.assertEqual((first['date'], first['end_date'], second['date']), ('2024-02-28', '2024-03-01', '2024-03-02'))
        self.assertIn('電腦本地日期 2024-03-01', answer['content'])
        self.assertIn('早於 2024-03-02', answer['content'])
        self.assertEqual([row['citation'] for row in answer['citation_checks']], ['[D1:L1]', '[D1:L3-L5]'])

    def test_relative_weeks_use_monday_and_sunday_across_two_years_and_tables(self):
        self.attach('left.csv', '日期,急件,時長(秒)\n2025-12-28,否,99\n2025-12-29,否,6\n2026-01-05,否,99')
        self.attach('right.tsv', '急件\t日期\t時長(秒)\n是\t未定\t10\n否\t2026-01-04\t8\n否\t2026-01-11\t7')
        stem = ('文件問答：計算 [D1:L1-L4] 與 [D2:L1-L4] 中「日期」在{}或「急件」為「是」'
                '的「時長」平均，單位為秒。')
        with patch.object(document_dates, 'observe_local_clock', return_value=MONDAY_CLOCK):
            _, last = self.ask(stem.format('上週'), schema=document_table.RELATIVE_DATE_AGGREGATE_RECEIPT)
            _, current = self.ask(stem.format('這週'), schema=document_table.RELATIVE_DATE_AGGREGATE_RECEIPT)
        self.assertEqual((last['result']['exact'], current['result']['exact']), ('8', '116/3'))
        self.assertIn('2025-12-29 到 2026-01-04', last['output'])
        self.assertIn('2026-01-05 到 2026-01-11', current['output'])
        self.assertEqual(last['groups'][1]['decisions'][0]['unresolved'], [])

    def test_relative_months_include_whole_calendar_period_and_leap_day(self):
        self.attach('dates.csv', '日期,箱數(箱)\n2024-01-31,99\n2024-02-01,2\n2024-02-29,5\n2024-03-01,3\n2024-03-31,4\n2024-04-01,99')
        stem = '文件問答：合計 [D1:L1-L7] 中「日期」{}的「箱數」，單位為箱。'
        with patch.object(document_dates, 'observe_local_clock', return_value=LEAP_CLOCK):
            for term, start, end in (('在上個月', '2024-02-01', '2024-02-29'),
                                     ('本月', '2024-03-01', '2024-03-31')):
                with self.subTest(term=term):
                    _, receipt = self.ask(stem.format(term), schema=document_table.RELATIVE_DATE_AGGREGATE_RECEIPT)
                    self.assertEqual(receipt['result']['exact'], '7')
                    self.assertIn(f'{start} 到 {end}', receipt['output'])

    def test_relative_comparisons_mixed_bounds_and_invalid_range(self):
        self.attach('dates.csv', '日期,箱數(箱)\n2024-02-29,2\n2024-03-01,3\n2024-03-02,5')
        stem = '文件問答：合計 [D1:L1-L4] 中「日期」{}的「箱數」，單位為箱。'
        with patch.object(document_dates, 'observe_local_clock', return_value=LEAP_CLOCK):
            for term, exact in (('是昨天', '2'), ('不早於今天', '8'), ('自2024/2/29至今天', '5')):
                with self.subTest(term=term):
                    _, receipt = self.ask(stem.format(term), schema=document_table.RELATIVE_DATE_AGGREGATE_RECEIPT)
                    self.assertEqual(receipt['result']['exact'], exact)
            answer, receipt = self.ask(stem.format('從明天到昨天'), schema=document_table.RELATIVE_DATE_AGGREGATE_RECEIPT)
        self.assertIn('起日 2024-03-02 晚於迄日 2024-02-29', answer['content'])
        self.assertEqual(receipt['status'], 'invalid_date_condition')
        self.assertIsNone(receipt['result'])
        self.assertEqual(receipt['groups'], [])

    def test_only_supported_relative_requests_read_clock_and_quoted_values_are_literal(self):
        stem = '文件問答：合計 [D1:L1-L2] 中「日期」{}的「箱數」，單位為箱。'
        with patch.object(document_dates, 'observe_local_clock', side_effect=AssertionError('unexpected clock read')):
            for term in ('在最近', '在上週五', '等於台北時間今天', '從上週到本週', '在本週，依工作日'):
                with self.subTest(term=term):
                    self.assertIsNone(document_calculation.parse_live_request(stem.format(term)))
            literal = document_calculation.parse_live_request(stem.format('為「今天」或「日期」為「昨天」'))
            absolute = document_calculation.parse_live_request(stem.format('等於2024-02-29'))
            relative = document_calculation.parse_live_request(stem.format('是今天'))
        self.assertEqual(literal['schema'], document_table.LOGIC_AGGREGATE_PLAN)
        self.assertEqual([item['value'] for item in literal['filters']], ['今天', '昨天'])
        self.assertEqual(absolute['schema'], document_table.DATE_AGGREGATE_PLAN)
        self.assertEqual(relative['schema'], document_table.RELATIVE_DATE_AGGREGATE_PLAN)
        self.assertNotIn('date_observation', relative)

    def test_relative_backup_uses_original_clock_and_rejects_clock_edits(self):
        self.attach('dates.csv', '日期,箱數(箱)\n2024-02-29,7\n2024-03-01,99')
        question = '文件問答：合計 [D1:L1-L3] 中「日期」是昨天的「箱數」，單位為箱。'
        with patch.object(document_dates, 'observe_local_clock', return_value=LEAP_CLOCK):
            answer, receipt = self.ask(question, schema=document_table.RELATIVE_DATE_AGGREGATE_RECEIPT)
        with patch.object(document_dates, 'observe_local_clock', side_effect=AssertionError('historical clock read')):
            archive = create_backup(self.store.path, self.root / 'relative.paibackup')
            restore_backup(archive['path'], self.root / 'restored-relative')
            restored = Store(self.root / 'restored-relative' / 'assistant.sqlite3')
            self.assertEqual(document_calculation.read(restored, answer['id']), receipt)
            self.assertEqual(receipt['result']['exact'], '7')

        context = self.store.answer_context(answer['id'])
        observation = context[document_calculation.FIELD]['plan']['date_observation']
        observation['local_datetime'] = '2024-03-01T00:16:00+08:00'
        with self.assertRaisesRegex(ValueError, '來源或結果'):
            document_calculation.validate(context[document_calculation.FIELD], answer['content'], question)
        observation['observed_at_utc'] = '2024-02-29T16:16:00+00:00'
        with self.store.connect() as db:
            db.execute('UPDATE answer_contexts SET payload=? WHERE message_id=?', (json.dumps(context), answer['id']))
        with self.assertRaisesRegex(ValueError, '與已完成流程不符'):
            create_backup(self.store.path, self.root / 'tampered-relative.paibackup')

    def test_retry_uses_original_relative_date_instead_of_a_new_clock_read(self):
        self.attach('dates.csv', '日期,箱數(箱)\n2024-02-29,7\n2024-03-01,99')
        app = DailyAssistant(self.store, NoModel())
        question = '文件問答：合計 [D1:L1-L3] 中「日期」是昨天的「箱數」，單位為箱。'
        with (patch.object(document_dates, 'observe_local_clock', return_value=LEAP_CLOCK),
              patch.object(document_calculation, 'evaluate', side_effect=EngineError('已停止', 'cancelled'))):
            with self.assertRaises(EngineError):
                app.ask(self.sid, question)
        with patch.object(document_dates, 'observe_local_clock', side_effect=AssertionError('retry clock read')):
            answer = app.retry(self.sid)
        receipt = document_calculation.read(self.store, answer['id'])
        self.assertEqual(receipt['plan']['date_observation'], LEAP_CLOCK)
        self.assertEqual(receipt['result']['exact'], '7')
        self.assertEqual([message['content'] for message in self.store.messages(self.sid) if message['role'] == 'user'],
                         [question])
