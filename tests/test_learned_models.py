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

from assistant import learned_models as models, learning_batches as batches, learning_queue as queue, local_models
from assistant.local_engine import LocalEngine
from assistant.engine import EngineError, Assistant
from assistant.store import Store
import test_learning_batches as fixtures
import test_local_engine as engine_fixtures


class LearnedModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.store=Store(self.root/'assistant.sqlite3')
        self.item=queue.confirm(self.store,'TEST_ONLY_PRIVATE_QUESTION','TEST_ONLY_PRIVATE_TARGET')
        self.bid=batches.prepare(self.store,[{'id':self.item['id'],'revision':1}])
        self.path=batches.folder(self.root,self.bid)
        manifest=batches.read_manifest(self.path)
        adapter=self.path/'adapter'; adapter.mkdir()
        (adapter/'README.md').write_text('synthetic test double only',encoding='utf-8')
        (adapter/'adapter_config.json').write_text(json.dumps({'peft_type':'LORA','task_type':'CAUSAL_LM'}),encoding='utf-8')
        (adapter/'adapter_model.safetensors').write_bytes(b'not-real-weights')
        reports={phase:fixtures.report(manifest,phase) for phase in batches.PHASES}
        reports['train']['adapter_files']=batches.adapter_files(adapter)
        for phase,report in reports.items(): batches.save(self.path/(phase+'.json'),report)
        batches.save(self.path/'receipt.json',{'status':'candidate','manifest_sha256':queue.digest(manifest),
                    'report_files':{phase:batches.file_hash(self.path/(phase+'.json')) for phase in reports},
                    'assessment':batches.assess(manifest,reports)})

    def test_selection_binds_candidate_and_rechecks_withdrawal_without_sending_examples(self):
        selected=models.select(self.root,batch=self.bid)
        models.validate(selected); models.check_context(selected)
        self.assertEqual(selected['batch']['id'],self.bid)
        self.assertNotIn('TEST_ONLY_PRIVATE',json.dumps(selected))
        described=models.description(selected)
        self.assertTrue(described['confirmed_batch']); self.assertNotIn(str(self.root),json.dumps(described))
        self.assertEqual(described['parameter_version'],'batch-'+self.bid)
        with patch.object(local_models,'verify_assets',return_value=(self.root/'base',None)):
            self.assertEqual(models.verify_assets(selected),(self.root/'base',self.path/'adapter'))
        queue.withdraw(self.store,self.item['id'],1)
        for check in (models.validate,models.check_context):
            with self.assertRaises(ValueError): check(selected)
        with self.assertRaises(ValueError): models.choose(self.root,self.bid,0)

    def test_source_reader_is_read_only_and_missing_database_not_created(self):
        with models.QueueReader(self.root).connect() as db:
            with self.assertRaises(sqlite3.OperationalError): db.execute('DELETE FROM learning_examples')
        absent=self.root/'absent'
        self.assertEqual(models.choice(absent),models.initial())
        self.assertEqual(models.history(absent),[])
        self.assertFalse(absent.exists())
        with self.assertRaises(sqlite3.OperationalError):
            with models.QueueReader(absent).connect(): pass
        self.assertFalse(absent.exists())

    def test_saved_choice_compare_and_swap_rollback_and_no_hot_swap(self):
        old=LocalEngine(self.root,active=True)
        chosen=models.choose(self.root,self.bid,0)
        self.assertEqual(chosen['revision'],1)
        with self.assertRaises(ValueError): models.validate(old.selection)
        engine=LocalEngine(self.root,active=True)
        self.assertTrue(engine.describe()['confirmed_batch'])
        with self.assertRaises(ValueError): models.choose(self.root,None,0)
        self.assertEqual(models.choose(self.root,self.bid,1),chosen)
        base=models.choose(self.root,None,1)
        self.assertEqual(base['revision'],2)
        with self.assertRaises(ValueError): models.check_context(engine.selection)
        current=LocalEngine(self.root,active=True)
        self.assertEqual(current.describe()['parameter_version'],'unadapted_base')
        self.assertEqual([r['revision'] for r in models.history(self.root)],[2,1])
        self.assertEqual([r['revision'] for r in models.history(self.root,2)],[1])
        models.choose(self.root,self.bid,2)
        self.assertEqual(LocalEngine(self.root,active=True).describe()['saved_choice_revision'],3)
        self.assertEqual(LocalEngine(self.root).describe()['parameter_version'],'unadapted_base')

    def test_two_real_writers_cannot_overwrite_observed_choice(self):
        barrier=threading.Barrier(2); results=[]
        def save():
            barrier.wait()
            try: results.append(models.choose(self.root,self.bid,0))
            except ValueError as exc: results.append(str(exc))
        workers=[threading.Thread(target=save) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(10); self.assertFalse(worker.is_alive())
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        self.assertEqual(len(models.history(self.root)),1)

    def test_saved_corrupt_candidate_can_be_replaced_with_base_without_loading_it(self):
        models.choose(self.root,self.bid,0)
        selected=models.select(self.root,active=True)
        (self.path/'adapter/adapter_model.safetensors').write_bytes(b'corruption')
        with self.assertRaises(ValueError): models.validate(selected)
        with self.assertRaises(ValueError): models.select(self.root,active=True)
        restored=models.choose(self.root,None,1)
        self.assertEqual(restored['revision'],2)
        models.validate(models.select(self.root,active=True))

    def test_metadata_changes_and_unverified_batch_are_rejected(self):
        selected=models.select(self.root,batch=self.bid)
        for field,value in [('fingerprint','wrong'),('inference_sources',{}),('base',{}),('data_dir','relative')]:
            altered=copy.deepcopy(selected); altered[field]=value
            with self.assertRaises(ValueError): models.validate(altered)
        receipt=local_models.read_json(self.path/'receipt.json'); receipt['status']='rejected'
        batches.save(self.path/'receipt.json',receipt)
        with self.assertRaises(ValueError): models.select(self.root,batch=self.bid)
        with self.assertRaises(ValueError): models.choose(self.root,self.bid,0)

    def test_choice_history_reserves_base_recovery_and_input_bounds(self):
        with patch.object(models,'MAX_CHOICES',1):
            models.choose(self.root,self.bid,0)
            self.assertEqual(models.choose(self.root,None,1)['revision'],2)
            with self.assertRaises(ValueError): models.choose(self.root,self.bid,2)
            self.assertEqual(models.choose(self.root,None,2)['revision'],2)
        for revision in (True,-1,'1'):
            with self.assertRaises(ValueError): models.choose(self.root,None,revision)
        for before in (True,-1,0,'1'):
            with self.assertRaises(ValueError): models.history(self.root,before)

    def test_backup_does_not_activate_selection_or_copy_external_batch(self):
        from assistant.backup import create_backup,restore_backup
        models.choose(self.root,self.bid,0)
        saved=create_backup(self.store.path,self.root/'backup.paibackup')
        restored=self.root/'restored'; restore_backup(saved['path'],restored)
        self.assertEqual(models.choice(restored),models.initial())
        self.assertFalse((restored/'learning-batches').exists())
        self.assertEqual(LocalEngine(restored,active=True).describe()['parameter_version'],'unadapted_base')

    def test_live_withdrawal_or_choice_change_stops_owned_child_and_saves_no_answer(self):
        for mode in ('withdraw','choice'):
            if mode=='choice': queue.confirm(self.store,self.item['prompt'],self.item['answer'],item_id=self.item['id'],expected_revision=2)
            if mode=='choice':
                # The old trained revision is now stale; a base active engine still
                # demonstrates cancellation when its saved choice revision changes.
                selected=LocalEngine(self.root,active=True,timeout=10)
                action=lambda: self.change_base_choice_revision()
            else:
                selected=LocalEngine(self.root,batch=self.bid,timeout=10)
                action=lambda:queue.withdraw(self.store,self.item['id'],1)
            timer=threading.Timer(0.3,action); timer.start()
            try:
                sid=self.store.new_session('interrupted selection test')
                with patch.object(selected,'command',return_value=[sys.executable,'-c','import time; time.sleep(15)']):
                    with self.assertRaises(EngineError) as error: Assistant(self.store,selected).ask(sid,'test')
                self.assertEqual(error.exception.code,'local_model_changed')
                self.assertEqual(list(selected.workdir.iterdir()),[])
                with self.store.connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND role='assistant'",(sid,)).fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT status FROM answer_runs WHERE session_id=?',(sid,)).fetchone()[0],'failed')
            finally: timer.cancel()

    def change_base_choice_revision(self):
        # A separately committed base selection is a real revision change; no
        # successful training claim is made by this concurrency fixture.
        db=sqlite3.connect(models.choice_path(self.root))
        try:
            db.execute('CREATE TABLE choices(revision INTEGER PRIMARY KEY,batch_id TEXT,evidence_fingerprint TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO choices(revision) VALUES (1)'); db.commit()
        finally: db.close()

    def test_actual_assistant_records_batch_identity_but_never_inserts_training_text(self):
        engine=LocalEngine(self.root,batch=self.bid); sid=self.store.new_session()
        def command(output):
            return [sys.executable,'-c',engine_fixtures.SUCCESS+engine_fixtures.WRITE,str(output)]
        with patch.object(engine,'command',side_effect=command): answer=Assistant(self.store,engine).ask(sid,'簡短回覆')
        context=self.store.answer_context(answer['id'])
        self.assertEqual(context['engine_selection']['parameter_version'],'batch-'+self.bid)
        self.assertNotIn('TEST_ONLY_PRIVATE',json.dumps(context))

    def test_cli_selection_diagnostics_history_and_conflicts(self):
        cmd=[sys.executable,'scripts/learning/selection.py']
        def run(args,code=0,entry=cmd):
            result=subprocess.run(entry+args+['--data-dir',str(self.root)],capture_output=True,text=True,encoding='utf-8',
                                  timeout=20,cwd=local_models.ROOT,env={**os.environ,'PYTHONIOENCODING':'utf-8'})
            self.assertEqual(result.returncode,code,result.stderr)
            self.assertNotIn('Traceback',result.stderr)
            return result
        self.assertEqual(json.loads(run(['status']).stdout)['choice']['revision'],0)
        self.assertEqual(json.loads(run(['use','--batch',self.bid,'--revision','0']).stdout)['choice']['revision'],1)
        self.assertTrue(json.loads(run(['status']).stdout)['eligible'])
        run(['use','--base','--revision','0'],1)
        run(['use','--base','--revision','1'])
        self.assertEqual(len(json.loads(run(['history']).stdout)['items']),2)
        app=[sys.executable,'-m','assistant']
        run(['--local-active','--doctor'],2,app)
        run(['--engine','local','--local-active','--local-batch',self.bid,'--doctor'],2,app)
        self.assertIn('unadapted_base',run(['--engine','local','--local-active','--doctor'],entry=app).stdout)
        self.assertIn('batch-'+self.bid,run(['--engine','local','--local-batch',self.bid,'--doctor'],entry=app).stdout)

    def test_private_batch_and_inference_workspaces_never_publish_under_source_folders(self):
        from scripts.publish import validate_path
        for name in ['assistant/learning-batches/id/manifest.json','docs/LEARNING-BATCHES/id/train.json',
                     'assistant/local-engine-workspace/answer/request.json','scripts/LOCAL-ENGINE-WORKSPACE/worker.txt',
                     'assistant/local-model-choice.sqlite3']:
            self.assertFalse(validate_path(name),name)
        self.assertTrue(validate_path('assistant/learned_models.py'))
        self.assertTrue(validate_path('scripts/learning/selection.py'))
        result=subprocess.run(['git','check-ignore','--no-index','assistant/learning-batches/id/manifest.json',
                               'docs/local-engine-workspace/answer/request.json'],cwd=local_models.ROOT,
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(result.stdout.splitlines()),2)


if __name__=='__main__': unittest.main()
