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


    def compound_waiting(self):
        title = '青梣 S4 的燈箱巡檢'
        third = autonomy.create_goal(self.store, self.sid, title)
        autonomy.run_goal(self.store, self.sid, third, ScriptedEngine(action('ask', question='復測地點是什麼？')))
        query = (f'比較「{self.titles[0]}」的「取交櫃」、「{self.titles[1]}」的「北側使用的驗收人」'
                 f'和「{title}」的「復測地點」最後已確認的欄值。每件一行，缺值填待確認。')
        Assistant(self.store, ScriptedEngine('枇翠櫃。驗收人及復測地點待確認。')).ask(self.sid, query)
        remaining = f'「{title}」的「復測地點」是哪裡？'
        gid, waiting = self.waiting(self.request + '\n' + remaining, files=True)
        return gid, waiting, third, remaining

    def test_compound_wait_preserves_original_step_and_resumes_after_partial_backup(self):
        gid, waiting, third, remaining = self.compound_waiting()
        self.assertEqual(waiting['comparison_waiting'], {'ask_sequence': 1, 'rows': [1, 2]})
        idle = ScriptedEngine()
        self.assertEqual(autonomy.run_goal(self.store, self.sid, gid, idle), waiting)
        self.assertFalse(idle.prompts)

        self.save_reply(self.pending, '葉光')
        partial = '取交櫃：枇翠櫃（補充 1）\n驗收人：葉光（補充 1）\n復測地點：待確認'
        engine = ScriptedEngine(action('ask', question=remaining, partial_result=partial))
        state = continuous.run_continuous(self.store, self.sid, gid, engine, max_steps=1, max_seconds=60)
        self.assertEqual(state['status'], 'needs_input')
        self.assertEqual(state['question'], remaining)
        self.assertEqual(state['steps'][0], waiting['steps'][0])
        self.assertEqual(state['steps'][1]['observation']['partial_result'], partial)
        self.assertEqual(state['comparison_waiting'], {'ask_sequence': 2, 'row': 2})
        self.assertNotIn('reply_sources', state)
        self.assertNotIn('withdrawn_questions', state)
        self.assertEqual(state['comparison_resolved_questions'], [
            {'ask_sequence': 1, 'question': waiting['question'], 'work': self.titles[1],
             'field': '北側使用的驗收人', 'scope': '北側'}])
        prompt = json.loads(engine.prompts[0].rsplit('\n目標與已觀察資料：\n', 1)[1])
        self.assertEqual([row.get('value') for row in prompt['verified_work_fields']], ['枇翠櫃', '葉光', None])
        self.assertEqual(prompt['comparison_resolved_questions'], state['comparison_resolved_questions'])

        restore_backup(create_backup(self.store.path)['path'], self.root / 'partial')
        self.store = Store(self.root / 'partial' / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(self.store, self.sid, gid), state)
        self.save_reply(third, '銀竹室')
        parents = [autonomy.get_goal(self.store, self.sid, work) for work in (self.source, self.pending, third)]
        content = '枇翠櫃｜葉光｜銀竹室'
        engine = ScriptedEngine(action('write_artifact', filename='complete.md', content=content),
                                action('deliver', filenames=['complete.md'], report='已交付。'))
        complete = autonomy.run_goal(self.store, self.sid, gid, engine)
        self.assertEqual(complete['status'], 'report_ready')
        self.assertEqual(complete['steps'][:2], state['steps'])
        self.assertEqual([(row['ask_sequence'], row['field']) for row in complete['comparison_resolved_questions']],
                         [(1, '北側使用的驗收人'), (2, '復測地點')])
        self.assertEqual([autonomy.get_goal(self.store, self.sid, work) for work in (self.source, self.pending, third)],
                         parents)
        file = artifacts.directory(self.store, gid) / 'complete.md'
        restore_backup(create_backup(self.store.path)['path'], self.root / 'complete')
        copy = Store(self.root / 'complete' / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(copy, self.sid, gid), complete)
        artifacts.rebuild(copy, self.sid, gid)
        self.assertEqual((artifacts.directory(copy, gid) / 'complete.md').read_bytes(), file.read_bytes())

    def test_compound_resume_records_every_confirmed_field_and_rejects_forged_subset(self):
        gid, waiting, third, _ = self.compound_waiting()
        self.save_reply(self.pending, '葉光')
        self.save_reply(third, '銀竹室')
        engine = ScriptedEngine(action('write_artifact', filename='all.md', content='枇翠櫃｜葉光｜銀竹室'),
                                action('deliver', filenames=['all.md'], report='已交付。'))
        state = autonomy.run_goal(self.store, self.sid, gid, engine)
        self.assertEqual(state['status'], 'report_ready')
        self.assertEqual(state['steps'][0], waiting['steps'][0])
        self.assertEqual([(row['ask_sequence'], row['field']) for row in state['comparison_resolved_questions']],
                         [(1, '北側使用的驗收人'), (1, '復測地點')])
        restore_backup(create_backup(self.store.path)['path'], self.root / 'all')
        restored = Store(self.root / 'all' / 'assistant.sqlite3')
        self.assertEqual(autonomy.get_goal(restored, self.sid, gid), state)
        with restored.connect() as db:
            raw, = db.execute('SELECT receipt_json FROM autonomous_comparison_resumes WHERE goal_id=?', (gid,)).fetchone()
            receipt = json.loads(raw)
            self.assertEqual(receipt['version'], 2)
            self.assertEqual(receipt['rows'], [1, 2])
            receipt['rows'] = [2]
            db.execute('UPDATE autonomous_comparison_resumes SET receipt_json=?,receipt_sha256=? WHERE goal_id=?',
                       (json.dumps(receipt, ensure_ascii=False), goal_conversation.digest(receipt), gid))
        with self.assertRaisesRegex(ValueError, '保存來源'):
            validate_database(restored.path)

    def test_compound_wait_does_not_consume_unrelated_duplicate_or_wrong_scope_question(self):
        _, _, third, remaining = self.compound_waiting()
        bad = (
            self.request + remaining + '還缺檔名嗎？',
            self.request + remaining + remaining,
            self.request + remaining.replace('復測地點', '北翼的復測地點'),
        )
        cases = [self.waiting(question) for question in bad]
        self.save_reply(self.pending, '葉光')
        self.save_reply(third, '銀竹室')
        for other, waiting in cases:
            with self.subTest(question=waiting['question']):
                self.assertNotIn('comparison_waiting', waiting)
                engine = ScriptedEngine()
                self.assertEqual(autonomy.run_goal(self.store, self.sid, other, engine), waiting)
                self.assertFalse(engine.prompts)


if __name__ == '__main__':
    unittest.main()
