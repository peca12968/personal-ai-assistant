import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import artifacts, autonomy, continuous, goal_conversation, goal_guidance, goal_replies
from assistant.autonomy_ui import AutonomousWindow
from assistant.backup import create_backup, restore_backup, validate_database
from assistant.engine import Assistant
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class ComparisonResumeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / 'source.sqlite3')
        self.sid = self.store.new_session()
        self.titles = ('紋杉 C3 的底片交換', '楓里 A2 的夜班點收')
        self.source, self.pending = [autonomy.create_goal(self.store, self.sid, name) for name in self.titles]
        autonomy.run_goal(self.store, self.sid, self.source, ScriptedEngine(action('ask', question='取交櫃是什麼？')))
        self.save_reply(self.source, '枇翠櫃')
        autonomy.run_goal(self.store, self.sid, self.pending, ScriptedEngine(action('ask', question='請提供北側使用的驗收人。')))
        query = (f'比較「{self.titles[0]}」的「取交櫃」和「{self.titles[1]}」的「北側使用的驗收人」'
                 '最後已確認的欄值。每件一行。')
        Assistant(self.store, ScriptedEngine('枇翠櫃與待確認。')).ask(self.sid, query)
        self.request = (f'請問「{self.titles[1]}」的「北側的驗收人」目前正式確認的值是什麼？'
                        '請一併提供該值的來源種類和編號。')

    def save_reply(self, gid, answer):
        view = goal_replies.snapshot(self.store, self.sid, gid)
        goal_replies.save(self.store, self.sid, gid, answer, expected_fingerprint=view['fingerprint'])

    def waiting(self, question, *, files=False):
        objective = '沿用剛才的比較表，整理交接清單。問題用「完整工作名稱」的「完整欄名」問該欄值。'
        gid = autonomy.create_goal(self.store, self.sid, objective,
                                   from_chat=True, file_delivery=files)
        state = autonomy.run_goal(self.store, self.sid, gid, ScriptedEngine(
            action('ask', question=question, partial_result='底片交換的取交櫃為枇翠櫃。')))
        self.assertEqual(state['status'], 'needs_input')
        return gid, state

    def test_waiting_button_rechecks_only_bound_source_and_delivers_after_restore(self):
        gid, waiting = self.waiting(self.request, files=True)
        self.assertEqual(waiting['comparison_waiting'], {'ask_sequence': 1, 'row': 1})
        proposal = goal_guidance.snapshot(self.store, self.sid, self.pending)
        goal_guidance.save(self.store, self.sid, self.pending, '提議：北側使用的驗收人改為張榕。',
                           expected_fingerprint=proposal['fingerprint'])
        idle = ScriptedEngine()
        still_waiting = autonomy.run_goal(self.store, self.sid, gid, idle)
        self.assertEqual(still_waiting, waiting)
        self.assertFalse(idle.prompts)
        self.save_reply(self.pending, '葉光')

        archive = create_backup(self.store.path)
        restore_backup(archive['path'], self.root / 'resume')
        store = Store(self.root / 'resume' / 'assistant.sqlite3')
        sources = [autonomy.get_goal(store, self.sid, work) for work in (self.source, self.pending)]
        engine = ScriptedEngine(action('write_artifact', filename='handoff.md', content='枇翠櫃｜葉光'),
                                action('deliver', filenames=['handoff.md'], report='已核對並交付。'))
        root = tk.Tk()
        root.withdraw()
        try:
            view = AutonomousWindow(root, store, self.sid, engine)
            view.window.withdraw()
            view.refresh(gid)
            self.assertEqual(view.start['text'], '重查原工作並接續')
            self.assertEqual(str(view.start['state']), 'normal')
            with patch('assistant.autonomy_ui.start_worker', side_effect=lambda work, error: work()):
                view.start.invoke()
        finally:
            for timer in root.tk.call('after', 'info'):
                root.tk.call('after', 'cancel', timer)
            root.destroy()
        final = autonomy.get_goal(store, self.sid, gid)
        self.assertEqual(final['status'], 'report_ready')
        self.assertEqual(final['steps'][0], waiting['steps'][0])
        self.assertNotIn('reply_sources', final)
        self.assertEqual(final['comparison_resolved_questions'], [
            {'ask_sequence': 1, 'question': self.request, 'work': self.titles[1],
             'field': '北側使用的驗收人', 'scope': '北側'}])
        self.assertEqual([autonomy.get_goal(store, self.sid, work) for work in (self.source, self.pending)], sources)
        self.assertEqual(len(engine.prompts), 2)
        first_prompt = json.loads(engine.prompts[0].rsplit('\n目標與已觀察資料：\n', 1)[1])
        self.assertEqual([row['value'] for row in first_prompt['verified_work_fields']], ['枇翠櫃', '葉光'])
        self.assertEqual(first_prompt['comparison_resolved_questions'], final['comparison_resolved_questions'])
        file = artifacts.directory(store, gid) / 'handoff.md'
        saved = create_backup(store.path)
        restore_backup(saved['path'], self.root / 'after')
        after = Store(self.root / 'after' / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(after, self.sid, gid), final)
        artifacts.rebuild(after, self.sid, gid)
        self.assertEqual((artifacts.directory(after, gid) / 'handoff.md').read_bytes(), file.read_bytes())

        with after.connect() as db:
            raw, = db.execute('SELECT receipt_json FROM autonomous_comparison_resumes WHERE goal_id=?', (gid,)).fetchone()
            receipt = json.loads(raw)
            receipt['evidence']['rows'][1]['value'] = '假值'
            db.execute('UPDATE autonomous_comparison_resumes SET receipt_json=?,receipt_sha256=? WHERE goal_id=?',
                       (json.dumps(receipt, ensure_ascii=False), goal_conversation.digest(receipt), gid))
        with self.assertRaisesRegex(ValueError, '保存來源'):
            validate_database(after.path)

    def test_continuous_resumes_exact_question_and_keeps_other_questions(self):
        legitimate, good = self.waiting(self.request)
        combined, _ = self.waiting(self.request + '還缺檔名嗎？')
        wrong_scope, _ = self.waiting(f'「{self.titles[1]}」的「南側用的驗收人」是誰？')
        self.save_reply(self.pending, '葉光')
        for unrelated in (combined, wrong_scope):
            engine = ScriptedEngine()
            held = continuous.run_continuous(self.store, self.sid, unrelated, engine, max_steps=1, max_seconds=60)
            self.assertEqual(held['status'], 'needs_input')
            self.assertNotIn('comparison_waiting', held)
            self.assertFalse(engine.prompts)
        engine = ScriptedEngine(action('finish', report='交收枇翠櫃，驗收葉光。'))
        finish = continuous.run_continuous(self.store, self.sid, legitimate, engine, max_steps=1, max_seconds=60)
        self.assertEqual(finish['status'], 'report_ready')
        self.assertEqual(finish['steps'][0], good['steps'][0])
        self.assertEqual(len(finish['steps']), 2)
        self.assertEqual(finish['continuous_runs'][0]['end_step'] - finish['continuous_runs'][0]['start_step'], 1)


if __name__ == '__main__':
    unittest.main()
