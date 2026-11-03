import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy, memory_drafts
from assistant.gui import App
from assistant.autonomy_ui import AutonomousWindow
from assistant.memory_draft_ui import MemoryDraftWindow
from assistant.search_ui import SearchWindow
from assistant.backup_ui import BackupWindow
from assistant.task_ui import TaskWindow
from assistant.tasks import plan_csv
from assistant.store import Store
from assistant.engine import EngineError


class FailingEngine:
    def answer(self, prompt, cancel=None):
        raise EngineError('合成服務失敗', 'process_failed')


class WorkerStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.store = Store(self.path / 'assistant.sqlite3')
        self.sid = self.store.new_session()
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.dispose)

    def dispose(self):
        try:
            for timer in self.root.tk.call('after', 'info'):
                self.root.tk.call('after', 'cancel', timer)
            self.root.destroy()
        except tk.TclError:
            pass

    def fail_start(self):
        return patch('threading.Thread.start', side_effect=RuntimeError('cannot start thread'))

    def test_chat_preserves_unsent_text_and_can_close_after_start_failure(self):
        app = App(self.root, self.store, None)
        original = '  尚未送出的問題\n包含第二行  '
        app.input.insert('1.0', original)
        with self.fail_start(), patch.object(app.assistant, 'ask') as ask:
            app.send()
            self.assertTrue(app.busy)
            app.send()  # A queued failure still owns the busy UI until consumed.
            ask.assert_not_called()
        self.assertEqual(app.input.get('1.0', 'end-1c'), original)
        self.assertEqual(self.store.messages(app.sid), [])
        with patch.object(self.root, 'destroy') as destroy:
            app.close()
            destroy.assert_not_called()
            app.poll()
            self.assertFalse(app.busy)
            self.assertEqual(str(app.send_btn['state']), 'normal')
            app.close()
            destroy.assert_called_once()

    def test_retry_launch_failure_does_not_duplicate_question_or_change_attempt(self):
        app = App(self.root, self.store, FailingEngine())
        with self.assertRaises(EngineError):
            app.assistant.ask(app.sid, '已保存的失敗問題')
        before = self.store.messages(app.sid)
        with self.fail_start(), patch.object(app.assistant, 'retry') as retry:
            app.retry_answer()
            retry.assert_not_called()
        app.poll()
        self.assertFalse(app.busy)
        self.assertEqual(self.store.messages(app.sid), before)
        self.assertIn('未啟動', app.chat.get('1.0', 'end'))

    def test_chat_can_start_once_after_failure_and_preserves_next_input(self):
        class Engine:
            calls = 0
            def answer(self, prompt, cancel=None):
                self.calls += 1
                return '這是合成回答。'
        engine = Engine()
        app = App(self.root, self.store, engine)
        app.input.insert('1.0', '請回覆合成問題')
        with self.fail_start():
            app.send()
        app.poll()
        self.assertEqual(engine.calls, 0)
        app.send()  # Use a real background thread and actual local persistence.
        self.assertEqual(app.input.get('1.0', 'end-1c'), '')
        app.input.insert('1.0', '下一題尚未送出')
        terminal = app.events.get(timeout=5)
        app.events.put(terminal)
        app.poll()
        self.assertEqual(engine.calls, 1)
        self.assertFalse(app.busy)
        self.assertEqual(app.input.get('1.0', 'end-1c'), '下一題尚未送出')
        self.assertEqual([m['role'] for m in self.store.messages(app.sid)], ['user', 'assistant'])

    def test_failure_event_does_not_overwrite_text_edited_before_poll(self):
        app = App(self.root, self.store, None)
        app.input.insert('1.0', '原輸入')
        with self.fail_start():
            app.send()
        app.input.delete('1.0', 'end')
        app.input.insert('1.0', '使用者的新修改')
        app.poll()
        self.assertEqual(app.input.get('1.0', 'end-1c'), '使用者的新修改')
        self.assertEqual(self.store.messages(app.sid), [])

    def test_goal_modes_fail_without_running_or_losing_objective(self):
        view = AutonomousWindow(self.root, self.store, self.sid, None)
        gid = autonomy.create_goal(self.store, self.sid, '保留此工作')
        view.refresh(gid)
        view.objective.insert('1.0', '尚未保存的下一個目標')
        for continuous in (False, True):
            with self.subTest(continuous=continuous), self.fail_start(), patch('assistant.autonomy.run_goal') as run:
                self.assertFalse(view.begin(continuous_mode=continuous))
                run.assert_not_called()
            self.assertTrue(view.busy)
            view.poll()
            self.assertFalse(view.busy)
            self.assertEqual(str(view.start['state']), 'normal')
            self.assertEqual(autonomy.get_goal(self.store, self.sid, gid)['status'], 'pending')
            self.assertEqual(autonomy.get_goal(self.store, self.sid, gid)['steps'], [])
            self.assertIn('尚未保存', view.objective.get('1.0', 'end'))

    def test_goal_draft_and_file_rebuild_start_failures_preserve_state(self):
        view = AutonomousWindow(self.root, self.store, self.sid, None)
        view.objective.insert('1.0', '請整理合成工作')
        with self.fail_start():
            view.draft_from_chat()
        view.poll()
        self.assertFalse(view.busy)
        self.assertEqual(view.objective.get('1.0', 'end').strip(), '請整理合成工作')
        self.assertEqual(autonomy.goals(self.store, self.sid), [])
        gid = autonomy.create_goal(self.store, self.sid, '交付合成文件', file_delivery=True)
        view.refresh(gid)
        with self.fail_start(), patch('assistant.artifacts.rebuild') as rebuild:
            view.rebuild_files()
            rebuild.assert_not_called()
        view.poll()
        self.assertFalse(view.busy)
        self.assertEqual(autonomy.get_goal(self.store, self.sid, gid)['artifacts'], [])
        self.assertIn('未啟動', view.notice.get())

    def test_memory_draft_constructor_failure_keeps_saved_draft_retryable(self):
        self.store.add_message(self.sid, 'user', '我偏好精簡的回答。')
        view = MemoryDraftWindow(self.root, self.store, self.sid, None)
        did = memory_drafts.create(self.store, self.sid)
        view.refresh(did)
        with patch('threading.Thread', side_effect=RuntimeError('thread constructor failed')):
            view.run(did)
            view.run(did)
        self.assertEqual(view.events.qsize(), 1)
        view.poll()
        self.assertFalse(view.busy)
        self.assertEqual(str(view.retry['state']), 'normal')
        self.assertEqual(self.store.memories(), [])
        self.assertIn('thread constructor failed', view.notice.get())
        self.assertNotEqual(view.current['status'], 'running')

    def test_search_start_failure_keeps_query_and_folder(self):
        view = SearchWindow(self.root, self.store, self.sid)
        view.folder.set(str(self.path)); view.query.insert(0, '查找此文字')
        with self.fail_start(), patch('assistant.search_ui.search_folder') as search:
            view.search()
            search.assert_not_called()
        view.poll()
        self.assertFalse(view.busy)
        self.assertEqual(view.folder.get(), str(self.path))
        self.assertEqual(view.query.get(), '查找此文字')
        self.assertEqual(str(view.search_btn['state']), 'normal')
        self.assertEqual(str(view.import_btn['state']), 'disabled')
        self.assertEqual(self.store.documents(self.sid), [])

    def test_backup_start_failure_restores_buttons_without_writing_files(self):
        view = BackupWindow(self.root, self.store)
        with self.fail_start(), patch('assistant.backup_ui.create_backup') as backup:
            view.backup()
            backup.assert_not_called()
        view.poll()
        self.assertFalse(view.running)
        self.assertEqual(str(view.backup_button['state']), 'normal')
        self.assertEqual(str(view.restore_button['state']), 'normal')
        self.assertIn('未啟動', view.notice.get())
        self.assertFalse((self.path / 'backups').exists())

    def test_csv_planning_and_execution_failures_keep_saved_plan(self):
        csv = self.path / 'source.csv'
        csv.write_text('類別,金額\n甲,10\n', encoding='utf-8')
        view = TaskWindow(self.root, self.store, self.sid)
        view.path = csv; view.request.insert(0, '依類別合計金額')
        with self.fail_start(), patch('assistant.task_ui.propose_workflow') as propose:
            view.propose()
            propose.assert_not_called()
        view.poll()
        self.assertFalse(view.busy)
        self.assertEqual(view.request.get(), '依類別合計金額')
        plan = plan_csv(self.store, self.sid, csv, '類別', '金額')
        view.refresh(plan['id'])
        with self.fail_start(), patch('assistant.task_ui.execute_task') as execute:
            view.execute()
            execute.assert_not_called()
        view.poll()
        self.assertFalse(view.busy)
        with self.store.connect() as db:
            status = db.execute('SELECT status FROM task_runs WHERE id=?', (plan['id'],)).fetchone()[0]
        self.assertEqual(status, 'planned')
        self.assertEqual(str(view.propose_btn['state']), 'normal')


if __name__ == '__main__':
    unittest.main()
