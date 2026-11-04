import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import learning_batches as batches, learning_queue as queue
from assistant.store import Store
from scripts.learning.protocol import EXPECTED_VERSIONS
from scripts.learning.batch_worker import encode
from scripts.learning.review_batch import review
from scripts.learning.runtime import gpu_lease


def report(manifest, phase, *, wrong=False):
    rows=[]
    for case in batches.evaluation_cases(manifest):
        incorrect=(phase=='baseline' and case['group']=='new') or (wrong and case['group']=='guard')
        rows.append({'id':case['id'],'output':'unknown' if incorrect else case['answer'],'tokens':2,'ended_with_eos':True})
    value={'schema':'confirmed_learning_phase_v1','phase':phase,'status':'complete',
           'manifest_sha256':queue.digest(manifest),'environment':EXPECTED_VERSIONS,'rows':rows,
           'weights_before':'b'*64 if phase=='reload' else 'a'*64,
           'weights_after':'a'*64 if phase=='baseline' else 'b'*64}
    if phase=='train':
        value['training']=[{'step':i+1,'loss':0.2} for i in range(batches.RECIPE['steps'])]
        value['training_rows_sha256']=queue.digest(batches.training_rows(manifest))
    return value


class LearningBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.store=Store(self.root/'assistant.sqlite3')
        self.example=queue.confirm(self.store,'合成站點Daxelor的標記？只答英文。','violet')
        self.selected=[{'id':self.example['id'],'revision':1}]
        self.bid=batches.prepare(self.store,self.selected,checks=[{'prompt':'Daxelor登記的標記為何？','answer':'violet'}])
        self.path=batches.folder(self.root,self.bid); self.manifest=batches.read_manifest(self.path)

    def command(self, path, phase):
        value=report(batches.read_manifest(path),phase)
        value_path=path/(phase+'-fixture.json'); value_path.write_text(json.dumps(value),encoding='utf-8')
        code='''
import json,sys,hashlib
from pathlib import Path
p=Path(sys.argv[1]); phase=sys.argv[2]
v=json.loads((p/(phase+'-fixture.json')).read_text('utf-8'))
if phase=='train':
 a=p/'adapter'; a.mkdir()
 for name in ['README.md','adapter_config.json','adapter_model.safetensors']: (a/name).write_bytes(b'test-double-only')
 v['adapter_files']={x.name:hashlib.sha256(x.read_bytes()).hexdigest() for x in a.iterdir()}
(p/(phase+'.json')).write_text(json.dumps(v),encoding='utf-8')
'''
        return [sys.executable,'-c',code,str(path),phase]

    def test_freezes_exact_confirmed_revisions_and_keeps_holdouts_out_of_training(self):
        self.assertEqual(self.manifest['items'][0],{k:v for k,v in self.example.items() if k!='readiness'})
        training=batches.training_rows(self.manifest)
        self.assertEqual(sum(row['prompt']==self.example['prompt'] for row in training),4)
        self.assertNotIn(self.manifest['checks'][0]['prompt'],{r['prompt'] for r in training})
        self.selected[0]['revision']=123
        self.assertEqual(batches.read_manifest(self.path)['items'][0]['revision'],1)
        queue.withdraw(self.store,self.example['id'],1)
        with self.assertRaises(batches.Stale): batches.examples_current(self.store,self.manifest)
        self.assertEqual(batches.read_manifest(self.path)['items'][0]['status'],'confirmed')

    def test_invalid_selection_checks_and_changed_source_cannot_launch(self):
        for selected in [[],self.selected*2,[{'id':self.example['id'],'revision':True}], [{'id':'../bad','revision':1}]]:
            with self.assertRaises(ValueError): batches.prepare(self.store,selected)
        for checks in [False,[{'prompt':self.example['prompt'],'answer':'violet'}], [{'prompt':'x','answer':'y','code':'bad'}]]:
            with self.assertRaises(ValueError): batches.prepare(self.store,self.selected,checks=checks)
        with patch.object(batches,'sources',return_value={}),patch.object(batches,'phase_command') as launch:
            receipt=batches.run(self.store,self.bid)
        self.assertEqual(receipt['status'],'stale'); launch.assert_not_called()
        with self.assertRaises(ValueError): batches.run(self.store,self.bid)

    def test_raw_answers_weight_lineage_and_reload_are_regraded(self):
        reports={p:report(self.manifest,p) for p in batches.PHASES}
        self.assertTrue(batches.assess(self.manifest,reports)['passed'])
        for phase,key in [('train','weights_after'),('reload','weights_after')]:
            altered=copy.deepcopy(reports); altered[phase][key]='a'*64
            self.assertFalse(batches.assess(self.manifest,altered)['passed'])
        altered=copy.deepcopy(reports); altered['train']['rows'][0]['output']='wrong'; altered['train']['passed']=True
        self.assertFalse(batches.assess(self.manifest,altered)['passed'])
        altered=copy.deepcopy(reports); altered['train']['rows'][-1]['output']='wrong'
        self.assertFalse(batches.assess(self.manifest,altered)['checks']['baseline_guards_retained'])
        altered=copy.deepcopy(reports); altered['train']['training'].pop()
        with self.assertRaises(ValueError): batches.assess(self.manifest,altered)
        altered=copy.deepcopy(reports); altered['train']['rows'][0]['tokens']=True
        with self.assertRaises(ValueError): batches.assess(self.manifest,altered)

    def test_real_child_pipeline_candidate_integrity_and_no_rerun(self):
        with patch.object(batches,'phase_command',side_effect=self.command):
            receipt=batches.run(self.store,self.bid)
        self.assertEqual(receipt['status'],'candidate'); self.assertTrue(receipt['worker_exit_confirmed'])
        batches.candidate(self.root,self.bid)
        summary = review(self.root, self.bid)
        self.assertEqual(summary['groups']['new']['improved_ids'], ['example-' + self.example['id']])
        self.assertEqual(summary['failed_checks'], [])
        with self.assertRaises(ValueError): batches.run(self.store,self.bid)
        (self.path/'adapter/adapter_model.safetensors').write_bytes(b'changed')
        with self.assertRaises(ValueError): batches.candidate(self.root,self.bid)
        with self.assertRaisesRegex(ValueError, '參數檔案'): review(self.root, self.bid)

    def test_rejected_batch_review_distinguishes_regression_and_preserves_raw_evidence(self):
        with self.assertRaisesRegex(ValueError, '已完成'):
            review(self.root, self.bid)
        def regress(path, phase):
            args = self.command(path, phase)
            fixture = path / (phase + '-fixture.json')
            value = json.loads(fixture.read_text('utf-8'))
            if phase != 'baseline':
                value['rows'][-1]['output'] = 'incorrect'
                fixture.write_text(json.dumps(value), encoding='utf-8')
            return args
        with patch.object(batches, 'phase_command', side_effect=regress):
            receipt = batches.run(self.store, self.bid)
        self.assertEqual(receipt['status'], 'rejected')
        snapshot = (self.path / 'receipt.json').read_bytes()
        summary = review(self.root, self.bid)
        guard = summary['groups']['guard']
        self.assertEqual([guard[key] for key in ('total','baseline','train','reload')], [8,8,7,7])
        self.assertEqual(guard['regressed_ids'], [batches.evaluation_cases(self.manifest)[-1]['id']])
        self.assertEqual(guard['regressed_ids'], guard['reload_missed_ids'])
        self.assertEqual(summary['failed_checks'], ['baseline_guards_retained'])
        self.assertNotIn(self.example['prompt'], json.dumps(summary, ensure_ascii=False))
        self.assertEqual((self.path / 'receipt.json').read_bytes(), snapshot)
        receipt['status'] = 'candidate'
        batches.save(self.path / 'receipt.json', receipt)
        with self.assertRaisesRegex(ValueError, '重算結果'): review(self.root, self.bid)
        (self.path / 'receipt.json').write_bytes(snapshot)
        with (self.path / 'reload.json').open('ab') as stream:
            stream.write(b'\n')
        with self.assertRaisesRegex(ValueError, '階段報告'): review(self.root, self.bid)

    def test_cancelled_progress_only_grades_finished_phases_and_checks_step_sequence(self):
        receipt = json.loads((self.path / 'receipt.json').read_text('utf-8'))
        receipt.update(status='cancelled', phase='train')
        batches.save(self.path / 'receipt.json', receipt)
        batches.save(self.path / 'baseline.json', report(self.manifest, 'baseline'))
        batches.save(self.path / 'train-answers.partial.json', ['unfinished'])
        summary = review(self.root, self.bid)
        self.assertIsNone(summary['recorded_training_steps'])
        self.assertEqual(set(summary['completed_phase_files']), {'baseline'})
        self.assertEqual(summary['completed_phase_files']['baseline']['groups']['guard'], {'total':8,'correct':8})
        history = [{'step':1, 'loss':0.8}, {'step':2, 'loss':0.6}]
        batches.save(self.path / 'training.partial.json', history)
        self.assertEqual(review(self.root, self.bid)['recorded_training_steps'], 2)
        history[-1]['step'] = 4
        batches.save(self.path / 'training.partial.json', history)
        with self.assertRaisesRegex(ValueError, '部分步驟'): review(self.root, self.bid)

    def test_already_correct_baseline_skips_training_and_cannot_be_promoted(self):
        called=[]
        def command(path,phase):
            called.append(phase); args=self.command(path,phase)
            fixture=path/(phase+'-fixture.json'); value=json.loads(fixture.read_text('utf-8'))
            for row,case in zip(value['rows'],batches.evaluation_cases(self.manifest)):
                row['output']=case['answer']
            fixture.write_text(json.dumps(value),encoding='utf-8')
            return args
        with patch.object(batches,'phase_command',side_effect=command): result=batches.run(self.store,self.bid)
        self.assertEqual(result['status'],'already_satisfied')
        self.assertEqual(called,['baseline']); self.assertFalse((self.path/'adapter').exists())
        with self.assertRaises(ValueError): batches.candidate(self.root,self.bid)

    def test_regressed_control_retains_candidate_files_but_rejects_acceptance(self):
        def command(path,phase):
            args=self.command(path,phase)
            if phase in ('train','reload'):
                fixture=path/(phase+'-fixture.json'); value=json.loads(fixture.read_text('utf-8'))
                value['rows'][-1]['output']='wrong'
                fixture.write_text(json.dumps(value),encoding='utf-8')
            return args
        with patch.object(batches,'phase_command',side_effect=command): result=batches.run(self.store,self.bid)
        self.assertEqual(result['status'],'rejected')
        self.assertTrue((self.path/'adapter').is_dir())
        self.assertFalse(result['assessment']['checks']['baseline_guards_retained'])

    def test_parent_replay_is_frozen_and_changed_parent_examples_block_new_batch(self):
        with patch.object(batches,'phase_command',side_effect=self.command): batches.run(self.store,self.bid)
        new=queue.confirm(self.store,'合成站點Lumet的標記？','silver')
        second=batches.prepare(self.store,[{'id':new['id'],'revision':1}],parent=self.bid)
        manifest=batches.read_manifest(batches.folder(self.root,second))
        self.assertEqual([r['id'] for r in manifest['items']],[self.example['id'],new['id']])
        self.assertEqual(manifest['new_ids'],[new['id']])
        self.assertEqual(manifest['checks'],self.manifest['checks'])
        queue.withdraw(self.store,self.example['id'],1)
        with self.assertRaises(batches.Stale): batches.prepare(self.store,[{'id':new['id'],'revision':1}],parent=self.bid)
        with patch.object(batches,'phase_command') as launch: result=batches.run(self.store,second)
        self.assertEqual(result['status'],'stale'); launch.assert_not_called()

    def test_cancel_timeout_and_source_change_reap_actual_owned_children(self):
        processes=[]; real=subprocess.Popen
        def spawn(*a,**kw):
            p=real(*a,**kw); processes.append(p); return p
        for mode in ['cancel','timeout','withdraw']:
            bid=batches.prepare(self.store,self.selected)
            cancel=threading.Event()
            action=cancel.set if mode=='cancel' else lambda:queue.withdraw(self.store,self.example['id'],1)
            timer=threading.Timer(0.3,action) if mode!='timeout' else None
            if timer: timer.start()
            try:
                with patch.object(batches,'phase_command',return_value=[sys.executable,'-c','import time; time.sleep(15)']),patch.object(batches.subprocess,'Popen',side_effect=spawn):
                    result=batches.run(self.store,bid,cancel=cancel,timeout=0.5 if mode=='timeout' else 10)
                self.assertEqual(result['status'],{'cancel':'cancelled','timeout':'failed','withdraw':'stale'}[mode])
                self.assertTrue(result['worker_exit_confirmed'])
                self.assertTrue(all(p.poll() is not None for p in processes))
            finally:
                if timer: timer.cancel()
                for p in processes:
                    if p.poll() is None: p.kill(); p.wait()

    def test_receipt_write_failure_after_spawn_still_reaps_owned_child(self):
        processes=[]; spawn=subprocess.Popen; save=batches.save; failed=[]
        def capture(*args,**kwargs):
            process=spawn(*args,**kwargs)
            if args[0][0]==sys.executable: processes.append(process)
            return process
        def fail_once(path,value):
            if path.name=='receipt.json' and value.get('worker_exit_confirmed') is False and not failed:
                failed.append(True)
                raise OSError('injected receipt storage failure')
            return save(path,value)
        try:
            with patch.object(batches,'phase_command',return_value=[sys.executable,'-c','import time; time.sleep(15)']),patch.object(batches.subprocess,'Popen',side_effect=capture),patch.object(batches,'save',side_effect=fail_once):
                result=batches.run(self.store,self.bid)
            self.assertEqual(len(processes),1); self.assertEqual(result['status'],'failed')
            self.assertIn('storage failure',result['error'])
            self.assertTrue(result['worker_exit_confirmed']); self.assertIsNotNone(processes[0].poll())
        finally:
            for process in processes:
                if process.poll() is None: process.kill(); process.wait()

    @unittest.skipUnless(os.name=='nt','Windows venv redirector exit observation')
    def test_actual_worker_exit_releases_process_held_lease(self):
        cancel=threading.Event(); marker=self.path/'baseline.started'; lease=self.root/'gpu.lock'
        code='''
import os,sys,time
from pathlib import Path
from scripts.learning.runtime import gpu_lease
with gpu_lease(Path(sys.argv[1])):
 Path(sys.argv[2]).write_text(str(os.getpid()),encoding='ascii')
 time.sleep(15)
'''
        def stop_after_start():
            deadline=time.monotonic()+8
            while time.monotonic()<deadline and not marker.exists(): time.sleep(0.01)
            cancel.set()
        watcher=threading.Thread(target=stop_after_start); watcher.start()
        try:
            with patch.object(batches,'phase_command',return_value=[sys.executable,'-c',code,str(lease),str(marker)]):
                result=batches.run(self.store,self.bid,cancel=cancel,timeout=12)
            self.assertEqual(result['status'],'cancelled'); self.assertTrue(result['worker_exit_confirmed'])
            self.assertEqual(result['actual_worker_pid'],int(marker.read_text('ascii')))
            with gpu_lease(lease): pass
        finally: watcher.join(timeout=10)

    def test_exit_observation_errors_do_not_claim_cleanup_or_allow_candidate(self):
        for method,error in [('observe',OSError('process observation denied')),('wait',TimeoutError('actual worker exit not confirmed'))]:
            with self.subTest(method=method):
                bid=batches.prepare(self.store,self.selected)
                with patch.object(batches,'PhaseExit') as observer,patch.object(batches,'phase_command',side_effect=self.command):
                    getattr(observer.return_value,method).side_effect=error
                    result=batches.run(self.store,bid)
                self.assertEqual(result['status'],'failed')
                self.assertFalse(result['worker_exit_confirmed'])
                observer.return_value.close.assert_called_once()
                with self.assertRaises(ValueError): batches.candidate(self.root,bid)

    def test_os_owner_lease_rejects_second_controller_and_running_record_never_restarts(self):
        with batches.owner(self.path):
            with self.assertRaises(sqlite3.OperationalError): batches.run(self.store,self.bid)
        batches.save(self.path/'receipt.json',{'status':'running','manifest_sha256':queue.digest(self.manifest)})
        with patch.object(batches,'phase_command') as launch:
            with self.assertRaises(ValueError): batches.run(self.store,self.bid)
            launch.assert_not_called()

    def test_training_target_mask_and_token_limits_never_truncate(self):
        class Tokenizer:
            eos_token='<eos>'
            def apply_chat_template(self,*args,**kwargs): return [1,2,3]
            def encode(self,*args,**kwargs): return [4,5]
        self.assertEqual(encode(Tokenizer(),{'prompt':'p','answer':'a'}),([1,2,3,4,5],[-100,-100,-100,4,5]))
        with patch.dict(batches.RECIPE,max_sequence_tokens=4):
            with self.assertRaises(ValueError): encode(Tokenizer(),{'prompt':'p','answer':'a'})
        with patch.dict(batches.RECIPE,max_output_tokens=1):
            with self.assertRaises(ValueError): encode(Tokenizer(),{'prompt':'p','answer':'a'})

    def test_cli_default_never_creates_database_or_imports_ml(self):
        absent=self.root/'absent'
        result=subprocess.run([sys.executable,str(batches.ROOT/'scripts/learning/batches.py'),'--data-dir',str(absent)],capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stderr); self.assertFalse(absent.exists())
        self.assertNotIn('torch',sys.modules)

    def test_cli_lists_prepares_and_reports_confirmed_revision_without_training(self):
        def cli(*args):
            result=subprocess.run([sys.executable,str(batches.ROOT/'scripts/learning/batches.py'),*args,'--data-dir',str(self.root)],capture_output=True,text=True,encoding='utf-8',timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)
        listed=cli('list')
        self.assertEqual(listed['items'][0]['id'],self.example['id'])
        self.assertEqual(listed['items'][0]['revision'],1)
        prepared=cli('prepare','--item',self.example['id']+':1')
        self.assertEqual(prepared['status'],'prepared')
        path=batches.folder(self.root,prepared['batch_id'])
        self.assertFalse((path/'baseline.started').exists())
        status=cli('status','--batch',prepared['batch_id'])
        self.assertEqual(status['status'],'prepared'); self.assertTrue(status['examples_still_current'])
        queue.withdraw(self.store,self.example['id'],1)
        status=cli('status','--batch',prepared['batch_id'])
        self.assertFalse(status['examples_still_current']); self.assertTrue(status['recorded_status_is_not_liveness'])


if __name__=='__main__': unittest.main()
