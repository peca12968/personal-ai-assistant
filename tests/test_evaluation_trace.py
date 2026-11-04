"""Actual engine-bound requests, fixed collector inputs, and legacy boundaries."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.engine import Assistant
from scripts import evaluate_understanding as ev
from scripts.evaluation_reports import (TRACE_MODE, canonical_digest, validate_report,
                                       verify_suite)
from scripts.review_understanding import apply_reviews, compare, summarize


class EvaluationTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        self.suite=self.folder/'suite.json';self.report=self.folder/'report.json'
        self.case={'id':'one','category':'context','prompt':'  請回答三加四。  ',
                   'criteria':['回答7'], 'history':[{'role':'user','content':'這是合成測試。'}]}
        self.suite.write_text(json.dumps([self.case],ensure_ascii=False),encoding='utf-8')
        self.captured=[]

    def collect(self, *, resume=False, response='7', hook=None):
        captured=self.captured
        class Engine:
            def __init__(self,*a,**k):pass
            def answer(self,request,cancel=None):
                captured.append(request)
                if hook:hook()
                return response
        argv=['evaluate','--live','--suite',str(self.suite),'--report',str(self.report)]
        if resume:argv.append('--resume')
        with patch('scripts.evaluate_understanding.CodexEngine',Engine), \
             patch('sys.argv',argv),patch('sys.stdout',io.StringIO()),patch('sys.stderr',io.StringIO()):
            ev.main()
        return json.loads(self.report.read_text(encoding='utf-8'))

    def test_local_reports_bind_each_request_and_reject_resume_with_different_weights(self):
        class Local:
            def __init__(inner, directory, *, batch=None):
                inner.selection = {'fingerprint': ('a' if batch is None else 'b') * 64}

            def describe(inner):
                return {'selection_fingerprint': inner.selection['fingerprint']}

            def answer(inner, request, cancel=None):
                self.captured.append(request)
                return '7'

        argv = ['evaluate', '--live', '--suite', str(self.suite), '--report', str(self.report),
                '--local-data-dir', str(self.folder)]
        with patch('assistant.local_engine.LocalEngine', Local), patch.object(ev, 'CodexEngine', side_effect=AssertionError), \
                patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
            with patch('sys.argv', argv):
                ev.main()
            original = self.report.read_bytes()
            report = json.loads(original)
            self.assertEqual(report['mode'], ev.LOCAL_TRACE_MODE)
            self.assertEqual(report['results'][0]['context']['engine_selection']['selection_fingerprint'], 'a' * 64)
            with patch('sys.argv', argv + ['--resume', '--local-batch', 'another']), self.assertRaises(SystemExit):
                ev.main()
            self.assertEqual(len(self.captured), 1)
            self.assertEqual(self.report.read_bytes(), original)
            report['fingerprint']['local_selection_sha256'] = 'b' * 64
            with self.assertRaisesRegex(ValueError, '引擎'):
                validate_report(report)
            wrong_mode = json.loads(original)
            wrong_mode['mode'] = TRACE_MODE
            with self.assertRaisesRegex(ValueError, '評估模式'):
                validate_report(wrong_mode)
            candidate_argv = argv.copy()
            candidate_argv[candidate_argv.index('--report') + 1] = str(self.folder/'candidate.json')
            with patch('sys.argv', candidate_argv + ['--local-batch', 'another']):
                ev.main()
            paired = compare(json.loads(original), json.loads((self.folder/'candidate.json').read_bytes()),
                             suite_bytes=self.suite.read_bytes())
            self.assertEqual(set(paired['changed_runtime_fingerprints']), {'local_selection_sha256'})
            self.assertEqual(paired['changed_request_ids'], ['one'])

    def test_local_flags_require_explicit_live_mode_and_data_directory(self):
        for options in (['--local-data-dir', str(self.folder)], ['--live', '--local-batch', 'candidate']):
            with self.subTest(options=options), patch('sys.argv', ['evaluate', *options]), \
                    patch('sys.stderr', io.StringIO()), patch('assistant.local_engine.LocalEngine') as engine:
                with self.assertRaises(SystemExit):
                    ev.main()
                engine.assert_not_called()

    def test_one_actual_request_and_persisted_context_not_a_preflight_reconstruction(self):
        calls=[];original=Assistant.prepare_context
        def observed(app,*args,**kwargs):
            calls.append(True)
            return original(app,*args,**kwargs)
        with patch.object(Assistant,'prepare_context',observed):
            report=self.collect(response='7 [D1:L999]')
        row=report['results'][0]
        self.assertEqual(len(calls),1);self.assertEqual(len(self.captured),1)
        self.assertEqual(row['request'],self.captured[0])
        self.assertEqual(row['request_sha256'],ev.sha(self.captured[0].encode()))
        self.assertEqual(row['prompt'],self.case['prompt'])
        self.assertEqual(row['context']['current_request'],self.case['prompt'].strip())
        self.assertEqual(row['engine_answer'],'7 [D1:L999]')
        self.assertNotEqual(row['answer'],row['engine_answer'])  # Assistant citation warning remains separate.
        self.assertEqual(row['fixture'],self.case)
        self.assertEqual(report['mode'],TRACE_MODE)
        validate_report(report);self.assertTrue(verify_suite(report,self.suite.read_bytes()))
        self.assertEqual(summarize(report)['request_trace_verified_ids'],['one'])

    def test_accidental_request_context_fixture_and_boundary_tampering_is_rejected(self):
        original=self.collect()
        changes=[lambda r:r.update(request=r['request']+' '),
                 lambda r:r['context'].update(current_request='另一個問題'),
                 lambda r:r['fixture']['history'][0].update(content='不同背景'),
                 lambda r:r.update(request_context_offset=True),
                 lambda r:r.update(request_context_offset=0),
                 lambda r:r.update(engine_call_count=0),
                 lambda r:r.update(engine_call_count=True),
                 lambda r:r.pop('engine_answer')]
        for change in changes:
            report=copy.deepcopy(original);change(report['results'][0])
            with self.subTest(change=changes.index(change)),self.assertRaises(ValueError):validate_report(report)
        report=copy.deepcopy(original);report['fingerprint'].pop('runner_sha256')
        with self.assertRaises(ValueError):validate_report(report)
        report=copy.deepcopy(original);row=report['results'][0]
        row.pop('prompt');row['fixture']['prompt']=None
        row['fixture_sha256']=canonical_digest(row['fixture'])
        with self.assertRaises(ValueError):validate_report(report)

    def test_full_fixture_is_verified_even_if_prompt_and_rubric_are_unchanged(self):
        report=self.collect();row=report['results'][0]
        row['fixture']['history'][0]['content']='另一段合成背景'
        row['fixture_sha256']=canonical_digest(row['fixture'])
        validate_report(report)  # Internally consistent hashes do not authenticate the origin.
        with self.assertRaises(ValueError):verify_suite(report,self.suite.read_bytes())
        with self.assertRaises(ValueError):compare(json.loads(self.report.read_text()),report)

    def test_resume_validates_saved_evidence_before_any_new_call_or_write(self):
        report=self.collect();apply_reviews(report,[{'id':'one','criteria_pass':[True],'note':'合成評讀'}],'development_agent')
        report['results'][0]['answer']='改動過的答案'
        ev.save_report(self.report,report);before=self.report.read_bytes()
        self.captured.clear()
        with self.assertRaises(SystemExit):self.collect(resume=True)
        self.assertEqual(self.captured,[]);self.assertEqual(self.report.read_bytes(),before)

    def test_completed_resume_makes_no_duplicate_call_and_legacy_mode_is_not_upgraded(self):
        report=self.collect();before=self.report.read_bytes();self.captured.clear()
        self.collect(resume=True)
        self.assertEqual(self.captured,[]);self.assertEqual(self.report.read_bytes(),before)
        legacy=copy.deepcopy(report);legacy.pop('mode')
        validate_report(legacy)
        self.assertEqual(summarize(legacy)['request_trace_verified_ids'],[])
        with self.assertRaises(ValueError):compare(legacy,report)
        ev.save_report(self.report,legacy);before=self.report.read_bytes()
        with self.assertRaises(SystemExit):self.collect(resume=True)
        self.assertEqual(self.report.read_bytes(),before)

    def test_collector_or_validator_changes_during_answer_do_not_record_last_result(self):
        for attribute in ('RUNNER_PATH','VALIDATOR_PATH'):
            with self.subTest(attribute=attribute):
                script=self.folder/(attribute+'.py');script.write_text('before',encoding='utf-8')
                self.report=self.folder/(attribute+'.json')
                with patch.object(ev,attribute,script),self.assertRaises(SystemExit):
                    self.collect(hook=lambda:script.write_text('after',encoding='utf-8'))
                report=json.loads(self.report.read_text())
                self.assertEqual(report['results'],[])

    def test_loaded_suite_change_is_detected_before_collection(self):
        original=ev.live_fingerprint
        def changed(path):
            self.suite.write_text(json.dumps([{**self.case,'prompt':'另一個測試'}]),encoding='utf-8')
            return original(path)
        with patch.object(ev,'live_fingerprint',changed),self.assertRaises(SystemExit):self.collect()
        self.assertEqual(self.captured,[]);self.assertFalse(self.report.exists())

    def test_local_command_is_not_misreported_as_a_model_answer(self):
        self.suite.write_text(json.dumps([{**self.case,'prompt':'列出記憶'}],ensure_ascii=False),encoding='utf-8')
        with self.assertRaises(SystemExit):self.collect()
        self.assertEqual(self.captured,[])
        self.assertEqual(json.loads(self.report.read_text())['results'],[])

    def test_duplicate_nonfinite_or_invalid_cases_stop_without_calls(self):
        samples=['[{"id":"x","id":"y"}]','[{"id":"x","value":NaN}]','[2]',
                 json.dumps([{**self.case,'category':True}]),json.dumps([{**self.case,'criteria':[' ']}])]
        for raw in samples:
            with self.subTest(raw=raw):
                self.suite.write_text(raw,encoding='utf-8')
                with self.assertRaises(SystemExit):self.collect()
                self.assertFalse(self.report.exists());self.assertEqual(self.captured,[])

    def test_oversized_or_nonfinite_write_preserves_previous_complete_file(self):
        report=self.collect();before=self.report.read_bytes()
        with patch.object(ev,'MAX_JSON_BYTES',100),self.assertRaises(ValueError):ev.save_report(self.report,report)
        self.assertEqual(self.report.read_bytes(),before)
        with self.assertRaises(ValueError):ev.save_report(self.report,{'bad':float('nan')})
        self.assertEqual(self.report.read_bytes(),before)


if __name__=='__main__':unittest.main()
