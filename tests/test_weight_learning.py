import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.learning import experiment, protocol, fetch_model
from scripts.learning.runtime import gpu_lease


class WeightLearningTests(unittest.TestCase):
    def test_frozen_split_and_balanced_replay(self):
        suite = protocol.make_suite()
        a, b = (protocol.training_rows(suite, phase) for phase in ('stage-a', 'stage-b'))
        self.assertEqual((len(a), len(b), len(suite['evaluation'])), (80, 112, 40))
        self.assertEqual([r for r in b if r['group'] in ('a', 'anchor')], a)
        self.assertEqual(sum(r['group'] == 'b' for r in b), 32)
        self.assertEqual(sum(r['group'] == 'anchor' for r in a), 48)
        self.assertFalse({r['prompt'] for r in b} & {r['prompt'] for r in suite['evaluation']})
        a[0]['answer'] = 'changed'
        self.assertNotEqual(suite['training'][0]['answer'], 'changed')

    def reports(self):
        reports = {}
        for phase in protocol.PHASES:
            rows = []
            for case in protocol.make_suite()['evaluation']:
                correct = case['group'] == 'guard' or phase in ('stage-b', 'reload-b') or (phase == 'stage-a' and case['group'] == 'a')
                rows.append({**case, 'output': case['answer'] if correct else 'unknown', 'correct': correct})
            reports[phase] = {'status': 'complete', 'evaluation': rows,
                              'weights_before': 'a' if phase == 'stage-b' else 'initial',
                              'weights_after': 'a' if phase == 'stage-a' else 'b'}
        return reports

    def test_acceptance_requires_learning_retention_and_saved_weights(self):
        reports = self.reports()
        self.assertTrue(protocol.assess(reports)['passed'])
        for group in ('a', 'b', 'guard'):
            changed = copy.deepcopy(reports)
            row = next(r for r in changed['stage-b']['evaluation'] if r['group'] == group)
            row.update(output='unknown', correct=False)
            self.assertFalse(protocol.assess(changed)['passed'])
        for phase, key in (('stage-b', 'weights_before'), ('reload-b', 'weights_after')):
            changed = copy.deepcopy(reports); changed[phase][key] = 'different'
            self.assertFalse(protocol.assess(changed)['passed'])
        # A transient regression must fail even when the final B stage recovers.
        changed = copy.deepcopy(reports)
        row = next(r for r in changed['stage-a']['evaluation'] if r['group'] == 'guard')
        row.update(output='unknown', correct=False)
        self.assertFalse(protocol.assess(changed)['checks']['stage_a_baseline_guards_retained'])
        reports['stage-a']['weights_before'] = reports['stage-a']['weights_after']
        self.assertFalse(protocol.assess(reports)['passed'])

    def test_reject_fabricated_grade_missing_rows_and_unfinished_phase(self):
        for mutate in (
            lambda r: r['baseline']['evaluation'][0].update(correct=True),
            lambda r: r['stage-a']['evaluation'].pop(),
            lambda r: r['stage-b'].update(status='failed'),
            lambda r: r['reload-b']['evaluation'][0].update(answer='altered'),
        ):
            reports = self.reports(); mutate(reports)
            with self.assertRaises(ValueError):
                protocol.assess(reports)

    def test_no_overwrite_and_changed_source_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(experiment, 'ROOT', Path(directory)):
            experiment.initialize('test')
            folder = experiment.local_folder('test')
            experiment.load_frozen(folder)
            with self.assertRaises(FileExistsError):
                experiment.initialize('test')
            suite = json.loads((folder/'suite.json').read_text(encoding='utf-8'))
            suite['training'][0]['answer'] = 'altered'
            (folder/'suite.json').write_text(json.dumps(suite), encoding='utf-8')
            with self.assertRaises(ValueError):
                experiment.load_frozen(folder)
            for name in ('../escape', 'C:\\outside', '', None):
                with self.assertRaises(ValueError):
                    experiment.local_folder(name)

    def test_default_command_offline_without_optional_dependencies(self):
        result = subprocess.run([sys.executable, str(protocol.HERE/'experiment.py')],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('no ML imports', result.stdout)

    def test_training_masks_prompt_and_rejects_truncation(self):
        class Tokenizer:
            eos_token = '<eos>'
            def apply_chat_template(self, *args, **kwargs):
                self.kwargs = kwargs
                return [10, 20, 30]
            def encode(self, text, **kwargs):
                return [40, 50]
        tokenizer = Tokenizer()
        ids, labels = experiment.encode_row(tokenizer, {'prompt': 'question', 'answer': 'answer'})
        self.assertEqual(ids, [10, 20, 30, 40, 50])
        self.assertEqual(labels, [-100, -100, -100, 40, 50])
        self.assertFalse(tokenizer.kwargs['enable_thinking'])
        with patch.dict(protocol.SETTINGS, max_sequence_tokens=4):
            with self.assertRaises(ValueError):
                experiment.encode_row(tokenizer, {'prompt': 'question', 'answer': 'answer'})

    def test_pinned_asset_hash_and_manifest_validation(self):
        manifest = json.loads(fetch_model.MANIFEST.read_text(encoding='utf-8'))
        fetch_model.validate_manifest(manifest)
        for value in (None, 42, 'bad'):
            changed = copy.deepcopy(manifest)
            item = changed['files'][0]
            item['git_blob_sha1'] = value
            with self.assertRaises(ValueError):
                fetch_model.validate_manifest(changed)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'asset'; path.write_bytes(b'example')
            for algorithm, content in (('sha256', b'example'), ('git_blob_sha1', b'blob 7\0example')):
                hash_function = hashlib.sha256 if algorithm == 'sha256' else hashlib.sha1
                item = {'name': 'asset', 'bytes': 7, algorithm: hash_function(content).hexdigest()}
                self.assertTrue(fetch_model.verify(path, item))
                path.write_bytes(b'changed')
                self.assertFalse(fetch_model.verify(path, item))
                path.write_bytes(b'example')

    def test_process_lease_excludes_other_process_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'gpu.lock'
            code = 'from scripts.learning.runtime import gpu_lease; import sys\nwith gpu_lease(sys.argv[1]): print("acquired")'
            with gpu_lease(path):
                blocked = subprocess.run([sys.executable, '-c', code, str(path)], cwd=protocol.ROOT,
                                         capture_output=True, text=True, timeout=20)
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn('holds the GPU lease', blocked.stderr)
            recovered = subprocess.run([sys.executable, '-c', code, str(path)], cwd=protocol.ROOT,
                                       capture_output=True, text=True, timeout=20)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
