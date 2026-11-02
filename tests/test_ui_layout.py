"""Hidden Tk geometry checks; these do not replace desktop visual acceptance."""
import tkinter as tk
import unittest
from types import SimpleNamespace

from assistant.feedback_ui import FeedbackWindow
from assistant.evolution_ui import EvolutionWindow


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
