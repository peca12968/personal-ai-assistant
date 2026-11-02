import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from assistant import memory_edits as edits
from assistant.store import Store
from assistant.backup import create_backup, restore_backup
from assistant.engine import Assistant


class MemoryEditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'assistant.sqlite3')
        self.mid = self.store.remember('星橋檔案存放C槽。')
        self.original = self.row()

    def row(self):
        return self.store.memories()[0]

    def save(self, original, **changes):
        draft = {k: original[k] for k in edits.FIELDS}
        draft.update(changes)
        return self.store.edit_memory(original['id'], **draft, expected=original)

    def test_disjoint_edits_preserve_new_fact_and_identity_even_with_same_timestamp(self):
        self.save(self.original, content='星橋檔案已搬到F槽。')
        with self.store.connect() as db:
            db.execute('UPDATE memories SET updated_at=? WHERE id=?', (self.original['updated_at'], self.mid))
        receipt = self.save(self.original, kind='preference')
        current = self.row()
        self.assertEqual(current['content'], '星橋檔案已搬到F槽。')
        self.assertEqual(current['kind'], 'preference')
        self.assertEqual(current['id'], self.mid)
        self.assertEqual(current['created_at'], self.original['created_at'])
        self.assertEqual(receipt['preserved_fields'], ['content'])
        self.assertEqual(receipt['memory'], current)

    def test_same_field_conflict_is_atomic_and_explicit_resolution_can_become_stale(self):
        self.save(self.original, content='星橋檔案在F槽。', active=0)
        before = self.row()
        with self.assertRaises(edits.MemoryConflict) as captured:
            self.save(self.original, content='星橋檔案在G槽。', kind='lesson')
        conflict = captured.exception
        self.assertEqual(conflict.comparison['conflicts'], ['content'])
        self.assertEqual(self.row(), before)
        with self.assertRaises(ValueError):
            edits.resolve(conflict.comparison, conflict.current, conflict.draft, {})
        selected = edits.resolve(conflict.comparison, conflict.current, conflict.draft, {'content':'draft'})
        self.assertEqual(selected['active'], 0)
        self.assertEqual(selected['kind'], 'lesson')
        self.save(before, content='星橋檔案在H槽。')
        with self.assertRaises(edits.MemoryConflict):
            self.save(before, **selected)
        self.assertEqual(self.row()['content'], '星橋檔案在H槽。')

    def test_equal_edits_and_keeping_saved_version_do_not_require_overwrite(self):
        self.save(self.original, content='星橋檔案在F槽。')
        self.save(self.original, content='星橋檔案在F槽。', kind='lesson')
        before = self.row()
        with self.assertRaises(edits.MemoryConflict) as captured:
            self.save(self.original, content='星橋檔案在G槽。')
        conflict = captured.exception
        selected = edits.resolve(conflict.comparison, conflict.current, conflict.draft, {'content':'current'})
        self.save(before, **selected)
        self.assertEqual(self.row()['content'], '星橋檔案在F槽。')
        self.assertEqual(self.row()['kind'], 'lesson')

    def test_newer_disable_and_expiry_survive_stale_content_edit(self):
        self.save(self.original, active=0, expires_on='2000-01-01')
        receipt = self.save(self.original, content='星橋檔案在F槽。')
        self.assertEqual(set(receipt['preserved_fields']), {'active', 'expires_on'})
        self.assertEqual(self.store.retrieve('星橋'), [])
        self.assertEqual(self.row()['active'], 0)
        self.assertEqual(self.row()['expires_on'], '2000-01-01')
        self.save(self.row(), active=1, expires_on=None)
        self.assertEqual(self.store.retrieve('星橋')[0]['content'], '星橋檔案在F槽。')

    def test_deleted_memory_cannot_be_resurrected_and_stale_delete_cannot_delete_new_fact(self):
        self.save(self.original, content='星橋檔案在F槽。')
        with self.assertRaisesRegex(ValueError, '已變更'):
            self.store.forget(self.mid, expected=self.original)
        current = self.row()
        self.store.forget(self.mid, expected=current)
        with self.assertRaisesRegex(ValueError, '未重新新增'):
            self.save(current, content='不可復活')
        with self.assertRaises(ValueError):
            self.store.forget(self.mid, expected=current)
        new = self.store.remember('新的獨立記憶')
        self.assertGreater(new, self.mid)

    def test_delete_compares_entire_observed_row_and_invalid_snapshot_is_rejected(self):
        for field, value in [('kind','lesson'), ('active',0), ('expires_on','2030-01-01'), ('content','新內容')]:
            with self.subTest(field=field):
                before = self.row()
                self.save(before, **{field:value})
                with self.assertRaises(ValueError):
                    self.store.forget(self.mid, expected=before)
        for value in ({}, {**self.original, 'id': self.mid+1}):
            with self.assertRaises(ValueError):
                self.store.edit_memory(self.mid, '不應寫入', expected=value)
        before = self.row()
        with self.assertRaises(ValueError):
            self.save(before, expires_on='not-a-date')
        self.assertEqual(self.row(), before)

    def test_two_concurrent_writers_merge_using_state_inside_write_transaction(self):
        gate = threading.Barrier(2)
        def worker(change):
            store = Store(self.store.path)
            draft = {k:self.original[k] for k in edits.FIELDS}
            draft.update(change)
            gate.wait(timeout=10)
            return store.edit_memory(self.mid, **draft, expected=self.original)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, changes) for changes in ({'content':'星橋檔案在F槽。'}, {'kind':'lesson'})]
            receipts = [future.result(timeout=20) for future in futures]
        self.assertEqual(self.row()['content'], '星橋檔案在F槽。')
        self.assertEqual(self.row()['kind'], 'lesson')
        self.assertEqual(sum(bool(r['preserved_fields']) for r in receipts), 1)

    def test_saved_history_and_backup_survive_merge(self):
        class Engine:
            def answer(self, prompt, cancel=None): return '歷史回答'
        sid = self.store.new_session()
        answer = Assistant(self.store, Engine()).ask(sid, '星橋檔案在哪？')
        historical = self.store.answer_context(answer['id'])
        self.save(self.original, content='星橋檔案在F槽。')
        self.save(self.original, kind='lesson')
        self.assertEqual(self.store.answer_context(answer['id']), historical)
        backup = create_backup(self.store.path)
        restore_backup(backup['path'], self.root / 'restored')
        restored = Store(self.root / 'restored' / 'assistant.sqlite3')
        self.assertEqual(restored.memories(), self.store.memories())
        self.assertEqual(restored.answer_context(answer['id']), historical)


if __name__ == '__main__':
    unittest.main()
