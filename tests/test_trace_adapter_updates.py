import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.learning import trace_adapter_updates as trace


def rows(cases, wrong=()):
    return [dict(id=case['id'], output='錯' if case['id'] in wrong else case['answer'],
                 tokens=2, ended_with_eos=True) for case in cases]


class AdapterTraceTests(unittest.TestCase):
    def setUp(self):
        self.cases = [dict(id=cid, group=group, answer='正') for cid, group in (
            ('known', 'parent_history'), ('missing', 'recent_history'), ('fresh', 'fresh'), ('trained', 'fit'),
        )]
        self.plan = dict(cases=self.cases, protected=['known', 'missing'], parent_weights='old')

    def test_first_observed_losses_exclude_existing_misses_and_track_recovery(self):
        reports = {21: {'rows': rows(self.cases, ('missing',))},
                   0: {'rows': rows(self.cases, ('missing', 'fresh', 'trained'))},
                   14: {'rows': rows(self.cases, ('missing',))},
                   7: {'rows': rows(self.cases, ('known', 'missing', 'fresh'))}}
        result = trace.summarize(self.plan, reports)
        self.assertEqual(result['first_lost'], {'known': 7})
        self.assertEqual(result['first_gained'], {'fresh': 14})
        self.assertEqual(result['points']['0']['missing_protected'], ['missing'])
        self.assertEqual(result['points']['7']['missing_protected'], ['known', 'missing'])
        self.assertEqual(result['points']['14']['missing_protected'], ['missing'])
        self.assertFalse(result['automatic_activation'])

    def test_snapshot_replay_requires_original_history_final_weights_and_current_files(self):
        snaps = [dict(step=step, weights=f'w-{step}', files={'adapter': f'f-{step}'}) for step in trace.POINTS]
        original = {'history': [{'samples': [0], 'loss': 0.2}], 'weights_after': 'w-21'}
        report = {'weights_before': 'old', 'history': copy.deepcopy(original['history']), 'snapshots': snaps}
        with (patch.object(trace.trial, 'phase_record', return_value=(report, 'raw', 'exit')),
              patch.object(trace.batches, 'adapter_files', side_effect=lambda folder: {'adapter': folder.name.replace('adapter', 'f')})):
            self.assertEqual(trace.snapshots_for(Path('.'), 'run', self.plan, original), snaps)
            report['history'][0]['loss'] = 0.3
            with self.assertRaisesRegex(ValueError, '未重現'):
                trace.snapshots_for(Path('.'), 'run', self.plan, original)
            report['history'][0]['loss'] = 0.2
            snaps[0]['files'] = {'adapter': 'changed'}
            with self.assertRaisesRegex(ValueError, '已變更'):
                trace.snapshots_for(Path('.'), 'run', self.plan, original)

    def test_final_original_logits_and_owned_exits_bind_the_recomputed_review(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            protocol = dict(schema=trace.SCHEMA, reference='older')
            run = dict(protocol_sha256=trace.digest(protocol), admitted={
                'ticks_ms': 900000, 'last_input_tick': 100000, 'idle_ms': 800000})
            parent = dict(environment={'test': 'pinned'}, gpu='test', rows=rows(self.cases, ('missing',)))
            original = dict(rows=rows(self.cases), logits='expected', history=[])
            snaps = [dict(step=step, weights=f'w-{step}', files={}) for step in trace.POINTS]
            for phase in trace.PHASES:
                report = {**parent, 'phase': phase, 'run_sha256': trace.digest(run), 'pid': 12}
                if phase == 'updates':
                    report.update(weights_before='old')
                else:
                    report.update(**original, weights_before='w-' + phase.removeprefix('step-'))
                trace.save(folder / (phase + '.json'), report)
                trace.save(folder / (phase + '.exit.json'), dict(worker=12, exit_confirmed=True, returncode=0))
                (folder / (phase + '.started')).write_text('12', encoding='ascii')
            inputs = protocol, self.plan, parent, original
            with patch.object(trace, 'snapshots_for', return_value=snaps):
                result = trace.recompute(folder, inputs, run)
                self.assertTrue(result['final_reproduced'])
                self.assertEqual(set(result['exit_sha256']), set(trace.PHASES))
                saved = trace.read(folder / 'step-21.json')
                saved['logits'] = 'mismatch'
                trace.save(folder / 'step-21.json', saved)
                with self.assertRaisesRegex(ValueError, '原答及 logits'):
                    trace.recompute(folder, inputs, run)
                trace.save(folder / 'step-14.exit.json', dict(worker=13, exit_confirmed=True, returncode=0))
                with self.assertRaisesRegex(ValueError, '退出'):
                    trace.recompute(folder, inputs, run)
