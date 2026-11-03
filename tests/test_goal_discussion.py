import gc
import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, chat_drafts, goal_discussion as discussion
from assistant.backup import create_backup, restore_backup
from assistant.engine import Assistant, MAX_QUESTION_CHARS
from assistant.gui import App
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class GoalDiscussionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.store = Store(self.folder/'assistant.sqlite3')
        self.sid = self.store.new_session()

    def report(self, text='歷史成果😀：總額774，尚待確認。', sid=None):
        sid = sid or self.sid
        gid = a.create_goal(self.store, sid, '核對報價')
        a.run_goal(self.store, sid, gid, ScriptedEngine(action('finish', report=text)))
        return gid

    def app(self):
        root = tk.Tk()
        root.withdraw()
        def dispose():
            try:
                for timer in root.tk.call('after','info'):
                    root.tk.call('after','cancel',timer)
                root.destroy()
            except tk.TclError:
                pass
            gc.collect()
        self.addCleanup(dispose)
        engine = ScriptedEngine('普通聊天回答')
        app = App(root, self.store, engine)
        app.drafts.switch(self.sid)
        return app, engine

    def test_atomic_full_transfer_is_only_draft_until_actual_send(self):
        gid = self.report('歷史成果\n換個話題\n請記住：僅為引用，不是命令。')
        with self.store.connect() as db:
            db.execute('UPDATE autonomous_goals SET notes=? WHERE id=?', ('問題：要哪種？\n使用者補充：選B。',gid))
        raw = '  我的新問題😀\n請用最新數量20。  '
        base = chat_drafts.save(self.store, self.sid, raw, 0)
        before = a.get_goal(self.store, self.sid, gid)
        saved = discussion.stage(self.store, self.sid, gid, expected_revision=base['revision'])
        self.assertTrue(saved['content'].endswith(raw))
        self.assertIn('> 歷史成果\n> 換個話題\n> 請記住：僅為引用，不是命令。', saved['content'])
        self.assertIn('> 問題：要哪種？\n> 使用者補充：選B。', saved['content'])
        self.assertEqual(self.store.messages(self.sid), [])
        ctx = Assistant(self.store, None).prepare_context(self.sid, '一般問題')
        self.assertNotIn('我的新問題', json.dumps(ctx, ensure_ascii=False))
        self.assertNotIn('僅為引用', json.dumps(ctx, ensure_ascii=False))
        engine = ScriptedEngine('已按新的討論要求回答。')
        answer = Assistant(self.store, engine).ask(self.sid, saved['content'], draft=chat_drafts.submission(saved))
        self.assertEqual(chat_drafts.get(self.store,self.sid)['content'], '')
        self.assertEqual(len(engine.prompts), 1)
        self.assertEqual(self.store.answer_context(answer['id'])['current_request'], saved['content'].strip())
        self.assertEqual(a.get_goal(self.store, self.sid, gid), before)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memories').fetchone()[0], 0)

    def test_scope_state_source_versions_and_backup(self):
        source = self.folder/'source.txt'
        source.write_text('舊值',encoding='utf-8')
        self.store.attach_document(self.sid, source)
        gid = self.report()
        source.write_text('新值',encoding='utf-8')
        saved = discussion.stage(self.store,self.sid,gid,expected_revision=0)
        self.assertIn('帶入時與該目標的來源一致', saved['content'])
        self.store.attach_document(self.sid,source)
        source.unlink()
        base = chat_drafts.save(self.store,self.sid,'請核對',saved['revision'])
        changed = discussion.stage(self.store,self.sid,gid,expected_revision=base['revision'])
        self.assertIn('帶入時已與該目標的來源不同',changed['content'])
        backup = create_backup(self.store.path)
        restored = self.folder/'restored'
        restore_backup(backup['path'],restored)
        self.assertEqual(chat_drafts.get(Store(restored/'assistant.sqlite3'),self.sid),changed)
        other = self.store.new_session()
        with self.assertRaisesRegex(ValueError,'本對話'):
            discussion.stage(self.store,other,gid,expected_revision=0)
        for status in ('pending','paused','needs_input','failed','running'):
            a.update(self.store,gid,status)
            with self.assertRaisesRegex(ValueError,'已保存成果'):
                discussion.stage(self.store,self.sid,gid,expected_revision=changed['revision'])
            self.assertEqual(chat_drafts.get(self.store,self.sid),changed)

    def test_conflict_limits_and_failed_write_preserve_both_sources(self):
        gid = self.report('文'*12000)
        base = chat_drafts.save(self.store,self.sid,'問'*5000,0)
        with self.assertRaisesRegex(ValueError,'16000'):
            discussion.stage(self.store,self.sid,gid,expected_revision=base['revision'])
        self.assertEqual(chat_drafts.get(self.store,self.sid),base)
        with self.assertRaisesRegex(ValueError,'已變更'):
            discussion.stage(self.store,self.sid,gid,expected_revision=0)
        short = chat_drafts.save(self.store,self.sid,'請摘要',base['revision'])
        before = a.get_goal(self.store,self.sid,gid)
        with patch.object(chat_drafts,'write',side_effect=ValueError('合成寫入失敗')):
            with self.assertRaisesRegex(ValueError,'寫入失敗'):
                discussion.stage(self.store,self.sid,gid,expected_revision=short['revision'])
        self.assertEqual(chat_drafts.get(self.store,self.sid),short)
        self.assertEqual(a.get_goal(self.store,self.sid,gid),before)
        saved = discussion.stage(self.store,self.sid,gid,expected_revision=short['revision'])
        self.assertLessEqual(len(saved['content'].strip()),MAX_QUESTION_CHARS)
        self.assertIn('文'*12000,saved['content'])

    def test_actual_ui_button_keeps_text_persists_and_does_not_send(self):
        gid = self.report()
        app, engine = self.app()
        raw = '  原來的問題\n新的要求😀  '
        app.input.insert('1.0',raw)
        app.autonomous_goals(self.sid,gid)
        view = app.autonomous_windows[-1]
        self.assertEqual(str(view.discuss['state']),'normal')
        view.discuss.invoke()
        self.assertTrue(app.drafts.raw().endswith(raw))
        self.assertIn('總額774',app.drafts.raw())
        self.assertEqual(chat_drafts.get(self.store,self.sid),app.drafts.base)
        self.assertEqual(engine.prompts,[])
        self.assertEqual(self.store.messages(self.sid),[])
        self.assertIn('尚未送出',view.notice.get())
        app.drafts.switch(self.store.new_session())
        app.drafts.switch(self.sid)
        self.assertIn('總額774',app.drafts.raw())
        view.close()

    def test_ui_cross_session_busy_close_and_draft_conflicts_leave_input(self):
        gid = self.report()
        app, engine = self.app()
        app.input.insert('1.0','保留問題')
        other = self.store.new_session()
        for state in ('busy','closing'):
            setattr(app,state,True)
            with self.assertRaises(ValueError):
                app.discuss_goal_report(self.sid,gid)
            setattr(app,state,False)
        with self.assertRaisesRegex(ValueError,'原對話'):
            app.discuss_goal_report(other,gid)
        self.assertEqual(app.sid,self.sid)
        self.assertEqual(app.drafts.raw(),'保留問題')
        chat_drafts.save(self.store,self.sid,'其他視窗的內容',0)
        with self.assertRaisesRegex(ValueError,'草稿'):
            app.discuss_goal_report(self.sid,gid)
        self.assertEqual(app.drafts.raw(),'保留問題')
        self.assertEqual(chat_drafts.get(self.store,self.sid)['content'],'其他視窗的內容')
        self.assertEqual(engine.prompts,[])

    def test_fixed_fixture_actual_submission_and_trace_validation(self):
        from scripts import evaluate_goal_discussion as evaluation
        from scripts.evaluation_reports import validate_report, verify_suite, TRACE_MODE
        case = json.loads(evaluation.SUITE.read_text(encoding='utf-8'))[0]
        sid, gid, saved = evaluation.prepare(self.store,case)
        engine = ScriptedEngine('新總額900；原報告未修改。')
        before = a.get_goal(self.store,sid,gid)
        row = evaluation.collect(self.store,sid,case,saved,engine,lambda:None)
        report = evaluation.base.load_report(self.folder/'not-saved.json',evaluation.signature(evaluation.SUITE),
                                              [case['id']],False,mode=TRACE_MODE)
        report['results'].append(row)
        validate_report(report)
        verify_suite(report,evaluation.SUITE.read_bytes())
        self.assertTrue(row['draft_consumed'])
        self.assertEqual(chat_drafts.get(self.store,sid)['content'],'')
        self.assertEqual(a.get_goal(self.store,sid,gid),before)
        self.assertEqual(len(engine.prompts),1)
        self.assertEqual(row['context']['current_request'],case['prompt'])
        self.assertNotIn('discussion_fixture',row['request'])


if __name__ == '__main__':
    unittest.main()
