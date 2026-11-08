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
from scripts.learning import decision_code_study, observation_language_study

SPEC = ROOT / 'evals/epistemic_learning.json'
CONTRAST_SPEC = ROOT / 'evals/prerequisite_contrast_learning.json'
CONTRAST_ASSISTANT_TRANSFER = ROOT / 'evals/prerequisite_contrast_assistant_transfer.json'
GENERALIZATION_SPEC = ROOT / 'evals/base_assistant_generalization.json'
DECISION_SPEC = ROOT / 'evals/prerequisite_decision_codes.json'
OBSERVATION_SPEC = ROOT / 'evals/prerequisite_observation_language.json'
SHORT_TRANSFER = ROOT / 'evals/base_checkpoint_transfer.json'
CONTRAST_SCHEMA = 'synthetic_prerequisite_contrast_v1'
LABELS = {'已確認成立', '已確認不成立', '未知'}
OBSERVATION_CHECKS = {'passcode-present', 'permit-revoked', 'route-alternative', 'previous-contrast-5'}


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


def decision_codes_data():
    sources = {path: path.read_bytes() for path in (DECISION_SPEC, CONTRAST_SPEC, SHORT_TRANSFER,
                                                    CONTRAST_ASSISTANT_TRANSFER, SPEC, GENERALIZATION_SPEC)}
    compiled = decision_code_study.compile_study(json.loads(sources[DECISION_SPEC]))
    prior = specification(sources[CONTRAST_SPEC])
    training = [*prior['training'], *compiled['training']]
    heldout = compiled['heldout']
    transfer = json.loads(sources[SHORT_TRANSFER])
    transfer['cases'].extend(dict(id=f'previous-contrast-{index}', prompt=row['prompt'], answer=row['answer'])
                             for index, row in enumerate(prior['heldout'], 1))
    full_transfer = parameter_acceptance.suite(json.loads(sources[CONTRAST_ASSISTANT_TRANSFER]))
    reference_controls = [row for row in prior['assistant_suite']['cases'] if row['group'] == 'control']
    targets = [dict(id=row['id'], group='target', question=row['prompt'], answer=row['answer'])
               for row in heldout]
    assistant_suite = parameter_acceptance.suite(dict(schema='full_assistant_cases_v1',
                                                     cases=[*targets, *reference_controls]))
    prompts = [row['prompt'] for row in [*training, *heldout, *transfer['cases']]]
    prompts.extend(row['question'] for row in [*full_transfer['cases'], *reference_controls])
    normalized_prompts = set(map(normalized, prompts))
    if len(normalized_prompts) != len(prompts):
        raise ValueError('Decision-code training, heldout and transfer questions must be distinct.')
    for path, field in ((SPEC, 'assistant_suite'), (GENERALIZATION_SPEC, None)):
        earlier = json.loads(sources[path])
        cases = earlier[field]['cases'] if field else earlier['cases']
        reserved = [row['question'] for row in cases]
        if field:
            reserved.extend(row['prompt'] for split in ('training', 'heldout') for row in earlier[split])
        if normalized_prompts.intersection(map(normalized, reserved)):
            raise ValueError('Decision-code cases must be separate from earlier evaluations.')
    return dict(schema=decision_code_study.SCHEMA, author='Codex synthetic decision-code contrasts',
                training=training, heldout=heldout, assistant_suite=assistant_suite,
                transfer_suite=transfer,
                sources={p.name: hashlib.sha256(raw).hexdigest() for p, raw in sources.items()},
                compiler_sha256=hashlib.sha256(Path(decision_code_study.__file__).read_bytes()).hexdigest())


def observation_language_data():
    source = OBSERVATION_SPEC.read_bytes()
    spec = json.loads(source)
    fresh = observation_language_study.compile_study(spec)
    prior_bytes, prior = load_study('decision-codes')
    earlier = load_study('epistemic')[1]
    previous_full = json.loads(CONTRAST_ASSISTANT_TRANSFER.read_bytes())['cases']
    generalization = json.loads(GENERALIZATION_SPEC.read_bytes())['cases']
    controls = [row for row in prior['assistant_suite']['cases'] if row['group'] == 'control']
    prior_transfer = prior['transfer_suite']['cases']
    reserved = {normalized(row['prompt']) for split in ('training', 'heldout') for row in earlier[split]}
    reserved.update(normalized(row['prompt']) for split in ('training', 'heldout') for row in prior[split])
    reserved.update(normalized(row['prompt']) for row in prior_transfer)
    reserved.update(normalized(row['question']) for row in [*controls, *previous_full, *generalization,
                                                             *earlier['assistant_suite']['cases']])

    transfer = spec['transfer']
    if not isinstance(transfer, list) or len(transfer) != 2:
        raise ValueError('The study requires two independent observation transfer cases.')
    transfer_ids = {row['id'] for row in prior_transfer} | {row['id'] for row in prior['heldout']}
    for row in transfer:
        if (not isinstance(row, dict) or set(row) != {'id', 'prompt', 'answer'}
                or not isinstance(row['id'], str) or not re.fullmatch(r'[a-z0-9_-]{1,40}', row['id'])
                or row['id'] in transfer_ids):
            raise ValueError('Invalid or duplicate observation transfer case.')
        queue.text(row['answer'], 120, 'Observation transfer answer')
        transfer_ids.add(row['id'])
    full_transfer = parameter_acceptance.suite(spec['full_transfer'])
    new_questions = [row['prompt'] for row in [*fresh['training'], *fresh['heldout'], *transfer]]
    new_questions.extend(row['question'] for row in full_transfer['cases'])
    for question in new_questions:
        key = normalized(queue.text(question, 2000, 'Observation language question'))
        if key in reserved:
            raise ValueError('Observation examples overlap training or reserved evaluations.')
        reserved.add(key)

    promoted = [row for row in prior_transfer if row['id'] in OBSERVATION_CHECKS]
    if len(promoted) != len(OBSERVATION_CHECKS):
        raise ValueError('The earlier explicit retention checks are missing.')
    old_checks, old_transfer = prior['heldout'][:6], prior['heldout'][6:]
    training = [*prior['training'], *fresh['training']]
    heldout = [*fresh['heldout'], *old_checks, *promoted]
    short_transfer = [*old_transfer, *(row for row in prior_transfer if row['id'] not in OBSERVATION_CHECKS), *transfer]
    short_transfer = [{key: row[key] for key in ('id', 'prompt', 'answer')} for row in short_transfer]
    if len(training) != 63 or len(heldout) != 16 or len(short_transfer) != 16:
        raise ValueError('Unexpected observation study or retention coverage.')
    targets = [dict(id=row['id'], group='target', question=row['prompt'], answer=row['answer']) for row in heldout]
    assistant_suite = parameter_acceptance.suite(dict(schema='full_assistant_cases_v1', cases=[*targets, *controls]))
    return dict(schema=observation_language_study.SCHEMA, author='Codex synthetic observation language contrasts',
                training=training, heldout=heldout, assistant_suite=assistant_suite,
                transfer_suite=dict(schema='base_checkpoint_transfer_v1', cases=short_transfer),
                full_transfer_suite=full_transfer,
                parent_study_sha256=hashlib.sha256(prior_bytes).hexdigest(),
                source_sha256=hashlib.sha256(source).hexdigest(),
                compiler_sha256=hashlib.sha256(Path(observation_language_study.__file__).read_bytes()).hexdigest())


def load_study(study):
    generators = {'decision-codes': decision_codes_data, 'observation-language': observation_language_data}
    if study in generators:
        data = generators[study]()
        return json.dumps(data, ensure_ascii=False, sort_keys=True).encode('utf-8'), data
    raw = study_bytes(study)
    return raw, specification(raw)


def prepare(name, study='epistemic'):
    if not re.fullmatch(r'[a-z0-9_-]{1,64}', name):
        raise ValueError('Use a simple new run name.')
    raw, data = load_study(study)
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
    if 'transfer_suite' in data:
        transfer_path = folder / 'short-transfer.json'
        batches.save(transfer_path, data['transfer_suite'])
        receipt['short_transfer'] = str(transfer_path)
    if 'full_transfer_suite' in data:
        transfer_path = folder / 'full-transfer.json'
        batches.save(transfer_path, data['full_transfer_suite'])
        receipt['full_transfer'] = str(transfer_path)
    batches.save(folder / 'study.json', data)
    batches.save(folder / 'preparation.json', receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', choices=('epistemic', 'prerequisite-contrast', 'decision-codes',
                                           'observation-language'), default='epistemic')
    parser.add_argument('--prepare', metavar='NEW_RUN')
    args = parser.parse_args()
    if args.prepare:
        print(json.dumps(prepare(args.prepare, args.study), ensure_ascii=False, indent=2))
    else:
        _, data = load_study(args.study)
        print(f"{len(data['training'])} synthetic training and {len(data['heldout'])} heldout cases validated; no database or GPU work started")


if __name__ == '__main__':
    main()
