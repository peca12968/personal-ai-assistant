import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.learning import prepare_epistemic as study


class EpistemicPreparationTests(unittest.TestCase):
    def test_training_and_heldout_are_disjoint_and_all_targets_come_from_heldout(self):
        data=study.specification()
        self.assertFalse({r['prompt'] for r in data['training']} & {r['prompt'] for r in data['heldout']})
        self.assertEqual(len(data['assistant_suite']['cases']),8)

    def test_default_entry_never_opens_database_or_prepares_gpu_work(self):
        with patch('sys.argv',['prepare_epistemic.py']), patch.object(study,'Store') as store, patch.object(study.batches,'prepare') as prepare:
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
