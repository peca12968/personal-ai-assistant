import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.learning import assess_base_checkpoint as assay
from scripts.learning import decision_code_study
from scripts.learning import prepare_epistemic
from scripts.learning import review_base_checkpoint as report


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

    def test_report_distinguishes_format_state_and_unfinished_output(self):
        for expected, actual, eos, outcome in (
            ('未知', '未知', True, 'exact'),
            ('未知', '「未知」。', True, 'literal_format'),
            ('{"ok":true}', '{"ok": true}', True, 'literal_format'),
            ('{"ok":true}', '{"ok":1}', True, 'other_output'),
            ('{"ok":true}', '{"ok":false,"ok":true}', True, 'other_output'),
            ('未知', '已確認成立', True, 'different_state'),
            ('S', '是', True, 'other_output'),
            ('未知', '未知', False, 'unfinished'),
        ):
            with self.subTest(expected=expected, actual=actual, eos=eos):
                self.assertEqual(report.outcome(expected, {'output': actual, 'ended_with_eos': eos}), outcome)

    def test_decision_code_review_uses_the_current_mapping_and_keeps_exact_scores_separate(self):
        scenario = dict(id='fiction', rule='required_marker', action='領取樣本',
                        conditions=['瓶上有本期標記'], codes=['A2', '甲', '9'])
        for heldout in (True, False):
            rendered = decision_code_study.compile_group(scenario, heldout)
            cases = [{**case, 'group': 'heldout'} for case in rendered]
            before = raw_rows(cases)
            before[0]['output'] = '「A2」。'
            before[1]['output'] = 'A2'
            before[2]['output'] = '答案是甲'
            before[3]['ended_with_eos'] = False
            phases = {'baseline': {'rows': before}, 'reload': {'rows': raw_rows(cases)}}
            observed = report.code_diagnostics(cases, phases)
            by_verdict = observed['by_expected_verdict']['heldout']
            self.assertEqual(by_verdict['baseline']['supported'],
                             {'correct_verdict_format_difference': 1, 'wrong_declared_verdict': 1})
            self.assertEqual(by_verdict['baseline']['refuted'], {'unrecognized_output': 1, 'unfinished': 1})
            self.assertEqual(by_verdict['reload']['supported'], {'exact': 2})
            pairs = observed['same_evidence_remapped']['heldout']
            pair = next(item for item in pairs if item['case_ids'] == ['fiction_1', 'fiction_2'])
            self.assertEqual(pair['baseline']['decoded'], ['supported', 'undetermined'])
            self.assertFalse(pair['baseline']['both_correct'])
            self.assertTrue(pair['reload']['both_exact'])
            self.assertTrue(pair['reload']['both_correct'])
            self.assertFalse(assay.grades(cases, before)['fiction_1'])

    def test_code_diagnostics_decline_ambiguous_declarations_and_wrappers(self):
        instruction = '確定可行回「A」，確定不可行回「B」，現有資料不足回「C」。只輸出指定代碼。'
        self.assertIsNone(report.declared_codes('引文：' + instruction + '結束。'))
        self.assertIsNone(report.declared_codes(instruction + '其他內容。' + instruction))
        self.assertIsNone(report.declared_codes(instruction.replace('「B」', '「A」') + '其他內容。'))
        self.assertIsNone(report.code_reply({'ended_with_eos': True, 'output': '「A』'}, {'A': 'supported'}))

    def test_observation_study_declarations_decode_their_distinct_output_styles(self):
        _, data = prepare_epistemic.load_study('observation-language')
        for row in [*data['training'][48:], *data['heldout'][:6]]:
            with self.subTest(case=row['id']):
                code_map, evidence = report.declared_codes(row['prompt'])
                self.assertEqual(code_map, {literal: state for state, literal in row['code_map'].items()})
                self.assertEqual(code_map[row['answer']], row['verdict'])
                self.assertIn('現在能否', evidence)

    def test_retention_compares_earlier_candidate_across_renumbered_splits(self):
        previous = [dict(id='old', prompt='原保留題', group='heldout', answer='R')]
        current = [dict(id='new', prompt='原保留題', group='transfer', answer='R')]
        old = (previous, {'baseline': {'rows': raw_rows(previous, ('old',))}},
               {'scores': {'reload': {'old': True}}})
        new = (current, {'baseline': {'rows': raw_rows(current, ('new',))}},
               {'scores': {'reload': {'new': False}}})
        result = report.retention(old, new)
        self.assertFalse(result['all_prior_correct_retained'])
        self.assertEqual(result['groups']['heldout'], {'count': 1, 'prior': 1, 'current': 0})
        self.assertEqual(result['regressed'], [{'previous_id': 'old', 'current_id': 'new', 'group': 'heldout'}])
        changed = copy.deepcopy(new)
        changed[1]['baseline']['rows'][0]['tokens'] += 1
        with self.assertRaisesRegex(ValueError, '原始底座回覆不一致'):
            report.retention(old, changed)
        current[0]['group'] = 'new'
        with self.assertRaisesRegex(ValueError, '缺少'):
            report.retention(old, new)

    def test_report_recomputes_rejected_result_without_writing_or_accepting_forged_exit(self):
        protocol = {'fixed': 'study'}
        admitted = {'ticks_ms': 800000, 'last_input_tick': 100000, 'idle_ms': 700000}
        folder = self.root / 'study'
        folder.mkdir()
        run = dict(protocol_sha256=assay.digest(protocol), receipt='a' * 64, admitted=admitted)
        assay.save(folder / 'run.json', run)
        self.reports['baseline']['rows'] = raw_rows(self.cases)
        for phase in ('restored', 'reload'):
            self.reports[phase]['rows'] = raw_rows(self.cases, ('heldout-0',))
        self.record['rows'] = self.reports['restored']['rows'][:-6]
        report_hashes, exit_hashes = {}, {}
        for phase in assay.PHASES:
            path = folder / (phase + '.json')
            assay.save(path, {**self.reports[phase], 'phase': phase, 'run_sha256': assay.digest(run), 'pid': 18})
            report_hashes[phase] = assay.batches.file_hash(path)
            exit_path = folder / (phase + '.exit.json')
            assay.save(exit_path, dict(worker=18, exit_confirmed=True, returncode=0))
            exit_hashes[phase] = assay.batches.file_hash(exit_path)
            (folder / (phase + '.started')).write_text('18', encoding='ascii')
        checked = assay.assess(self.cases, self.reports, self.record, self.weights)
        assay.save(folder / 'review.json', {**checked, 'run_sha256': assay.digest(run),
                                            'report_sha256': report_hashes, 'exit_sha256': exit_hashes})
        loaded = (protocol, self.root, {'idle_ms': 600000}, self.manifest, self.cases,
                  {'blob': 'synthetic-blob'}, self.record)
        before = {path.name: path.read_bytes() for path in folder.iterdir()}
        with (patch.object(assay, 'PARENT', self.root), patch.object(assay, 'inputs', return_value=loaded),
              patch.object(assay.segments, 'pin_blob') as pin,
              patch.object(assay.batches, 'adapter_files', return_value=self.weights)):
            result = report.review('study')
            self.assertFalse(result['passed'])
            self.assertNotIn('decision_codes', result)
            self.assertEqual(result['groups']['heldout']['regressed_ids'], ['heldout-0'])
            self.assertIn('baseline_correct_cases_retained', result['failed_checks'])
            self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, before)
            pin.assert_called_once_with(self.root, 'synthetic-blob')
            assay.save(folder / 'reload.exit.json', dict(worker=18, exit_confirmed=False, returncode=0))
            with self.assertRaisesRegex(ValueError, '退出'):
                report.review('study')
