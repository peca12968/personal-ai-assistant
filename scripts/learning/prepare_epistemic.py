"""Validate synthetic epistemic examples or explicitly prepare one isolated idle job."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from assistant import learning_batches as batches, learning_queue as queue, parameter_acceptance, idle_learning
from assistant.store import Store

SPEC = ROOT / 'evals/epistemic_learning.json'


def specification(raw=None):
    raw = SPEC.read_bytes() if raw is None else raw
    data = json.loads(raw.decode('utf8'))
    if data.get('schema') != 'synthetic_epistemic_v1':
        raise ValueError('Synthetic data schema mismatch.')
    prompts = set()
    labels = {'已確認成立', '已確認不成立', '未知'}
    for split in ('training', 'heldout'):
        counts = {label: 0 for label in labels}
        for row in data[split]:
            queue.text(row['prompt'], 2000, 'Synthetic question')
            if row['prompt'] in prompts or row['answer'] not in labels:
                raise ValueError('Duplicate training/heldout question or invalid label.')
            prompts.add(row['prompt'])
            counts[row['answer']] += 1
        if min(counts.values()) == 0 or len(set(counts.values())) != 1:
            raise ValueError('Each split must balance all three labels.')
    if not 1 <= len(data['training']) <= 64 or len(data['heldout']) > 16:
        raise ValueError('Data exceeds the existing batch bounds.')
    data['assistant_suite'] = parameter_acceptance.suite(data['assistant_suite'])
    targets = {(c['question'], c['answer']) for c in data['assistant_suite']['cases'] if c['group']=='target'}
    if targets != {(c['prompt'], c['answer']) for c in data['heldout']}:
        raise ValueError('Full-assistant targets must match the frozen heldout questions.')
    return data


def prepare(name):
    if not re.fullmatch(r'[a-z0-9_-]{1,64}', name):
        raise ValueError('Use a simple new run name.')
    raw = SPEC.read_bytes()
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
    parser.add_argument('--prepare', metavar='NEW_RUN')
    args = parser.parse_args()
    if args.prepare:
        print(json.dumps(prepare(args.prepare), ensure_ascii=False, indent=2))
    else:
        data = specification()
        print(f"{len(data['training'])} synthetic training and {len(data['heldout'])} heldout cases validated; no database or GPU work started")


if __name__ == '__main__':
    main()
