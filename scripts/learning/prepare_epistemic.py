"""Validate synthetic epistemic examples or explicitly prepare one isolated idle job."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from assistant import learning_batches as batches, learning_queue as queue, parameter_acceptance, idle_learning
from assistant.store import Store

SPEC = ROOT / 'evals/epistemic_learning.json'
CONTRAST_SPEC = ROOT / 'evals/prerequisite_contrast_learning.json'
CONTRAST_ASSISTANT_TRANSFER = ROOT / 'evals/prerequisite_contrast_assistant_transfer.json'
GENERALIZATION_SPEC = ROOT / 'evals/base_assistant_generalization.json'
CONTRAST_SCHEMA = 'synthetic_prerequisite_contrast_v1'
LABELS = {'已確認成立', '已確認不成立', '未知'}


def normalized(prompt):
    return ' '.join(unicodedata.normalize('NFKC', prompt).split())


def verify_contrasts(data, prompts):
    assigned = set()
    for split in ('training', 'heldout'):
        groups = {}
        for row in data[split]:
            name = row.get('contrast')
            if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9_]{1,48}', name):
                raise ValueError('Contrast cases require a stable group name.')
            groups.setdefault(name, []).append(row['answer'])
        if assigned.intersection(groups) or any(len(answers) != 3 or set(answers) != LABELS
                                                for answers in groups.values()):
            raise ValueError('Each contrast group must contain all three labels in a single split.')
        assigned.update(groups)
    earlier = json.loads(SPEC.read_text(encoding='utf-8'))
    generalization = json.loads(GENERALIZATION_SPEC.read_text(encoding='utf-8'))
    previous_prompts = [row['prompt'] for split in ('training', 'heldout') for row in earlier[split]]
    previous_prompts.extend(row['question'] for row in earlier['assistant_suite']['cases'])
    previous_prompts.extend(row['question'] for row in generalization['cases'])
    previous = set(map(normalized, previous_prompts))
    if prompts.intersection(previous):
        raise ValueError('Contrast cases must be separate from earlier training and evaluation questions.')
    transfer = parameter_acceptance.suite(json.loads(CONTRAST_ASSISTANT_TRANSFER.read_text(encoding='utf-8')))
    transfer_prompts = {normalized(row['question']) for row in transfer['cases']}
    if len(transfer_prompts) != len(transfer['cases']) or transfer_prompts.intersection(prompts | previous):
        raise ValueError('Full-assistant transfer questions must be separate from the other cases.')


def specification(raw=None):
    raw = SPEC.read_bytes() if raw is None else raw
    data = json.loads(raw.decode('utf8'))
    if data.get('schema') not in ('synthetic_epistemic_v1', CONTRAST_SCHEMA):
        raise ValueError('Synthetic data schema mismatch.')
    prompts = set()
    for split in ('training', 'heldout'):
        counts = {label: 0 for label in LABELS}
        for row in data[split]:
            queue.text(row['prompt'], 2000, 'Synthetic question')
            prompt = normalized(row['prompt'])
            if prompt in prompts or row['answer'] not in LABELS:
                raise ValueError('Duplicate training/heldout question or invalid label.')
            prompts.add(prompt)
            counts[row['answer']] += 1
        if min(counts.values()) == 0 or len(set(counts.values())) != 1:
            raise ValueError('Each split must balance all three labels.')
    if not 1 <= len(data['training']) <= 64 or len(data['heldout']) > 16:
        raise ValueError('Data exceeds the existing batch bounds.')
    if data['schema'] == CONTRAST_SCHEMA:
        verify_contrasts(data, prompts)
    data['assistant_suite'] = parameter_acceptance.suite(data['assistant_suite'])
    targets = {(c['question'], c['answer']) for c in data['assistant_suite']['cases'] if c['group']=='target'}
    if targets != {(c['prompt'], c['answer']) for c in data['heldout']}:
        raise ValueError('Full-assistant targets must match the frozen heldout questions.')
    return data


def study_bytes(study):
    return {'epistemic': SPEC, 'prerequisite-contrast': CONTRAST_SPEC}[study].read_bytes()


def prepare(name, study='epistemic'):
    if not re.fullmatch(r'[a-z0-9_-]{1,64}', name):
        raise ValueError('Use a simple new run name.')
    raw = study_bytes(study)
    data = specification(raw)
    folder = ROOT / 'data/learning/epistemic' / name
    folder.mkdir(parents=True, exist_ok=False)
    # This store contains synthetic study examples only; no personal assistant DB is opened.
    store = Store(folder / 'assistant.sqlite3')
    selected = []
    for row in data['training']:
        record = queue.confirm(store, row['prompt'], row['answer'])
        selected.append(dict(id=record['id'], revision=record['revision']))
    checks = [{k: row[k] for k in ('prompt', 'answer')} for row in data['heldout']]
    batch = batches.prepare(store, selected, checks=checks)
    job = idle_learning.prepare(folder, batch, data['assistant_suite'],
                                idle_seconds=600, wait_seconds=900, work_seconds=1800)
    receipt = dict(schema='synthetic_epistemic_preparation_v1',
                   author=data['author'], source_sha256=hashlib.sha256(raw).hexdigest(),
                   training_count=len(selected), heldout_count=len(checks),
                   data_dir=str(folder), batch_id=batch, idle_job_id=job)
    batches.save(folder / 'study.json', data)
    batches.save(folder / 'preparation.json', receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', choices=('epistemic', 'prerequisite-contrast'), default='epistemic')
    parser.add_argument('--prepare', metavar='NEW_RUN')
    args = parser.parse_args()
    if args.prepare:
        print(json.dumps(prepare(args.prepare, args.study), ensure_ascii=False, indent=2))
    else:
        data = specification(study_bytes(args.study))
        print(f"{len(data['training'])} synthetic training and {len(data['heldout'])} heldout cases validated; no database or GPU work started")


if __name__ == '__main__':
    main()
