"""Hidden Tk geometry checks; these do not replace desktop visual acceptance."""
import tkinter as tk
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace

from assistant.feedback_ui import FeedbackWindow
from assistant.evolution_ui import EvolutionWindow
from assistant.autonomy_ui import AutonomousWindow
from assistant.store import Store
from assistant import autonomy, artifacts
from assistant.companion_ui import CompanionWindow
from assistant.companion_profile import get_profile


class FormGeometryTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def allocate(self, view):
        # Allocate the viewport without displaying or focusing a desktop window.
        view.window.withdraw()
        view.layout.pack_forget()
        view.layout.place(x=0, y=0, width=880, height=500)
        self.root.update_idletasks()
        return view.layout

    def assert_reachable(self, layout, widget):
        layout.canvas.yview_moveto(0)
        layout._focus(SimpleNamespace(widget=widget))
        top = widget.winfo_rooty() - layout.body.winfo_rooty()
        visible = layout.canvas.canvasy(0)
        self.assertGreaterEqual(top, visible - 1)
        self.assertLessEqual(top + widget.winfo_height(), visible + layout.canvas.winfo_height() + 1)

    def test_small_feedback_form_reaches_length_and_expanded_followup_controls(self):
        view = FeedbackWindow(self.root, SimpleNamespace(feedback_summary=lambda: []))
        layout = self.allocate(view)
        initial_height = layout.canvas.bbox('all')[3]
        self.assertGreater(initial_height, 500)
        self.assert_reachable(layout, view.expected)
        view.kind.set('接續前文檢索')
        view.change_kind()
        self.root.update_idletasks()
        self.assertGreater(layout.canvas.bbox('all')[3], initial_height)
        self.assert_reachable(layout, view.turns)
        layout.canvas.yview_moveto(1)
        self.assertAlmostEqual(layout.canvas.yview()[1], 1)
        view.kind.set('回答長度辨識')
        view.change_kind()
        self.root.update_idletasks()
        self.assertEqual(layout.canvas.bbox('all')[3], initial_height)

    def test_wheel_preserves_inner_editors_and_scrolls_outer_form_only(self):
        view = FeedbackWindow(self.root, SimpleNamespace(feedback_summary=lambda: []))
        layout = self.allocate(view)
        before = layout.canvas.yview()
        for widget in [view.fields['history'], view.listing, view.kind, view.turns]:
            layout._wheel(SimpleNamespace(widget=widget, delta=-120))
            self.assertEqual(layout.canvas.yview(), before)
        layout._wheel(SimpleNamespace(widget=view.next, delta=-120))
        self.assertGreater(layout.canvas.yview()[0], before[0])
        self.assert_reachable(layout, view.fields['question'])

    def test_improvement_actions_reachable_with_imported_cases(self):
        draft = {'goal': '驗證表單', 'selected': ['assistant/context.py'],
                 'acceptance': {'version': 1, 'cases': [
                     {'id': 'synthetic', 'kind': 'followup', 'question': '換個主題',
                      'history': [], 'expect': None}]}}
        view = EvolutionWindow(self.root, initial_draft=draft)
        layout = self.allocate(view)
        self.assertGreater(layout.canvas.bbox('all')[3], 500)
        self.assert_reachable(layout, view.start)
        self.assert_reachable(layout, view.stop)
        self.assert_reachable(layout, view.goal)
        self.assertEqual(view.imported_suite, draft['acceptance'])
        self.assertFalse(view.busy)

    def test_autonomous_goal_entry_and_saved_state_reachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3')
            view = AutonomousWindow(self.root, store, store.new_session(), None)
            layout = self.allocate(view)
            self.assert_reachable(layout, view.start)
            self.assert_reachable(layout, view.reply)
            view.objective.insert('1.0', '整理比較報告')
            view.new_goal()
            self.assertIn('整理比較報告', view.output.get('1.0', 'end'))
            self.assertEqual(len(view.listing.get_children()), 1)
            self.assertFalse(view.busy)
            self.assertEqual(str(view.reply['state']), 'disabled')
            gid = view.selected_id()
            autonomy.update(store, gid, 'needs_input', question='日期？')
            view.refresh(gid)
            self.assertEqual(str(view.start['state']), 'disabled')
            self.assertEqual(str(view.reply['state']), 'normal')
            autonomy.update(store, gid, 'report_ready', result='已保存的成果')
            view.refresh(gid)
            self.assertEqual(str(view.start['state']), 'disabled')
            self.assertEqual(str(view.reply['state']), 'disabled')
            self.assertIn('後續工作請保存新目標', view.output.get('1.0', 'end'))
            self.assertEqual(str(view.follow['state']), 'normal')
            self.assert_reachable(layout, view.follow)
            view.objective.delete('1.0', 'end')
            view.objective.insert('1.0', '根據上一份成果規劃下一步')
            view.next_stage()
            child = view.selected_id()
            self.assertNotEqual(child, gid)
            self.assertEqual(str(view.follow['state']), 'disabled')
            self.assertEqual(str(view.previous['state']), 'normal')
            self.assertIn('已保存的成果', view.output.get('1.0', 'end'))
            view.previous_stage()
            self.assertEqual(view.selected_id(), gid)

    def test_companion_form_saves_reloads_and_resets_reachable_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3')
            view = CompanionWindow(self.root, store)
            layout = self.allocate(view)
            self.assert_reachable(layout, view.save_button)
            self.assert_reachable(layout, view.reset_button)
            view.fields['assistant_name'].delete(0, 'end')
            view.fields['assistant_name'].insert(0, '小禾')
            view.save()
            self.assertEqual(get_profile(store)['settings']['assistant_name'], '小禾')
            view.reload()
            self.assertEqual(view.revision, 1)
            view.reset()
            self.assertEqual(view.revision, 2)
            self.assertEqual(view.fields['assistant_name'].get(), '個人 AI 助理')

    def test_autonomous_file_mode_and_saved_artifact_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3')
            sid = store.new_session()
            view = AutonomousWindow(self.root, store, sid, None)
            layout = self.allocate(view)
            self.assert_reachable(layout, view.file_option)
            view.file_delivery.set(True)
            view.objective.insert('1.0', '交付一份整理文件')
            view.new_goal()
            gid = view.selected_id()
            self.assertTrue(autonomy.get_goal(store, sid, gid)['file_delivery'])
            self.assertEqual(str(view.rebuild['state']), 'disabled')
            artifacts.write(store, gid, 'notes.md', '# 已整理內容')
            view.refresh(gid)
            self.assertEqual(str(view.files['state']), 'normal')
            self.assertEqual(str(view.rebuild['state']), 'normal')
            self.assertIn('notes.md', view.output.get('1.0', 'end'))
            self.assertIn('狀態不是即時檔案檢查', view.output.get('1.0', 'end'))
            self.assert_reachable(layout, view.rebuild)
            view.busy = True
            view.select()
            self.assertEqual(str(view.rebuild['state']), 'disabled')
            view.busy = False
