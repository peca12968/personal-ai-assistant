import tempfile
import unittest
from pathlib import Path

from assistant.document_ranges import requested_ranges, range_status_text
from assistant.documents import retrieve_documents, check_citations
from assistant.engine import Assistant
from assistant.store import Store


class DocumentRangeTests(unittest.TestCase):
    def documents(self, text=None):
        return [{'id': 1, 'name': 'notes.txt', 'sha256': 'v1',
                 'content': text or ('一般背景。\n' * 29 +
                     '備份保留天數：一般案件十四天。\n\n\n待審案件必須保全至結案。')}] + [
            {'id': i, 'name': 'notes.txt', 'sha256': f'v{i}', 'content': f'其他來源 {i}。'}
            for i in range(2, 9)]

    def test_supported_selectors_normalize_width_deduplicate_and_preserve_order(self):
        for text in ['[D1:L33-L35]', 'Ｄ１ 第３３到３５行', 'D1 的第33至第35行',
                     'D1 第33-35行', 'd1 lines 33 to 35', 'D1 lines 33–35']:
            with self.subTest(text=text):
                rows, limited = requested_ranges(text + ' [D1:L33-L35] [D2:L1]')
                self.assertFalse(limited)
                self.assertEqual(rows, [{'document_id': 1, 'start_line': 33, 'end_line': 35},
                                        {'document_id': 2, 'start_line': 1, 'end_line': 1}])

    def test_ambiguous_or_malformed_text_does_not_guess_a_file_or_partial_number(self):
        for text in ['第33行', 'notes.txt 第33行', '[D1:L3-L99999999999999]',
                     'D1 lines 3-99999999999999', 'D1 line 3.5', 'XD1 line 3',
                     '[D1:L3-L]', 'D1 第三十三行']:
            with self.subTest(text=text):
                self.assertEqual(requested_ranges(text), ([], False))

    def test_requested_line_wins_over_topic_words_and_keeps_other_files(self):
        result = retrieve_documents('查看 D1 第33行，確認備份保留天數的例外。', self.documents())
        row = next(r for r in result['excerpts'] if r.get('requested_range'))
        self.assertEqual(row['text'], 'L33: 待審案件必須保全至結案。')
        self.assertEqual(row['sha256'], 'v1')
        self.assertEqual({r['document_id'] for r in result['excerpts']}, set(range(1, 9)))
        self.assertEqual(result['requested_ranges'][0]['status'], 'provided')
        self.assertTrue(check_citations('[D1:L33]', result['excerpts'])[0]['range_valid'])
        self.assertFalse(check_citations('[D2:L33]', result['excerpts'])[0]['range_valid'])

    def test_whole_requested_range_and_contained_repeat_need_one_slot(self):
        result = retrieve_documents('比較 [D1:L1-L33] 與 [D1:L30-L33]', self.documents(), max_excerpts=1)
        self.assertEqual(len(result['excerpts']), 1)
        self.assertEqual([r['status'] for r in result['requested_ranges']], ['provided', 'provided'])
        self.assertEqual(len(result['excerpts'][0]['text'].splitlines()), 33)
        self.assertTrue(result['coverage_limited'])  # The other seven documents were not shown.
        alone = retrieve_documents('[D1:L1-L33]', self.documents()[:1], max_excerpts=1)
        self.assertFalse(alone['coverage_limited'])

    def test_missing_reversed_out_of_bounds_and_too_large_ranges_are_explicit(self):
        result = retrieve_documents('[D999:L1] [D1:L0] [D1:L3-L2] [D1:L99] [D1:L1-L201]', self.documents())
        self.assertEqual([r['status'] for r in result['requested_ranges']],
                         ['document_not_attached', 'invalid_range', 'invalid_range',
                          'line_out_of_bounds', 'range_too_large'])
        self.assertTrue(all(r['explanation'] for r in result['requested_ranges']))
        self.assertNotIn(999, {r['document_id'] for r in result['excerpts']})
        self.assertIn('不在目前對話', range_status_text(result))

    def test_shared_budget_never_reports_unprovided_range_as_complete(self):
        docs = self.documents('短行\n' + 'x' * 15000 + '\n尾行')[:1]
        result = retrieve_documents('[D1:L2] [D1:L3]', docs, max_chars=100, max_excerpts=1)
        self.assertEqual([r['status'] for r in result['requested_ranges']], ['character_limit', 'provided'])
        self.assertLessEqual(sum(len(r['text']) for r in result['excerpts']), 100)
        self.assertTrue(result['coverage_limited'])
        result = retrieve_documents('[D1:L1] [D1:L3]', docs, max_excerpts=1)
        self.assertEqual([r['status'] for r in result['requested_ranges']], ['provided', 'excerpt_limit'])
        empty = retrieve_documents('[D1:L1]', docs, max_excerpts=0)
        self.assertEqual(empty['excerpts'], [])
        self.assertEqual(empty['requested_ranges'][0]['status'], 'excerpt_limit')

    def test_complete_long_line_and_blank_lines_keep_exact_numbers(self):
        text = '\n' + '字' * 3000 + '\n'
        result = retrieve_documents('[D1:L1-L2]', self.documents(text)[:1])
        row = result['excerpts'][0]
        self.assertFalse(row['partial_line'])
        self.assertEqual(row['text'], 'L1: \nL2: ' + '字' * 3000)
        self.assertFalse(result['coverage_limited'])

    def test_request_cap_is_visible_and_not_overridden_by_history(self):
        question = ' '.join(f'[D1:L{i}]' for i in range(1, 11))
        result = retrieve_documents(question, self.documents())
        self.assertEqual(len(result['requested_ranges']), 8)
        self.assertTrue(result['requested_ranges_limited'])
        self.assertIn('其餘尚未處理', range_status_text(result))
        historical = retrieve_documents(question, self.documents(), reference_query='換成清單')
        self.assertEqual(historical['requested_ranges'], [])

    def test_real_context_is_session_scoped_and_keeps_imported_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3')
            sid, other = store.new_session(), store.new_session()
            source = Path(tmp) / 'notes.txt'
            source.write_text('原文第一行\n原文第二行', encoding='utf-8')
            did = store.attach_document(sid, source)
            query = f'請讀取 [D{did}:L2]'
            app = Assistant(store, None)
            self.assertEqual(app.prepare_context(other, query)['documents']['requested_ranges'][0]['status'],
                             'document_not_attached')
            source.write_text('磁碟變更', encoding='utf-8')
            class Engine:
                def answer(self, *args):
                    return f'原文第二行。[D{did}:L2]'
            answer = Assistant(store, Engine()).ask(sid, query)
            saved = store.answer_context(answer['id'])['documents']
            self.assertIn('原文第二行', saved['excerpts'][0]['text'])
            store.add_message(sid, 'user', '請說明別的事情')
            self.assertEqual(app.prepare_context(sid, '換成清單')['documents']['requested_ranges'], [])
            store.attach_document(sid, source)
            fresh = app.prepare_context(sid, query)['documents']
            self.assertEqual(fresh['requested_ranges'][0]['status'], 'line_out_of_bounds')
            store.detach_document(sid, did)
            self.assertEqual(app.prepare_context(sid, query)['documents']['requested_ranges'][0]['status'],
                             'document_not_attached')
            self.assertEqual(store.answer_context(answer['id'])['documents'], saved)
