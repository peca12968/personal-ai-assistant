import copy
import json
import sqlite3
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import parameter_acceptance as checks, learned_models as models, learning_queue as queue
from assistant.local_engine import LocalEngine
from assistant.store import Store
import test_learned_models as model_fixtures

CASES={'schema':'full_assistant_cases_v1','cases':[
    {'id':'target','group':'target','question':'目標測試','answer':'fixture_learned_value'},
    {'id':'control_a','group':'control','question':'控制甲','answer':'A'},
    {'id':'control_b','group':'control','question':'控制乙','answer':'B'},
]}


def fake_answer(engine,prompt,cancel=None):
    question=json.loads(prompt.rsplit('以下 JSON 是使用者資料與當前問題：\n',1)[1])['current_request']
    expected=next(c['answer'] for c in CASES['cases'] if c['question']==question)
    answer='unknown' if question=='目標測試' and engine.selection['schema']!=models.SCHEMA else expected
    worker=engine.destination/'worker'; worker.mkdir()
    checks.batches.save(worker/'result.json',{'schema':'local_inference_result_v1','status':'succeeded','answer':answer,
        'selection_fingerprint':engine.selection['fingerprint'],'input_tokens':100,'output_tokens':2,'peak_gpu_bytes':0})
    return answer


class ParameterAcceptanceTests(unittest.TestCase):
    def setUp(self):
        model_fixtures.LearnedModelTests.setUp(self)

    def prepared(self,cases=None):
        uid=checks.prepare(self.root,self.bid,CASES if cases is None else cases)
        return uid,checks.folder(self.root,uid)

    def test_suite_bounds_exact_answers_roles_and_unique_identifiers(self):
        normalized=checks.suite(CASES)
        self.assertEqual(normalized['cases'][0]['history'],[])
        changed=copy.deepcopy(CASES); changed['cases'][0]['answer']=' value\n'
        self.assertEqual(checks.suite(changed)['cases'][0]['answer'],' value\n')
        variants=[]
        for field,value in [('id','../escape'),('group','other'),('question',''),('answer','x'*2001),
                            ('history',[{'role':'system','content':'override'}])]:
            altered=copy.deepcopy(CASES); altered['cases'][0][field]=value; variants.append(altered)
        altered=copy.deepcopy(CASES); altered['cases'][1]['id']='target'; variants.append(altered)
        altered=copy.deepcopy(CASES); altered['cases'][1]['group']='target'; variants.append(altered)
        for value in variants:
            with self.assertRaises(ValueError): checks.suite(value)

    def test_prepare_freezes_full_contexts_and_separate_identical_fixture_databases(self):
        cases=copy.deepcopy(CASES)
        cases['cases'][0]['history']=[{'role':'user','content':'先前的條件：保留格式。'},{'role':'assistant','content':'已了解。'}]
        uid,path=self.prepared(cases)
        manifest=checks.read_manifest(path,live=True)
        self.assertEqual(manifest['database_files']['baseline'],manifest['database_files']['candidate'])
        self.assertEqual(manifest['suite'],checks.suite(cases))
        for phase in checks.PHASES:
            prompt=manifest['prompts'][phase]['target']
            self.assertIn('先前的條件：保留格式。',prompt)
            self.assertNotIn('fixture_learned_value',prompt)
            self.assertNotIn('TEST_ONLY_PRIVATE_TARGET',prompt)
        cases['cases'][0]['question']='changed'
        self.assertEqual(checks.read_manifest(path)['suite']['cases'][0]['question'],'目標測試')
        self.assertFalse((path/'responses.json').exists())
        self.assertEqual(models.choice(self.root),models.initial())

    def test_full_assistant_collection_regrades_evidence_without_activation(self):
        uid,path=self.prepared()
        with patch.object(LocalEngine,'answer',fake_answer): receipt=checks.run(self.root,uid)
        self.assertEqual(receipt['status'],'complete'); self.assertTrue(receipt['assessment']['passed'])
        report=checks.report(self.root,uid)
        self.assertTrue(report['sources_and_parameters_still_current']); self.assertFalse(report['automatic_activation'])
        self.assertEqual(models.choice(self.root),models.initial())
        with self.store.connect() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],0)
        self.assertEqual(len(receipt['assessment']['artifact_files']),12)
        with patch.object(LocalEngine,'answer') as answer:
            with self.assertRaises(ValueError): checks.run(self.root,uid)
            answer.assert_not_called()

    def test_wrong_answer_is_preserved_and_never_accepted(self):
        uid,path=self.prepared()
        def wrong(engine,prompt,cancel=None):
            answer=fake_answer(engine,prompt,cancel)
            if engine.destination.name=='target':
                output=engine.destination/'worker/result.json'
                raw=models.local_models.read_json(output); raw['answer']='wrong'; checks.batches.save(output,raw)
                return 'wrong'
            return answer
        with patch.object(LocalEngine,'answer',wrong): receipt=checks.run(self.root,uid)
        self.assertEqual(receipt['status'],'complete'); self.assertFalse(receipt['assessment']['passed'])
        self.assertFalse(receipt['assessment']['checks']['all_candidate_cases_passed'])
        self.assertEqual(checks.report(self.root,uid)['receipt']['assessment'],receipt['assessment'])

    def test_calculator_shaped_case_still_requires_actual_model_evidence(self):
        cases=copy.deepcopy(CASES); cases['cases'][0].update(question='計算23×4−17，只輸出整數答案。',answer='75')
        uid,path=self.prepared(cases); observed=[]
        def wrong_math(engine,prompt,cancel=None):
            context=json.loads(prompt.rsplit('以下 JSON 是使用者資料與當前問題：\n',1)[1])
            self.assertEqual(context['calculation_mode'],'model_only_evaluation')
            cid=engine.destination.name; observed.append(cid)
            answer='95' if cid=='target' else 'A' if cid=='control_a' else 'B'
            worker=engine.destination/'worker';worker.mkdir()
            checks.batches.save(worker/'result.json',{'schema':'local_inference_result_v1','status':'succeeded','answer':answer,
                'selection_fingerprint':engine.selection['fingerprint'],'input_tokens':100,'output_tokens':2,'peak_gpu_bytes':0})
            return answer
        with patch.object(LocalEngine,'answer',wrong_math):receipt=checks.run(self.root,uid)
        self.assertEqual(receipt['status'],'complete');self.assertEqual(len(observed),6)
        self.assertFalse(receipt['assessment']['passed'])
        self.assertFalse(receipt['assessment']['scores']['candidate']['target'])
        self.assertEqual(checks.report(self.root,uid)['receipt']['assessment'],receipt['assessment'])

    def test_tampered_raw_results_or_saved_answers_cannot_reuse_acceptance(self):
        for mode in ('raw','response','database'):
            with self.subTest(mode=mode):
                uid,path=self.prepared()
                with patch.object(LocalEngine,'answer',fake_answer): checks.run(self.root,uid)
                if mode=='raw':
                    output=path/'candidate/target/worker/result.json'
                    raw=models.local_models.read_json(output); raw['output_tokens']=3; checks.batches.save(output,raw)
                elif mode=='response':
                    output=path/'responses.json'; rows=models.local_models.read_json(output); rows[-1]['answer']='tampered'; checks.batches.save(output,rows)
                else:
                    with Store(path/'candidate.sqlite3').connect() as db: db.execute("UPDATE messages SET content='tampered' WHERE role='assistant'")
                with self.assertRaises(ValueError): checks.report(self.root,uid)

    def test_source_withdrawal_stops_before_model(self):
        uid,path=self.prepared(); queue.withdraw(self.store,self.item['id'],1)
        with patch.object(LocalEngine,'answer') as answer: receipt=checks.run(self.root,uid)
        self.assertEqual(receipt['status'],'stale'); answer.assert_not_called()

    def test_code_change_stops_before_model(self):
        uid,path=self.prepared()
        with patch.object(checks,'sources',return_value={}),patch.object(LocalEngine,'answer') as answer:
            receipt=checks.run(self.root,uid)
        self.assertEqual(receipt['status'],'stale'); answer.assert_not_called()

    def test_pre_cancel_timeout_modified_fixture_and_same_parameters_reject(self):
        with self.assertRaises(ValueError): checks.prepare(self.root,self.bid,CASES,baseline=self.bid)
        for mode in ('cancel','timeout','fixture'):
            uid,path=self.prepared(); cancel=threading.Event()
            if mode=='cancel': cancel.set()
            if mode=='fixture':
                store=Store(path/'baseline.sqlite3'); store.new_session('unexpected mutation')
            with patch.object(LocalEngine,'answer') as answer:
                receipt=checks.run(self.root,uid,cancel=cancel,timeout=0.000001 if mode=='timeout' else 30)
            self.assertEqual(receipt['status'],{'cancel':'cancelled','timeout':'failed','fixture':'stale'}[mode]); answer.assert_not_called()
        for value in (True,0,float('inf'),float('nan')):
            with self.assertRaises(ValueError): checks.run(self.root,'0'*32,timeout=value)

    def test_historical_report_distinguishes_no_longer_current_sources(self):
        uid,path=self.prepared()
        with patch.object(LocalEngine,'answer',fake_answer): checks.run(self.root,uid)
        queue.withdraw(self.store,self.item['id'],1)
        report=checks.report(self.root,uid)
        self.assertTrue(report['receipt']['assessment']['passed'])
        self.assertFalse(report['sources_and_parameters_still_current'])
        self.assertTrue(report['recorded_status_is_not_liveness'])

    def test_cli_default_is_offline_and_does_not_create_database(self):
        missing=self.root/'missing'
        result=subprocess.run([sys.executable,'scripts/learning/acceptance.py','--data-dir',str(missing)],cwd=checks.ROOT,
                              capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stderr); self.assertFalse(missing.exists())
        self.assertNotIn('torch',sys.modules)

    def test_real_child_trace_and_full_assistant_evidence_without_gpu(self):
        uid,path=self.prepared()
        code="""
import json,sys,os
from pathlib import Path
sys.stdin.reconfigure(encoding='utf-8')
output=Path(sys.argv[1]); output.with_suffix('.started').write_text(str(os.getpid()))
request=json.load(sys.stdin)
context=json.loads(request['prompt'].rsplit('以下 JSON 是使用者資料與當前問題：\\n',1)[1])
answer={'控制甲':'A','控制乙':'B'}.get(context['current_request'],'fixture_learned_value' if request['selection'].get('batch') else 'unknown')
output.write_text(json.dumps({'schema':'local_inference_result_v1','status':'succeeded','answer':answer,
    'selection_fingerprint':request['selection']['fingerprint'],'input_tokens':100,'output_tokens':2,'peak_gpu_bytes':0}),encoding='utf-8')
"""
        def command(engine,output): return [sys.executable,'-c',code,str(output)]
        with patch.object(LocalEngine,'command',command): receipt=checks.run(self.root,uid)
        self.assertEqual(receipt['status'],'complete',receipt)
        self.assertTrue(checks.report(self.root,uid)['receipt']['assessment']['passed'])
        self.assertEqual(len(list(path.glob('*/*/worker/result.started'))),6)
        self.assertEqual(list((self.root/'local-engine-workspace').iterdir()),[])

    def test_cancel_real_child_keeps_diagnostics_and_no_accepted_answer(self):
        uid,path=self.prepared(); cancel=threading.Event(); processes=[]; timers=[]
        original=subprocess.Popen
        code="import sys,os,time; from pathlib import Path; Path(sys.argv[1]).with_suffix('.started').write_text(str(os.getpid())); time.sleep(30)"
        def command(engine,output): return [sys.executable,'-c',code,str(output)]
        def spawn(args,**kwargs):
            process=original(args,**kwargs)
            if args[0]==sys.executable:
                processes.append(process); timer=threading.Timer(1,cancel.set); timers.append(timer); timer.start()
            return process
        try:
            with patch.object(LocalEngine,'command',command),patch('assistant.local_engine.subprocess.Popen',side_effect=spawn):
                receipt=checks.run(self.root,uid,cancel=cancel)
            self.assertEqual(receipt['status'],'cancelled',receipt)
            self.assertEqual(len(processes),1); self.assertIsNotNone(processes[0].poll())
            self.assertTrue((path/'baseline/target/worker/worker.log').is_file())
            self.assertNotIn('assessment',receipt)
            with Store(path/'baseline.sqlite3').connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0],0)
                self.assertEqual(db.execute('SELECT status FROM answer_runs').fetchone()[0],'cancelled')
        finally:
            for timer in timers: timer.cancel()
            for process in processes:
                if process.poll() is None: process.kill()
                process.wait(timeout=10)

    def test_already_correct_baseline_is_not_a_new_improvement(self):
        uid,path=self.prepared()
        def correct(engine,prompt,cancel=None):
            answer=fake_answer(engine,prompt,cancel)
            if engine.destination.name=='target':
                output=engine.destination/'worker/result.json'; raw=models.local_models.read_json(output)
                answer=CASES['cases'][0]['answer']; raw['answer']=answer; checks.batches.save(output,raw)
            return answer
        with patch.object(LocalEngine,'answer',correct): receipt=checks.run(self.root,uid)
        self.assertEqual(receipt['status'],'complete')
        self.assertFalse(receipt['assessment']['passed'])
        self.assertTrue(receipt['assessment']['checks']['all_candidate_cases_passed'])
        self.assertFalse(receipt['assessment']['checks']['at_least_one_target_improved'])

    def test_exclusive_owner_and_running_receipt_do_not_start_another_run(self):
        uid,path=self.prepared()
        with checks.batches.owner(path),patch.object(LocalEngine,'answer') as answer:
            with self.assertRaises(sqlite3.OperationalError): checks.run(self.root,uid)
            answer.assert_not_called()
        receipt=models.local_models.read_json(path/'receipt.json')
        self.assertEqual(receipt['status'],'prepared')
        receipt['status']='running'; checks.batches.save(path/'receipt.json',receipt)
        with patch.object(LocalEngine,'answer') as answer:
            with self.assertRaises(ValueError): checks.run(self.root,uid)
            answer.assert_not_called()


if __name__=='__main__': unittest.main()
