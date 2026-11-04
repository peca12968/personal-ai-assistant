from contextlib import closing
from contextlib import nullcontext
import sqlite3
import unittest
from unittest.mock import patch

from assistant import idle_learning as idle, learning_batches as batches, learning_queue
from scripts.learning import idle_segments as segments
import test_learned_models as model_fixture
from test_parameter_acceptance import CASES


class IdleSegmentTests(unittest.TestCase):
    def setUp(self):
        model_fixture.LearnedModelTests.setUp(self)
        self.new = learning_queue.confirm(self.store,'synthetic segment question','fixture')
        batch = batches.prepare(self.store,[{'id':self.new['id'],'revision':1}],parent=self.bid,
                                checks=[{'prompt':'segment control','answer':'fixture'}])
        reference = idle.prepare(self.root,batch,CASES)
        self.sample = {'ticks_ms':900000,'last_input_tick':100000,'idle_ms':800000}
        for obj, key, value in [(segments,'PARENT',self.root/'sessions'),(segments,'gpu_lease',nullcontext),
                                (idle,'idle_sample',lambda:dict(self.sample))]:
            m=patch.object(obj,key,value)
            m.start()
            self.addCleanup(m.stop)
        self.session=segments.prepare(self.root,reference,4)
        self.folder=segments.PARENT/self.session
        self.plan=segments.read(self.folder/'plan.json')

    def fake_owned(self, command, job, phase, check_input, check_sources):
        check_input()
        check_sources()
        req=segments.read(job/'request.json')
        raw=('synthetic boundary '+str(req['end'])).encode()
        sha=segments.gpu.ck.digest(raw)
        segments.gpu.ck.commit_bytes(self.folder/'checkpoints'/(sha+'.safetensors'),raw)
        segments.gpu.save(job/(phase+'.json'),{'phase':phase,'consumed':req['from_blob'],'step':req['end'],
            'sha':sha,'pid':17,'binding':{'experiment':req['plan'],'initial_adapter_sha256':'initial'},
            'weights':'changed','history':list(range(req['end'])),'generated':[2],'logits_sha256':sha})
        (job/(phase+'.logits')).write_bytes(raw)
        segments.gpu.save(job/(phase+'.exit.json'),{'worker':17,'exit_confirmed':True,'returncode':0})

    def test_separate_idle_windows_require_the_current_committed_receipt(self):
        with patch.object(segments.gpu,'run_owned',side_effect=self.fake_owned) as launches:
            first=segments.segment(self.session,'start',2,None)
            with self.assertRaises(ValueError):
                segments.segment(self.session,'continue',2,'0'*64)
            self.sample={'ticks_ms':910000,'last_input_tick':909990,'idle_ms':10}
            with self.assertRaises(batches.Stopped):
                segments.segment(self.session,'continue',2,first['receipt'])
            self.sample={'ticks_ms':1610000,'last_input_tick':909990,'idle_ms':700010}
            second=segments.segment(self.session,'continue',2,first['receipt'])
            self.assertNotEqual(first['job'],second['job'])
            request=segments.read(self.folder/'jobs'/second['job']/'request.json')
            self.assertEqual(request['admitted'],self.sample)
            self.assertEqual(request['from_receipt'],first['receipt'])
            with self.assertRaises(ValueError):
                segments.segment(self.session,'continue',1,first['receipt'])
            self.assertEqual(launches.call_count,2)

    def test_interruption_after_output_leaves_prior_commit_usable(self):
        with patch.object(segments.gpu,'run_owned',side_effect=self.fake_owned):
            first=segments.segment(self.session,'start',1,None)
        def interrupted(*args):
            self.fake_owned(*args)
            raise batches.Stopped('input_resumed')
        with patch.object(segments.gpu,'run_owned',side_effect=interrupted), self.assertRaises(batches.Stopped):
            segments.segment(self.session,'continue',2,first['receipt'])
        with closing(segments.ledger(self.folder)) as db:
            current=segments.head(db,self.folder,self.plan)
        self.assertEqual(segments.digest(current),first['receipt'])
        orphan=segments.gpu.ck.digest(b'synthetic boundary 3')
        self.assertTrue((self.folder/'checkpoints'/(orphan+'.safetensors')).exists())
        with self.assertRaises(ValueError):
            segments.segment(self.session,'continue',1,orphan)
        learning_queue.withdraw(self.store,self.new['id'],1)
        with self.assertRaises(batches.Stale), patch.object(segments.gpu,'run_owned',side_effect=AssertionError):
            segments.segment(self.session,'continue',2,first['receipt'])

    def test_changed_boundary_file_or_exit_record_is_rejected_before_launch(self):
        with patch.object(segments.gpu,'run_owned',side_effect=self.fake_owned):
            first=segments.segment(self.session,'start',2,None)
        job=self.folder/'jobs'/first['job']
        row=segments.read(job/'first.json')
        path=self.folder/'checkpoints'/(row['sha']+'.safetensors')
        saved=path.read_bytes()
        path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'實體檔案'):
            segments.segment(self.session,'continue',2,first['receipt'])
        path.write_bytes(saved)
        segments.gpu.save(job/'first.exit.json',{'exit_confirmed':False,'worker':17,'returncode':0})
        with self.assertRaisesRegex(ValueError,'退出紀錄'):
            segments.segment(self.session,'continue',2,first['receipt'])

    def test_edited_preparation_refused_even_before_first_commit(self):
        segments.gpu.save(self.folder/'plan.json',{**self.plan,'training_steps':30})
        with self.assertRaisesRegex(batches.Stale,'準備'), patch.object(segments.gpu,'run_owned',side_effect=AssertionError):
            segments.segment(self.session,'start',1,None)

    def test_failed_ledger_insert_and_competing_owner_preserve_last_commit(self):
        with patch.object(segments.gpu,'run_owned',side_effect=self.fake_owned) as launches:
            first=segments.segment(self.session,'start',1,None)
            with batches.owner(self.folder), self.assertRaises(sqlite3.OperationalError):
                segments.segment(self.session,'continue',1,first['receipt'])
            self.assertEqual(launches.call_count,1)
            with closing(segments.ledger(self.folder)) as db, db:
                db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON boundaries BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
            with self.assertRaisesRegex(sqlite3.IntegrityError,'synthetic failure'):
                segments.segment(self.session,'continue',1,first['receipt'])
        with closing(segments.ledger(self.folder,readonly=True)) as db:
            self.assertEqual(segments.digest(segments.head(db,self.folder,self.plan)),first['receipt'])
            self.assertEqual(db.execute('SELECT count(*) FROM boundaries').fetchone()[0],1)
