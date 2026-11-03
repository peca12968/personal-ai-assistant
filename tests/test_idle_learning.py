import copy,json,shutil,subprocess,sys,threading,unittest
from pathlib import Path
from unittest.mock import patch
from assistant import idle_learning as idle,learning_batches as batches,learned_models as models,learning_queue as queue
from assistant.local_engine import LocalEngine
import test_learned_models as model_fixture
import test_learning_batches as batch_fixture
from test_parameter_acceptance import CASES,fake_answer

class Clock:
    def __init__(self): self.now=0
    def __call__(self): return self.now
    def sleep(self,seconds): self.now+=seconds

def sample(): return {'ticks_ms':60000,'last_input_tick':0,'idle_ms':60000}

class IdleLearningTests(unittest.TestCase):
    def setUp(self):
        model_fixture.LearnedModelTests.setUp(self)
        self.pending=batches.prepare(self.store,[{'id':self.item['id'],'revision':1}])
        original=idle.gpu_lease
        lease=patch.object(idle,'gpu_lease',side_effect=lambda:original(self.root/'fixture-gpu.lock'))
        lease.start(); self.addCleanup(lease.stop)

    def prepared(self,**kwargs):
        uid=idle.prepare(self.root,self.pending,CASES,idle_seconds=30,**kwargs)
        return uid,idle.folder(self.root,uid)

    def train_double(self,store,bid,**kwargs):
        path=batches.folder(self.root,bid); manifest=batches.read_manifest(path)
        shutil.copytree(self.path/'adapter',path/'adapter')
        reports={phase:batch_fixture.report(manifest,phase) for phase in batches.PHASES}
        reports['train']['adapter_files']=batches.adapter_files(path/'adapter')
        for phase,value in reports.items(): batches.save(path/(phase+'.json'),value)
        receipt={'status':'candidate','manifest_sha256':queue.digest(manifest),
                 'report_files':{phase:models.file_hash(path/(phase+'.json')) for phase in reports},
                 'assessment':batches.assess(manifest,reports)}
        batches.save(path/'receipt.json',receipt); return receipt

    def test_idle_sample_wrap_invalid_future_and_non_integer(self):
        self.assertEqual(idle.validate_sample({'ticks_ms':2**32+100,'last_input_tick':2**32-100,'idle_ms':200})['idle_ms'],200)
        for value in ({'ticks_ms':100,'last_input_tick':200,'idle_ms':2**32-100},
                      {'ticks_ms':60000,'last_input_tick':0,'idle_ms':True},
                      {'ticks_ms':60000,'last_input_tick':0,'idle_ms':1}):
            with self.assertRaises(ValueError): idle.validate_sample(value)

    def test_prepare_freezes_cases_and_never_opens_ml_or_starts(self):
        uid,path=self.prepared(); manifest=idle.read_manifest(path,live=True)
        self.assertEqual(manifest['suite'],idle.checks.suite(CASES)); self.assertFalse(manifest['policy']['automatic_activation'])
        self.assertEqual(models.local_models.read_json(batches.folder(self.root,self.pending)/'receipt.json')['status'],'prepared')
        self.assertFalse((path/'check_id').exists()); self.assertNotIn('torch',sys.modules)
        for values in [(29,1,1),(30,0,1),(True,1,1),(30,1,3601)]:
            with self.assertRaises(ValueError): idle.limits(*values)
        with self.assertRaises(ValueError): idle.prepare(self.root,self.bid,CASES)

    def test_wait_expiration_and_gpu_busy_do_not_start_training(self):
        for busy in (False,True):
            uid,path=self.prepared(wait_seconds=2); clock=Clock()
            sensor=sample if busy else lambda:{'ticks_ms':100,'last_input_tick':100,'idle_ms':0}
            with patch.object(idle,'gpu_lease',side_effect=RuntimeError('busy')),patch.object(batches,'run') as train:
                receipt=idle.run(self.root,uid,sensor=sensor,clock=clock,sleep=clock.sleep)
            self.assertEqual(receipt['status'],'wait_expired'); train.assert_not_called()
            self.assertFalse(idle.report(self.root,uid)['automatic_activation'])
            self.assertEqual(clock.now,2)

    def test_success_runs_frozen_full_assistant_checks_but_does_not_activate(self):
        uid,path=self.prepared()
        with patch.object(batches,'run',side_effect=self.train_double),patch.object(LocalEngine,'answer',fake_answer):
            receipt=idle.run(self.root,uid,sensor=sample)
        self.assertEqual(receipt['status'],'validated_candidate',receipt)
        report=idle.report(self.root,uid)
        self.assertTrue(report['validation']['receipt']['assessment']['passed'])
        self.assertTrue(report['sources_still_current']); self.assertEqual(models.choice(self.root),models.initial())
        with self.assertRaises(ValueError): idle.run(self.root,uid,sensor=sample)
        queue.withdraw(self.store,self.item['id'],1)
        self.assertFalse(idle.report(self.root,uid)['sources_still_current'])

    def test_wrong_candidate_is_not_validated_or_activated(self):
        uid,path=self.prepared()
        def wrong(engine,prompt,cancel=None):
            answer=fake_answer(engine,prompt,cancel)
            if engine.destination.name=='target':
                target=engine.destination/'worker/result.json'; raw=models.local_models.read_json(target)
                raw['answer']='wrong'; batches.save(target,raw); return 'wrong'
            return answer
        with patch.object(batches,'run',side_effect=self.train_double),patch.object(LocalEngine,'answer',wrong):
            receipt=idle.run(self.root,uid,sensor=sample)
        self.assertEqual(receipt['status'],'validation_rejected',receipt)
        self.assertFalse(idle.report(self.root,uid)['validation']['receipt']['assessment']['passed'])
        self.assertEqual(models.choice(self.root),models.initial())

    def test_precancel_observation_failure_stale_and_no_running_resume(self):
        for mode in ('cancel','sensor','source','withdraw'):
            uid,path=self.prepared(); cancel=threading.Event()
            if mode=='cancel': cancel.set()
            if mode=='withdraw': queue.withdraw(self.store,self.item['id'],1)
            sensor=sample if mode!='sensor' else lambda: (_ for _ in ()).throw(OSError('observation unavailable'))
            hashes=idle.sources() if mode!='source' else {}
            with patch.object(idle,'sources',return_value=hashes),patch.object(batches,'run') as train:
                receipt=idle.run(self.root,uid,cancel=cancel,sensor=sensor)
            self.assertEqual(receipt['status'],{'cancel':'cancelled','sensor':'failed','source':'stale','withdraw':'stale'}[mode],receipt)
            train.assert_not_called()

    def test_real_child_stops_when_input_changes_and_preserves_exit_evidence(self):
        uid,path=self.prepared(); active=threading.Event(); processes=[]; timers=[]; real=subprocess.Popen
        def sensor(): return {'ticks_ms':60001,'last_input_tick':60001,'idle_ms':0} if active.is_set() else sample()
        code="import sys,os,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
        def command(batch_path,phase): return [sys.executable,'-c',code,str(batch_path/(phase+'.started'))]
        def spawn(args,**kwargs):
            process=real(args,**kwargs)
            if args[0]==sys.executable:
                processes.append(process); timer=threading.Timer(.4,active.set); timers.append(timer); timer.start()
            return process
        try:
            with patch.object(batches,'phase_command',command),patch('assistant.learning_batches.subprocess.Popen',side_effect=spawn):
                receipt=idle.run(self.root,uid,sensor=sensor)
            self.assertEqual(receipt['status'],'cancelled',receipt); self.assertEqual(receipt['stop_reason'],'input_resumed')
            self.assertEqual(len(processes),1); self.assertIsNotNone(processes[0].poll())
            trained=models.local_models.read_json(batches.folder(self.root,self.pending)/'receipt.json')
            self.assertEqual(trained['status'],'cancelled'); self.assertTrue(trained['worker_exit_confirmed'])
            self.assertNotIn('check_id',receipt)
        finally:
            for timer in timers: timer.cancel()
            for process in processes:
                if process.poll() is None: process.kill()
                process.wait(timeout=10)

    def test_work_deadline_is_shared_with_training_and_no_validation_after_stop(self):
        uid,path=self.prepared(work_seconds=2); clock=Clock()
        def train(store,bid,**kwargs):
            self.assertLessEqual(kwargs['timeout'],2); clock.sleep(3)
            self.assertTrue(kwargs['cancel'].is_set()); return {'status':'cancelled'}
        with patch.object(batches,'run',side_effect=train),patch.object(idle.checks,'prepare') as validate:
            receipt=idle.run(self.root,uid,sensor=sample,clock=clock,sleep=clock.sleep)
        self.assertEqual(receipt['status'],'cancelled'); self.assertEqual(receipt['stop_reason'],'work_timeout'); validate.assert_not_called()

    def test_exclusive_owner_and_interrupted_receipt_refuse_restart(self):
        uid,path=self.prepared()
        with batches.owner(path):
            with self.assertRaises(idle.sqlite3.OperationalError): idle.run(self.root,uid,sensor=sample)
        r=models.local_models.read_json(path/'receipt.json'); r['status']='training'; batches.save(path/'receipt.json',r)
        with patch.object(batches,'run') as train:
            with self.assertRaises(ValueError): idle.run(self.root,uid,sensor=sample)
            train.assert_not_called()

    def test_withdrawal_while_waiting_stops_without_launching(self):
        uid,path=self.prepared(wait_seconds=5); clock=Clock()
        def sleep(seconds):
            queue.withdraw(self.store,self.item['id'],1); clock.sleep(seconds)
        with patch.object(batches,'run') as train:
            receipt=idle.run(self.root,uid,sensor=lambda:{'ticks_ms':100,'last_input_tick':100,'idle_ms':0},clock=clock,sleep=sleep)
        self.assertEqual(receipt['status'],'stale'); train.assert_not_called()
        self.assertEqual(models.local_models.read_json(batches.folder(self.root,self.pending)/'receipt.json')['status'],'prepared')

    def test_report_refuses_check_from_another_candidate(self):
        uid,path=self.prepared()
        other=idle.checks.prepare(self.root,self.bid,CASES)
        receipt=models.local_models.read_json(path/'receipt.json')
        receipt.update(status='validated_candidate',check_id=other); batches.save(path/'receipt.json',receipt)
        with self.assertRaises(ValueError): idle.report(self.root,uid)

    def test_backward_input_marker_is_still_new_input(self):
        uid,path=self.prepared(); manifest=idle.read_manifest(path); samples=iter([
            {'ticks_ms':70000,'last_input_tick':100,'idle_ms':69900},
            {'ticks_ms':71000,'last_input_tick':99,'idle_ms':70901}])
        gate=idle.Gate(path,manifest,None,lambda:next(samples),lambda:0)
        gate.check(fresh=True); gate.admitted=gate.sample.copy()
        with self.assertRaises(batches.Stopped): gate.check(fresh=True)
        self.assertEqual(gate.reason,'input_resumed')

    def test_cli_default_and_malformed_combinations_are_offline(self):
        missing=self.root/'missing'
        for options,code in (([],0),(['run'],2),(['--idle-seconds','30'],2)):
            result=subprocess.run([sys.executable,'scripts/learning/idle.py','--data-dir',str(missing)]+options,
                    cwd=idle.ROOT,capture_output=True,text=True,encoding='utf-8',timeout=20)
            self.assertEqual(result.returncode,code,result.stderr)
        self.assertFalse(missing.exists())

if __name__=='__main__': unittest.main()
