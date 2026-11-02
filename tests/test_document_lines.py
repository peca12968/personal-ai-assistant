import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from assistant import autonomy as a, document_lines as d
from assistant.backup import create_backup, restore_backup
from assistant.documents import check_citations, retrieve_documents
from assistant.engine import EngineError
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


def doc(content, did=1):
    return {'id': did, 'name': 'rules.txt', 'content': content,
            'sha256': hashlib.sha256(content.encode()).hexdigest(),
            'source_path': 'PRIVATE_PATH_NEVER_REOPEN'}


def args(start=1, line=1, did=1):
    return {'document_id': did, 'line_number': line, 'start_char': start}


class DocumentLineTests(unittest.TestCase):
    def test_pages_preserve_unicode_exactly_and_do_not_reopen_or_mutate_source(self):
        content = '甲😀e\u0301\t' * 1701
        documents = [doc('heading\r\n' + content + '\nend')]
        before = copy.deepcopy(documents)
        start, received, excerpts = 1, '', []
        while start is not None:
            page = d.read(documents, args(start, 2))
            row = page['excerpts'][0]
            received += row['text'][len('L2: '):]
            excerpts.append(row)
            self.assertLessEqual(page['end_char'] - page['start_char'] + 1, 4000)
            self.assertEqual(page['total_chars'], len(content))
            self.assertEqual(page['omitted_before'], page['start_char'] - 1)
            self.assertEqual(page['omitted_after'], len(content) - page['end_char'])
            self.assertFalse(page['line_complete'])
            self.assertNotIn('PRIVATE_PATH', json.dumps(page))
            start = page['next_start_char']
        self.assertEqual(received, content)
        self.assertEqual(documents, before)
        self.assertTrue(check_citations(f'[D1:L2:C1-C{len(content)}]', excerpts)[0]['range_valid'])
        self.assertFalse(check_citations('[D1:L2]', excerpts)[0]['range_valid'])

    def test_empty_short_end_and_out_of_range_positions(self):
        documents = [doc('\nabc\n')]
        blank = d.read(documents, args())
        self.assertEqual((blank['total_chars'], blank['end_char'], blank['next_start_char']), (0, 0, None))
        self.assertTrue(blank['line_complete'])
        self.assertTrue(check_citations('[D1:L1]', blank['excerpts'])[0]['range_valid'])
        self.assertFalse(check_citations('[D1:L1:C1]', blank['excerpts'])[0]['range_valid'])
        full = d.read(documents, args(line=2))
        self.assertTrue(full['line_complete'])
        self.assertTrue(all(x['range_valid'] for x in check_citations('[D1:L2] [D1:L2:C1-C3]', full['excerpts'])))
        tail = d.read(documents, args(3, 2))
        self.assertEqual(tail['excerpts'][0]['text'], 'L2: c')
        self.assertFalse(tail['line_complete'])
        for request in (args(2), args(4, 2), args(line=3), args(did=2)):
            with self.subTest(request=request), self.assertRaises(ValueError):
                d.read(documents, request)

    def test_schema_and_cancellation(self):
        for request in ({}, dict(args(), path='other.txt'), args(0), args(True), args(-1),
                        args('1'), args(line=False), args(did=10**12)):
            with self.subTest(request=request), self.assertRaises(ValueError):
                a.validate_action({'tool': 'read_line', 'args': request, 'note': 'read'})
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(EngineError):
            d.read([doc('abc')], args(), cancel)

    def test_character_citations_reject_gaps_mixed_versions_and_unseen_ranges(self):
        documents = [doc('x'*9000)]
        pages = [d.read(documents, args(start))['excerpts'][0] for start in (1, 4001, 8001)]
        self.assertTrue(check_citations('[D1:L1:C3999-C8002]', pages)[0]['range_valid'])
        self.assertFalse(check_citations('[D1:L1:C1-C9000]', [pages[0], pages[2]])[0]['range_valid'])
        mixed = copy.deepcopy(pages); mixed[1]['sha256'] = 'other'
        self.assertFalse(check_citations('[D1:L1:C1-C9000]', mixed)[0]['range_valid'])
        for token in ('[D1:L1]', '[D1:L1:C0-C9]', '[D1:L1:C9-C2]', '[D1:L1:C1-C9001]',
                      '[D1:L2:C1]', '[D2:L1:C1]', '[D1:L1:Cx]', '[D1:L1:C1-L2:C9]'):
            with self.subTest(token=token):
                self.assertFalse(check_citations(token, pages)[0]['range_valid'])
        old = {'document_id': 1, 'start_line': 1, 'end_line': 1, 'partial_line': True, 'text': 'L1: x'}
        self.assertFalse(any(x['range_valid'] for x in check_citations('[D1:L1] [D1:L1:C1]', [old])))

    def test_search_chunks_have_positions_and_full_lines_allow_bounded_character_citations(self):
        result = retrieve_documents('marker', [doc('marker' + 'x'*5000)], max_excerpts=1)
        row = result['excerpts'][0]
        self.assertEqual((row['start_char'], row['end_char'], row['line_chars']), (1, 1400, 5006))
        self.assertFalse(check_citations('[D1:L1]', result['excerpts'])[0]['range_valid'])
        self.assertTrue(check_citations('[D1:L1:C1-C6]', result['excerpts'])[0]['range_valid'])
        self.assertFalse(check_citations('[D1:L1:C1401]', result['excerpts'])[0]['range_valid'])
        full = retrieve_documents('[D1:L1-L2]', [doc('abc\n😀z')])['excerpts']
        self.assertTrue(all(x['range_valid'] for x in check_citations('[D1:L1:C2-C3] [D1:L2:C1-C2]', full)))
        self.assertFalse(check_citations('[D1:L2:C3]', full)[0]['range_valid'])

    def test_real_loop_citation_checks_backup_and_stage_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = Store(root/'state.sqlite3'); sid = store.new_session()
            path = root/'long.txt'; path.write_text('甲'*4000+'識別碼snow-783'+'乙'*1000, encoding='utf-8')
            store.attach_document(sid, path)
            gid = a.create_goal(store, sid, '讀取規則')
            goal = a.run_goal(store, sid, gid, ScriptedEngine(
                action('read_line', **args(4001)),
                action('finish', report='已讀整行[D1:L1]')), max_steps=2)
            self.assertEqual(goal['status'], 'paused')
            self.assertEqual(goal['steps'][-1]['action']['tool'], 'invalid')
            child = a.create_goal(store, sid, '接續', parent_id=gid)
            background = a.get_goal(store, sid, child)['previous_stage']['checkpoint']
            raw = json.dumps(background, ensure_ascii=False)
            self.assertNotIn('snow-783', raw)
            self.assertIn('start_char', raw)
            self.assertNotIn('excerpts', raw)
            invalid = a.run_goal(store, sid, child, ScriptedEngine(action('finish', report='識別碼[D1:L1:C4001-C4011]')), max_steps=1)
            self.assertEqual(invalid['steps'][0]['action']['tool'], 'invalid')
            result = a.run_goal(store, sid, gid, ScriptedEngine(action('finish', report='識別碼snow-783[D1:L1:C4001-C4011]')), max_steps=1)
            self.assertEqual(result['status'], 'report_ready')
            archive = root/'snapshot.paibackup'; create_backup(store.path, archive)
            restored_dir = root/'restored'; restore_backup(archive, restored_dir)
            restored = a.get_goal(Store(restored_dir/'assistant.sqlite3'), sid, gid)
            self.assertEqual(restored['steps'], result['steps'])
            self.assertEqual(path.read_text(encoding='utf-8'), '甲'*4000+'識別碼snow-783'+'乙'*1000)

    def test_source_change_stops_before_reading_and_other_session_is_not_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = Store(root/'state.sqlite3'); sid = store.new_session()
            path = root/'source.txt'; path.write_text('old', encoding='utf-8'); store.attach_document(sid, path)
            other = store.new_session(); other_id = store.attach_document(other, path)
            gid = a.create_goal(store, sid, 'read')
            result = a.run_goal(store, sid, gid, ScriptedEngine(action('read_line', **args(did=other_id))), max_steps=1)
            self.assertEqual(result['steps'][0]['action']['tool'], 'invalid')
            def change():
                path.write_text('new', encoding='utf-8'); store.attach_document(sid, path)
                return action('read_line', **args())
            result = a.run_goal(store, sid, gid, ScriptedEngine(change), max_steps=1)
            self.assertEqual(len(result['steps']), 1)
            self.assertIn('改變', result['notice'])

    def test_file_delivery_uses_actual_character_coverage(self):
        from assistant import artifacts
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = Store(root/'state.sqlite3'); sid = store.new_session()
            path = root/'source.txt'; path.write_text('x'*4000+'保留14天', encoding='utf-8')
            store.attach_document(sid, path)
            gid = a.create_goal(store, sid, '交付規則摘要', file_delivery=True)
            content = '保留14天[D1:L1:C4001-C4005]'
            goal = a.run_goal(store, sid, gid, ScriptedEngine(
                action('read_line', **args(4001)),
                action('write_artifact', filename='bad.txt', content='錯誤範圍[D1:L1:C1-C4005]'),
                action('write_artifact', filename='rules.txt', content=content),
                action('deliver', report='已保存摘要，待確認。', filenames=['rules.txt'])), max_steps=4)
            self.assertEqual(goal['steps'][1]['action']['tool'], 'invalid')
            self.assertEqual(goal['status'], 'report_ready')
            self.assertEqual(len(goal['artifacts']), 1)
            self.assertEqual(goal['artifacts'][0]['name'], 'rules.txt')
            self.assertEqual((artifacts.directory(store, gid)/'rules.txt').read_text(encoding='utf-8'), content)
            self.assertFalse((artifacts.directory(store, gid)/'bad.txt').exists())
            self.assertEqual(goal['steps'][2]['observation']['citation_checks'],
                             [{'citation': '[D1:L1:C4001-C4005]', 'range_valid': True}])


if __name__ == '__main__':
    unittest.main()
