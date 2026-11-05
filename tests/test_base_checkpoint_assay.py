import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.learning import assess_base_checkpoint as assay


def raw_rows(cases, wrong=()):
    return [dict(id=case['id'], output='其他答案' if case['id'] in wrong else case['answer'],
                 tokens=2, ended_with_eos=True) for case in cases]


class BaseCheckpointAssayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = {'items': [{'id': 'item1', 'prompt': '合成訓練題', 'answer': '已確認成立'}],
                         'new_ids': ['item1'], 'checks': [{'prompt': '合成保留題', 'answer': '未知'}]}
        self.transfer = assay.read(assay.TRANSFER)
        self.cases = assay.cases_for(self.manifest, self.transfer)
        self.weights = {'adapter_model.safetensors': 'weight-hash'}
        self.record = dict(weights='trained', logits_sha256='trained-logits',
                           binding={'initial_adapter_sha256': 'initial'},
                           rows=raw_rows([case for case in self.cases if case['group'] != 'transfer']))
        self.reports = {
            'baseline': dict(rows=raw_rows(self.cases, ('heldout-0',))),
            'restored': dict(rows=raw_rows(self.cases), weights='trained', logits='trained-logits',
                             adapter_files=self.weights),
            'reload': dict(rows=raw_rows(self.cases), weights='trained', logits='trained-logits',
                           adapter_files=self.weights),
        }

    def test_transfer_questions_cannot_duplicate_training_or_each_other(self):
        self.assertEqual(len([case for case in self.cases if case['group'] == 'transfer']), 6)
        for prompt in ('合成訓練題', '  合成訓練題  ', '合成保留題', self.transfer['cases'][1]['prompt']):
            suite = copy.deepcopy(self.transfer)
            suite['cases'][0]['prompt'] = prompt
            with self.subTest(prompt=prompt), self.assertRaisesRegex(ValueError, '重複'):
                assay.cases_for(self.manifest, suite)

    def test_compare_separates_retention_reload_and_heldout_or_transfer_gains(self):
        clean = assay.assess(self.cases, self.reports, self.record, self.weights)
        self.assertTrue(clean['passed'])
        self.assertFalse(clean['automatic_activation'])
        self.assertEqual(clean['groups']['transfer'], {'baseline': 6, 'reload': 6, 'count': 6})
        reports = copy.deepcopy(self.reports)
        reports['reload']['rows'][-1]['output'] = '錯誤'
        result = assay.assess(self.cases, reports, self.record, self.weights)
        self.assertFalse(result['checks']['baseline_correct_cases_retained'])
        self.assertFalse(result['checks']['reload_answers_match'])
        self.assertFalse(result['passed'])
        reports = copy.deepcopy(self.reports)
        reports['baseline']['rows'] = raw_rows(self.cases, ('example-item1',))
        result = assay.assess(self.cases, reports, self.record, self.weights)
        self.assertFalse(result['checks']['heldout_or_transfer_improved'])
        self.assertFalse(result['passed'])

    def test_exact_label_requires_eos_and_rejects_forged_answer_order(self):
        rows = raw_rows(self.cases)
        rows[-1]['ended_with_eos'] = False
        self.assertFalse(assay.grades(self.cases, rows)[self.cases[-1]['id']])
        rows[0], rows[1] = rows[1], rows[0]
        with self.assertRaises(ValueError):
            assay.grades(self.cases, rows)

    def test_changed_protocol_is_rejected_before_series_or_gpu_access(self):
        folder = self.root / 'study'
        folder.mkdir()
        protocol = {'session': 'old'}
        assay.save(folder / 'protocol.json', {'session': 'new'})
        (folder / 'protocol.sha256').write_text(assay.digest(protocol), encoding='ascii')
        with (patch.object(assay.segments, 'stored_plan') as plan,
              self.assertRaisesRegex(ValueError, '規約指紋')):
            assay.inputs(folder, '0' * 64)
        plan.assert_not_called()

    def test_recent_input_does_not_create_run_or_launch_a_worker(self):
        folder = self.root / 'study'
        folder.mkdir()
        loaded = ({}, self.root, {'idle_ms': 600000}, self.manifest, self.cases,
                  {'blob': 'synthetic-blob'}, self.record)
        with (patch.object(assay, 'PARENT', self.root), patch.object(assay, 'inputs', return_value=loaded),
              patch.object(assay.segments.idle, 'idle_sample', return_value={'idle_ms': 12345}),
              patch.object(assay.gpu, 'run_owned') as launch,
              self.assertRaisesRegex(ValueError, '閒置門檻')):
            assay.collect('study', 'a' * 64)
        launch.assert_not_called()
        self.assertFalse((folder / 'run.json').exists())

    def test_owned_phase_reports_bind_confirmed_worker_exits_and_never_rerun(self):
        protocol = {'fixed': 'study'}
        loaded = (protocol, self.root, {'idle_ms': 600000}, self.manifest, self.cases,
                  {'blob': 'synthetic-blob'}, self.record)
        admitted = {'ticks_ms': 800000, 'last_input_tick': 100000, 'idle_ms': 700000}
        folder = self.root / 'study'
        folder.mkdir()

        def run_owned(command, path, phase, check_input, check_sources):
            check_input()
            check_sources()
            report = {**self.reports[phase], 'phase': phase, 'run_sha256': command[-1], 'pid': 18}
            assay.save(path / (phase + '.json'), report)
            assay.save(path / (phase + '.exit.json'), dict(worker=18, exit_confirmed=True, returncode=0))

        with (patch.object(assay, 'PARENT', self.root), patch.object(assay, 'inputs', return_value=loaded),
              patch.object(assay.segments, 'pin_blob'), patch.object(assay.segments.idle, 'idle_sample', return_value=admitted),
              patch.object(assay.batches, 'adapter_files', return_value=self.weights),
              patch.object(assay.gpu, 'run_owned', side_effect=run_owned) as launch):
            result = assay.collect('study', 'a' * 64)
            self.assertTrue(result['passed'])
            self.assertEqual(set(result['exit_sha256']), set(assay.PHASES))
            with self.assertRaisesRegex(ValueError, '已嘗試'):
                assay.collect('study', 'a' * 64)
            self.assertEqual(launch.call_count, 3)
