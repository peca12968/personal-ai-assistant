import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from assistant.backup import create_backup, inspect_backup, restore_backup
from assistant.engine import Assistant
from assistant.store import Store
from scripts.publish import validate_path


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'original' / 'assistant.sqlite3')
        self.sid = self.store.new_session('還原測試')
        self.store.remember('專案保存在 F 槽', 'fact', '2099-12-31', active=False)
        self.store.save_session_brief(self.sid, '完成報告', '保留原檔', '已確認格式')
        document = self.root / 'note.txt'
        document.write_text('訂單：X184\n數量：12\n', encoding='utf-8')
        self.store.attach_document(self.sid, document)
        class Engine:
            def answer(self, prompt, cancel=None):
                return '共 12 件 [D1:L2]'
        self.mid = Assistant(self.store, Engine()).ask(self.sid, '數量多少？')['id']
        self.store.rate(self.mid, -1, '更精簡一點')

    def backup(self):
        return Path(create_backup(self.store.path)['path'])

    def rewrite_archive(self, path, mutate):
        with zipfile.ZipFile(path) as archive:
            values = {name: archive.read(name) for name in archive.namelist()}
        mutate(values)
        with zipfile.ZipFile(path, 'w') as archive:
            for name, value in values.items():
                archive.writestr(name, value)

    def test_complete_roundtrip_and_independent_writes(self):
        backup = self.backup()
        restored = self.root / 'restored'
        restore_backup(backup, restored)
        other = Store(restored / 'assistant.sqlite3')
        with self.store.connect() as before, other.connect() as after:
            tables = [r[0] for r in before.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for name in tables:
                self.assertEqual([tuple(r) for r in before.execute(f'SELECT * FROM {name}')],
                                 [tuple(r) for r in after.execute(f'SELECT * FROM {name}')])
        other.remember('還原後新增')
        self.assertEqual(len(self.store.memories()), 1)
        self.assertEqual(len(other.memories()), 2)
        self.assertEqual(other.answer_checks(self.mid)[0]['range_valid'], True)

    def test_wal_committed_content_included_and_uncommitted_excluded(self):
        connection = sqlite3.connect(self.store.path)
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute("INSERT INTO sessions(id,title) VALUES ('committed','可見')")
        connection.commit()
        connection.execute("INSERT INTO sessions(id,title) VALUES ('pending','不可見')")
        backup = self.backup()
        self.assertEqual(inspect_backup(backup)['counts']['sessions'], 2)
        connection.rollback()

    def test_existing_backup_and_data_directory_never_overwritten(self):
        backup = self.backup()
        original = backup.read_bytes()
        with self.assertRaises(FileExistsError):
            create_backup(self.store.path, backup)
        with self.assertRaises(FileExistsError):
            restore_backup(backup, self.store.path.parent)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(self.store.sessions()[0]['id'], self.sid)

    def test_hash_mismatch_does_not_create_destination(self):
        backup = self.backup()
        self.rewrite_archive(backup, lambda values: values.update({'assistant.sqlite3': values['assistant.sqlite3'][:-1] + b'!'}))
        target = self.root / 'bad'
        with self.assertRaises(ValueError):
            restore_backup(backup, target)
        self.assertFalse(target.exists())

    def test_corrupt_zip_is_rejected(self):
        backup = self.root / 'bad.paibackup'
        backup.write_bytes(b'not a zip')
        with self.assertRaises(zipfile.BadZipFile):
            restore_backup(backup, self.root / 'restored')
        self.assertFalse((self.root / 'restored').exists())

    def test_extra_path_entry_rejected_without_extraction(self):
        backup = self.backup()
        with zipfile.ZipFile(backup, 'a') as archive:
            archive.writestr('../escape.txt', 'bad')
        with self.assertRaises(ValueError):
            restore_backup(backup, self.root / 'restored')
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_future_format_and_wrong_counts_rejected(self):
        for field, value in [('format_version', 99), ('counts', {})]:
            backup = self.backup()
            def mutate(values):
                data = json.loads(values['manifest.json'])
                data[field] = value
                values['manifest.json'] = json.dumps(data)
            self.rewrite_archive(backup, mutate)
            with self.assertRaises(ValueError):
                inspect_backup(backup)

    def test_trigger_schema_rejected_even_with_updated_hash(self):
        backup = self.backup()
        def mutate(values):
            path = self.root / 'tampered.sqlite3'
            path.write_bytes(values['assistant.sqlite3'])
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TRIGGER surprise AFTER INSERT ON sessions BEGIN DELETE FROM memories; END")
                db.commit()
            values['assistant.sqlite3'] = path.read_bytes()
            metadata = json.loads(values['manifest.json'])
            metadata.update(sha256=hashlib.sha256(values['assistant.sqlite3']).hexdigest(), database_bytes=path.stat().st_size)
            values['manifest.json'] = json.dumps(metadata)
        self.rewrite_archive(backup, mutate)
        with self.assertRaises(ValueError):
            inspect_backup(backup)

    def test_foreign_key_failure_prevents_backup(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("INSERT INTO messages(session_id,role,content) VALUES ('missing','user','bad')")
            db.commit()
        with self.assertRaises(ValueError):
            self.backup()
        self.assertFalse(list(self.store.path.parent.glob('backups/*.paibackup')))

    def test_size_limit_and_missing_source_fail_without_creating_database(self):
        with patch('assistant.backup.MAX_DATABASE_BYTES', 100):
            with self.assertRaises(ValueError):
                self.backup()
        missing = self.root / 'missing.sqlite3'
        with self.assertRaises(FileNotFoundError):
            create_backup(missing)
        self.assertFalse(missing.exists())

    def test_archive_contains_only_database_and_manifest_and_publish_rejects_it(self):
        (self.store.path.parent / 'auth.json').write_text('test-auth-never-backup')
        with zipfile.ZipFile(self.backup()) as archive:
            self.assertEqual(set(archive.namelist()), {'assistant.sqlite3', 'manifest.json'})
            self.assertNotIn(b'test-auth-never-backup', archive.read('assistant.sqlite3'))
        self.assertFalse(validate_path('docs/personal.paibackup'))

    def test_cli_restore_inspect_without_model_or_initializing_default_data(self):
        backup = self.backup()
        target = self.root / 'cli-restored'
        result = subprocess.run([sys.executable, '-m', 'assistant', '--restore', str(backup), '--data-dir', str(target)],
                                capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((target / 'assistant.sqlite3').exists())
        result = subprocess.run([sys.executable, '-m', 'assistant', '--inspect-backup', str(backup)],
                                capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['counts']['sessions'], 1)


if __name__ == '__main__':
    unittest.main()
