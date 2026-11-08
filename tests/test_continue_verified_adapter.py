import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.learning import continue_verified_adapter as trial


def replies(cases, wrong=()):
    return [dict(id=case['id'], output='未解出' if case['id'] in wrong else case['answer'],
                 tokens=2, ended_with_eos=True) for case in cases]


class ContinuedAdapterTests(unittest.TestCase):
    def setUp(self):
        cases = [dict(id=group, group=group, prompt=group, answer='結果')
                 for group in ('parent_history', 'recent_history', 'fresh', 'fit')]
        old = replies(cases, ('recent_history', 'fresh', 'fit'))
        self.files = {'adapter': 'synthetic-new'}
        self.plan = dict(cases=cases, schedule=[[0, 1, 2, 3]], protected=['parent_history', 'recent_history'],
                         parent_weights='old', parent_files={'adapter': 'synthetic-old'},
                         reproduce={'parent_history': {k: v for k, v in old[0].items() if k != 'id'}})
        shared = dict(environment=trial.EXPECTED_VERSIONS, gpu='synthetic-test-device')
        self.reports = {
            'parent': dict(**shared, rows=old, weights_before='old', weights_after='old', history=[],
                           logits='parent', adapter_files=self.plan['parent_files']),
            'trained': dict(**shared, rows=replies(cases), weights_before='old', weights_after='new',
                            history=[dict(samples=[0, 1, 2, 3], loss=0.1)], logits='child', adapter_files=self.files),
            'reload': dict(**shared, rows=replies(cases), weights_before='new', weights_after='new', history=[],
                           logits='child', adapter_files=self.files),
        }

    def test_fixed_schedule_uses_each_example_and_the_three_to_one_ratio(self):
        order = trial.schedule(63, 21)
        self.assertEqual(order, trial.schedule(63, 21))
        self.assertEqual(len(order), 21)
        self.assertEqual(sorted(index for batch in order for index in batch), list(range(84)))
        self.assertTrue(all(len(batch) == 4 and all(index < 63 for index in batch[:3]) and batch[3] >= 63
                            for batch in order))
        with self.assertRaises(ValueError):
            trial.schedule(62, 21)

    def test_compact_object_fixture_rejects_scalar_duplicate_or_leaked_questions(self):
        data = trial.fixture()
        self.assertEqual(len(data['training']), 21)
        self.assertEqual(len(data['checks']), 12)
        for mutation in ('scalar', 'duplicate_key', 'repeated_prompt'):
            changed = copy.deepcopy(data)
            if mutation == 'repeated_prompt':
                changed['checks'][0]['prompt'] = '\u3000' + changed['training'][0]['prompt']
            else:
                item = next(row for row in changed['training'] if row['kind'] == 'json')
                item['answer'] = 'true' if mutation == 'scalar' else '{"tagged":false,"tagged":true}'
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'fixture.json'
                path.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
                with patch.object(trial, 'FIXTURE', path), self.assertRaises(ValueError):
                    trial.fixture()

    def test_report_requires_protected_recent_skills_and_exact_reload(self):
        result = trial.assess(self.plan, self.reports, self.files)
        self.assertTrue(result['passed'])
        self.assertFalse(result['automatic_activation'])
        self.assertEqual(result['gained'], ['recent_history', 'fresh'])
        changed = copy.deepcopy(self.reports)
        changed['trained']['rows'][1]['output'] = changed['reload']['rows'][1]['output'] = '錯誤'
        result = trial.assess(self.plan, changed, self.files)
        self.assertFalse(result['passed'])
        self.assertTrue(result['checks']['reload_matches'])
        self.assertEqual(result['regressed'], ['recent_history'])
        changed['reload']['rows'][2]['output'] = '錯誤'
        self.assertFalse(trial.assess(self.plan, changed, self.files)['checks']['reload_matches'])
        changed['trained']['history'][0]['samples'] = [0, 1, 1, 3]
        with self.assertRaisesRegex(ValueError, '取樣'):
            trial.assess(self.plan, changed, self.files)

    def test_owned_phases_are_read_back_once_and_forged_exit_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'trial'
            folder.mkdir()
            admitted = {'ticks_ms': 900000, 'last_input_tick': 100000, 'idle_ms': 800000}

            def fake_worker(command, path, phase, check_input, check_sources):
                check_input()
                check_sources()
                record = {**self.reports[phase], 'phase': phase, 'run_sha256': command[-1], 'pid': 45}
                trial.save(path / (phase + '.json'), record)
                trial.save(path / (phase + '.exit.json'), dict(worker=45, exit_confirmed=True, returncode=0))
                (path / (phase + '.started')).write_text('45', encoding='ascii')

            with (patch.object(trial, 'PARENT', root), patch.object(trial, 'checked_plan', return_value=self.plan),
                  patch.object(trial, 'frozen', return_value=self.plan),
                  patch.object(trial.short.segments.idle, 'idle_sample', return_value=admitted),
                  patch.object(trial.batches, 'adapter_files', return_value=self.files),
                  patch.object(trial.gpu, 'run_owned', side_effect=fake_worker) as launch):
                self.assertTrue(trial.collect('trial')['passed'])
                self.assertTrue(trial.verify('trial')['passed'])
                with self.assertRaisesRegex(ValueError, '已嘗試'):
                    trial.collect('trial')
                self.assertEqual(launch.call_count, 3)
                trial.save(folder / 'reload.exit.json', dict(worker=44, exit_confirmed=True, returncode=0))
                with self.assertRaisesRegex(ValueError, '退出'):
                    trial.verify('trial')
