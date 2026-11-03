"""Normal shutdown owns data work until its terminal UI result is consumed."""
import gc
import tempfile
import threading
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from assistant.backup import create_backup, restore_backup
from assistant.backup_ui import BackupWindow
from assistant.document_ui import DocumentWindow
from assistant.folder_search import search_folder
from assistant.gui import App
from assistant.search_ui import SearchWindow
from assistant.store import Store
from assistant.task_ui import TaskWindow
from assistant.tasks import execute_task, plan_csv


class DataWindowLifecycleTests(unittest.TestCase):
    def setUp(self):
        gc.collect()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.store = Store(self.path / 'original' / 'assistant.sqlite3')
        self.sid = self.store.new_session('合成生命週期測試')
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App.__new__(App)
        self.app.root, self.app.store, self.app.sid = self.root, self.store, self.sid
        self.app.busy = self.app.closing = False
        self.app.reminder_timer = None
        self.app.cancel = threading.Event()
        self.app.autonomous_windows = []
        self.app.memory_draft_windows = []
        self.app.drafts = Mock()
        self.app.drafts.flush.return_value = True
        self.app.status = tk.StringVar(self.root)
        self.threads, self.releases = [], []
        self.addCleanup(self.dispose)

    def dispose(self):
        for event in self.releases:
            event.set()
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        try:
            for timer in self.root.tk.call('after', 'info'):
                self.root.tk.call('after', 'cancel', timer)
            self.root.destroy()
        except tk.TclError:
            pass
        gc.collect()

    def launch(self, target, on_failure, *, daemon=False):
        thread = threading.Thread(target=target, daemon=daemon)
        self.threads.append(thread)
        thread.start()
        return True

    def gated(self, operation):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def run(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test release timeout')
            return operation(*args, **kwargs)
        return run, entered, release

    def finish_worker(self, view):
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertFalse(view.events.empty())
        self.assertTrue(view.active())  # Queued completion is not yet consumed.

    def csv(self):
        path = self.path / 'source.csv'
        path.write_text('類別,金額\n甲,10\n乙,20\n', encoding='utf-8')
        task = plan_csv(self.store, self.sid, path, '類別', '金額')
        view = TaskWindow(self.root, self.store, self.sid)
        view.path = path
        view.refresh(task['id'])
        return view, task

    def test_root_waits_for_every_terminal_event(self):
        task = TaskWindow(self.root, self.store, self.sid)
        search = SearchWindow(self.root, self.store, self.sid)
        backup = BackupWindow(self.root, self.store)
        task.busy = search.busy = backup.running = True
        timers = [w.poll_timer for w in (task, search, backup)]
        with patch.object(self.root, 'destroy') as destroy:
            self.app.close()
            destroy.assert_not_called()
            self.assertTrue(task.cancel.is_set() and search.cancel.is_set())
            self.assertFalse(hasattr(backup, 'cancel'))
            task.events.put((None, None, '合成停止'))
            search.events.put((False, '合成停止'))
            backup.events.put((False, '合成備份失敗'))
            self.app.close()
            destroy.assert_not_called()
            task.poll(); search.poll()
            self.app.close()
            destroy.assert_not_called()
            backup.poll()
            self.assertEqual(self.root._assistant_data_windows, [])
            self.assertTrue(all(w.closed for w in (task, search, backup)))
            for timer in timers:
                self.assertNotIn(timer, self.root.tk.call('after', 'info'))
            self.app.close()
            destroy.assert_called_once()

    def test_real_csv_cancellation_is_saved_before_close(self):
        view, task = self.csv()
        operation, entered, release = self.gated(execute_task)
        with patch('assistant.task_ui.start_worker', self.launch), patch('assistant.task_ui.execute_task', operation):
            view.execute()
            self.assertTrue(entered.wait(3))
            view.close()
            self.assertTrue(view.window.winfo_exists())
            self.assertTrue(view.cancel.is_set())
            release.set()
            self.finish_worker(view)
            self.assertEqual(self.store.task(self.sid, task['id'])['status'], 'cancelled')
            view.poll()
            self.assertTrue(view.closed)
            self.assertIn('停止', view.notice.get())

    def test_real_search_cancels_and_survives_original_parent(self):
        parent = tk.Toplevel(self.root)
        view = SearchWindow(parent, self.store, self.sid)
        view.folder.set(str(self.path))
        view.query.insert(0, '合成搜尋')
        operation, entered, release = self.gated(search_folder)
        with patch('assistant.search_ui.start_worker', self.launch), patch('assistant.search_ui.search_folder', operation):
            view.search()
            self.assertTrue(entered.wait(3))
            parent.destroy()
            self.assertTrue(view.window.winfo_exists())
            self.assertFalse(view.cancel.is_set())
            view.close()
            self.assertTrue(view.window.winfo_exists())
            release.set()
            self.finish_worker(view)
            view.poll()
            self.assertTrue(view.closed)
            self.assertTrue(view.report['cancelled'])
            self.assertEqual(self.store.documents(self.sid), [])

    def test_real_backup_and_restore_finish_without_restart(self):
        self.store.remember('合成記憶，僅供備份測試')
        archive = create_backup(self.store.path)['path']
        # Exercise a noncanonical alias even on machines without 8.3 names.
        (self.path / 'restore-parent').mkdir()
        restored = self.path / 'restore-parent' / '..' / 'restored'
        for operation in (lambda: create_backup(self.store.path), lambda: restore_backup(archive, restored)):
            with self.subTest(operation=operation):
                self.root._assistant_closing = False
                view = BackupWindow(self.root, self.store)
                run, entered, release = self.gated(operation)
                with patch('assistant.backup_ui.start_worker', self.launch), patch.object(self.root, 'destroy') as destroy, patch('assistant.backup_ui.subprocess.Popen') as popen:
                    view.work(run)
                    self.assertTrue(entered.wait(3))
                    self.app.close()
                    view.close()
                    self.assertTrue(view.window.winfo_exists())
                    self.assertTrue(view.running)
                    destroy.assert_not_called()
                    release.set()
                    self.finish_worker(view)
                    self.app.close()
                    destroy.assert_not_called()
                    view.poll()
                    self.assertTrue(view.closed)
                    self.assertFalse(view.running)
                    self.app.close()
                    destroy.assert_called_once()
                    view.open_restored()
                    popen.assert_not_called()
        other = Store(restored / 'assistant.sqlite3')
        self.assertEqual(other.memories(), self.store.memories())
        self.assertTrue(Path(view.restored).samefile(restored))

    def test_import_after_document_parent_close_keeps_captured_session(self):
        folder = self.path / 'documents'
        folder.mkdir()
        (folder / 'note.txt').write_text('合成搜尋來源', encoding='utf-8')
        parent = tk.Toplevel(self.root)
        callback = Mock()
        view = SearchWindow(parent, self.store, self.sid, callback)
        view.busy = True
        view.events.put((True, search_folder(folder, '合成')))
        view.poll()
        view.listing.selection_set('0')
        parent.destroy()
        other_sid = self.store.new_session('另一段對話')
        with patch('assistant.document_import_ui.start_worker',self.launch):
            progress=view.import_selected()
            self.finish_worker(progress)
        progress.poll()
        callback.assert_not_called()
        self.assertEqual(len(self.store.documents(self.sid)), 1)
        self.assertEqual(self.store.documents(other_sid), [])
        view.close()

    def test_shutdown_rejects_direct_entrypoints_and_new_dialogs(self):
        task, saved = self.csv()
        search = SearchWindow(self.root, self.store, self.sid)
        backup = BackupWindow(self.root, self.store)
        documents = DocumentWindow(self.root, self.store, self.sid)
        source = self.path / 'reference.txt'
        source.write_text('合成關閉驗證', encoding='utf-8')
        self.store.attach_document(self.sid, source)
        documents.refresh()
        documents.listing.selection_set(documents.listing.get_children()[0])
        backup.restored = str(self.path)
        self.root._assistant_closing = self.app.closing = True
        with patch('assistant.task_ui.filedialog.askopenfilename') as open_file, patch('assistant.task_ui.filedialog.asksaveasfilename') as save_file, patch('assistant.search_ui.filedialog.askdirectory') as folder, patch('assistant.task_ui.plan_csv') as plan, patch('assistant.task_ui.start_worker') as csv_worker, patch('assistant.search_ui.start_worker') as search_worker, patch('assistant.search_ui.attach_result') as attach, patch('assistant.backup_ui.start_worker') as backup_worker, patch('assistant.backup_ui.subprocess.Popen') as popen:
            for method in (task.choose, task.plan, task.execute, task.propose, task.export, search.choose, search.search, search.import_selected, backup.backup, backup.restore, backup.open_restored):
                method()
            backup.work(Mock())
            before = list(self.root._assistant_data_windows)
            self.app.backups(); self.app.work_tools(); documents.search_folder()
            with patch.object(self.store, 'attach_document') as import_doc, patch.object(self.store, 'detach_document') as detach_doc, patch('assistant.document_ui.filedialog.askopenfilenames') as open_docs:
                documents.add(); documents.reload(); documents.remove(); documents.import_paths([source])
                import_doc.assert_not_called(); detach_doc.assert_not_called(); open_docs.assert_not_called()
            self.assertEqual(before, self.root._assistant_data_windows)
            for mocked in (open_file, save_file, folder, plan, csv_worker, search_worker, attach, backup_worker, popen):
                mocked.assert_not_called()
        self.assertEqual(self.store.task(self.sid, saved['id'])['status'], 'planned')
        for view in (task, search, backup):
            view.request_shutdown()
            view.poll()
            self.assertTrue(view.closed)

    def test_file_dialog_rechecks_shutdown_before_using_selection(self):
        task, saved = self.csv()
        search = SearchWindow(self.root, self.store, self.sid)
        backup = BackupWindow(self.root, self.store)
        def selected(*args, **kwargs):
            self.root._assistant_closing = True
            return str(self.path)
        with patch('assistant.task_ui.filedialog.askopenfilename', selected), patch('assistant.task_ui.load_csv') as load:
            task.choose()
            load.assert_not_called()
        self.root._assistant_closing = False
        with patch('assistant.task_ui.filedialog.asksaveasfilename', selected), patch('assistant.task_ui.export_task') as export:
            task.export()
            export.assert_not_called()
        self.root._assistant_closing = False
        with patch('assistant.search_ui.filedialog.askdirectory', selected):
            search.choose()
            self.assertEqual(search.folder.get(), '')
        self.root._assistant_closing = False
        with patch('assistant.backup_ui.filedialog.askopenfilename', selected), patch('assistant.backup_ui.filedialog.askdirectory') as directory:
            backup.restore()
            directory.assert_not_called()
        self.root._assistant_closing = False
        with patch('assistant.backup_ui.filedialog.askopenfilename', return_value='synthetic.paibackup'), patch('assistant.backup_ui.filedialog.askdirectory', selected), patch('assistant.backup_ui.start_worker') as worker:
            backup.restore()
            worker.assert_not_called()

    def test_launch_failures_remain_owned_until_consumed(self):
        task, saved = self.csv()
        search = SearchWindow(self.root, self.store, self.sid)
        search.folder.set(str(self.path))
        backup = BackupWindow(self.root, self.store)
        with patch('threading.Thread.start', side_effect=RuntimeError('synthetic failure')):
            task.execute(); search.search(); backup.backup()
        for view in (task, search, backup):
            view.close()
            self.assertTrue(view.window.winfo_exists())
            self.assertTrue(view.active())
            view.poll()
            self.assertTrue(view.closed)
            self.assertIn('未啟動', view.notice.get())
        self.assertEqual(self.store.task(self.sid, saved['id'])['status'], 'planned')
        self.assertFalse((self.store.path.parent / 'backups').exists())

    def test_idle_close_cancels_callbacks_and_is_idempotent(self):
        for view in (TaskWindow(self.root, self.store, self.sid), SearchWindow(self.root, self.store, self.sid), BackupWindow(self.root, self.store)):
            timer = view.poll_timer
            view.close(); view.close(); view.poll()
            self.assertTrue(view.closed)
            self.assertIsNone(view.poll_timer)
            self.assertNotIn(timer, self.root.tk.call('after', 'info'))
            self.assertNotIn(view, self.root._assistant_data_windows)

    def test_unsaved_chat_still_blocks_shutdown_before_cancelling_work(self):
        view = SearchWindow(self.root, self.store, self.sid)
        view.busy = True
        self.app.drafts.flush.return_value = False
        self.app.poll_reminders = Mock()
        with patch.object(self.root, 'destroy') as destroy:
            self.app.close()
            destroy.assert_not_called()
            self.assertFalse(view.cancel.is_set())
            self.assertFalse(view.closing)
            self.assertFalse(self.root._assistant_closing)
        view.events.put((False, '合成結束'))
        view.poll(); view.close()


if __name__ == '__main__':
    unittest.main()
