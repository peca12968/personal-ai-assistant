from pathlib import Path
import tempfile
import unittest

from assistant.engine import Assistant, EngineError
from assistant.store import Store
from scripts.learning import confidence_probe as probe


class Engine:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.inputs = []

    def describe(self):
        return {'selection_fingerprint': 'synthetic'}

    def answer(self, prompt, cancel=None):
        self.inputs.append(prompt)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


class ConfidenceProbeTests(unittest.TestCase):
    @staticmethod
    def corpus(count):
        return [dict(question=f'試題 {i}', answer='A', format='choice') for i in range(count)]

    def test_scores_keep_missing_malformed_and_ties(self):
        pairs = [('A', '80'), ('B', '80'), ('A', '95'), ('A。', '30%'), (None, None), ('B', None)]
        r = probe.metrics(self.corpus(6), pairs)
        self.assertEqual((r['correct'], r['total'], r['scored_pairs']), (2, 6, 3))
        self.assertEqual((r['answer_errors'], r['answer_format_errors'], r['confidence_format_errors'], r['confidence_errors']), (1, 1, 1, 1))
        self.assertAlmostEqual(r['brier'], (0.2**2 + 0.8**2 + 0.05**2)/3)
        self.assertEqual(r['ranking'], .75)
        self.assertEqual(r['high_confidence_coverage'], .5)
        self.assertAlmostEqual(r['high_confidence_accuracy'], 2/3)

    def test_unavailable_statistics_and_incomplete_pairs(self):
        r = probe.metrics(self.corpus(2), [('A', '00'), ('B', '101')])
        for key in ('brier', 'ranking', 'high_confidence_accuracy'):
            self.assertIsNone(r[key])
        with self.assertRaises(ValueError):
            probe.metrics(self.corpus(2), [('A', '100')])
        with self.assertRaises(ValueError):
            probe.metrics(self.corpus(2), [(None, '100'), ('B', None)])

    def test_regrade_real_store_and_detect_changed_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            engine = Engine(['B', '10', EngineError('busy', 'local_gpu_busy')])
            store = Store(folder/'assistant.sqlite3')
            app = Assistant(store, engine, allow_local_calculation=False)
            corpus = self.corpus(2)
            first = probe.ask(app, store, corpus[0]['question'])
            confidence = probe.ask(app, store, probe.confidence_question(corpus[0]['question'], first['answer']))
            second = probe.ask(app, store, corpus[1]['question'])
            plan = dict(cases=corpus, policy=probe.POLICY, sources=probe.sources(), description=engine.describe())
            (folder/'collector.py').write_bytes(Path(probe.__file__).read_bytes())
            signature = probe.digest(plan)
            rows = [dict(response=first, confidence=confidence), dict(response=second, confidence=None)]
            probe.save(folder/'plan.json', plan)
            probe.save(folder/'responses.json', rows)
            report = probe.review(folder, signature)
            self.assertEqual((report['answer_errors'], report['correct'], report['scored_pairs']), (1, 0, 1))
            self.assertAlmostEqual(report['brier'], .01)
            rows[0]['response']['answer'] = 'A'
            probe.save(folder/'responses.json', rows)
            with self.assertRaises(ValueError):
                probe.review(folder, signature)
            rows[0]['response']['answer'] = 'B'
            probe.save(folder/'responses.json', rows)
            (folder/'collector.py').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                probe.review(folder, signature)
