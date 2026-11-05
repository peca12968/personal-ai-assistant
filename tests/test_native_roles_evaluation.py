import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.engine import EngineError
from assistant.local_engine import LocalEngine
from scripts import evaluate_native_roles as evaluation
from scripts.evaluation_attempts import exclusive_json
from scripts.evaluation_reports import canonical_digest, load_json


class NativeRoleEvidenceTests(unittest.TestCase):
    def test_attempt_is_durable_before_worker_failure_and_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = LocalEngine(root)
            folder = root / 'study'
            suite = evaluation.ROOT / 'evals/work_comprehension.json'
            with patch.object(evaluation, 'LocalEngine', return_value=engine):
                evaluation.prepare(folder, suite, ['causal_direction'], 'split')
                plan = load_json(folder / 'plan.json')
                row = plan['rows'][0]
                exclusive_json(folder / 'preflight.json', {
                    'plan_sha256': canonical_digest(plan),
                    'rows': [{'id': row['case']['id'], 'wire_sha256': canonical_digest(row['wire']),
                              'input_tokens': 100}],
                })

                def fail(guide, data):
                    attempt = load_json(folder / '0.request.json')
                    self.assertEqual(attempt['wire']['guide'], guide)
                    self.assertEqual(attempt['wire']['data'], data)
                    self.assertEqual(attempt['messages'], row['messages'])
                    raise EngineError('fixture failure', 'local_gpu_busy')

                with patch.object(engine, 'answer_roles', side_effect=fail) as invoke:
                    with self.assertRaisesRegex(EngineError, 'fixture failure'):
                        evaluation.run(folder, plan)
                    with self.assertRaises(FileExistsError):
                        evaluation.run(folder, plan)
                    invoke.assert_called_once()
                outcome = load_json(folder / '0.outcome.json')
                self.assertEqual(outcome['error'], 'local_gpu_busy')
                self.assertEqual(outcome['request_sha256'], canonical_digest(load_json(folder / '0.request.json')))
                self.assertFalse((folder / 'collected.json').exists())
                row = evaluation.audit(folder, plan)['rows'][0]
                self.assertEqual(row['id'], 'causal_direction')
                self.assertEqual(row['status'], 'error')
                (folder / 'source-0.bin').write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, '指紋'):
                    evaluation.audit(folder, plan)


if __name__ == '__main__':
    unittest.main()
