import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.engine import EngineError
from scripts.learning import qualified_segment as qualified, segment_chat as chat


class SegmentChatTests(unittest.TestCase):
    def test_exact_short_scores_require_eos_and_order(self):
        cases = [dict(id='one', answer='A'), dict(id='two', answer='B')]
        rows = [dict(id='one', output='A', ended_with_eos=False, tokens=1),
                dict(id='two', output='B', ended_with_eos=True, tokens=2)]
        self.assertEqual(qualified.short_scores(cases, rows), {'one': False, 'two': True})
        for broken in (rows[:1], rows[::-1], [rows[0], {**rows[1], 'tokens': True}]):
            with self.assertRaises(ValueError):
                qualified.short_scores(cases, broken)

    def test_missing_or_replaced_index_cannot_select(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(qualified, 'RELEASE', Path(tmp)):
            with self.assertRaises(FileNotFoundError):
                chat.SegmentEngine(tmp)
            (Path(tmp) / 'evidence-index.json').write_text('{"passed":true}')
            with self.assertRaises(ValueError):
                chat.SegmentEngine(tmp)
            self.assertFalse((Path(tmp) / 'local-engine-workspace').exists())

    def test_revoke_in_flight_and_cancel_before_call(self):
        description = {'selection_fingerprint': 'sample'}
        selection = ({}, {}, description, {}, 'weights')
        with tempfile.TemporaryDirectory() as tmp, patch.object(qualified, 'qualify', return_value=selection):
            engine = chat.SegmentEngine(tmp)
            event = threading.Event()
            event.set()
            with patch.object(qualified.segments.gpu, 'run_owned') as launch:
                with self.assertRaises(EngineError) as stopped:
                    engine.answer('sample', event)
                self.assertEqual(stopped.exception.code, 'cancelled')
                launch.assert_not_called()

            def worker(cmd, folder, phase, guard, live):
                (folder / 'chat.log').touch()
                live()
                self.fail('a withdrawn source must stop the in-flight answer')

            with patch.object(Path, 'is_file', return_value=True), patch.object(
                    qualified, 'current', side_effect=ValueError('withdrawn')), patch.object(
                    qualified.segments.gpu, 'run_owned', side_effect=worker):
                with self.assertRaisesRegex(EngineError, 'withdrawn'):
                    engine.answer('sample')
            self.assertEqual(list(engine.workspace.iterdir()), [])

    def test_reverify_after_worker_returns(self):
        selection = ({}, {}, {'selection_fingerprint': 'sample'}, {}, 'weights')
        def worker(cmd, folder, phase, guard, live):
            qualified.segments.gpu.save(folder / 'result.json', dict(fingerprint='sample', answer='draft'))
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, 'is_file', return_value=True), patch.object(
                qualified, 'qualify', side_effect=[selection, selection, ValueError('changed')]), patch.object(
                qualified.segments.gpu, 'run_owned', side_effect=worker):
            engine = chat.SegmentEngine(tmp)
            with self.assertRaisesRegex(EngineError, 'changed'):
                engine.answer('sample')
            self.assertEqual(list(engine.workspace.iterdir()), [])

