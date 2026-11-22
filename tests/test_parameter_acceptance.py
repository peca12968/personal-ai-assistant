import copy
from contextlib import nullcontext
import io
import json
import sqlite3
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from assistant import parameter_acceptance as checks, learned_models as models, learning_queue as queue
from assistant.local_engine import LocalEngine
from assistant.store import Store
from scripts.learning import acceptance as cli, run_daily_comparison as daily
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

    def test_daily_comparison_keeps_timeouts_separate_and_attempts_each_turn_once(self):
        uid, path = self.prepared()
        receipt = (path / 'receipt.json').read_bytes()
        failure = daily.EngineError('fixture timeout', 'timeout')
        with patch.object(daily.IdleWindow, 'is_set', return_value=False), patch.object(
                LocalEngine, '_answer', side_effect=failure) as answer:
            result = daily.run(self.root, uid, timeout=300)
        self.assertEqual(result['timeout_seconds'], 300)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(answer.call_count, 6)
        self.assertEqual(len({(r['phase'], r['id']) for r in result['rows']}), 6)
        self.assertTrue(all(r['error_code'] == 'timeout' and 'exact' not in r for r in result['rows']))
        self.assertEqual(receipt, (path / 'receipt.json').read_bytes())

    def test_daily_comparison_retains_worker_result_from_string_temp_path(self):
        engine = object.__new__(daily.DailyEngine)
        engine.location = self.root / 'observation'
        engine.location.mkdir()
        with patch.object(LocalEngine, 'response_directory', return_value=nullcontext(str(self.root))):
            with engine.response_directory() as folder:
                (Path(folder) / 'result.json').write_text('{"status":"succeeded"}', encoding='utf8')
        self.assertEqual(json.loads((engine.location / 'worker-result.json').read_text('utf8')), {'status':'succeeded'})

    def test_daily_comparison_uses_isolated_stores_and_rejects_unrelated_worker_answers(self):
        uid, path = self.prepared()
        manifest = checks.read_manifest(path)
        receipt = (path / 'receipt.json').read_bytes()

        def answer(engine, payload, texts, cancel):
            self.assertEqual(payload['schema'], 'local_chat_budget_request_v1')
            question = json.loads(payload['prompt'].rsplit('以下 JSON 是使用者資料與當前問題：\n', 1)[1])['current_request']
            value = next(case['answer'] for case in CASES['cases'] if case['question'] == question)
            checks.batches.save(engine.location / 'worker-result.json', {
                'schema': 'local_inference_result_v1', 'status': 'succeeded', 'answer': value,
                'selection_fingerprint': engine.selection['fingerprint'], 'input_tokens': 10})
            return value, None

        with patch.object(daily.IdleWindow, 'is_set', return_value=False), patch.object(LocalEngine, '_answer', answer):
            report = daily.run(self.root, uid)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(len(report['rows']), 6)
        self.assertTrue(all(row['exact'] and row['raw_exact'] for row in report['rows']))
        self.assertEqual((path / 'receipt.json').read_bytes(), receipt)
        for phase in checks.PHASES:
            self.assertEqual(checks.models.file_hash(path / (phase + '.sqlite3')), manifest['database_files'][phase])
        out, = (path.parents[1] / '_daily-comparisons').iterdir()
        row = report['rows'][0]
        case = CASES['cases'][0]
        location = out / row['phase'] / case['id']
        result = location / 'worker-result.json'
        info = json.loads(result.read_text(encoding='utf8'))
        checks.batches.save(result, {**info, 'answer': 'different'})
        context = json.loads((location / 'request.json').read_text(encoding='utf8'))['prompt']
        context = json.loads(context.rsplit('以下 JSON 是使用者資料與當前問題：\n', 1)[1])
        answer_row = {'id': row['message_id'], 'run_id': row['run_id'], 'content': row['answer']}
        with self.assertRaisesRegex(ValueError, '原答'):
            daily.verify_turn(location, out / (row['phase'] + '.sqlite3'), manifest, row['phase'],
                              manifest['suite']['cases'][0], answer_row, context)

    def test_preflight_counts_both_selections_in_worker_order_including_at_the_limit(self):
        _, path = self.prepared()
        manifest = checks.read_manifest(path)
        limit = cli.local_models.MAX_INPUT_TOKENS
        for exceeds in (False, True):
            lengths = [limit, limit + int(exceeds), 1, 2, 3, 4]
            with self.subTest(exceeds=exceeds), patch.object(
                    cli.local_worker, 'request_messages', wraps=cli.local_worker.request_messages) as framing:
                tokenizer = Mock()
                tokenizer.apply_chat_template.side_effect = [range(n) for n in lengths]
                result = cli.count_inputs(manifest, tokenizer)
                self.assertEqual(result['all_inputs_fit'], not exceeds)
                self.assertEqual([r['input_tokens'] for r in result['cases']], lengths)
                self.assertEqual([r['over_by'] for r in result['cases']], [0, int(exceeds), 0, 0, 0, 0])
                for row, call, template in zip(result['cases'], framing.call_args_list,
                                               tokenizer.apply_chat_template.call_args_list, strict=True):
                    phase, cid = row['phase'], row['case_id']
                    self.assertEqual(call.args[0], {'schema': 'local_inference_request_v1',
                        'prompt': manifest['prompts'][phase][cid], 'selection': manifest['selections'][phase]})
                    self.assertEqual(template.kwargs, {'tokenize': True, 'add_generation_prompt': True,
                                                       'enable_thinking': False})

    def test_run_cli_stops_at_oversized_input_without_consuming_the_prepared_check(self):
        uid, path = self.prepared()
        receipt = (path / 'receipt.json').read_bytes()
        failure = subprocess.CompletedProcess([], 1, '{"all_inputs_fit":false}')
        with patch.object(sys, 'argv', ['acceptance.py','run','--data-dir',str(self.root),'--id',uid]), \
                patch.object(sys, 'prefix', str(self.root)), patch.object(cli.subprocess, 'run', return_value=failure), \
                patch.object(checks, 'run') as run, \
                patch.object(sys, 'stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main(), 1)
            run.assert_not_called()
        self.assertEqual((path / 'receipt.json').read_bytes(), receipt)

    def test_run_cli_continues_to_model_assessment_when_inputs_fit(self):
        uid, _ = self.prepared()
        fits = subprocess.CompletedProcess([], 0, '{"all_inputs_fit":true}')
        with patch.object(sys, 'argv', ['acceptance.py','run','--data-dir',str(self.root),'--id',uid]), \
                patch.object(sys, 'prefix', str(self.root)), patch.object(cli.subprocess, 'run', return_value=fits), \
                patch.object(checks, 'run', return_value={'assessment':{'passed':False}}) as run, \
                patch.object(sys, 'stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main(), 1)
            run.assert_called_once_with(self.root, uid)

    def test_preflight_skips_only_weight_contents_on_a_rejected_input(self):
        import hashlib

        revision = 'b' * 40
        base = self.root / 'data/learning/models/Qwen3-4B' / revision
        base.mkdir(parents=True)
        assets = {'tokenizer.json': b'abc', 'model.safetensors': b'xyz'}
        for name, content in assets.items():
            (base / name).write_bytes(content)
        spec = {'model_id': 'Qwen/Qwen3-4B', 'revision': revision,
                'files': [{'name': name, 'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
                          for name, value in assets.items()]}
        pinned = self.root / 'scripts/learning/model.json'
        pinned.parent.mkdir(parents=True)
        pinned.write_text(json.dumps(spec), encoding='utf8')
        check_id = 'a' * 32
        out = checks.folder(self.root, check_id)
        out.mkdir(parents=True)
        with patch.object(cli.local_models, 'ROOT', self.root):
            selected = cli.local_models.select()
            manifest = {'selections': dict.fromkeys(checks.PHASES, selected)}
            checks.batches.save(out / 'receipt.json', {'schema': 'full_assistant_receipt_v1',
                                                       'manifest_sha256': checks.queue.digest(manifest)})
            module = Mock()
            with patch.object(checks, 'read_manifest', return_value=manifest), patch.object(
                    cli.importlib.metadata, 'version', return_value=cli.EXPECTED_VERSIONS['transformers']), patch.dict(
                    sys.modules, {'transformers': module}), patch.dict(cli.os.environ), patch.object(
                    cli, 'count_inputs') as count:
                count.side_effect = lambda *args: {'all_inputs_fit': False}
                (base / 'model.safetensors').write_bytes(b'bad')
                rejected = cli.preflight(self.root, check_id)
                self.assertEqual(rejected['asset_check'], 'configuration_hashes_and_weight_sizes')
                module.AutoTokenizer.from_pretrained.assert_called_once_with(
                    base, local_files_only=True, trust_remote_code=False)
                count.side_effect = lambda *args: {'all_inputs_fit': True}
                with self.assertRaisesRegex(ValueError, 'asset'):
                    cli.preflight(self.root, check_id)
                (base / 'model.safetensors').write_bytes(assets['model.safetensors'])
                self.assertEqual(cli.preflight(self.root, check_id)['asset_check'], 'full_base_assets')
                count.side_effect = lambda *args: {'all_inputs_fit': False}
                (base / 'model.safetensors').write_bytes(b'x')
                with self.assertRaisesRegex(ValueError, '大小'):
                    cli.preflight(self.root, check_id)
                (base / 'model.safetensors').write_bytes(assets['model.safetensors'])
                (base / 'tokenizer.json').write_bytes(b'bad')
                with self.assertRaisesRegex(ValueError, '設定檔'):
                    cli.preflight(self.root, check_id)

    def test_preflight_rejects_a_malformed_receipt_before_loading_the_tokenizer(self):
        uid, path = self.prepared()
        checks.batches.save(path / 'receipt.json', ['invalid receipt'])
        with self.assertRaisesRegex(ValueError, '收據'), patch.object(cli.importlib.metadata, 'version') as version:
            cli.preflight(self.root, uid)
        version.assert_not_called()

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

    def test_report_distinguishes_matching_answers_without_relaxing_exact_scores(self):
        cases = checks.suite(models.local_models.read_json(checks.ROOT/'evals/local_field_revision_acceptance.json'))
        expected = {case['id']: case['answer'] for case in cases['cases']}
        uid, _ = self.prepared(cases)
        self.assertIsNone(checks.report(self.root, uid)['verified_mismatches'])

        def answer(engine, prompt, cancel=None):
            cid = engine.destination.name
            value = expected[cid]
            if engine.selection['schema'] == models.SCHEMA:
                if cid == 'current_north_revision':
                    value = expected['original_north_flag']
                elif cid == 'unchanged_south_flag':
                    value += '。'
            elif cid == 'recall_after_incorrect_reply':
                value = '待查'
            worker = engine.destination/'worker'
            worker.mkdir()
            checks.batches.save(worker/'result.json', {'schema':'local_inference_result_v1', 'status':'succeeded',
                                'selection_fingerprint':engine.selection['fingerprint'], 'answer':value})
            return value

        with patch.object(LocalEngine, 'answer', answer):
            receipt = checks.run(self.root, uid)
        report = checks.report(self.root, uid)
        self.assertEqual(receipt['status'], 'complete')
        self.assertTrue(receipt['assessment']['checks']['at_least_one_target_improved'])
        self.assertFalse(receipt['assessment']['checks']['baseline_correct_cases_retained'])
        self.assertFalse(report['receipt']['assessment']['passed'])
        self.assertEqual(report['verified_mismatches'], [
            {'phase':'baseline', 'case_id':'recall_after_incorrect_reply', 'matches_expected_with_added_period':False,
             'matches_other_case_ids':[]},
            {'phase':'candidate', 'case_id':'current_north_revision', 'matches_expected_with_added_period':False,
             'matches_other_case_ids':['original_north_flag', 'unadopted_suggestion']},
            {'phase':'candidate', 'case_id':'unchanged_south_flag', 'matches_expected_with_added_period':True,
             'matches_other_case_ids':[]},
        ])

    def test_evidence_binds_the_original_turn_even_when_prior_messages_have_identical_text(self):
        cases = copy.deepcopy(CASES)
        cases['cases'][0]['history'] = [{'role':'user','content':'目標測試'},
                                        {'role':'assistant','content':'fixture_learned_value'}]
        uid, path = self.prepared(cases)
        with patch.object(LocalEngine, 'answer', fake_answer):
            self.assertEqual(checks.run(self.root, uid)['status'], 'complete')
        cases_manifest = checks.read_manifest(path)
        store = Store(path/'candidate.sqlite3')
        sid = cases_manifest['sessions']['target']
        with store.connect() as db:
            old_q, old_a, _, new_a = [r[0] for r in db.execute(
                'SELECT id FROM messages WHERE session_id=? ORDER BY id', (sid,))]
        alterations = [
            ('UPDATE answer_runs SET user_message_id=? WHERE session_id=?', (old_q,sid)),
            ('UPDATE answer_runs SET answer_message_id=? WHERE session_id=?', (old_a,sid)),
            ("UPDATE messages SET content='other question' WHERE id=?", (old_q,)),
            ("UPDATE answer_contexts SET payload='{}' WHERE message_id=?", (new_a,)),
        ]
        for sql, params in alterations:
            with self.subTest(sql=sql), store.connect() as db:
                # Make the changed copy visible to the independent, read-only report.
                with sqlite3.connect(':memory:') as original:
                    db.backup(original)
                    db.execute(sql, params)
                    db.commit()
                    try:
                        with self.assertRaises(ValueError):
                            checks.report(self.root, uid)
                    finally:
                        original.backup(db)
        self.assertTrue(checks.report(self.root, uid)['receipt']['assessment']['passed'])

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
