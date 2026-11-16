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

    def test_history_search_option_is_explicit_reachable_and_saves_without_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite3');sid=store.new_session()
            store.add_message(sid,'user','橡樹原始條件')
            view=AutonomousWindow(self.root,store,sid,None)
            layout=self.allocate(view)
            self.assert_reachable(layout,view.history_option)
            self.assertFalse(view.history_search.get())
            view.history_option.invoke()
            self.assertTrue(view.from_chat.get());self.assertTrue(view.history_search.get())
            view.chat_option.invoke()
            self.assertFalse(view.from_chat.get());self.assertFalse(view.history_search.get())
            view.history_option.invoke()
            view.objective.delete('1.0','end');view.objective.insert('1.0','查找橡樹條件')
            with patch('assistant.autonomy_ui.messagebox.showerror',side_effect=AssertionError):
                view.new_goal()
            goals=autonomy.goals(store,sid);self.assertEqual(len(goals),1)
            goal=autonomy.get_goal(store,sid,goals[0]['id'])
            self.assertEqual(goal['status'],'pending');self.assertEqual(goal['steps'],[])
            self.assertEqual(goal['conversation_history']['captured_message_count'],1)
            self.assertIn('可搜尋歷史',view.output.get('1.0','end'))

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

    def test_memory_edit_merges_two_windows_without_reverting_newer_fact(self):
        from assistant.memory_ui import MemoryWindow
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'assistant.sqlite3')
            mid = store.remember('專案在C槽。')
            first = MemoryWindow(self.root, store)
            stale = MemoryWindow(self.root, Store(store.path))
            for view in (first, stale):
                view.window.withdraw()
                view.listing.selection_set(str(mid));view.select()
            first.content.delete('1.0', 'end');first.content.insert('1.0', '專案已搬到F槽。')
            first.save()
            stale.kind.set('偏好');stale.save()
            self.assertEqual(store.memories()[0]['content'], '專案已搬到F槽。')
            self.assertEqual(store.memories()[0]['kind'], 'preference')
            self.assertIn('已保留其他地方較新的內容', stale.notice.get())
            self.assertEqual(stale.content.get('1.0', 'end').strip(), '專案已搬到F槽。')
            stale.content.insert('end', '尚未保存')
            stale.select()  # Repeated selection events must preserve the draft.
            self.assertIn('尚未保存', stale.content.get('1.0', 'end'))

    def test_memory_conflict_review_preserves_draft_and_requires_separate_save(self):
        from assistant.memory_ui import MemoryWindow
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'assistant.sqlite3')
            mid = store.remember('專案在C槽。')
            view = MemoryWindow(self.root, store);view.window.withdraw()
            view.listing.selection_set(str(mid));view.select()
            store.edit_memory(mid, '專案在F槽。', active=False)
            view.content.delete('1.0', 'end');view.content.insert('1.0', '專案在G槽。')
            view.kind.set('經驗');view.save()
            self.assertEqual(store.memories()[0]['content'], '專案在F槽。')
            self.assertEqual(view.content.get('1.0', 'end').strip(), '專案在G槽。')
            review = view.merge_view
            layout = self.allocate(review)
            self.assert_reachable(layout, review.apply_button)
            self.assertEqual(set(review.choices), {'content'})
            preview = review.previews[0].get('1.0', 'end')
            for text in ('專案在C槽。', '專案在F槽。', '專案在G槽。'):
                self.assertIn(text, preview)
            review.choices['content'].set('draft');review.apply()
            self.assertEqual(store.memories()[0]['content'], '專案在F槽。')
            self.assertFalse(view.active.get())
            self.assertEqual(view.kind.get(), '經驗')
            view.save()
            self.assertEqual(store.memories()[0]['content'], '專案在G槽。')
            self.assertEqual(store.memories()[0]['active'], 0)

    def test_memory_review_cannot_overwrite_new_typing_or_a_second_external_change(self):
        from assistant.memory_ui import MemoryWindow
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'assistant.sqlite3')
            mid = store.remember('專案在C槽。')
            view = MemoryWindow(self.root, store);view.window.withdraw()
            view.listing.selection_set(str(mid));view.select()
            view.content.delete('1.0', 'end');view.content.insert('1.0', '專案在G槽。')
            store.edit_memory(mid, '專案在F槽。')
            view.review();old = view.merge_view;old.window.withdraw()
            view.content.insert('end', '另加限制。')
            with patch('assistant.memory_merge_ui.messagebox.showerror') as error:
                old.apply();error.assert_called_once()
            self.assertIn('另加限制', view.content.get('1.0', 'end'))
            view.review();review=view.merge_view;review.window.withdraw()
            review.choices['content'].set('draft');review.apply()
            store.edit_memory(mid, '專案又搬到H槽。')
            view.save();view.merge_view.window.withdraw()
            self.assertEqual(store.memories()[0]['content'], '專案又搬到H槽。')
            self.assertIn('另加限制', view.content.get('1.0', 'end'))

    def test_memory_stale_delete_and_deleted_save_preserve_unsaved_draft(self):
        from assistant.memory_ui import MemoryWindow
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'assistant.sqlite3')
            mid = store.remember('舊資訊')
            view = MemoryWindow(self.root, store);view.window.withdraw()
            view.listing.selection_set(str(mid));view.select()
            view.content.insert('end', '我的草稿')
            store.edit_memory(mid, '剛更正的資訊')
            with patch('assistant.memory_ui.messagebox.showerror') as error:
                view.delete();error.assert_called_once()
            self.assertEqual(store.memories()[0]['content'], '剛更正的資訊')
            store.forget(mid)
            with patch('assistant.memory_ui.messagebox.showerror') as error:
                view.save();error.assert_called_once()
            self.assertEqual(store.memories(), [])
            self.assertIn('我的草稿', view.content.get('1.0', 'end'))

    def test_memory_alias_settings_preview_stale_reload_and_entry_preserve_drafts(self):
        from assistant.memory_ui import MemoryWindow
        from assistant.memory_alias_ui import MemoryAliasesWindow
        from assistant import memory_aliases
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'assistant.sqlite3')
            store.remember('星橋素材放在F槽。')
            with patch('assistant.memory_alias_ui.MemoryAliasesWindow') as opened:
                memories = MemoryWindow(self.root, store)
                memories.window.withdraw()
                memories.content.insert('1.0', '尚未保存的記憶草稿')
                memories.alias_button.invoke()
                opened.assert_called_once_with(memories.window, store)
                self.assertEqual(memories.content.get('1.0', 'end').strip(), '尚未保存的記憶草稿')
            view = MemoryAliasesWindow(self.root, store)
            layout = self.allocate(view)
            for widget in (view.editor, view.save_button, view.reload_button, view.query_entry, view.preview_button):
                self.assert_reachable(layout, widget)
            view.editor.insert('1.0', '星橋 | StarBridge')
            view.save()
            self.assertEqual(memory_aliases.get(store)['groups'], [['星橋', 'StarBridge']])
            view.query.set('StarBridge')
            view.preview()
            self.assertIn('星橋素材放在F槽', view.result.get('1.0', 'end'))
            view.editor.delete('1.0', 'end')
            view.editor.insert('1.0', '星橋 | 小星')
            memory_aliases.save(store, [['星橋', 'NewStar']], expected_revision=1)
            with patch('assistant.memory_alias_ui.messagebox.showerror') as errors:
                view.save()
                errors.assert_called_once()
            self.assertEqual(view.editor.get('1.0', 'end').strip(), '星橋 | 小星')
            view.reload()
            self.assertEqual(view.editor.get('1.0', 'end').strip(), '星橋 | 小星')
            self.assertIn('NewStar', view.saved.get('1.0', 'end'))
            self.assertNotIn('星橋素材放在F槽', view.result.get('1.0', 'end'))
            view.save()
            self.assertEqual(memory_aliases.get(store)['groups'], [['星橋', '小星']])
            view.query.set('StarBridge')
            view.preview()
            self.assertIn('沒有選用記憶', view.result.get('1.0', 'end'))
            view.editor.delete('1.0', 'end')
            view.save()
            self.assertEqual(memory_aliases.get(store)['groups'], [])
            self.assertEqual(memories.content.get('1.0', 'end').strip(), '尚未保存的記憶草稿')

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

    def test_feedback_edit_and_withdraw_are_explicit_and_reachable(self):
        from assistant.answer_feedback import for_chat
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'state.sqlite3')
            sid = store.new_session()
            mid = store.add_message(sid,'assistant','原回答')
            store.rate(mid,-1,'原更正')
            view = FeedbackWindow(self.root,store)
            layout = self.allocate(view)
            view.listing.selection_set(str(mid));view.select()
            self.assert_reachable(layout,view.saved_correction)
            self.assert_reachable(layout,view.clear_feedback)
            # Old answers with no model snapshot can be revised, but cannot be
            # silently turned into a code-acceptance case.
            self.assertEqual(str(view.next['state']),'disabled')
            view.saved_correction.delete('1.0','end');view.saved_correction.insert('1.0','更正為新版')
            view.save_feedback.invoke()
            self.assertEqual(for_chat(store,sid,'接續')['items'][0]['correction'],'更正為新版')
            view.clear_feedback.invoke()
            self.assertEqual(for_chat(store,sid,'接續')['items'],[])
            self.assertEqual(view.saved_correction.get('1.0','end').strip(),'')

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

    def test_holdout_import_controls_and_started_run_use_frozen_separate_cases(self):
        import json
        draft={'goal':'修正風格','selected':['assistant/communication.py'],
               'acceptance':{'version':1,'cases':[{'id':'provided','kind':'communication','question':'公開例子','history':[],'expect':{'length_hint':'detailed'}}]}}
        held={'version':1,'cases':[{'id':'held','kind':'communication','question':'保留例子','history':[],'expect':{'length_hint':'concise'}}]}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'holdout.json';path.write_text(json.dumps(held),encoding='utf-8')
            view=EvolutionWindow(self.root,initial_draft=draft);layout=self.allocate(view)
            for widget in (view.holdout_import,view.holdout_clear,view.holdout_show,view.start):self.assert_reachable(layout,widget)
            with patch('assistant.evolution_ui.filedialog.askopenfilename',return_value=str(path)):view.holdout_import.invoke()
            self.assertEqual(view.holdout_suite,held)
            queued=[]
            with patch('assistant.evolution_ui.threading.Thread',side_effect=lambda target,**kwargs:SimpleNamespace(start=lambda:queued.append(target))):view.begin()
            self.assertTrue(view.busy)
            view.clear_holdout();self.assertIsNotNone(view.holdout_suite)
            view.holdout_suite['cases'][0]['expect']['length_hint']='detailed'
            with patch('assistant.evolution_ui.improve',return_value={'status':'no_change_needed'}) as improve:queued[0]()
            self.assertEqual(improve.call_args.kwargs['acceptance'],draft['acceptance'])
            self.assertEqual(improve.call_args.kwargs['holdout'],held)
            view.poll();self.assertFalse(view.busy)
            view.holdout_clear.invoke();self.assertIsNone(view.holdout_suite)
            view.clear_cases();view.holdout_suite=held
            with patch('assistant.evolution_ui.messagebox.showerror') as error:view.begin()
            error.assert_called_once();self.assertFalse(view.busy)

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

    def test_work_plan_and_delivered_report_precede_raw_step_details(self):
        from test_autonomy import ScriptedEngine, action
        from test_work_plan import item
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'assistant.sqlite3');sid=store.new_session()
            gid=autonomy.create_goal(store,sid,'整理工作')
            autonomy.run_goal(store,sid,gid,ScriptedEngine(
                action('calculate',operation='sum',values=['3','4']),
                action('update_plan',items=[item(state='done',refs=[1])])),max_steps=2)
            view=AutonomousWindow(self.root,store,sid,None)
            self.allocate(view);view.refresh(gid)
            text=view.output.get('1.0','end')
            self.assertLess(text.index('工作清單（助理回報'),text.index('步驟 1 ·'))
            self.assertIn('依據步驟 1',text)
            self.assertIn('已暫停',text)
            with store.connect() as db:
                db.execute("UPDATE autonomous_goals SET notes='更正條件' WHERE id=?",(gid,))
            view.refresh(gid)
            self.assertIn('完成標記需重新核對',view.output.get('1.0','end'))
            autonomy.run_goal(store,sid,gid,ScriptedEngine(action('finish',report='最後的報告')))
            view.refresh(gid)
            text=view.output.get('1.0','end')
            self.assertLess(text.index('最後的報告'),text.index('步驟 1 ·'))

    def test_stopped_goal_checkpoint_entry_preserves_drafts_and_does_not_start(self):
        from test_autonomy import ScriptedEngine, action
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '先計算')
            autonomy.run_goal(store, sid, gid, ScriptedEngine(action('calculate', operation='sum', values=['3', '4'])), max_steps=1)
            view = AutonomousWindow(self.root, store, sid, None)
            layout = self.allocate(view)
            view.refresh(gid)
            self.assertEqual(str(view.follow['state']), 'normal')
            self.assert_reachable(layout, view.follow)
            view.objective.insert('1.0', '接續彙整結果')
            draft = view.objective.get('1.0', 'end')
            view.next_stage()
            child = view.selected_id()
            self.assertNotEqual(child, gid)
            self.assertEqual(view.objective.get('1.0', 'end'), draft)
            self.assertFalse(view.busy)
            self.assertIn('上一階段尚未交付', view.output.get('1.0', 'end'))
            self.assertIn('歷史步驟 1', view.output.get('1.0', 'end'))
            self.assertEqual(autonomy.get_goal(store, sid, child)['steps'], [])
            self.assertEqual(str(view.follow['state']), 'disabled')
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
            def hidden_window(*args, **kwargs):
                window = AutonomousWindow(*args, **kwargs)
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

    def test_chat_opens_exact_older_goal_and_keeps_current_drafts(self):
        from assistant.engine import Assistant
        from assistant.gui import App
        from assistant import goal_replies
        from test_autonomy import ScriptedEngine, action

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '晴溪 Q4 的展件清單')
            original = '封條是什麼顏色？'
            autonomy.run_goal(store, sid, gid, ScriptedEngine(action('ask', question=original)))
            for i in range(8):
                newer = autonomy.create_goal(store, sid, f'晴溪 Q4{i} 的展件清單')
            answer = Assistant(store, ScriptedEngine(original)).ask(sid, '查看「晴溪 Q4」目前等我回答哪個問題？')
            view = goal_replies.snapshot(store, sid, gid)
            goal_replies.save(store, sid, gid, '銀灰。', expected_fingerprint=view['fingerprint'])
            current = '到館時間是什麼？'
            before = autonomy.run_goal(store, sid, gid, ScriptedEngine(action('ask', question=current)))
            rival_before = autonomy.get_goal(store, sid, newer)
            app = App(self.root, store, SimpleNamespace(answer=lambda *args: self.fail('navigation started inference')))
            self.assertEqual(app.last_answer, answer['id'])
            self.assertEqual(app.open_chat_goal_btn.winfo_manager(), 'pack')
            app.input.insert('1.0', '保留我的聊天草稿 ')
            view = AutonomousWindow(self.root, store, sid, None, initial_objective='保留我的工作草稿 ')
            self.allocate(view)
            app.autonomous_windows.append(view)
            view.refresh(newer)

            app.set_busy(True)
            app.open_chat_goal_btn.invoke()
            self.assertEqual(view.selected_id(), newer)
            app.set_busy(False)
            app.open_chat_goal_btn.invoke()
            self.assertEqual(app.autonomous_windows, [view])
            self.assertEqual(view.selected_id(), gid)
            self.assertIn('需要你的補充：' + current, view.output.get('1.0', 'end'))
            self.assertEqual(str(view.reply['state']), 'normal')
            self.assertGreater(view.listing.yview()[0], 0)
            self.assertEqual(app.input.get('1.0', 'end-1c'), '保留我的聊天草稿 ')
            self.assertEqual(view.objective.get('1.0', 'end-1c'), '保留我的工作草稿 ')
            self.assertEqual(autonomy.get_goal(store, sid, gid), before)
            self.assertEqual(autonomy.get_goal(store, sid, newer), rival_before)

            view.refresh(newer)
            store.add_message(sid, 'user', '下一題尚未回答。')
            app.open_chat_goal_btn.invoke()
            self.assertEqual(app.open_chat_goal_btn.winfo_manager(), '')
            self.assertEqual(view.selected_id(), newer)
            other_sid = store.new_session()
            app.drafts.switch(other_sid)
            app.render()
            self.assertEqual(app.open_chat_goal_btn.winfo_manager(), '')
            self.assertEqual(app.sid, other_sid)

    def test_chat_reply_draft_uses_only_selected_user_text_and_preserves_an_open_draft(self):
        from assistant.engine import Assistant
        from assistant.goal_reply_ui import GoalReplyWindow
        from assistant.gui import App
        from test_autonomy import ScriptedEngine, action

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'assistant.sqlite3')
            sid = store.new_session()
            gid = autonomy.create_goal(store, sid, '晴溪 Q4 的展件')
            other = autonomy.create_goal(store, sid, '晴溪 Q40 的展件')
            before = autonomy.run_goal(store, sid, gid, ScriptedEngine(action('ask', question='封條顏色是什麼？')))
            other_before = autonomy.get_goal(store, sid, other)
            quote = '封條顏色為銀灰。'
            store.add_message(sid, 'user', '較早的非本輪文字。' + quote)
            Assistant(store, ScriptedEngine(quote)).ask(
                sid, '查看「晴溪 Q4」目前等我回答哪個問題？🙂 ' + quote)
            app = App(self.root, store, SimpleNamespace(answer=lambda *args: self.fail('draft staging started inference')))
            self.assertEqual(app.chat_reply_draft_btn.winfo_manager(), 'pack')
            app.input.insert('1.0', '我的聊天草稿 ')
            editors = []

            def hidden_view(*args, **kwargs):
                view = AutonomousWindow(*args, **kwargs)
                view.window.withdraw()
                return view

            def hidden_editor(*args, **kwargs):
                view = GoalReplyWindow(*args, **kwargs)
                view.window.withdraw()
                editors.append(view)
                return view

            def select(start):
                app.chat.tag_remove('sel', '1.0', 'end')
                app.chat.tag_add('sel', start, f'{start}+{len(quote)}c')

            with patch('assistant.autonomy_ui.AutonomousWindow', side_effect=hidden_view), \
                 patch('assistant.goal_reply_ui.GoalReplyWindow', side_effect=hidden_editor):
                old = app.chat.search(quote, '1.0', stopindex='end')
                selected = app.chat.search(quote, f'{old}+1c', stopindex='end')
                model = app.chat.search(quote, f'{selected}+1c', stopindex='end')
                self.assertTrue(old and selected and model)
                for invalid in (old, model):
                    select(invalid)
                    app.chat_reply_draft_btn.invoke()
                    self.assertEqual(app.autonomous_windows, [])
                    self.assertIn('使用者訊息', app.status.get())
                select(selected)
                app.chat_reply_draft_btn.invoke()
                editor, = editors
                self.assertEqual(editor.gid, gid)
                self.assertEqual(editor.content.get('1.0', 'end-1c'), quote)
                self.assertIn('所選原文', editor.notice.get())
                editor.content.delete('1.0', 'end')
                editor.content.insert('1.0', '我已編輯的草稿 ')
                select(selected)
                app.chat_reply_draft_btn.invoke()
                self.assertEqual(editors, [editor])
                self.assertEqual(editor.content.get('1.0', 'end-1c'), '我已編輯的草稿 ')
                self.assertIn('已保留原草稿', editor.notice.get())
            self.assertEqual(app.input.get('1.0', 'end-1c'), '我的聊天草稿 ')
            self.assertEqual(autonomy.get_goal(store, sid, gid), before)
            self.assertEqual(autonomy.get_goal(store, sid, other), other_before)
            self.assertEqual(app.autonomous_windows[0].selected_id(), gid)

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


    def test_memory_draft_generation_edit_save_and_sources_are_reachable(self):
        import json
        from assistant.memory_draft_ui import MemoryDraftWindow
        from assistant import memory_drafts
        from test_autonomy import ScriptedEngine
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite3');sid=store.new_session()
            mid=store.add_message(sid,'user','星橋專案位於F槽。')
            reply=json.dumps({'items':[{'kind':'fact','content':'星橋專案位於F槽。',
                'sources':[{'key':'M'+str(mid),'quote':'星橋專案位於F槽'}]}]},ensure_ascii=False)
            view=MemoryDraftWindow(self.root,store,sid,ScriptedEngine(reply))
            layout=self.allocate(view)
            self.assert_reachable(layout,view.start);self.assert_reachable(layout,view.save)
            with patch('assistant.memory_draft_ui.threading.Thread',side_effect=lambda target,**kwargs:SimpleNamespace(start=target)):
                view.start.invoke()
            view.poll()
            self.assertEqual(store.memories(),[])
            view.items.selection_set('0');view.select_item()
            self.assertIn('星橋專案位於F槽',view.evidence.get('1.0','end'))
            view.content.delete('1.0','end');view.content.insert('1.0','星橋專案放F槽，已核對。')
            view.save.invoke()
            self.assertEqual(store.memories()[0]['content'],'星橋專案放F槽，已核對。')
            self.assertEqual(len(memory_drafts.provenance(store,store.memories()[0]['id'])),1)
            view.items.selection_set('0');view.select_item()
            self.assertEqual(str(view.save['state']),'disabled')
            view.close()
            self.assertFalse(view.window.winfo_exists())

    def test_memory_draft_selected_update_rejects_stale_view_and_preserves_disabled_state(self):
        import json
        from assistant.memory_draft_ui import MemoryDraftWindow
        from assistant import memory_drafts as d
        from test_autonomy import ScriptedEngine
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite3');sid=store.new_session()
            old=store.remember('星橋專案位於D槽。')
            with store.connect() as db:
                db.execute("UPDATE memories SET active=0,expires_on='2030-01-01' WHERE id=?",(old,))
            mid=store.add_message(sid,'user','星橋專案位於F槽。')
            did=d.create(store,sid)
            d.generate(store,sid,did,ScriptedEngine(json.dumps({'items':[{'kind':'fact','content':'星橋專案位於F槽。',
                'sources':[{'key':'M'+str(mid),'quote':'星橋專案位於F槽。'}]}]},ensure_ascii=False)))
            view=MemoryDraftWindow(self.root,store,sid,None);layout=self.allocate(view)
            view.refresh(did);view.items.selection_set('0');view.select_item()
            self.assertEqual(view.target.current(),0)
            self.assert_reachable(layout,view.target);self.assert_reachable(layout,view.reload_targets_button)
            view.target.current(view.target_choices.index(old));view.target_changed()
            self.assertIn('更新後仍停用',view.target_preview.get('1.0','end'))
            self.assertEqual(view.expires.get(),'2030-01-01')
            view.target.current(0);view.target_changed()
            self.assertEqual(view.expires.get(),'')
            view.target.current(view.target_choices.index(old));view.target_changed()
            with store.connect() as db:db.execute("UPDATE memories SET content='別的視窗已改成E槽。' WHERE id=?",(old,))
            with patch('assistant.memory_draft_ui.messagebox.showerror') as error:
                view.save.invoke()
            error.assert_called_once()
            self.assertEqual(d.get(store,sid,did)['acceptances'],[])
            self.assertIn('E槽',d.memory_target(store,old)['memory']['content'])
            view.reload_targets_button.invoke()
            self.assertIn('E槽',view.target_preview.get('1.0','end'))
            view.save.invoke()
            row=d.memory_target(store,old)['memory']
            self.assertEqual((row['content'],row['active'],row['expires_on']),('星橋專案位於F槽。',0,'2030-01-01'))
            self.assertEqual(len(store.memories()),1)
            receipt=d.get(store,sid,did)['acceptances'][0]
            self.assertIn('E槽',receipt['replacement']['before']['content'])
            view.items.selection_set('0');view.select_item()
            self.assertEqual(str(view.save['state']),'disabled')
            self.assertIn('before_sha256',view.target_preview.get('1.0','end'))
            view.close()

    def test_reminder_settings_reachable_and_stale_editor_rejected(self):
        from assistant.reminder_ui import ReminderWindow
        from assistant import reminders
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite3');changed=[]
            view=ReminderWindow(self.root,store,lambda:changed.append(True));layout=self.allocate(view)
            other=ReminderWindow(self.root,store);other.window.withdraw()
            self.assertFalse(view.enabled.get())
            self.assert_reachable(layout,view.save_button);self.assert_reachable(layout,view.reload_button)
            view.enabled.set(True);view.time.delete(0,'end');view.time.insert(0,'20:30')
            view.save_button.invoke()
            self.assertEqual(changed,[True])
            self.assertEqual(reminders.settings(store)['daily_time'],'20:30')
            with patch('assistant.reminder_ui.messagebox.showerror') as error:other.save_button.invoke()
            error.assert_called_once();self.assertTrue(reminders.settings(store)['enabled'])
            other.reload_button.invoke();other.enabled.set(False);other.save_button.invoke()
            self.assertFalse(reminders.settings(store)['enabled'])

    def test_app_reminder_timer_banner_snooze_review_and_close_keep_chat_intact(self):
        import threading
        from datetime import datetime,timedelta,timezone
        from assistant.gui import App
        from assistant import reminders
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite3');old=store.new_session()
            gid=autonomy.create_goal(store,old,'查看季度報表')
            autonomy.update(store,gid,'needs_input',question='要哪個月份？')
            sid=store.new_session();app=App(self.root,store,None);app.sid=sid
            app.input.insert('1.0','還沒送出的聊天')
            reminders.save_settings(store,enabled=True,daily_time='19:00',expected_revision=0)
            now=datetime(2026,11,3,19,0,tzinfo=timezone(timedelta(hours=8)))
            with patch('assistant.reminders._now',return_value=now):
                app.busy=True;app.check_reminders();self.assertIsNone(app.reminder_notice)
                app.busy=False
                self.root.after_cancel(app.reminder_timer)
                app.reminder_timer=self.root.after(0,app.poll_reminders)
                self.root.update()
                self.assertIsNotNone(app.reminder_timer)
                self.assertIn(app.reminder_timer,self.root.tk.call('after','info'))
                self.assertIn('先保存必要補充',app.reminder_text.get('1.0','end'))
                self.assertTrue(app.reminder_frame.grid_info())
                app.reminder_snooze.invoke()
                self.assertFalse(app.reminder_frame.grid_info())
                self.assertIsNone(app.reminder_notice)
            with patch('assistant.reminders._now',return_value=now+timedelta(minutes=15)):
                app.check_reminders()
                self.assertIsNotNone(app.reminder_notice)
                with patch.object(app,'review_goals') as review:app.reminder_open.invoke()
                review.assert_called_once()
                self.assertIsNone(app.reminder_notice)
            self.assertEqual(app.sid,sid)
            self.assertEqual(app.input.get('1.0','end').strip(),'還沒送出的聊天')
            self.assertEqual(store.messages(sid),[])
            self.assertEqual(autonomy.get_goal(store,old,gid)['status'],'needs_input')
            handle=app.reminder_timer
            fake=SimpleNamespace(cancel=threading.Event(),busy=True)
            app.memory_draft_windows.append(fake);app.close()
            self.assertIsNone(app.reminder_timer)
            self.assertNotIn(handle,self.root.tk.call('after','info'))
            self.assertTrue(app.closing)
            fake.busy=False

    def test_long_reminder_content_has_bounded_height_and_chat_controls_fit_small_viewport(self):
        from assistant.gui import App
        with tempfile.TemporaryDirectory() as tmp:
            app=App(self.root,Store(Path(tmp)/'state.sqlite3'),None)
            app.reminder_text.configure(state='normal')
            app.reminder_text.insert('1.0','很長的專案名稱與下一步說明'*200)
            app.reminder_text.configure(state='disabled')
            # Explicitly allocate hidden child viewports: a withdrawn root does
            # not reliably allocate its grid. No desktop window is shown.
            app.reminder_frame.grid_forget();app.reminder_frame.place(x=0,y=0,width=840)
            self.root.update_idletasks()
            banner_height=app.reminder_frame.winfo_reqheight()
            self.assertLess(banner_height,160)
            body=app.input.master.master;body.grid_forget()
            available=600-79-39-banner_height
            body.place(x=0,y=79,width=840,height=available)
            self.root.update_idletasks()
            for widget in (app.input,app.send_btn,app.stop_btn):
                top=0;ancestor=widget
                while ancestor is not body:
                    top+=ancestor.winfo_y();ancestor=ancestor.master
                self.assertGreaterEqual(top,0)
                self.assertLessEqual(top+widget.winfo_height(),available)
                self.assertGreater(widget.winfo_height(),10)
            self.assertEqual(str(app.reminder_text['state']),'disabled')

    def test_goal_guidance_reviews_current_state_rejects_stale_and_preserves_unsaved_drafts(self):
        from assistant.goal_guidance_ui import GoalGuidanceWindow
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite3');sid=store.new_session()
            gid=autonomy.create_goal(store,sid,'計算10人的費用')
            view=AutonomousWindow(self.root,store,sid,None);layout=self.allocate(view)
            view.objective.insert('1.0','另一份尚未保存的目標')
            reply_gid=autonomy.create_goal(store,sid,'另一個等待回答的目標')
            autonomy.update(store,reply_gid,'needs_input',question='需要幾份？')
            view.refresh(reply_gid)
            reply_editor=view.add_reply();reply_editor.window.withdraw()
            reply_editor.content.insert('1.0','尚未送出的回答')
            view.refresh(gid);self.assert_reachable(layout,view.guidance)
            editor=view.add_guidance();editor_layout=self.allocate(editor)
            other=GoalGuidanceWindow(self.root,store,sid,gid);other.window.withdraw()
            self.assert_reachable(editor_layout,editor.save_button)
            self.assertIn('計算10人',editor.context.get('1.0','end'))
            editor.content.insert('1.0','更正：改為12人。');editor.save_button.invoke()
            other.content.insert('1.0','再補充：先給總額。')
            with patch('assistant.goal_guidance_ui.messagebox.showerror') as error:other.save_button.invoke()
            error.assert_not_called()
            self.assertIn('先給總額',other.content.get('1.0','end'))
            self.assertIn('12人',other.context.get('1.0','end'))
            self.assertIn('草稿仍保留', other.notice.get())
            self.assertNotIn('先給總額', autonomy.get_goal(store, sid, gid)['notes'])
            other.save_button.invoke()
            goal=autonomy.get_goal(store,sid,gid)
            self.assertEqual(goal['status'],'pending');self.assertEqual(goal['steps'],[])
            self.assertIn('12人',goal['notes']);self.assertIn('先給總額',goal['notes'])
            self.assertEqual(view.objective.get('1.0','end').strip(),'另一份尚未保存的目標')
            self.assertEqual(reply_editor.content.get('1.0','end').strip(),'尚未送出的回答')
            view.busy=True;view.select();self.assertEqual(str(view.guidance['state']),'disabled')
            view.busy=False;autonomy.update(store,gid,'report_ready',result='結果')
            view.refresh(gid);self.assertEqual(str(view.guidance['state']),'disabled')
            editor.reload();self.assertEqual(str(editor.save_button['state']),'disabled')

    def test_goal_reply_editor_exposes_fixed_question_and_reachable_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite3');sid=store.new_session()
            gid=autonomy.create_goal(store,sid,'核對人數')
            autonomy.update(store,gid,'needs_input',question='有幾人？')
            view=AutonomousWindow(self.root,store,sid,None);self.allocate(view)
            view.refresh(gid);editor=view.add_reply();layout=self.allocate(editor)
            self.assertIn('有幾人？',editor.context.get('1.0','end'))
            for widget in (editor.content,editor.save_button,editor.resume_button,editor.reload_button):
                self.assert_reachable(layout,widget)
            self.assertEqual(str(editor.resume_button['state']),'normal')

    def test_stale_guidance_refreshes_plan_before_preserving_confirmed_work_and_finishing_read(self):
        from assistant.backup import create_backup, restore_backup, validate_database
        from assistant.goal_guidance_ui import GoalGuidanceWindow
        from assistant.goal_reply_ui import GoalReplyWindow
        from test_autonomy import ScriptedEngine, action
        from test_work_plan import item

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / 'state.sqlite3')
            sid = store.new_session()
            document = root / 'handoff.txt'
            document.write_text('青櫃負責本批收件。\n檢核碼是 CK-35。\n', encoding='utf-8')
            did = store.attach_document(sid, document)
            gid = autonomy.create_goal(store, sid, '確認領用人及入場門號，查讀檢核碼並交文字。')
            tasks = [item(1, '確認領用人'), item(2, '確認入場門號'), item(3, '查讀檢核碼'), item(4, '安排窗口')]
            requirement = {'id': 3, 'ranges': [{'document_id': did, 'start_line': 2, 'end_line': 2}]}
            parts = [{'question': '領用人是誰？', 'items': [1]}, {'question': '入場門號是多少？', 'items': [2]},
                     {'question': '窗口是誰？', 'items': [4]}]
            initial = ScriptedEngine(action('update_plan', items=tasks, verify_delivery=True, evidence_requirements=[requirement]),
                                     action('ask', question='\n'.join(part['question'] for part in parts), plan_questions=parts))
            autonomy.run_goal(store, sid, gid, initial)
            changes = []
            editor = GoalGuidanceWindow(self.root, store, sid, gid, changed=lambda: changes.append(gid))
            editor.window.withdraw()
            draft = '取消「確認入場門號」。  '
            editor.content.insert('1.0', draft)

            reply = GoalReplyWindow(self.root, store, sid, gid, prepare_resume=lambda: 3, saved=lambda *_: None)
            reply.window.withdraw()
            reply.content.insert('1.0', '取消「安排窗口」。領用人是林曉晴。入場門號尚未確認。')
            reply.save()
            before = autonomy.get_goal(store, sid, gid)
            self.assertEqual(before['question'], '入場門號是多少？')
            self.assertEqual(before['work_plan']['items'][0]['state'], 'done')
            self.assertEqual(before['work_plan']['items'][2]['state'], 'pending')

            with patch('assistant.goal_guidance_ui.messagebox.showerror') as error:
                editor.save()
            error.assert_not_called()
            self.assertEqual(editor.content.get('1.0', 'end-1c'), draft)
            self.assertEqual(changes, [])
            self.assertEqual(autonomy.get_goal(store, sid, gid), before)
            context = editor.context.get('1.0', 'end')
            self.assertIn('尚待回答：入場門號是多少？', context)
            self.assertIn('確認領用人 — 助理回報完成', context)
            self.assertIn('查讀檢核碼 — 待處理', context)
            self.assertIn('領用人是林曉晴', context)

            editor.save()
            self.assertEqual(changes, [gid])
            revised = autonomy.get_goal(store, sid, gid)
            self.assertEqual(revised['status'], 'paused')
            self.assertEqual(revised['work_plan']['items'][0]['evidence_steps'], before['work_plan']['items'][0]['evidence_steps'])
            self.assertEqual(revised['work_plan']['delivery_review']['open_items'], [3])
            backup = create_backup(store.path, root / 'before-read.paibackup')
            restore_backup(backup['path'], root / 'restored')
            restored = Store(root / 'restored' / 'assistant.sqlite3')
            self.assertEqual(autonomy.get_goal(restored, sid, gid), revised)
            ref = len(revised['steps']) + 2
            items = [dict(row) for row in revised['work_plan']['items']]
            items[2].update(state='done', evidence_steps=[ref])
            finish = ScriptedEngine(action('finish', report='已完成。'),
                                    action('read', document_id=did, start_line=2, end_line=2),
                                    action('update_plan', items=items, evidence_requirements=[requirement]),
                                    action('finish', report=f'領用人林曉晴 [補充#2]。檢核碼 CK-35 [D{did}:L2]。'))
            final = autonomy.run_goal(restored, sid, gid, finish)
            self.assertEqual(final['status'], 'report_ready')
            self.assertIn('未完成項目 3', final['steps'][ref - 2]['observation']['error'])
            self.assertEqual(final['work_plan']['items'][2]['evidence_steps'], [ref])
            self.assertTrue(final['work_plan']['delivery_review']['ready'])
            self.assertEqual(autonomy.get_goal(store, sid, gid), revised)
            validate_database(restored.path)

    def test_app_memory_draft_entry_keeps_session_and_close_requests_worker_stop(self):
        import threading
        from assistant.gui import App
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite3')
            app=App(self.root,store,None);sid=app.sid
            fake=SimpleNamespace(cancel=threading.Event(),busy=True)
            with patch('assistant.memory_draft_ui.MemoryDraftWindow',return_value=fake) as constructor:
                app.draft_memories()
            self.assertEqual(constructor.call_args.args[2],sid)
            self.assertEqual(app.sid,sid)
            app.close()
            self.assertTrue(fake.cancel.is_set())
            self.assertTrue(self.root.winfo_exists())
            fake.busy=False
