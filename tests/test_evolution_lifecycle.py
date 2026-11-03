"""Keep owned self-edit workers observable until their terminal result."""
import threading
import tkinter as tk
import unittest
from unittest.mock import patch, Mock

from assistant.evolution_ui import EvolutionWindow
from assistant.gui import App


class EvolutionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App.__new__(App)
        self.app.root = self.root
        self.app.reminder_timer = None
        self.app.cancel = threading.Event()
        self.app.autonomous_windows = []
        self.app.memory_draft_windows = []
        self.app.busy = False
        self.app.drafts = Mock()
        self.app.drafts.flush.return_value = True
        self.app.status = tk.StringVar(self.root)
        self.addCleanup(self.dispose)

    def dispose(self):
        try:
            for timer in self.root.tk.call('after', 'info'):
                self.root.tk.call('after', 'cancel', timer)
            self.root.destroy()
        except tk.TclError:
            pass

    def view(self, parent=None):
        view = EvolutionWindow(parent or self.root)
        view.window.withdraw()
        view.goal.insert('1.0', '改善合成測試案例')
        return view

    def test_feedback_parent_close_preserves_worker_window(self):
        parent = tk.Toplevel(self.root)
        view = self.view(parent)
        view.busy = True
        parent.destroy()
        self.assertTrue(view.window.winfo_exists())
        view.events.put(('error', '合成失敗，原程式保留'))
        view.poll()
        self.assertFalse(view.busy)
        self.assertIn('合成失敗', view.output.get('1.0', 'end'))

    def test_root_close_waits_for_all_workers_and_prevents_new_runs(self):
        first, second = self.view(), self.view()
        first.busy = second.busy = True
        with patch.object(self.root, 'destroy') as destroy:
            self.app.close()
            self.assertFalse(destroy.called)
            self.assertTrue(first.cancel.is_set())
            self.assertTrue(second.cancel.is_set())
            first.events.put(('done', {'status': 'published'}))
            first.poll()
            self.app.close()
            self.assertFalse(destroy.called)
            with patch('assistant.evolution_ui.improve') as improve:
                first.begin()
                self.assertFalse(improve.called)
                self.assertFalse(first.busy)
            self.assertEqual(str(first.start['state']), 'disabled')
            second.events.put(('error', '套用前停止'))
            second.poll()
            self.app.close()
            destroy.assert_called_once()

    def test_thread_start_failure_restores_controls_and_keeps_draft(self):
        view = self.view()
        with patch('assistant.evolution_ui.threading.Thread.start', side_effect=RuntimeError('cannot start thread')):
            view.begin()
        view.poll()
        self.assertFalse(view.busy)
        self.assertEqual(str(view.start['state']), 'normal')
        self.assertEqual(str(view.stop['state']), 'disabled')
        self.assertIn('cannot start thread', view.output.get('1.0', 'end'))
        self.assertIn('改善合成測試案例', view.goal.get('1.0', 'end'))

    def test_idle_close_unregisters_window_and_cancels_poll(self):
        view = self.view()
        timer = view.poll_timer
        view.close()
        self.assertEqual(self.root._assistant_evolution_windows, [])
        self.assertNotIn(timer, self.root.tk.call('after', 'info'))
        self.assertFalse(view.window.winfo_exists())
        # The application can then close normally without touching dead widgets.
        self.app.close()

    def test_busy_window_close_only_requests_stop_until_result_arrives(self):
        view = self.view()
        view.busy = True
        view.close()
        self.assertTrue(view.cancel.is_set())
        self.assertTrue(view.window.winfo_exists())
        self.assertIn(view, self.root._assistant_evolution_windows)
        view.events.put(('error', '套用前停止，沒有改碼'))
        view.poll()
        self.assertIn('沒有改碼', view.output.get('1.0', 'end'))
        view.close()
        self.assertEqual(self.root._assistant_evolution_windows, [])

    def test_publication_worker_finishes_before_root_can_close(self):
        view = self.view()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def work(*args, **kwargs):
            kwargs['progress']('publishing')
            entered.set()
            try:
                if not release.wait(5):
                    raise RuntimeError('test release timeout')
                return {'status': 'published', 'test_only': True}
            finally:
                finished.set()
        with patch('assistant.evolution_ui.improve', side_effect=work), patch.object(self.root, 'destroy') as destroy:
            try:
                view.begin()
                self.assertTrue(entered.wait(3))
                self.app.close()
                self.assertFalse(destroy.called)
                self.assertTrue(view.cancel.is_set())
                view.poll()
                self.assertIn('發布中', view.output.get('1.0', 'end'))
            finally:
                release.set()
                self.assertTrue(finished.wait(3))
            # Wait for the worker's actual terminal queue item, not its status text.
            item = view.events.get(timeout=3)
            self.assertEqual(item[0], 'done')
            view.events.put(item)
            view.poll()
            self.assertIn('published', view.output.get('1.0', 'end'))
            self.app.close()
            destroy.assert_called_once()


if __name__ == '__main__':
    unittest.main()
