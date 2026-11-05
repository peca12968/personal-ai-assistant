"""Failed requests stay private and cannot be silently retried on resume."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.engine import CodexEngine, EngineError
from scripts import evaluate_understanding as ev
from scripts import evaluation_attempts as attempts
from scripts.publish import validate_path


class EvaluationAttemptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.suite = self.root / 'suite.json'
        self.report = self.root / 'report.json'
        self.case = {'id': 'one', 'category': 'context', 'prompt': '請回答紫色。', 'criteria': ['紫色']}
        self.suite.write_text(json.dumps([self.case], ensure_ascii=False), encoding='utf-8')
        self.calls = []

    @property
    def folder(self):
        return self.report.with_name(self.report.name + '.attempts')

    def read(self, name):
        return json.loads((self.folder / name).read_bytes())

    def run_cli(self, *, resume=False, answer='紫色', hook=None):
        calls = self.calls
        class Engine:
            def __init__(self, *args, **kwargs):
                pass
            def answer(self, request, cancel=None):
                # The exact engine input has already reached durable storage.
                captured = self_test.read(f'{len(calls)}.request.json')
                self_test.assertEqual(captured['request'], request)
                calls.append(request)
                if hook:
                    hook()
                return answer
        self_test = self
        argv = ['evaluate', '--live', '--suite', str(self.suite), '--report', str(self.report)]
        if resume:
            argv.append('--resume')
        with patch.object(ev, 'CodexEngine', Engine), patch('sys.argv', argv), \
                patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
            ev.main()

    def test_error_keeps_exact_request_fixture_sources_and_typed_error_without_answer(self):
        def fail():
            raise EngineError('合成輸出長度失敗', 'local_generation_limit')
        with self.assertRaises(SystemExit):
            self.run_cli(hook=fail)
        request, outcome, binding = self.read('0.request.json'), self.read('0.outcome.json'), self.read('binding.json')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(request['request'], self.calls[0])
        self.assertEqual(request['fixture'], self.case)
        self.assertEqual(request['fixture_sha256'], ev.canonical_digest(self.case))
        self.assertEqual(request['run_sha256'], ev.canonical_digest(binding))
        self.assertEqual(binding['fingerprint'], ev.live_fingerprint(self.suite))
        self.assertEqual(outcome['request_evidence_sha256'], ev.canonical_digest(request))
        self.assertEqual(outcome['status'], 'error')
        self.assertEqual(outcome['error_type'], 'EngineError')
        self.assertEqual(outcome['error_code'], 'local_generation_limit')
        self.assertNotIn('engine_answer', outcome)
        self.assertEqual(json.loads(self.report.read_bytes())['results'], [])
        before = self.report.read_bytes()
        with self.assertRaises(SystemExit):
            self.run_cli(resume=True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.report.read_bytes(), before)

    def test_returned_raw_survives_source_change_but_no_scored_or_collected_row(self):
        source = self.root / 'attempts.py'
        source.write_text('before', encoding='utf-8')
        with patch.object(ev, 'ATTEMPTS_PATH', source), self.assertRaises(SystemExit):
            self.run_cli(answer='原始回答', hook=lambda: source.write_text('after', encoding='utf-8'))
        self.assertEqual(self.read('0.outcome.json')['engine_answer'], '原始回答')
        self.assertEqual(self.read('0.outcome.json')['status'], 'returned')
        self.assertEqual(json.loads(self.report.read_bytes())['results'], [])

    def test_partial_or_started_request_never_implies_liveness_or_authorizes_retry(self):
        report = ev.load_report(self.report, ev.live_fingerprint(self.suite), ['one'], False, mode=ev.TRACE_MODE)
        ev.save_report(self.report, report)
        journal = attempts.AttemptJournal(self.report, report)
        request_path, _ = journal.paths('one')
        request_path.write_text('{partial', encoding='utf-8')
        with self.assertRaises(SystemExit):
            self.run_cli(resume=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(request_path.read_text(), '{partial')

    def test_success_keeps_raw_separate_and_completed_resume_makes_no_call(self):
        self.run_cli(answer='紫色 [D1:L999]')
        report = json.loads(self.report.read_bytes())
        row = report['results'][0]
        self.assertNotEqual(row['answer'], row['engine_answer'])
        self.assertEqual(self.read('0.outcome.json')['engine_answer'], row['engine_answer'])
        self.assertIsNone(row['human_grade'])
        before = self.report.read_bytes()
        self.run_cli(resume=True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.report.read_bytes(), before)

    def test_resume_can_collect_only_case_that_never_reached_boundary(self):
        second = {**self.case, 'id': 'two', 'prompt': '請回答綠色。'}
        self.suite.write_text(json.dumps([self.case, second]), encoding='utf-8')
        original = ev.collect_case
        def stop_before_second(*args, **kwargs):
            if args[2]['id'] == 'two':
                raise ValueError('在第二題呼叫邊界前停止')
            return original(*args, **kwargs)
        with patch.object(ev, 'collect_case', side_effect=stop_before_second), self.assertRaises(SystemExit):
            self.run_cli()
        self.assertEqual(len(self.calls), 1)
        self.run_cli(resume=True, answer='綠色')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([r['answer'] for r in json.loads(self.report.read_bytes())['results']], ['紫色', '綠色'])

    def test_tampered_or_missing_outcome_blocks_resume_before_report_write(self):
        self.run_cli()
        before = self.report.read_bytes()
        outcome_path = self.folder / '0.outcome.json'
        raw = outcome_path.read_bytes()
        outcome = json.loads(raw)
        outcome['engine_answer'] = '竄改'
        outcome_path.write_text(json.dumps(outcome), encoding='utf-8')
        with self.assertRaises(SystemExit):
            self.run_cli(resume=True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.report.read_bytes(), before)
        outcome_path.unlink()
        with self.assertRaises(SystemExit):
            self.run_cli(resume=True)
        self.assertEqual(self.report.read_bytes(), before)

    def test_journal_is_bound_to_report_not_just_matching_case_id(self):
        self.run_cli()
        binding = self.read('binding.json')
        binding['fingerprint']['source_sha256'] = 'a' * 64
        (self.folder / 'binding.json').write_text(json.dumps(binding), encoding='utf-8')
        before = self.report.read_bytes()
        with self.assertRaises(SystemExit):
            self.run_cli(resume=True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.report.read_bytes(), before)

    def test_report_lock_prevents_parallel_writer_and_leftover_file_allows_new_owner(self):
        with attempts.report_lease(self.report):
            with self.assertRaises(SystemExit):
                self.run_cli()
            self.assertFalse(self.report.exists())
            self.assertFalse(self.folder.exists())
        self.run_cli()
        self.assertEqual(len(self.calls), 1)

    def test_start_write_failure_stops_before_engine_and_outcome_write_failure_forbids_retry(self):
        original = attempts.exclusive_json
        def fail_request(path, value):
            if path.name.endswith('.request.json'):
                raise OSError('合成磁碟錯誤')
            original(path, value)
        with patch.object(attempts, 'exclusive_json', side_effect=fail_request), self.assertRaises(SystemExit):
            self.run_cli()
        self.assertEqual(self.calls, [])
        def fail_outcome(path, value):
            if path.name.endswith('.outcome.json'):
                raise OSError('合成回傳保存錯誤')
            original(path, value)
        with patch.object(attempts, 'exclusive_json', side_effect=fail_outcome), self.assertRaises(SystemExit):
            self.run_cli(resume=True)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(SystemExit):
            self.run_cli(resume=True)
        self.assertEqual(len(self.calls), 1)

    def test_invalid_return_is_not_a_fabricated_success_or_silently_truncated_answer(self):
        for index, (raw, code) in enumerate(((None, 'non_text'), ('', 'empty_response'),
                                             ('\ud800', 'invalid_utf8'), ('x' * 250000, 'output_too_large'))):
            self.report = self.root / f'invalid-{index}.json'
            self.calls.clear()
            with patch.object(attempts, 'MAX_TRACE_BYTES', 200000), self.assertRaises(SystemExit):
                self.run_cli(answer=raw)
            outcome = self.read('0.outcome.json')
            self.assertEqual(outcome['status'], 'invalid_return')
            self.assertEqual(outcome['error_code'], code)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(json.loads(self.report.read_bytes())['results'], [])
            if code == 'output_too_large':
                self.assertNotIn('engine_answer', outcome)
                self.assertEqual(outcome['engine_answer_bytes'], 250000)

    def test_pre_call_bounds_and_exclusive_claim_do_not_invoke_engine_twice(self):
        from unittest.mock import Mock
        report = ev.load_report(self.report, ev.live_fingerprint(self.suite), ['one'], False, mode=ev.TRACE_MODE)
        journal = attempts.AttemptJournal(self.report, report)
        engine = Mock()
        engine.answer.return_value = '原始答覆'
        for bad in (None, '\ud800', 'x' * 30):
            with patch.object(attempts, 'MAX_TRACE_BYTES', 20), self.assertRaises(ValueError):
                journal.invoke(self.case, bad, engine, None)
        engine.answer.assert_not_called()
        self.assertFalse(journal.paths('one')[0].exists())
        self.assertEqual(journal.invoke(self.case, '實際請求', engine, None), '原始答覆')
        request_before = journal.paths('one')[0].read_bytes()
        with self.assertRaises(ValueError):
            journal.invoke(self.case, '第二次請求', engine, None)
        engine.answer.assert_called_once_with('實際請求', None)
        self.assertEqual(journal.paths('one')[0].read_bytes(), request_before)

    def test_actual_owned_child_failure_survives_engine_temp_cleanup(self):
        # A real local child, no model/API/GPU: this probes the normal engine's
        # process-failure and temp-cleanup path, not model reasoning quality.
        import sys
        engine = CodexEngine(self.root / 'child-data')
        argv = ['evaluate', '--live', '--suite', str(self.suite), '--report', str(self.report)]
        with patch.object(engine, 'command', return_value=[sys.executable, '-c',
                'import sys; sys.stdin.buffer.read(); sys.exit(7)']), \
                patch.object(ev, 'CodexEngine', return_value=engine), patch('sys.argv', argv), \
                patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            ev.main()
        self.assertEqual(self.read('0.outcome.json')['error_code'], 'process_failed')
        self.assertEqual(self.read('0.request.json')['fixture'], self.case)
        self.assertEqual(list(engine.workdir.iterdir()), [])

    def test_nested_attempt_evidence_is_not_publishable_even_outside_default_data(self):
        for name in ('scripts/report.json.attempts/0.request.json',
                     'docs/Other.ATTEMPTS/binding.json', 'tests/run.attempts/0.outcome.json'):
            self.assertFalse(validate_path(name))
        self.assertTrue(validate_path('scripts/evaluation_attempts.py'))


if __name__ == '__main__':
    unittest.main()
