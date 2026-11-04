import itertools
import unittest
from unittest.mock import patch

from scripts import device_reasoning as r
from scripts import evaluate_device_components as evaluation


class DeviceReasoningTests(unittest.TestCase):
    def test_three_valued_composition_matches_all_possible_boolean_worlds(self):
        possibilities={'能':[True],'不能':[False],'資料不足':[False,True]}
        for labels in itertools.product(possibilities,repeat=3):
            outcomes={common and (a or b) for common,a,b in itertools.product(*(possibilities[v] for v in labels))}
            expected='能' if outcomes=={True} else '不能' if outcomes=={False} else '資料不足'
            self.assertEqual(r.combine(list(labels)),expected)
        for bad in ([],['能','不能'],['能','不能','猜測']):
            with self.assertRaises(ValueError):r.combine(bad)

    def test_shared_facts_and_both_device_sentences_are_preserved(self):
        for case in evaluation.cases():
            plan=r.plan(case['question'])
            reconstructed='。'.join([plan['header'],*plan['devices'],*plan['shared'],plan['question']])+'？只回「能」、「不能」或「資料不足」。'
            self.assertEqual(reconstructed,case['question'])
            self.assertIn(plan['question'],r.prompt(plan))
        q=evaluation.cases()[0]['question']
        for bad in (q.replace('任選一台即可','兩台必須同時用'),q.replace('柏機','松機'), '翻譯「'+q+'」'):
            self.assertIsNone(r.plan(bad))

    def test_labels_must_be_three_ordered_unqualified_judgments(self):
        self.assertEqual(r.decode('共同：不能\n一：資料不足\n二：能'), ['不能','資料不足','能'])
        for raw in ('共同：能\n一：能', '共同：能\n二：不能\n一：能',
                    '共同：能\n一：能\n二：能，可能不行'):
            self.assertIsNone(r.decode(raw))

    def test_offline_entry_does_not_call_local_model(self):
        with patch('sys.argv',['evaluate_device_components.py']), patch('assistant.local_engine.LocalEngine') as model:
            evaluation.main()
            model.assert_not_called()
