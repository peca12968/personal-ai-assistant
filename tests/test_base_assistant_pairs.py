from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.learning import assess_base_assistant as assay
from scripts.learning import assess_base_assistant_pairs as pairs


IDLE = {'idle_ms': 610000, 'ticks_ms': 615000, 'last_input_tick': 5000}


class BaseAssistantPairTests(unittest.TestCase):
    def setUp(self):
        parent = self.enterContext(tempfile.TemporaryDirectory())
        self.parent = Path(parent)
        self.master = self.parent / 'study'
        base = assay.local_models.select()
        candidate = dict(schema=assay.CANDIDATE, base=base, session='a' * 32, fingerprint='b' * 64)
        self.state = SimpleNamespace(selections={'baseline': base, 'candidate': candidate}, series=self.parent,
                                     head={'blob': 'c' * 64}, idle_ms=600000, old_prompts=set())
        source = Path(assay.__file__)
        self.enterContext(patch.object(assay, 'PARENT', self.parent))
        self.enterContext(patch.object(assay, 'evidence', return_value=self.state))
        self.enterContext(patch.object(assay, 'sources', return_value={
            source.relative_to(assay.ROOT).as_posix(): assay.batches.file_hash(source)}))
        self.enterContext(patch.object(assay.segments, 'pin_blob'))
        self.idle = self.enterContext(patch.object(assay.segments.idle, 'idle_sample', return_value=IDLE))
        self.enterContext(patch.object(assay.gpu, 'input_gate'))
        self.child = self.enterContext(patch.object(assay.gpu, 'run_owned', side_effect=self.fake_child))
        self.suite = dict(schema='full_assistant_cases_v1', cases=[
            dict(id='a', group='target', question='還不知道是否到貨，請判斷。', answer='未知',
                 history=[{'role': 'user', 'content': '昨天還在等消息。'}]),
            dict(id='b', group='control', question='只輸出 5。', answer='5'),
            dict(id='c', group='control', question='只輸出 9。', answer='9')])
        self.meta = assay.prepare('study', 'short-assay', self.suite)
        counts = {cid: 100 for cid in self.meta['sessions']}
        assay.save(self.master / 'preflight.json', dict(manifest_sha256=assay.digest(self.meta),
                   input_tokens={phase: counts for phase in assay.PHASES}))
        self.cli('prepare')
        self.plan = assay.read(self.master / 'pair-study.json')

    def cli(self, action):
        with patch.object(sys, 'argv', ['assess_base_assistant_pairs', action, 'study']), redirect_stdout(StringIO()):
            pairs.main()

    def fake_child(self, command, output, phase, input_check, source_check):
        self.assertEqual(phase, 'inference')
        side, cid = command[-3:-1]
        source_check()
        answer = next(case['answer'] for case in self.suite['cases'] if case['id'] == cid)
        if side == 'baseline' and cid == 'a':
            answer = '已知'
        (output / 'inference.started').write_text('1001', encoding='ascii')
        assay.save(output / 'inference.exit.json', dict(worker=1001, launcher=1000, exit_confirmed=True, returncode=0))
        assay.save(output / 'result.json', dict(schema='local_inference_result_v1', status='succeeded', answer=answer,
                   selection_fingerprint=self.meta['selections'][side]['fingerprint'], input_tokens=100))

    def test_separate_idle_runs_collect_next_case_and_leave_master_unchanged(self):
        self.cli('run')
        first = pairs.pair_folder(self.plan, self.suite['cases'][0])
        first_receipt = (first / 'pair-review.json').read_bytes()
        self.assertEqual(pairs.report(self.meta, self.plan)['groups']['target']['exact'],
                         {'baseline': 0, 'candidate': 1})
        self.idle.return_value = {'idle_ms': 620000, 'ticks_ms': 630000, 'last_input_tick': 10000}
        self.cli('run')
        report = pairs.report(self.meta, self.plan)
        self.assertEqual(report['completed_pairs'], 2)
        self.assertEqual(report['exact'], {'baseline': 1, 'candidate': 2})
        self.assertEqual(report['cases']['c']['status'], 'pending')
        self.assertEqual(assay.read(first / 'run.json')['admitted'], IDLE)
        second = pairs.pair_folder(self.plan, self.suite['cases'][1])
        self.assertEqual(assay.read(second / 'run.json')['admitted'], self.idle.return_value)
        self.assertEqual((first / 'pair-review.json').read_bytes(), first_receipt)
        self.assertFalse((self.master / 'run.json').exists())
        self.assertFalse((first / 'review.json').exists())
        self.assertEqual(pairs.master(self.master)[0], self.meta)
        self.assertEqual(self.child.call_count, 4)

    def test_recent_input_and_changed_preflight_refuse_to_start(self):
        self.idle.return_value = {'idle_ms': 15}
        with self.assertRaisesRegex(ValueError, '閒置'):
            self.cli('run')
        first = pairs.pair_folder(self.plan, self.suite['cases'][0])
        self.assertFalse(first.exists())
        path = self.master / 'preflight.json'
        value = assay.read(path)
        value['input_tokens']['candidate']['a'] += 1
        assay.save(path, value)
        with self.assertRaisesRegex(ValueError, '規約'):
            self.cli('run')
        self.child.assert_not_called()

    def test_interrupted_pair_retains_partial_answer_and_is_not_repeated(self):
        def fail_candidate(command, *args):
            if command[-3] == 'candidate':
                raise assay.batches.Stopped('input_resumed')
            self.fake_child(command, *args)

        self.child.side_effect = fail_candidate
        with self.assertRaises(assay.batches.Stopped):
            self.cli('run')
        first = pairs.pair_folder(self.plan, self.suite['cases'][0])
        rows = assay.read(first / 'responses.json')
        self.assertEqual([row['status'] for row in rows], ['complete', 'failed'])
        report = pairs.report(self.meta, self.plan)
        self.assertEqual(report['cases']['a']['status'], 'failed')
        self.assertEqual(report['completed_pairs'], 0)
        self.child.side_effect = self.fake_child
        self.cli('run')
        self.assertEqual(pairs.report(self.meta, self.plan)['completed_pairs'], 1)
        self.assertEqual(assay.read(first / 'responses.json'), rows)

    def test_report_recomputes_exit_and_request_evidence(self):
        self.cli('run')
        first = pairs.pair_folder(self.plan, self.suite['cases'][0])
        path = first / 'candidate/a/worker/inference.exit.json'
        receipt = assay.read(path)
        assay.save(path, {**receipt, 'exit_confirmed': False})
        with self.assertRaisesRegex(ValueError, '程序'):
            self.cli('report')
