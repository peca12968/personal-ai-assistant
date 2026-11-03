import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import goal_drafts as d
from assistant.autonomy_ui import AutonomousWindow
from assistant.store import Store
from test_autonomy import ScriptedEngine
from test_goal_drafts import proposal


class GoalDraftUiTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw()
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.store=Store(Path(temp.name)/'state.sqlite3');self.sid=self.store.new_session()
        self.raw='比較兩方案六個月費用，不需產檔。'
        self.mid=self.store.add_message(self.sid,'user',self.raw)
        self.engine=ScriptedEngine(json.dumps(proposal('M'+str(self.mid),self.raw)))
        self.view=AutonomousWindow(self.root,self.store,self.sid,self.engine,initial_objective='依對話整理')
        self.view.window.withdraw()
        def dispose():
            for timer in self.root.tk.call('after','info'):self.root.tk.call('after','cancel',timer)
            self.root.destroy()
        self.addCleanup(dispose)

    def result(self):
        source=d.capture(self.store,self.sid,'依對話整理')
        return d.generate(source,self.engine)

    def test_fill_then_explicit_save_keeps_permissions_and_pending_status(self):
        result=self.result();self.view.receive_draft(result,'依對話整理')
        self.assertEqual(self.view.objective.get('1.0','end').strip(),result['objective_text'])
        self.assertFalse(self.view.from_chat.get());self.assertFalse(self.view.file_delivery.get())
        self.assertIsNone(self.view.selected_csv_id())
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM autonomous_goals').fetchone()[0],0)
        self.view.preview_draft()
        self.view.new_goal()
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT status FROM autonomous_goals').fetchone()[0],'pending')

    def test_typing_and_source_changes_preserve_user_text_and_keep_review_available(self):
        result=self.result()
        self.view.objective.insert('end','；我正在補充')
        edited=self.view.objective.get('1.0','end-1c')
        self.view.receive_draft(result,'依對話整理')
        self.assertEqual(self.view.objective.get('1.0','end-1c'),edited)
        self.assertIs(self.view.goal_draft_result,result)
        self.view.objective.delete('1.0','end');self.view.objective.insert('1.0','依對話整理')
        self.store.add_message(self.sid,'user','更正：改成三個月。')
        self.view.receive_draft(result,'依對話整理')
        self.assertEqual(self.view.objective.get('1.0','end-1c'),'依對話整理')
        self.assertIn('已變更',self.view.notice.get())

    def test_new_message_after_fill_prevents_stale_save_and_manual_rewrite_is_preserved(self):
        self.view.receive_draft(self.result(),'依對話整理')
        self.store.add_message(self.sid,'user','更正：改成三個月。')
        with patch('assistant.autonomy_ui.messagebox.showerror') as error:
            self.view.new_goal();self.assertTrue(error.called)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM autonomous_goals').fetchone()[0],0)
        self.view.objective.delete('1.0','end');self.view.objective.insert('1.0','比較三個月費用。')
        self.assertIsNone(self.view.draft_guard())
        self.view.new_goal()
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT objective FROM autonomous_goals').fetchone()[0],'比較三個月費用。')

    def test_generation_event_restores_controls_without_saving_and_close_cancels_timer(self):
        class InlineThread:
            def __init__(self,target,daemon):self.target=target
            def start(self):self.target()
        with patch('assistant.autonomy_ui.threading.Thread',InlineThread):self.view.draft_from_chat()
        self.assertTrue(self.view.busy)
        self.view.window.after_cancel(self.view.poll_timer)
        self.view.poll()
        self.assertFalse(self.view.busy)
        self.assertEqual(str(self.view.draft_button['state']),'normal')
        self.assertIn('比較兩方案六個月',self.view.objective.get('1.0','end'))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM autonomous_goals').fetchone()[0],0)
        timer=self.view.poll_timer;self.view.close()
        self.assertNotIn(timer,self.root.tk.call('after','info'))


if __name__=='__main__':unittest.main()
