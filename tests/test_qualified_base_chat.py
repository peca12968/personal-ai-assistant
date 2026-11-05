import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from assistant.engine import EngineError
from assistant.store import Store
from scripts import daily_chat
from scripts.learning import qualified_base_chat as chat


class QualifiedBaseChatTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        base = chat.local_models.select()
        self.selection = dict(schema=chat.assay.CANDIDATE, base=base, qualification='study',
                              session='a' * 32, fingerprint='b' * 64)

    def result(self):
        return dict(schema='local_inference_result_v1', status='succeeded',
                    selection_fingerprint=self.selection['fingerprint'], answer='未知',
                    input_tokens=50, output_tokens=3, peak_gpu_bytes=0)

    def test_qualification_recomputes_gate_and_binds_full_receipt(self):
        meta = {'sources': {}, 'suite': {'name': 'fixed'}}
        raw = {'schema': chat.assay.CANDIDATE, 'fingerprint': 'c' * 64}
        state = SimpleNamespace(selections={'candidate': raw}, series=self.root, head={'blob': 'd' * 64},
                                adapter=self.root / 'adapter')
        report = dict(passed=True, automatic_activation=False)
        with patch.object(chat.assay, 'location', return_value=self.root), \
                patch.object(chat.assay, 'frozen', return_value=(meta, state)), \
                patch.object(chat.assay, 'read', return_value=report), \
                patch.object(chat.assay, 'review', return_value=report) as verify, \
                patch.object(chat.assay.segments, 'pin_blob') as pin:
            selected, adapter = chat.qualify('study')
            self.assertEqual(adapter, state.adapter)
            self.assertEqual(selected['full_manifest_sha256'], chat.assay.digest(meta))
            self.assertEqual(selected['full_review_sha256'], chat.assay.digest(report))
            self.assertEqual(selected['fingerprint'], chat.assay.digest(
                {k: v for k, v in selected.items() if k != 'fingerprint'}))
            pin.assert_called_once_with(self.root, 'd' * 64)
            verify.return_value = {**report, 'passed': False}
            with self.assertRaisesRegex(ValueError, '驗收'):
                chat.qualify('study')

    def test_explicit_chat_preserves_provenance_and_does_not_save_choice(self):
        class Raw:
            display_name = 'fixture'

            def describe(inner):
                return chat.description(self.selection)

            def answer(inner, prompt, cancel=None):
                return '未知'

        with patch.object(chat, 'QualifiedBaseEngine', return_value=Raw()) as selected, \
                patch.object(daily_chat, 'LocalEngine') as base, patch('sys.argv', [
                    'daily_chat', '--qualified-base-assay', 'study', '--data-dir', str(self.root), '--ask', '請回答未知。'
                ]), redirect_stdout(io.StringIO()) as output:
            daily_chat.main()
        selected.assert_called_once_with(self.root, 'study')
        base.assert_not_called()
        sid = output.getvalue().split()[-1]
        store = Store(self.root / 'assistant.sqlite3')
        message = store.messages(sid, 1)[0]
        self.assertEqual(message['content'], '未知')
        self.assertEqual(store.answer_context(message['id'])['engine_selection'], chat.description(self.selection))
        self.assertFalse((self.root / 'local-model-choice.sqlite3').exists())

    def test_conflicting_engine_flags_are_rejected_before_opening_data(self):
        for flags in (['--engine', 'local'], ['--qualified-segment'], ['--local-active'], ['--bogus']):
            with self.subTest(flags=flags), patch('sys.argv', [
                    'daily_chat', '--qualified-base-assay', 'study', '--data-dir', str(self.root), *flags
                ]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                daily_chat.main()
        self.assertFalse((self.root / 'assistant.sqlite3').exists())

    def test_running_inference_cannot_return_after_selection_changes(self):
        python = self.root / 'data/learning/runtime' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        python.parent.mkdir(parents=True)
        python.touch()

        def simulate(command, folder, phase, guard, live):
            self.assertEqual(phase, 'result')
            self.assertEqual(chat.assay.read(folder / 'request.json')['selection'], self.selection)
            guard()
            live()

        with patch.object(chat.assay, 'ROOT', self.root), \
                patch.object(chat, 'qualify', return_value=(self.selection, self.root)), \
                patch.object(chat, 'current', return_value={**self.selection, 'fingerprint': 'z'}), \
                patch.object(chat.assay.gpu, 'run_owned', side_effect=simulate):
            engine = chat.QualifiedBaseEngine(self.root, 'study')
            with self.assertRaises(EngineError) as exc:
                engine.answer('只回未知')
            self.assertEqual(exc.exception.code, 'local_model_changed')

    def test_deliver_preserves_typed_gpu_failure_and_rejects_wrong_fingerprint(self):
        self.assertEqual(chat.deliver('問題', self.selection, self.result()), ('未知', None))
        failure = dict(schema='local_inference_result_v1', status='failed', code='gpu_busy')
        for result, code in ((failure, 'local_gpu_busy'),
                             ({**self.result(), 'selection_fingerprint': 'bad'}, 'invalid_response')):
            with self.subTest(code=code), self.assertRaises(EngineError) as exc:
                chat.deliver('問題', self.selection, result)
            self.assertEqual(exc.exception.code, code)
