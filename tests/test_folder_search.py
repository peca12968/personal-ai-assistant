import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.engine import Assistant
from assistant.folder_search import attach_result, search_folder
from assistant.documents import read_document
from assistant.scope import opened_path
from assistant.store import Store


class FolderSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'selected'
        self.root.mkdir()

    def file(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def test_nested_chinese_content_and_late_line_found(self):
        self.file('notes/recovery.md', '一般資訊。\n' * 180 + '復原代號：雲杉-731\n')
        self.file('unrelated.txt', '午餐吃麵。')
        report = search_folder(self.root, '復原代號')
        self.assertEqual(len(report['results']), 1)
        row = report['results'][0]
        self.assertEqual(row['relative_path'], 'notes/recovery.md')
        self.assertIn('L181: 復原代號：雲杉-731', row['text'])

    def test_filename_and_bilingual_search(self):
        self.file('database.toml', 'port = 8080')
        result = search_folder(self.root, '資料庫')['results']
        self.assertEqual(result[0]['relative_path'], 'database.toml')

    def test_exclusions_binary_and_scope(self):
        for name in ['.env.json', '.git/notes.txt', 'node_modules/a.txt', 'data/chat.json', 'auth.json', 'credentials.json']:
            self.file(name, 'needle secret')
        self.file('good.txt', 'needle public')
        (self.base / 'outside.txt').write_text('needle outside')
        (self.root / 'binary.txt').write_bytes(b'needle\x00')
        result = search_folder(self.root, 'needle')
        self.assertEqual([r['relative_path'] for r in result['results']], ['good.txt'])
        self.assertGreater(result['excluded'], 0)
        self.assertEqual(result['unreadable'], 1)

    def test_open_handle_path_is_the_actual_file(self):
        path = self.file('中文.txt', 'needle')
        with path.open('rb') as stream:
            self.assertEqual(opened_path(stream), path.resolve())

    def test_scope_rejects_sibling_prefix_and_parent_traversal(self):
        outside = self.base / 'selected-other'
        outside.mkdir()
        file = outside / 'text.txt'
        file.write_text('secret')
        for path in [file, self.root / '../selected-other/text.txt']:
            with self.assertRaises(ValueError):
                read_document(path, scope=self.root)

    def test_open_handle_outside_scope_rejected_before_content_read(self):
        path = self.file('text.txt', 'needle')
        with patch('assistant.scope.opened_path', return_value=self.base / 'outside.txt'):
            with self.assertRaises(ValueError):
                read_document(path, scope=self.root)

    def test_linked_directory_and_link_root_excluded(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'secret.txt').write_text('needle')
        link = self.root / 'linked'
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        else:
            link.symlink_to(outside, target_is_directory=True)
        self.assertEqual(search_folder(self.root, 'needle')['results'], [])
        with self.assertRaises(ValueError):
            search_folder(link, 'needle')

    def test_hard_link_excluded(self):
        outside = self.base / 'outside.txt'
        outside.write_text('needle')
        os.link(outside, self.root / 'linked.txt')
        self.assertEqual(search_folder(self.root, 'needle')['results'], [])

    def test_file_byte_depth_and_result_caps_are_reported(self):
        for name in ['a.txt', 'b.txt', 'c.txt']:
            self.file(name, 'needle x')
        self.file('nested/deeper.txt', 'needle')
        for kwargs, expected in [({'max_files': 1}, 'files'), ({'max_bytes': 8}, 'bytes'),
                                  ({'max_depth': 0}, 'depth'), ({'max_results': 1}, 'results'),
                                  ({'max_entries': 1}, 'entries')]:
            report = search_folder(self.root, 'needle', **kwargs)
            self.assertIn(expected, report['limits_hit'])
        self.assertLessEqual(search_folder(self.root, 'needle', max_bytes=8)['bytes_read_budget'], 8)

    def test_cancel_and_deadline_return_partial_report(self):
        self.file('a.txt', 'needle')
        event = threading.Event()
        event.set()
        report = search_folder(self.root, 'needle', cancel=event)
        self.assertTrue(report['cancelled'])
        self.assertEqual(report['files_read'], 0)
        with patch('assistant.folder_search.time.monotonic', side_effect=[0, 20]):
            report = search_folder(self.root, 'needle', timeout=1)
        self.assertIn('time', report['limits_hit'])

    def test_import_hash_rejects_changed_file_and_preserves_store(self):
        path = self.file('order.txt', '訂單數量：12')
        report = search_folder(self.root, '訂單數量')
        store = Store(self.base / 'state.sqlite3')
        sid = store.new_session()
        path.write_text('訂單數量：99', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '變更'):
            attach_result(store, sid, report, report['results'][0])
        self.assertEqual(store.documents(sid), [])

    def test_import_to_conversation_then_cited_answer(self):
        path = self.file('notes/order.txt', '訂單數量：12')
        before = path.read_bytes()
        report = search_folder(self.root, '訂單數量')
        store = Store(self.base / 'state.sqlite3')
        sid = store.new_session()
        attach_result(store, sid, report, report['results'][0])
        class Engine:
            def answer(self, prompt, cancel=None):
                self.prompt = prompt
                return '訂單數量是 12 [D1:L1]'
        engine = Engine()
        answer = Assistant(store, engine).ask(sid, '數量多少？')
        self.assertEqual(answer['citation_checks'], [{'citation': '[D1:L1]', 'range_valid': True}])
        self.assertIn('訂單數量', engine.prompt)
        self.assertNotIn(str(self.root), engine.prompt)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(store.documents(store.new_session()), [])

    def test_import_cannot_escape_scope_via_modified_result(self):
        outside = self.base / 'secret.txt'
        outside.write_text('secret')
        store = Store(self.base / 'state.sqlite3')
        with self.assertRaises(ValueError):
            attach_result(store, store.new_session(), {'root': str(self.root)},
                          {'relative_path': '../secret.txt', 'sha256': 'bogus'})

    def test_invalid_queries_and_file_roots_fail(self):
        path = self.file('a.txt', 'needle')
        for query in ['', ' ', '?' * 4, 'a' * 2001]:
            with self.assertRaises(ValueError):
                search_folder(self.root, query)
        with self.assertRaises(ValueError):
            search_folder(path, 'needle')

    def test_cli_search_does_not_create_data_or_call_model(self):
        self.file('order.txt', 'quantity 12')
        target = self.base / 'never-created'
        result = subprocess.run([sys.executable, '-m', 'assistant', '--search-folder', str(self.root),
                                 '--query', 'quantity', '--data-dir', str(target)], capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['results'][0]['relative_path'], 'order.txt')
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
