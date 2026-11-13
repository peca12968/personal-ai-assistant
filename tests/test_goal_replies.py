import json
import os
import subprocess
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

from assistant import autonomy as a, goal_replies as r, goal_guidance
from assistant.answer_runs import lease, LeaseBusy
from assistant.autonomy_ui import AutonomousWindow
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class ReplyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.store = Store(self.folder / 'state.sqlite3'); self.sid = self.store.new_session()
        self.gid = self.ask('人數？')

    def ask(self, question):
        gid = a.create_goal(self.store, self.sid, '計算所需數量')
        a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question=question)))
        return gid

    def token(self, gid=None):
        return r.snapshot(self.store, self.sid, gid or self.gid)['fingerprint']

    def save(self, answer, token=None):
        return a.reply(self.store, self.sid, self.gid, answer, expected_fingerprint=token or self.token())

    def test_repeated_question_and_replay_reject_old_answer_without_mutation(self):
        old = self.token(); self.save('3人', old)
        a.run_goal(self.store, self.sid, self.gid, ScriptedEngine(action('ask', question='人數？')))
        before = a.get_goal(self.store, self.sid, self.gid)
        with self.assertRaisesRegex(r.StaleReply, '已變更'): self.save('5人', old)
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid), before)
        result = self.save('改成5人')
        self.assertEqual(result['goal']['status'], 'paused')
        with self.assertRaises(ValueError): self.save('重送', result['fingerprint'])

    def test_other_goal_session_and_missing_tokens_are_rejected(self):
        other = self.ask('份數？')
        for token in (None, '', '0'*64, self.token(other)):
            with self.assertRaises(ValueError):
                r.save(self.store, self.sid, self.gid, '3人', expected_fingerprint=token)
        with self.assertRaises(ValueError):
            r.save(self.store, self.store.new_session(), self.gid, '3人', expected_fingerprint=self.token())
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid)['notes'], '')

    def test_guidance_and_sources_invalidate_token_and_save_keeps_scope(self):
        old = self.token()
        goal_guidance.save(self.store, self.sid, self.gid, '不要產檔', expected_fingerprint=old)
        with self.assertRaises(ValueError): self.save('3人', old)
        old = self.token(); path = self.folder/'note.txt'; path.write_text('新資料', encoding='utf-8')
        self.store.attach_document(self.sid, path)
        with self.assertRaises(ValueError): self.save('3人', old)
        self.save('3人')  # Explicitly reviewed changed sources: save for a new stage.
        result = a.get_goal(self.store, self.sid, self.gid)
        self.assertFalse(result['file_delivery']); self.assertIsNone(result['csv_source'])
        self.assertEqual(len(result['steps']), 1); self.assertIn('不要產檔', result['notes'])
        self.assertEqual(result['sources'], [])
        engine = ScriptedEngine()
        a.run_goal(self.store, self.sid, self.gid, engine)
        self.assertEqual(engine.prompts, [])

    def test_lease_and_overflow_leave_question_and_notes_unchanged(self):
        token = self.token()
        with lease(self.store, self.gid):
            with self.assertRaises(LeaseBusy): self.save('3人', token)
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_goals SET notes=? WHERE id=?', ('舊'*11999, self.gid))
        before = a.get_goal(self.store, self.sid, self.gid)
        with self.assertRaises(ValueError): self.save('3人')
        self.assertEqual(a.get_goal(self.store, self.sid, self.gid), before)

    def test_cli_requires_observed_fingerprint_and_does_not_execute(self):
        base = [sys.executable, '-m', 'assistant', '--data-dir', str(self.folder), '--session', self.sid]
        # CLI uses its conventional database name.
        db_path = self.folder/'assistant.sqlite3'
        with self.store.connect() as source:
            import sqlite3
            with closing(sqlite3.connect(db_path)) as dest: source.backup(dest)
        def command(*args):
            return subprocess.run(base+list(args), capture_output=True, text=True, encoding='utf-8',
                                  env={**os.environ, 'PYTHONIOENCODING':'utf-8'})
        observed = command('--goal-reply-status', self.gid)
        self.assertEqual(observed.returncode, 0, observed.stderr)
        token = json.loads(observed.stdout)['fingerprint']
        self.assertNotEqual(command('--reply-goal', self.gid, '--goal-reply', '3人').returncode, 0)
        saved = command('--reply-goal', self.gid, '--goal-reply', '3人', '--goal-fingerprint', token)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        result = json.loads(saved.stdout)
        self.assertEqual(result['status'], 'paused'); self.assertEqual(len(result['steps']), 1)
        self.assertNotEqual(command('--reply-goal', self.gid, '--goal-reply', '5人', '--goal-fingerprint', token).returncode, 0)


class ReplyUiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk(); self.root.withdraw()
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name)/'state.sqlite3'); self.sid = self.store.new_session()
        self.ids = []
        for question in ('人數？', '份數？'):
            gid = a.create_goal(self.store, self.sid, '核對'+question)
            a.run_goal(self.store, self.sid, gid, ScriptedEngine(action('ask', question=question)))
            self.ids.append(gid)
        self.engine = ScriptedEngine(action('finish', report='依回答完成整理'))
        self.view = AutonomousWindow(self.root, self.store, self.sid, self.engine)
        self.view.window.withdraw(); self.view.refresh(self.ids[0])
        self.editor = self.view.add_reply(); self.editor.window.withdraw()
        def dispose():
            for timer in self.root.tk.call('after', 'info'): self.root.tk.call('after', 'cancel', timer)
            self.root.destroy()
        self.addCleanup(dispose)

    def test_selection_change_cannot_redirect_answer_and_save_only_never_calls_engine(self):
        self.editor.content.insert('1.0', '3人'); self.view.refresh(self.ids[1]); self.editor.save()
        self.assertIn('人數？\n使用者補充：3人', a.get_goal(self.store,self.sid,self.ids[0])['notes'])
        self.assertEqual(a.get_goal(self.store,self.sid,self.ids[1])['notes'], '')
        self.assertEqual(self.engine.prompts, []); self.assertFalse(self.view.busy)

    def test_stale_reload_keeps_draft_and_requires_new_explicit_save(self):
        self.editor.content.insert('1.0', '5人')
        r.save(self.store,self.sid,self.ids[0],'3人',expected_fingerprint=self.editor.current['fingerprint'])
        a.run_goal(self.store,self.sid,self.ids[0],ScriptedEngine(action('ask',question='份數？')))
        with patch('assistant.goal_reply_ui.messagebox.showerror') as show: self.editor.save(resume=True)
        show.assert_not_called()
        self.assertEqual(self.editor.content.get('1.0','end').strip(),'5人')
        self.assertFalse(self.view.busy)
        context = self.editor.context.get('1.0', 'end')
        self.assertIn('目前問題：份數？', context)
        self.assertIn('草稿輸入時的問題：\n人數？', context)
        self.assertEqual(self.editor.content.get('1.0','end').strip(),'5人')
        self.assertEqual(a.get_goal(self.store,self.sid,self.ids[0])['status'],'needs_input')

    def test_withdrawn_subquestion_keeps_draft_for_review_and_preserves_prior_work(self):
        source = self.store.path.parent / 'packing.txt'
        source.write_text('固定窗口為柳櫃。\n', encoding='utf-8')
        doc_id = self.store.attach_document(self.sid, source)
        gid = a.create_goal(self.store, self.sid, '整理本批裝箱通知，列總片數、固定窗口及簽收人。')
        original_question = '櫃號是多少？簽收人是誰？'
        before = a.run_goal(self.store, self.sid, gid, ScriptedEngine(
            action('read', document_id=doc_id, start_line=1, end_line=1),
            action('calculate', operation='product', values=['6', '8']),
            action('ask', question=original_question, partial_result=f'固定窗口為柳櫃 [D{doc_id}:L1]，總片數 48。')))
        self.view.refresh(gid)
        editor = self.view.add_reply(); editor.window.withdraw()
        draft = '櫃號是 C13。\n簽收人是何雨安。  '
        editor.content.insert('1.0', draft)
        goal_guidance.save(self.store, self.sid, gid, '新版免填櫃號，原窗口和片數照舊。撤下分問「櫃號是多少？」。',
                           expected_fingerprint=editor.current['fingerprint'])
        changed = a.get_goal(self.store, self.sid, gid)

        with patch('assistant.goal_reply_ui.messagebox.showerror') as show:
            editor.save(resume=True)
        show.assert_not_called()
        context = editor.context.get('1.0', 'end')
        self.assertIn('目前問題：簽收人是誰？', context)
        self.assertIn('草稿輸入時的問題：\n' + original_question, context)
        self.assertEqual(editor.content.get('1.0', 'end-1c'), draft)
        self.assertIn('刪除已不適用的內容', editor.notice.get())
        self.assertEqual(a.get_goal(self.store, self.sid, gid), changed)
        self.assertFalse(self.view.busy)
        self.assertEqual(self.engine.prompts, [])
        editor.reload()
        self.assertIn('草稿輸入時的問題：\n' + original_question, editor.context.get('1.0', 'end'))

        editor.content.delete('1.0', 'end')
        editor.content.insert('1.0', '簽收人是何雨安。')
        editor.save()
        saved = a.get_goal(self.store, self.sid, gid)
        self.assertEqual(saved['steps'][:len(before['steps'])], before['steps'])
        self.assertEqual(saved['reply_sources'][-1]['question'], '簽收人是誰？')
        self.assertEqual(saved['reply_sources'][-1]['answer'], '簽收人是何雨安。')
        self.assertNotIn('C13', saved['notes'])
        self.assertNotIn('草稿輸入時的問題', editor.context.get('1.0', 'end'))
        engine = ScriptedEngine(action('finish', report=f'柳櫃 [D{doc_id}:L1]，總片數 48，簽收人何雨安 [補充#4]。'))
        final = a.run_goal(self.store, self.sid, gid, engine)
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual(len(engine.prompts), 1)
        context = json.loads(engine.prompts[0].rsplit('目標與已觀察資料：\n', 1)[1])
        self.assertEqual(context['withdrawn_questions'], [{'ask_sequence': 3, 'guidance_citation': '[更正#1]'}])
        self.assertIn('48', json.dumps(context['steps'][1]['observation']))
        self.assertEqual(context['reply_sources'][-1]['answer'], '簽收人是何雨安。')

    def test_stale_draft_stays_visible_if_the_entire_question_is_withdrawn(self):
        self.editor.content.insert('1.0', '還未送出的草稿')
        goal_guidance.save(self.store, self.sid, self.ids[0], '已取消這項工作。請撤下目前問題。',
                           expected_fingerprint=self.editor.current['fingerprint'])
        self.editor.save()
        self.assertEqual(str(self.editor.save_button['state']), 'disabled')
        self.assertEqual(str(self.editor.resume_button['state']), 'disabled')
        self.assertEqual(self.editor.content.get('1.0', 'end-1c'), '還未送出的草稿')
        self.assertIn('草稿輸入時的問題：\n人數？', self.editor.context.get('1.0', 'end'))
        self.assertFalse(a.get_goal(self.store, self.sid, self.ids[0]).get('reply_sources'))

    def test_save_and_resume_targets_fixed_goal_with_ordinary_budget(self):
        self.editor.content.insert('1.0','3人'); self.view.budget.set('1'); self.view.refresh(self.ids[1])
        class InlineThread:
            def __init__(self,target,daemon): self.target=target
            def start(self): self.target()
        with patch('assistant.autonomy_ui.threading.Thread',InlineThread): self.editor.save(resume=True)
        self.assertEqual(self.view.active_goal,self.ids[0])
        self.view.window.after_cancel(self.view.poll_timer); self.view.poll()
        result=a.get_goal(self.store,self.sid,self.ids[0])
        self.assertEqual(result['status'],'report_ready'); self.assertEqual(len(result['steps']),2)
        self.assertEqual(result['continuous_runs'],[]); self.assertIn('3人',self.engine.prompts[0])
        self.assertEqual(a.get_goal(self.store,self.sid,self.ids[1])['status'],'needs_input')

    def test_invalid_budget_busy_and_start_failure_do_not_lose_saved_state(self):
        self.editor.content.insert('1.0','3人'); self.view.budget.set('99')
        with patch('assistant.goal_reply_ui.messagebox.showerror'): self.editor.save(resume=True)
        self.assertEqual(a.get_goal(self.store,self.sid,self.ids[0])['status'],'needs_input')
        self.view.budget.set('2'); self.view.busy=True
        with patch('assistant.goal_reply_ui.messagebox.showerror'): self.editor.save(resume=True)
        self.assertEqual(self.editor.content.get('1.0','end').strip(),'3人'); self.view.busy=False
        with patch('assistant.autonomy_ui.threading.Thread.start',side_effect=RuntimeError('無法啟動')):
            self.editor.save(resume=True)
        self.assertEqual(a.get_goal(self.store,self.sid,self.ids[0])['status'],'paused')
        self.assertIn('回答已保存，但未能啟動', self.editor.notice.get())
        self.assertEqual(self.editor.content.get('1.0','end').strip(),'')
        self.assertTrue(self.view.busy)  # Pending launch failure still owns the UI.
        with patch('assistant.autonomy_ui.threading.Thread') as thread:
            self.view.begin()
            thread.assert_not_called()
        self.assertEqual(self.view.events.qsize(), 1)
        self.view.window.after_cancel(self.view.poll_timer); self.view.poll()
        self.assertFalse(self.view.busy)
        self.assertEqual(str(self.view.start['state']),'normal')
        self.assertIn('接續未啟動',self.view.notice.get())


if __name__ == '__main__': unittest.main()
