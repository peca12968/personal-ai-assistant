from contextlib import closing, nullcontext
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from assistant import idle_learning as idle, learning_batches as batches, learning_queue as queue
from scripts.learning import base_idle_segments as segments
import test_learned_models as fixture
from test_parameter_acceptance import CASES


class BaseIdleSegmentTests(unittest.TestCase):
    def setUp(self):
        fixture.LearnedModelTests.setUp(self)
        self.item = queue.confirm(self.store, 'synthetic base segment', 'fixture')
        self.batch = batches.prepare(self.store, [{'id':self.item['id'], 'revision':1}],
                                     checks=[{'prompt':'held out base check', 'answer':'fixture'}])
        self.reference = idle.prepare(self.root, self.batch, CASES)
        self.sample = dict(ticks_ms=900000, last_input_tick=100000, idle_ms=800000)
        for obj, key, value in ((segments, 'PARENT', self.root/'base-segments'),
                                (segments, 'gpu_lease', nullcontext),
                                (idle, 'idle_sample', lambda:dict(self.sample))):
            mocked=patch.object(obj,key,value); mocked.start(); self.addCleanup(mocked.stop)
        self.session = segments.prepare(self.root, self.reference, 4)
        self.folder = segments.PARENT/self.session
        self.plan = segments.stored_plan(self.folder)

    def fake_owned(self, command, job, phase, check_input, check_sources):
        check_input(); check_sources()
        req = segments.read(job/'request.json')
        raw = ('base boundary '+str(req['end'])).encode()
        sha = segments.gpu.ck.digest(raw)
        segments.gpu.ck.commit_bytes(self.folder/'checkpoints'/(sha+'.safetensors'),raw)
        record = dict(phase=phase, consumed=req['from_blob'], step=req['end'], sha=sha, pid=17,
                      binding={'experiment':req['plan'], 'initial_adapter_sha256':'initial'},
                      weights='changed', history=list(range(req['end'])))
        if req['end'] == self.plan['training_steps']:
            record.update(generated=[2], logits_sha256=sha, rows=[{'output':'synthetic'}])
            (job/(phase+'.logits')).write_bytes(raw)
        segments.gpu.save(job/(phase+'.json'),record)
        (job/(phase+'.started')).write_text('17',encoding='ascii')
        segments.gpu.save(job/(phase+'.exit.json'),dict(worker=17,exit_confirmed=True,returncode=0))

    def test_prepare_is_base_only_and_cannot_reuse_cancelled_or_parent_batches(self):
        manifest=segments.verify_plan(self.folder,self.plan)
        self.assertIsNone(manifest['parent'])
        self.assertEqual(self.plan['idle_ms'],600000)
        for steps in (True,0,129):
            with self.assertRaises(ValueError):segments.prepare(self.root,self.reference,steps)
        parent=batches.prepare(self.store,[{'id':self.item['id'],'revision':1}],parent=self.bid,
                               checks=[{'prompt':'parent holdout','answer':'fixture'}])
        ref=idle.prepare(self.root,parent,CASES)
        with self.assertRaisesRegex(ValueError,'底座'):segments.prepare(self.root,ref,4)
        no_checks=batches.prepare(self.store,[{'id':self.item['id'],'revision':1}])
        empty_ref=idle.prepare(self.root,no_checks,CASES)
        with self.assertRaisesRegex(ValueError,'保留題'):segments.prepare(self.root,empty_ref,4)
        path=batches.folder(self.root,self.batch)
        original=segments.read(path/'receipt.json')
        segments.gpu.save(path/'receipt.json',{**original,'status':'cancelled'})
        with self.assertRaises(ValueError):segments.prepare(self.root,self.reference,4)

    def test_two_explicit_segments_require_the_current_commit_and_new_idle_admission(self):
        with patch.object(segments.gpu,'run_owned',side_effect=self.fake_owned) as launches:
            first=segments.segment(self.session,'start',2)
            self.assertFalse(first['automatic_activation'])
            self.sample=dict(ticks_ms=910000,last_input_tick=909990,idle_ms=10)
            with self.assertRaises(batches.Stopped):segments.segment(self.session,'continue',2,first['receipt'])
            self.sample=dict(ticks_ms=1610000,last_input_tick=909990,idle_ms=700010)
            with self.assertRaises(ValueError):segments.segment(self.session,'continue',2,'0'*64)
            second=segments.segment(self.session,'continue',2,first['receipt'])
            self.assertEqual(second['step'],4)
            self.assertEqual(launches.call_count,2)
            with self.assertRaises(ValueError):segments.segment(self.session,'start',1)
            with self.assertRaises(ValueError):segments.segment(self.session,'continue',1,first['receipt'])
        self.assertEqual(segments.read(batches.folder(self.root,self.batch)/'receipt.json')['status'],'prepared')

    def test_input_after_checkpoint_write_never_commits_or_resumes_the_orphan(self):
        with patch.object(segments.gpu,'run_owned',side_effect=self.fake_owned):
            first=segments.segment(self.session,'start',1)
        def interrupted(*args):
            self.fake_owned(*args)
            raise batches.Stopped('input_resumed')
        with patch.object(segments.gpu,'run_owned',side_effect=interrupted),self.assertRaises(batches.Stopped):
            segments.segment(self.session,'continue',2,first['receipt'])
        with closing(segments.ledger(self.folder,readonly=True)) as db:
            self.assertEqual(segments.digest(segments.head(db,self.folder,self.plan)),first['receipt'])
        orphan=segments.gpu.ck.digest(b'base boundary 3')
        self.assertTrue((self.folder/'checkpoints'/(orphan+'.safetensors')).exists())
        with self.assertRaises(ValueError):segments.segment(self.session,'continue',1,orphan)

    def test_reference_job_start_and_example_withdrawal_refuse_a_future_segment(self):
        with patch.object(segments.gpu,'run_owned',side_effect=self.fake_owned):
            first=segments.segment(self.session,'start',1)
        job=idle.folder(self.root,self.reference)/'receipt.json'
        original=segments.read(job)
        segments.gpu.save(job,{**original,'status':'waiting'})
        with patch.object(segments.gpu,'run_owned') as launch,self.assertRaises(ValueError):
            segments.segment(self.session,'continue',1,first['receipt'])
        launch.assert_not_called()
        segments.gpu.save(job,original)
        queue.withdraw(self.store,self.item['id'],1)
        with self.assertRaises(batches.Stale):segments.segment(self.session,'continue',1,first['receipt'])

    def test_changed_source_plan_blob_and_exit_evidence_refuse_reuse(self):
        with patch.object(segments.gpu,'run_owned',side_effect=self.fake_owned):
            first=segments.segment(self.session,'start',1)
        record_path=self.folder/'jobs'/first['job']/'first.exit.json'
        segments.gpu.save(record_path,dict(worker=17,exit_confirmed=False,returncode=0))
        with self.assertRaises(ValueError):segments.segment(self.session,'continue',1,first['receipt'])
        segments.gpu.save(record_path,dict(worker=17,exit_confirmed=True,returncode=0))
        blob=self.folder/'checkpoints'/(segments.gpu.ck.digest(b'base boundary 1')+'.safetensors')
        saved=blob.read_bytes();blob.write_bytes(b'changed physical checkpoint')
        with self.assertRaisesRegex(ValueError,'實體檔案'):
            segments.segment(self.session,'continue',1,first['receipt'])
        blob.write_bytes(saved)
        with patch.object(segments,'sources',return_value={}):
            with self.assertRaises(batches.Stale):segments.segment(self.session,'continue',1,first['receipt'])
        segments.gpu.save(self.folder/'plan.json',{**self.plan,'training_steps':20})
        with self.assertRaises(batches.Stale):segments.segment(self.session,'continue',1,first['receipt'])

    def test_failed_ledger_commit_and_competing_owner_preserve_prior_boundary(self):
        with patch.object(segments.gpu,'run_owned',side_effect=self.fake_owned) as launch:
            first=segments.segment(self.session,'start',1)
            with batches.owner(self.folder),self.assertRaises(sqlite3.OperationalError):
                segments.segment(self.session,'continue',1,first['receipt'])
            self.assertEqual(launch.call_count,1)
            with closing(segments.ledger(self.folder)) as db,db:
                db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON boundaries BEGIN SELECT RAISE(ABORT,'write failed'); END")
            with self.assertRaises(sqlite3.IntegrityError):segments.segment(self.session,'continue',1,first['receipt'])
        with closing(segments.ledger(self.folder,readonly=True)) as db:
            self.assertEqual(segments.digest(segments.head(db,self.folder,self.plan)),first['receipt'])

    def test_four_step_control_requires_equal_state_and_runs_only_once(self):
        with patch.object(segments.gpu,'run_owned',side_effect=self.fake_owned):
            first=segments.segment(self.session,'start',2)
            second=segments.segment(self.session,'continue',2,first['receipt'])
            checked=segments.segment(self.session,'control',receipt=second['receipt'])
            self.assertEqual(checked['status'],'verified')
            self.assertTrue(all(checked['equal'].values()))
            with self.assertRaises(FileExistsError):segments.segment(self.session,'control',receipt=second['receipt'])

    def test_invalid_update_counts_never_launch_a_worker(self):
        with patch.object(segments.gpu,'run_owned') as launch:
            for value in (None,True,0,-1,5,1.5):
                with self.assertRaises(ValueError):segments.segment(self.session,'start',value)
            launch.assert_not_called()

    def test_changed_source_snapshot_is_refused_without_a_model_call(self):
        name=next(iter(self.plan['sources']))
        (self.folder/'sources'/name).write_bytes(b'changed saved source')
        with patch.object(segments.gpu,'run_owned') as launch,self.assertRaises(batches.Stale):
            segments.segment(self.session,'start',1)
        launch.assert_not_called()

    def test_controller_import_does_not_load_optional_ml_packages(self):
        code=('import sys; import scripts.learning.base_idle_segments; '
              'assert not set(("torch","peft","transformers","safetensors")) & set(sys.modules)')
        result=subprocess.run([sys.executable,'-c',code],cwd=segments.ROOT,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
