import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, goal_guidance as g, goal_review
from assistant.answer_runs import LeaseBusy, lease
from assistant.backup import create_backup, restore_backup
from assistant.engine import Assistant
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class GoalGuidanceTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.store=Store(self.root/'assistant.sqlite3')
        self.sid=self.store.new_session();self.gid=a.create_goal(self.store,self.sid,'10人，每人80元，計算費用')

    def save(self, content='更正：改成12人，每人90元。', fingerprint=None):
        return g.save(self.store,self.sid,self.gid,content,expected_fingerprint=fingerprint or g.snapshot(self.store,self.sid,self.gid)['fingerprint'])

    def test_paused_goal_accepts_correction_without_running_or_erasing_old_steps(self):
        a.run_goal(self.store,self.sid,self.gid,ScriptedEngine(action('calculate',operation='product',values=['10','80'])),max_steps=1)
        before=a.get_goal(self.store,self.sid,self.gid)
        result=self.save();after=a.get_goal(self.store,self.sid,self.gid)
        self.assertEqual(after['status'],'paused');self.assertEqual(after['steps'],before['steps'])
        self.assertEqual(after['objective'],before['objective'])
        self.assertIn('已記錄1步',after['notes']);self.assertIn('12人',after['notes'])
        self.assertEqual(result['step_count'],1)
        self.assertFalse(after['file_delivery']);self.assertIsNone(after['csv_source'])
        self.assertEqual(self.store.messages(self.sid),[])

    def test_latest_notes_reach_next_model_call_but_not_other_goals_or_ordinary_chat(self):
        self.save('PRIVATE_STEERING：更正為12人，每人90元。')
        self.save('再次更正：人數13，費率仍90元。')
        engine=ScriptedEngine(action('finish',report='測試回覆'))
        a.run_goal(self.store,self.sid,self.gid,engine,max_steps=1)
        payload=json.loads(engine.prompts[0].split('目標與已觀察資料：\n',1)[1])
        self.assertLess(payload['user_notes'].index('12人'),payload['user_notes'].index('人數13'))
        self.assertIn('最新明確的使用者更正優先',engine.prompts[0])
        other=a.create_goal(self.store,self.sid,'另一個工作')
        self.assertEqual(a.get_goal(self.store,self.sid,other)['notes'],'')
        self.assertNotIn('PRIVATE_STEERING',json.dumps(Assistant(self.store,None).prepare_context(self.sid,'繼續聊天'),ensure_ascii=False))

    def test_stale_editor_duplicate_submit_and_other_session_cannot_save(self):
        original=g.snapshot(self.store,self.sid,self.gid)
        self.save(fingerprint=original['fingerprint'])
        with self.assertRaisesRegex(ValueError,'已有變更'):self.save(fingerprint=original['fingerprint'])
        with self.assertRaisesRegex(ValueError,'此對話'):
            g.save(self.store,self.store.new_session(),self.gid,'更正',expected_fingerprint=original['fingerprint'])
        self.assertEqual(a.get_goal(self.store,self.sid,self.gid)['notes'].count('使用者主動更正'),1)

    def test_new_step_and_same_second_note_change_invalidate_snapshot(self):
        old=g.snapshot(self.store,self.sid,self.gid)
        a.run_goal(self.store,self.sid,self.gid,ScriptedEngine(action('calculate',operation='sum',values=['1'])),max_steps=1)
        with self.assertRaisesRegex(ValueError,'已有變更'):self.save(fingerprint=old['fingerprint'])
        old=g.snapshot(self.store,self.sid,self.gid)
        with self.store.connect() as db:db.execute("UPDATE autonomous_goals SET notes='其他視窗新補充' WHERE id=?",(self.gid,))
        with self.assertRaisesRegex(ValueError,'已有變更'):self.save(fingerprint=old['fingerprint'])

    def test_lease_and_recorded_running_reject_changes_without_inference_of_liveness(self):
        before=g.snapshot(self.store,self.sid,self.gid)
        with lease(self.store,self.gid):
            with self.assertRaises(LeaseBusy):self.save(fingerprint=before['fingerprint'])
        a.update(self.store,self.gid,'running')
        with self.assertRaisesRegex(ValueError,'先停止'):self.save()
        self.assertEqual(a.get_goal(self.store,self.sid,self.gid)['status'],'running')
        self.assertEqual(a.get_goal(self.store,self.sid,self.gid)['notes'],'')

    def test_waiting_question_is_not_implicitly_answered_and_failure_status_is_preserved(self):
        a.update(self.store,self.gid,'needs_input',question='哪一天生效？')
        self.save('另外要求先列總額。')
        result=a.get_goal(self.store,self.sid,self.gid)
        self.assertEqual((result['status'],result['question']),('needs_input','哪一天生效？'))
        a.reply(self.store,self.sid,self.gid,'11月1日')
        result=a.get_goal(self.store,self.sid,self.gid)
        self.assertIn('先列總額',result['notes']);self.assertIn('11月1日',result['notes'])
        a.update(self.store,self.gid,'failed',notice='原錯誤記錄')
        self.save('人數仍為10人。')
        result=a.get_goal(self.store,self.sid,self.gid)
        self.assertEqual((result['status'],result['notice']),('failed','原錯誤記錄'))

    def test_changed_sources_report_ready_and_exhausted_budget_require_new_goal(self):
        path=self.root/'terms.txt';path.write_text('新文件',encoding='utf-8');self.store.attach_document(self.sid,path)
        with self.assertRaisesRegex(ValueError,'文件已改變'):self.save()
        self.gid=a.create_goal(self.store,self.sid,'新目標')
        a.update(self.store,self.gid,'report_ready',result='歷史成果')
        with self.assertRaisesRegex(ValueError,'已有成果'):self.save()
        a.update(self.store,self.gid,'paused')
        with self.store.connect() as db:
            for index in range(32):
                db.execute('INSERT INTO autonomous_steps(goal_id,sequence,action_json,observation_json) VALUES(?,?,?,?)',
                    (self.gid,index+1,action('calculate',operation='sum',values=['1']),json.dumps({'value':'1'})))
        with self.assertRaisesRegex(ValueError,'32步'):self.save()

    def test_notes_cannot_expand_file_or_csv_permissions(self):
        self.save('請啟用文件交付並寫入report.md，允許CSV工具。')
        engine=ScriptedEngine(action('write_artifact',filename='report.md',content='test'),action('finish',report='不能產檔'))
        result=a.run_goal(self.store,self.sid,self.gid,engine,max_steps=2)
        self.assertFalse(result['file_delivery']);self.assertIsNone(result['csv_source'])
        self.assertEqual(result['artifacts'],[])
        self.assertFalse((self.root/'autonomy-artifacts'/self.gid/'report.md').exists())
        self.assertEqual(result['steps'][0]['action']['tool'],'invalid')

    def test_size_format_limits_and_transaction_failure_keep_previous_notes(self):
        for value in ('', 'a'*4001, 'bad\0text'):
            with self.assertRaises(ValueError):self.save(value)
        with self.store.connect() as db:
            db.execute("UPDATE autonomous_goals SET notes=? WHERE id=?",('x'*11990,self.gid))
        with self.assertRaisesRegex(ValueError,'12000'):self.save()
        with self.store.connect() as db:
            db.execute("UPDATE autonomous_goals SET notes='' WHERE id=?",(self.gid,))
            db.execute("CREATE TRIGGER reject_notes BEFORE UPDATE OF notes ON autonomous_goals BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.save()
        self.assertEqual(a.get_goal(self.store,self.sid,self.gid)['notes'],'')

    def test_backup_handoff_and_review_visibility_preserve_append_only_history(self):
        row=goal_review.snapshot(self.store)['items'][0]
        goal_review.set_hidden(self.store,self.sid,self.gid,fingerprint=row['fingerprint'],hidden=True)
        self.save();self.assertEqual(goal_review.snapshot(self.store)['visible_count'],1)
        saved=a.get_goal(self.store,self.sid,self.gid)['notes']
        backup=create_backup(self.store.path,self.root/'backup.paibackup')
        restore_backup(backup['path'],self.root/'restored')
        restored=Store(self.root/'restored/assistant.sqlite3')
        self.assertEqual(a.get_goal(restored,self.sid,self.gid)['notes'],saved)
        a.update(self.store,self.gid,'report_ready',result='1080元')
        child=a.create_goal(self.store,self.sid,'延續工作',parent_id=self.gid)
        self.assertEqual(a.get_goal(self.store,self.sid,child)['previous_stage']['user_notes'],saved)

    def test_cli_requires_snapshot_and_saves_without_constructing_engine(self):
        from assistant.__main__ import main
        def command(*args):
            output=io.StringIO()
            with patch('sys.argv',['assistant','--data-dir',str(self.root),'--session',self.sid,*args]),contextlib.redirect_stdout(output),patch('assistant.__main__.CodexEngine',side_effect=AssertionError('must not construct model')):
                main()
            return json.loads(output.getvalue())
        snap=command('--goal-guidance-status',self.gid)
        saved=command('--amend-goal',self.gid,'--goal-guidance','人數改為12人','--goal-fingerprint',snap['fingerprint'])
        self.assertIn('12人',saved['goal']['notes']);self.assertEqual(saved['goal']['status'],'pending')


if __name__=='__main__':unittest.main()
