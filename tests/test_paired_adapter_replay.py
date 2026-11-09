import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.learning import paired_adapter_replay as replay


class PairedReplayTests(unittest.TestCase):
    def setUp(self):
        self.cases = [dict(id=cid, group=group, answer='正') for cid, group in (
            ('retained', 'parent_history'), ('new', 'new_holdout'),
            ('past', 'recent_history'), ('fit', 'new_fit'))]
        self.plan = dict(cases=self.cases, schedule=[[i] for i in range(6)],
                         protected=['retained', 'past'], parent_weights='parent')
        self.parent = {'gpu': 'fixture'}
        self.files = {'adapter_model.safetensors': 'pinned'}

    def reports(self, phase, previous, wrong=()):
        rows = [dict(id=c['id'], output='錯' if c['id'] in wrong else '正',
                     tokens=2, ended_with_eos=True) for c in self.cases]
        trained = dict(rows=rows, weights_before=previous, weights_after=f'changed-{phase}',
                       adapter_files=self.files, logits='logits', gpu=self.parent['gpu'],
                       environment=replay.prior.EXPECTED_VERSIONS,
                       history=[dict(samples=samples, loss=0.5)
                                for samples in replay.stage_schedule(self.plan, f'train-{phase}')])
        reloaded = copy.deepcopy(trained)
        reloaded.update(weights_before=trained['weights_after'], history=[])
        return trained, reloaded

    def test_fixture_separates_matched_array_cardinalities_and_holdouts(self):
        fixtures = replay.fixture()
        all_prompts = []
        for groups in fixtures.values():
            self.assertEqual([len(rows) for rows in groups], [3] * 6)
            for rows in (groups[2], groups[5]):
                outputs = [next(iter(json.loads(row['answer']).values())) for row in rows]
                self.assertEqual(outputs[:2], [[], ['']])
                self.assertEqual(len(outputs[2]), 1)
                self.assertTrue(outputs[2][0])
            all_prompts.extend(replay.prior.normalized(row['prompt']) for rows in groups for row in rows)
        self.assertEqual(len(set(all_prompts)), 36)

    def test_equal_total_and_gain_cannot_compensate_for_any_lost_case(self):
        before = dict(retained=True, new=False, past=False, fit=True)
        trained, reloaded = self.reports(1, 'parent', ('retained', 'past'))
        result, after = replay.stage_result(self.plan, trained, reloaded, before, before,
                                            self.parent, self.files, 1, 'parent')
        self.assertEqual(sum(before.values()), sum(after.values()))
        self.assertEqual(result['lost'], ['retained'])
        self.assertEqual(result['gained_holdout'], ['new'])
        self.assertFalse(result['advance'])
        self.assertFalse(result['passed'])

    def test_stage_two_protects_new_gains_and_verifies_its_own_sample_order(self):
        before = dict(retained=True, new=False, past=False, fit=True)
        previous = dict(retained=True, new=True, past=True, fit=True)
        trained, reloaded = self.reports(2, 'changed-1', ('new', 'fit'))
        result, _ = replay.stage_result(self.plan, trained, reloaded, before, previous,
                                        self.parent, self.files, 2, 'changed-1')
        self.assertEqual(result['lost'], ['new'])
        self.assertFalse(result['advance'])
        trained['history'][0]['samples'] = [0]
        with self.assertRaisesRegex(ValueError, '取樣'):
            replay.stage_result(self.plan, trained, reloaded, before, previous, self.parent, self.files, 2, 'changed-1')
        trained['history'][0]['samples'] = self.plan['schedule'][3]
        reloaded['logits'] = 'changed'
        with self.assertRaisesRegex(ValueError, '重載'):
            replay.stage_result(self.plan, trained, reloaded, before, previous, self.parent, self.files, 2, 'changed-1')

    def test_driver_never_launches_second_stage_after_retention_loss(self):
        admitted = {'ticks_ms': 900000, 'last_input_tick': 100000, 'idle_ms': 800000}
        run = dict(protocol_sha256=replay.digest(self.plan), admitted=admitted)
        failed = dict(advance=False, weights='changed-1', passed=False)
        before = dict(retained=True, new=False, past=False, fit=True)
        with tempfile.TemporaryDirectory() as path:
            folder = Path(path)
            launched = []
            with (patch.object(replay.prior, 'phase_record', return_value=(self.parent, 'parent', 'exit')),
                  patch.object(replay, 'parent_scores', return_value=before),
                  patch.object(replay, 'stage_evidence', return_value=(failed, {})) as evidence):
                result = replay.result(folder, self.plan, run, launched.append)
                self.assertEqual(launched, ['parent', 'train-1', 'reload-1'])
                evidence.assert_called_once()
                self.assertFalse(result['complete_schedule'])
                self.assertFalse(result['passed'])
                self.assertFalse(result['automatic_activation'])
                (folder / 'train-2.started').touch()
                with self.assertRaisesRegex(ValueError, '仍啟動第二段'):
                    replay.result(folder, self.plan, run)

    def test_driver_continues_from_verified_weights_and_passes_only_after_second_stage(self):
        admitted = {'ticks_ms': 900000, 'last_input_tick': 100000, 'idle_ms': 800000}
        run = dict(protocol_sha256=replay.digest(self.plan), admitted=admitted)
        before = dict(retained=True, new=False, past=False, fit=True)
        gained = dict(before, new=True)
        first = dict(advance=True, weights='changed-1', passed=False)
        second = dict(advance=True, weights='changed-2', passed=True)
        with tempfile.TemporaryDirectory() as path:
            folder = Path(path)
            launched = []
            with (patch.object(replay.prior, 'phase_record', return_value=(self.parent, 'parent', 'exit')),
                  patch.object(replay, 'parent_scores', return_value=before),
                  patch.object(replay, 'stage_evidence', side_effect=((first, gained), (second, gained))) as evidence):
                result = replay.result(folder, self.plan, run, launched.append)
        self.assertEqual(launched, list(replay.PHASES))
        self.assertEqual(evidence.call_args_list[1].args[-3:], (gained, 2, 'changed-1'))
        self.assertTrue(result['complete_schedule'])
        self.assertTrue(result['passed'])
        self.assertFalse(result['automatic_activation'])
