import json
from collections import Counter
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.learning import prepare_epistemic as study
from scripts.learning import decision_code_study as codes


class EpistemicPreparationTests(unittest.TestCase):
    def test_training_and_heldout_are_disjoint_and_all_targets_come_from_heldout(self):
        data=study.specification()
        self.assertFalse({r['prompt'] for r in data['training']} & {r['prompt'] for r in data['heldout']})
        self.assertEqual(len(data['assistant_suite']['cases']),8)

    def test_default_entry_never_opens_database_or_prepares_gpu_work(self):
        for arguments in ([], ['--study', 'decision-codes']):
            with (self.subTest(arguments=arguments), patch('sys.argv', ['prepare_epistemic.py', *arguments]),
                  patch.object(study, 'Store') as store, patch.object(study.batches, 'prepare') as prepare):
                study.main()
                store.assert_not_called()
                prepare.assert_not_called()

    def test_preparation_uses_an_isolated_store_and_existing_bounded_idle_policy(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(study,'ROOT',Path(tmp)), \
                patch.object(study.batches,'prepare',return_value='a'*32) as batch, \
                patch.object(study.idle_learning,'prepare',return_value='b'*32) as idle:
            receipt=study.prepare('test')
            self.assertEqual((receipt['training_count'],receipt['heldout_count']),(12,6))
            self.assertEqual(Path(receipt['data_dir']),Path(tmp)/'data/learning/epistemic/test')
            self.assertEqual(len(batch.call_args.args[1]),12)
            self.assertEqual(len(batch.call_args.kwargs['checks']),6)
            self.assertEqual(idle.call_args.kwargs,dict(idle_seconds=600,wait_seconds=900,work_seconds=1800))
            with self.assertRaises(FileExistsError): study.prepare('test')
            with self.assertRaises(ValueError): study.prepare('../test')

    def test_changed_labels_or_overlap_are_rejected(self):
        original=study.specification()
        for mutation in ('overlap','label'):
            data=json.loads(json.dumps(original))
            if mutation=='overlap':data['heldout'][0]['prompt']=data['training'][0]['prompt']
            else:data['training'][0]['answer']='猜測'
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'spec.json';path.write_text(json.dumps(data),encoding='utf8')
                with patch.object(study,'SPEC',path),self.assertRaises(ValueError):study.specification()

    def test_prerequisite_contrasts_have_disjoint_full_triplets(self):
        data = study.specification(study.CONTRAST_SPEC.read_bytes())
        self.assertEqual((len(data['training']), len(data['heldout'])), (18, 6))
        groups = [{row['contrast'] for row in data[split]} for split in ('training', 'heldout')]
        self.assertEqual([len(group) for group in groups], [6, 2])
        self.assertTrue(groups[0].isdisjoint(groups[1]))
        data['training'][0]['contrast'] = data['heldout'][0]['contrast']
        with self.assertRaisesRegex(ValueError, 'single split'):
            study.specification(json.dumps(data).encode('utf-8'))

    def test_prerequisite_training_cannot_include_the_active_generalization_case(self):
        data = json.loads(study.CONTRAST_SPEC.read_bytes())
        prior = json.loads(study.GENERALIZATION_SPEC.read_bytes())['cases'][0]['question']
        data['training'][0]['prompt'] = '\u3000' + prior
        with self.assertRaisesRegex(ValueError, 'separate from earlier training and evaluation'):
            study.specification(json.dumps(data).encode('utf-8'))

    def test_irrelevant_unknowns_and_necessary_only_rules_keep_correct_verdicts(self):
        for rule, patterns, expected in (
            ('either', ('TF', 'TU'), 'supported'),
            ('shared_backup', ('TTF', 'TTU'), 'supported'),
            ('shared_backup', ('FTF', 'FTU'), 'refuted'),
            ('shared_backup', ('TFU', 'UTF'), 'undetermined'),
            ('all', ('FT', 'FU'), 'refuted'),
            ('necessary_only', ('TT', 'TU'), 'undetermined'),
            ('required_marker', ('T', 'U'), 'undetermined'),
            ('required_marker', ('F',), 'refuted'),
        ):
            with self.subTest(rule=rule, patterns=patterns):
                self.assertEqual({codes.decide(rule, pattern) for pattern in patterns}, {expected})
        self.assertEqual(codes.decide('necessary_only', 'TT', witnessed=True), 'supported')

    def test_same_facts_require_different_codes_without_changing_decision(self):
        spec = json.loads(study.DECISION_SPEC.read_bytes())
        compiled = codes.compile_study(spec)
        first, alternate = compiled['training'][:2]
        self.assertEqual(first['verdict'], alternate['verdict'])
        self.assertNotEqual(first['answer'], alternate['answer'])
        self.assertEqual(first['prompt'].split('只輸出指定代碼。')[1],
                         alternate['prompt'].split('只輸出指定代碼。')[1])
        for split in compiled.values():
            for row in split:
                self.assertEqual(row['answer'], row['code_map'][row['verdict']])
            for group in {row['contrast'] for row in split}:
                outcomes = [row for row in split if row['contrast'] == group]
                self.assertEqual(Counter(row['verdict'] for row in outcomes), dict.fromkeys(codes.VERDICTS, 2))
                self.assertEqual(set(Counter(row['answer'] for row in outcomes).values()), {2})
        spec['heldout'][0]['id'] = spec['training'][0]['id']
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            codes.compile_study(spec)

    def test_decision_codes_replay_training_but_reserve_old_heldout_for_transfer(self):
        raw, data = study.load_study('decision-codes')
        self.assertEqual(json.loads(raw), data)
        self.assertEqual((len(data['training']), len(data['heldout']), len(data['transfer_suite']['cases'])),
                         (48, 12, 12))
        prior = study.specification(study.CONTRAST_SPEC.read_bytes())
        self.assertEqual(data['training'][:18], prior['training'])
        old_heldout = {(row['prompt'], row['answer']) for row in prior['heldout']}
        self.assertEqual({(row['prompt'], row['answer']) for row in data['transfer_suite']['cases'][-6:]},
                         old_heldout)
        self.assertTrue({row['prompt'] for row in data['training']}.isdisjoint(
            {row['prompt'] for row in [*data['heldout'], *data['transfer_suite']['cases']]}))
