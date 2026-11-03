import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import autonomy as a, artifacts, continuous
from assistant.backup import create_backup, restore_backup
from assistant.store import Store
from test_autonomy import ScriptedEngine, action


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.store = Store(self.path/'data'/'state.sqlite3')
        self.sid = self.store.new_session()
        self.gid = a.create_goal(self.store,self.sid,'修正失敗的步驟並保留已知條件')

    def run_steps(self, *actions, gid=None):
        return a.run_goal(self.store,self.sid,gid or self.gid,ScriptedEngine(*actions),max_steps=len(actions))

    def test_failure_retains_exact_attempt_on_restart_and_valid_step_stays_ordinary(self):
        attempt = action('calculate',operation='quotient',values=['731.2500','0'])
        first = self.run_steps(attempt)['steps'][0]
        self.assertEqual(first['action']['tool'],'invalid')
        self.assertEqual(first['observation']['error_phase'],'execution')
        self.assertEqual(first['observation']['failed_action'],json.loads(attempt))
        engine = ScriptedEngine(action('calculate',operation='quotient',values=['731.2500','3']))
        result = a.run_goal(Store(self.store.path),self.sid,self.gid,engine,max_steps=1)
        self.assertIn('731.2500',engine.prompts[0])
        self.assertEqual(result['steps'][0],first)
        self.assertEqual(result['steps'][1]['observation']['decimal'],'243.75')
        self.assertNotIn('failed_action',result['steps'][1]['observation'])

    def test_malformed_and_unallowed_requests_never_save_raw_or_previous_attempt(self):
        attempts = [action('calculate',operation='quotient',values=['42','0']),
                    'RAW_UNPARSEABLE_SECRET', action('shell',command='RAW_UNALLOWED_SECRET'),
                    action('read_json',document_id=True,pointer='/RAW_INVALID_SECRET',offset=0)]
        goal = self.run_steps(*attempts)
        for step in goal['steps'][1:]:
            self.assertEqual(step['action']['tool'],'invalid')
            self.assertEqual(step['observation']['error_phase'],'validation')
            self.assertNotIn('failed_action',step['observation'])
            self.assertNotIn('RAW_',json.dumps(step,ensure_ascii=False))
        self.assertEqual(len(goal['steps']),4)

    def test_failed_delivery_and_plan_evidence_do_not_become_success(self):
        goal = self.run_steps(action('finish',report='不存在的來源[D1:L1]'),
            action('update_plan',items=[{'id':1,'task':'完成核對','state':'done','evidence_steps':[1],'note':'宣稱完成'}]))
        self.assertEqual(goal['status'],'paused')
        self.assertEqual(goal['result'],'')
        self.assertEqual(goal['work_plan'],None)
        for step in goal['steps']:
            self.assertEqual(step['action']['tool'],'invalid')
            self.assertIn('failed_action',step['observation'])
            self.assertIn('error',step['observation'])

    def test_file_collision_keeps_old_bytes_and_handoff_omits_failed_body(self):
        gid = a.create_goal(self.store,self.sid,'交付文件',file_delivery=True)
        goal = self.run_steps(action('write_artifact',filename='first.txt',content='old'),
            action('write_artifact',filename='first.txt',content='PRIVATE_FAILED_BODY'),gid=gid)
        self.assertEqual((artifacts.directory(self.store,gid)/'first.txt').read_text(),'old')
        failed = goal['steps'][1]['observation']['failed_action']
        self.assertEqual(failed['args']['content'],'PRIVATE_FAILED_BODY')
        child = a.create_goal(self.store,self.sid,'接續',parent_id=gid)
        context = a.get_goal(self.store,self.sid,child)['previous_stage']
        self.assertNotIn('PRIVATE_FAILED_BODY',json.dumps(context))
        step = context['checkpoint']['activity'][1]
        self.assertEqual(step['tool'],'invalid')
        self.assertEqual(step['failed_attempt']['tool'],'write_artifact')
        self.assertEqual(step['failed_attempt']['request']['value'],{'filename':'first.txt'})
        other = a.create_goal(self.store,self.sid,'其他目標')
        engine = ScriptedEngine(action('ask',question='需要哪份文件？'))
        a.run_goal(self.store,self.sid,other,engine,max_steps=1)
        self.assertNotIn('PRIVATE_FAILED_BODY',engine.prompts[0])
        restore_backup(create_backup(self.store.path)['path'],self.path/'restored')
        restored = Store(self.path/'restored'/'assistant.sqlite3')
        self.assertEqual(a.get_goal(restored,self.sid,gid)['steps'],goal['steps'])
        self.assertEqual(a.get_goal(restored,self.sid,child)['previous_stage'],context)

    def test_cancelled_failed_execution_retains_detached_request_and_stops(self):
        cancel = threading.Event()
        def fail(store,documents,request,*args,**kwargs):
            request['args']['values'][0] = 'MUTATED'
            cancel.set()
            raise ValueError('合成失敗')
        attempt = action('calculate',operation='quotient',values=['731.25','0'])
        with patch.object(a,'execute',side_effect=fail):
            result = a.run_goal(self.store,self.sid,self.gid,ScriptedEngine(attempt),max_steps=3,cancel=cancel)
        self.assertEqual(result['status'],'paused')
        self.assertEqual(len(result['steps']),1)
        self.assertEqual(result['steps'][0]['observation']['failed_action'],json.loads(attempt))

    def test_continuous_errors_still_stop_after_three_attempts(self):
        attempt = action('calculate',operation='quotient',values=['31','0'])
        goal = continuous.run_continuous(self.store,self.sid,self.gid,ScriptedEngine(*([attempt]*4)),max_steps=4)
        self.assertEqual(goal['status'],'paused')
        self.assertEqual(len(goal['steps']),3)
        self.assertEqual(goal['continuous_runs'][0]['reason'],'repeated_errors')
        self.assertTrue(all(s['observation']['failed_action']==json.loads(attempt) for s in goal['steps']))


if __name__ == '__main__': unittest.main()
