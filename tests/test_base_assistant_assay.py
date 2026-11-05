from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.learning import assess_base_assistant as assay


class BaseAssistantAssayTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.parent = Path(folder.name)
        self.folder = self.parent / 'study'
        base = assay.local_models.select()
        candidate = dict(schema=assay.CANDIDATE, base=base, session='a' * 32, fingerprint='b' * 64)
        self.state = SimpleNamespace(selections={'baseline': base, 'candidate': candidate},
                                     series=self.parent, head={'blob': 'c' * 64},
                                     idle_ms=600000, old_prompts={'Ｏｌｄ prompt'})
        for label, value in (
                ('PARENT', self.parent),
                ('evidence', lambda name: self.state)):
            patcher = patch.object(assay, label, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        pin = patch.object(assay.segments, 'pin_blob')
        pin.start()
        self.addCleanup(pin.stop)
        self.suite = {'schema': 'full_assistant_cases_v1', 'cases': [
            {'id': 'a', 'group': 'target', 'question': '目前資料有值嗎？', 'answer': '未知',
             'history': [{'role': 'user', 'content': '昨天還沒測試。'}]},
            {'id': 'b', 'group': 'control', 'question': '回答數字 5。', 'answer': '5'},
            {'id': 'c', 'group': 'control', 'question': '回答數字 9。', 'answer': '9'},
        ]}

    def prepare(self):
        self.meta = assay.prepare('study', 'short-assay', self.suite)
        count = {cid: 100 for cid in self.meta['sessions']}
        assay.save(self.folder / 'preflight.json', {'manifest_sha256': assay.digest(self.meta),
                   'input_tokens': {phase: count for phase in assay.PHASES}})

    def test_prepare_keeps_history_and_model_only_measurement_separate(self):
        self.prepare()
        self.assertEqual(assay.frozen(self.folder)[0], self.meta)
        self.assertEqual(len(set(self.meta['database_files'].values())), 1)
        for name, sha in self.meta['sources'].items():
            self.assertEqual(assay.batches.file_hash(self.folder / 'sources' / name), sha)
        old = self.meta['contexts']['baseline']['a']
        new = self.meta['contexts']['candidate']['a']
        self.assertEqual(old, {**new, 'engine_selection': old['engine_selection']})
        self.assertNotEqual(old['engine_selection'], new['engine_selection'])
        app = assay.raw_assistant(None, None)
        self.assertFalse(app.allow_local_calculation)
        self.assertFalse(app.allow_local_transcription)
        self.assertFalse(app.allow_local_evidence)

    def test_prepare_rejects_normalized_training_duplicate_before_creating_run(self):
        self.suite['cases'][0]['question'] = 'Old    prompt'
        with self.assertRaisesRegex(ValueError, '重複'):
            assay.prepare('study', 'short-assay', self.suite)
        self.assertFalse(self.folder.exists())

    def test_no_worker_or_run_for_recent_input_and_changed_preflight(self):
        self.prepare()
        with patch.object(assay.segments.idle, 'idle_sample', return_value={'idle_ms': 100}),                 patch.object(assay.gpu, 'run_owned') as run:
            with self.assertRaisesRegex(ValueError, '閒置'):
                assay.compare(self.folder)
            self.assertFalse((self.folder / 'run.json').exists())
            prep = self.folder / 'preflight.json'
            tampered = assay.read(prep)
            tampered['input_tokens']['candidate']['a'] = True
            assay.save(prep, tampered)
            with self.assertRaisesRegex(ValueError, '預檢'):
                assay.compare(self.folder)
            run.assert_not_called()

    def test_completed_result_binds_actual_inputs_saved_answer_and_exits(self):
        self.prepare()
        def fake_child(command, output, phase, input_check, source_check):
            self.assertEqual(phase, 'inference')
            side, cid = command[-3:-1]
            answer = next(case['answer'] for case in self.suite['cases'] if case['id'] == cid)
            if side == 'baseline' and cid == 'a':
                answer = '已知'
            source_check()
            (output / 'inference.started').write_text('1001', encoding='ascii')
            assay.save(output / 'inference.exit.json', {'worker': 1001, 'launcher': 1000,
                       'exit_confirmed': True, 'returncode': 0})
            assay.save(output / 'result.json', {'schema': 'local_inference_result_v1',
                       'selection_fingerprint': self.meta['selections'][side]['fingerprint'],
                       'answer': answer, 'status': 'succeeded', 'input_tokens': 100})

        with patch.object(assay.segments.idle, 'idle_sample', return_value={'idle_ms': 610000, 'ticks_ms': 615000, 'last_input_tick': 5000}),                 patch.object(assay.gpu, 'input_gate'), patch.object(assay.gpu, 'run_owned', side_effect=fake_child):
            result = assay.compare(self.folder)
            self.assertTrue(result['passed'])
            self.assertEqual(len(result['exit_files']), 6)
            with self.assertRaisesRegex(ValueError, '已執行'):
                assay.compare(self.folder)
        run = assay.read(self.folder / 'run.json')
        rows = assay.read(self.folder / 'responses.json')
        path = self.folder / 'candidate/a/worker/inference.exit.json'
        bad = {**assay.read(path), 'exit_confirmed': False}
        assay.save(path, bad)
        with self.assertRaisesRegex(ValueError, '程序'):
            assay.review(self.folder, self.meta, run, rows)
