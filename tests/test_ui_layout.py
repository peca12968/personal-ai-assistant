"""Hidden Tk geometry checks; these do not replace desktop visual acceptance."""
import tkinter as tk
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from assistant.feedback_ui import FeedbackWindow
from assistant.evolution_ui import EvolutionWindow
from assistant.autonomy_ui import AutonomousWindow
from assistant.store import Store
from assistant import autonomy, artifacts
from assistant.companion_ui import CompanionWindow
from assistant.companion_profile import get_profile
from assistant.goal_review_ui import GoalReviewWindow
from assistant import goal_review
from assistant import autonomy_csv
from assistant.autonomy_csv_ui import CsvResultsWindow


class FormGeometryTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        def dispose():
            # Tk timers outlive destroyed widgets; remove test-owned callbacks first.
            for timer in self.root.tk.call('after', 'info'):
                self.root.tk.call('after', 'cancel', timer)
            self.root.destroy()
        self.addCleanup(dispose)

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

    def test_continuous_controls_start_selected_goal_save_reason_and_stop(self):
        self.addCleanup(patch.stopall)
        patch('assistant.autonomy_ui.messagebox.showerror', side_effect=AssertionError('unexpected dialog')).start()
        patch('assistant.autonomy_ui.messagebox.showinfo', side_effect=AssertionError('unexpected dialog')).start()
        from test_autonomy import ScriptedEngine, action
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3')
            sid = store.new_session()
            view = AutonomousWindow(self.root, store, sid, None)
            layout = self.allocate(view)
            self.assert_reachable(layout, view.continue_start)
            self.assert_reachable(layout, view.continuous_minutes)
            self.assert_reachable(layout, view.stop)
            view.objective.insert('1.0', '完成整理')
            view.new_goal()
            view.continuous_steps.set('10')
            view.continuous_minutes.set('2')
            def verify_busy():
                self.assertTrue(view.busy)
                for widget in (view.start, view.continue_start, view.continuous_steps, view.continuous_minutes):
                    self.assertEqual(str(widget['state']), 'disabled')
                return action('calculate', operation='sum', values=['1'])
            view.engine = ScriptedEngine(verify_busy, *([action('calculate', operation='sum', values=['1'])] * 6),
                action('finish', report='完成整理'))
            with patch('assistant.autonomy_ui.threading.Thread', side_effect=lambda target, **kwargs: SimpleNamespace(start=target)):
                view.continue_start.invoke()
            view.poll()
            result = autonomy.get_goal(store, sid, view.selected_id())
            self.assertEqual(len(result['steps']), 8)
            self.assertEqual(result['continuous_runs'][0]['max_seconds'], 120)
            self.assertIn('已記錄8步', view.output.get('1.0', 'end'))
            self.assertEqual(str(view.continue_start['state']), 'disabled')
            view.new_goal()
            def stop():
                view.stop.invoke()
                return action('finish', report='不能保存')
            view.engine = ScriptedEngine(stop)
            with patch('assistant.autonomy_ui.threading.Thread', side_effect=lambda target, **kwargs: SimpleNamespace(start=target)):
                view.continue_start.invoke()
            view.poll()
            result = autonomy.get_goal(store, sid, view.selected_id())
            self.assertEqual(result['continuous_runs'][0]['reason'], 'cancelled')
            self.assertEqual(result['result'], '')
            self.assertFalse(view.busy)

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

    def test_review_form_opens_selected_session_and_hides_without_mutating_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '尚待補充的工作')
            autonomy.update(store, gid, 'needs_input', question='生效日期？')
            selected = []
            view = GoalReviewWindow(self.root, store, lambda sid, gid: selected.append((sid, gid)))
            layout = self.allocate(view)
            view.listing.selection_set(gid)
            view.select()
            self.assertIn('先保存必要補充', view.output.get('1.0', 'end'))
            self.assertIn('生效日期？', view.output.get('1.0', 'end'))
            self.assert_reachable(layout, view.open)
            self.assert_reachable(layout, view.hide)
            view.open_selected()
            self.assertEqual(selected, [(sid, gid)])
            view.toggle_hidden()
            self.assertEqual(len(view.listing.get_children()), 0)
            self.assertEqual(str(view.open['state']), 'disabled')
            self.assertEqual(autonomy.get_goal(store, sid, gid)['status'], 'needs_input')
            view.include_hidden.set(True)
            view.reset_page()
            view.listing.selection_set(gid)
            view.select()
            self.assertEqual(view.hide['text'], '重新顯示在回顧')
            view.toggle_hidden()
            self.assertEqual(goal_review.snapshot(store)['visible_count'], 1)

    def test_app_startup_review_count_and_old_goal_navigation_keep_chat_session(self):
        from assistant.gui import App
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3')
            old_sid = store.new_session()
            gid = autonomy.create_goal(store, old_sid, '舊對話目標')
            recent_sid = store.new_session()
            engine = SimpleNamespace(answer=lambda *args: self.fail('navigation must not call model'))
            app = App(self.root, store, engine)
            app.sid = recent_sid
            app.render()
            self.assertEqual(app.review_btn['text'], '接續與回顧（1）')
            app.sidebar_layout.pack_forget()
            app.sidebar_layout.place(x=0, y=0, width=220, height=450)
            self.root.update_idletasks()
            self.assertGreater(app.sidebar_layout.canvas.bbox('all')[3], 450)
            self.assert_reachable(app.sidebar_layout, app.backup_btn)
            before = app.sidebar_layout.canvas.yview()
            app.sidebar_layout._wheel(SimpleNamespace(widget=app.send_btn, delta=120))
            self.assertEqual(app.sidebar_layout.canvas.yview(), before)
            def hidden_window(*args):
                window = AutonomousWindow(*args)
                window.window.withdraw()
                return window
            with patch('assistant.autonomy_ui.AutonomousWindow', side_effect=hidden_window):
                app.autonomous_goals(old_sid, gid)
                app.autonomous_goals(old_sid, gid)
            self.assertEqual(app.sid, recent_sid)
            self.assertEqual(len(app.autonomous_windows), 1)
            self.assertEqual(app.autonomous_windows[0].sid, old_sid)
            self.assertEqual(app.autonomous_windows[0].selected_id(), gid)
            self.assertEqual(autonomy.get_goal(store, old_sid, gid)['status'], 'pending')

    def test_autonomous_csv_selection_result_view_and_explicit_full_export(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3')
            sid = store.new_session()
            path = Path(tmp) / 'data.csv'
            path.write_text('group,value\nA,10\nA,20\n', encoding='utf-8')
            did = store.attach_document(sid, path)
            view = AutonomousWindow(self.root, store, sid, None)
            layout = self.allocate(view)
            self.assertIsNone(view.selected_csv_id())
            view.csv_input.current(1)
            self.assertEqual(view.selected_csv_id(), did)
            self.assert_reachable(layout, view.csv_input)
            view.objective.insert('1.0', '分析這份CSV')
            view.new_goal()
            gid = view.selected_id()
            self.assertEqual(autonomy.get_goal(store, sid, gid)['csv_source']['document_id'], did)
            plan = autonomy_csv.plan(store, gid, {'group_column': 'group', 'value_column': 'value',
                'filters': [], 'sort': {'by': 'sum', 'direction': 'desc'}, 'top_n': None})
            pid = plan['plan_id']
            results = CsvResultsWindow(self.root, store, sid, gid)
            results_layout = self.allocate(results)
            results.listing.selection_set(pid)
            results.select()
            self.assertEqual(str(results.export_button['state']), 'disabled')
            autonomy_csv.execute(store, sid, gid, pid)
            results.refresh()
            self.assertEqual(str(results.export_button['state']), 'normal')
            self.assert_reachable(results_layout, results.export_button)
            destination = Path(tmp) / 'result.json'
            with patch('assistant.autonomy_csv_ui.filedialog.asksaveasfilename', return_value=str(destination)):
                results.export()
            self.assertEqual(json.loads(destination.read_bytes())['mean'], '15.0000')
            view.refresh(gid)
            self.assertEqual(str(view.csv_results['state']), 'normal')
            self.assert_reachable(layout, view.csv_results)

    def test_chat_handoff_draft_preserves_input_and_other_unsaved_goal(self):
        from assistant.gui import App
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3')
            sid = store.new_session()
            store.add_message(sid, 'user', '更正：選乙，生效日11月1日')
            app = App(self.root, store, None)
            app.input.insert('1.0', '沿用已確認方案計算費用')
            existing = AutonomousWindow(self.root, store, sid, None, initial_objective='原本未保存的草稿')
            existing.window.withdraw()
            app.autonomous_windows.append(existing)
            app.set_busy(True)
            app.goal_from_chat()
            self.assertEqual(len(app.autonomous_windows), 1)
            app.set_busy(False)
            def hidden_window(*args, **kwargs):
                view = AutonomousWindow(*args, **kwargs)
                view.window.withdraw()
                return view
            with patch('assistant.autonomy_ui.AutonomousWindow', side_effect=hidden_window):
                app.goal_from_chat()
            view = app.autonomous_windows[-1]
            layout = self.allocate(view)
            self.assert_reachable(layout, view.chat_option)
            self.assertTrue(view.from_chat.get())
            self.assertEqual(view.objective.get('1.0', 'end').strip(), '沿用已確認方案計算費用')
            self.assertEqual(app.input.get('1.0', 'end').strip(), '沿用已確認方案計算費用')
            self.assertEqual(existing.objective.get('1.0', 'end').strip(), '原本未保存的草稿')
            self.assertEqual(autonomy.goals(store, sid), [])
            view.new_goal()
            goal = autonomy.get_goal(store, sid, view.selected_id())
            self.assertEqual(goal['status'], 'pending')
            self.assertEqual(len(goal['conversation_context']['messages']), 1)
            self.assertFalse(goal['file_delivery'])
            self.assertIsNone(goal['csv_source'])
            self.assertIn('選乙', view.output.get('1.0', 'end'))
            self.assertEqual(len(store.messages(sid)), 1)
