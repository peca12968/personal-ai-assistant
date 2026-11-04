"""Re-qualify the explicitly supported local segmented release from its original artifacts."""
from contextlib import closing
import hashlib
import json
from pathlib import PureWindowsPath


from assistant import learned_models, parameter_acceptance as acceptance
from assistant.engine import render_prompt
from scripts.learning import idle_segments as segments

batches = segments.batches
ROOT = segments.ROOT
RELEASE = ROOT / 'data/learning/segment-training-128-001'
INDEX_HASH = '7785d934175a07fff8c4be26e063aba632d645a0a272152e23b96f00d9afb4cf'
read = segments.read


def require(condition):
    if not condition:
        raise ValueError('分段模型的來源、權重或驗收資料不符。')


def check_exits(folder, phases):
    for phase in phases:
        record = read(folder / (phase + '.exit.json'))
        pid = int((folder / (phase + '.started')).read_text(encoding='ascii'))
        require(pid > 0 and record['worker'] == pid and record['exit_confirmed'] is True
                and record['returncode'] == 0)


def short_scores(cases, rows):
    scores = {}
    for case, raw in zip(cases, rows, strict=True):
        require(case['id'] == raw['id'] and type(raw['ended_with_eos']) is bool
                and type(raw['tokens']) is int and raw['tokens'] > 0)
        scores[case['id']] = raw['ended_with_eos'] and raw['output'] == case['answer']
    return scores


def qualify():
    index_file = RELEASE / 'evidence-index.json'
    with index_file.open('rb') as stream:
        raw = stream.read(65537)
    require(len(raw) <= 65536 and hashlib.sha256(raw).hexdigest() == INDEX_HASH)
    index = json.loads(raw)
    for name, sha in index['evidence_sha256'].items():
        require(batches.file_hash(RELEASE.joinpath(*PureWindowsPath(name).parts)) == sha)

    series = segments.PARENT / segments.identifier(index['session'])
    plan = segments.stored_plan(series)
    require(segments.digest(plan) == index['session_plan_digest'])
    batch = segments.verify_plan(series, plan)
    with closing(segments.ledger(series, readonly=True)) as db:
        head = segments.head(db, series, plan)
    require(head and head['end'] == plan['training_steps'] and head['blob'] == index['blob']
            and segments.digest(head) == index['boundary'])
    segments.pin_blob(series, head['blob'])
    final = read(series / 'jobs' / head['job'] / (head['phase'] + '.json'))
    _, parent_weights = segments.gpu.parent_evidence(plan, batch)
    short = RELEASE / 'short-001'
    check_exits(short, ('baseline', 'restored', 'reload'))
    cases = batches.evaluation_cases(batch)
    old, restored, new = (read(short / (name + '.json')) for name in ('baseline', 'restored', 'reload'))
    require(restored == new and new['logits'] == final['logits_sha256']
            and new['weights'] == final['weights'] == index['trained_weights']
            and old['weights'] == parent_weights == index['parent_weights'] != new['weights']
            and batches.adapter_files(short / 'adapter') == new['adapter_files'] == index['adapter_files'])
    before, after = (short_scores(cases, report['rows']) for report in (old, new))
    require(sum(before[c['id']] for c in cases if c['group'] == 'guard') >= 6
            and all(after[c['id']] for c in cases if c['group'] != 'guard' or before[c['id']])
            and any(after[c['id']] and not before[c['id']] for c in cases if c['group'] in ('new', 'heldout')))

    full = RELEASE / 'full-001'
    check_exits(full, acceptance.PHASES)
    manifest = read(full / 'manifest.json', 4 * 1024 * 1024)
    require(manifest['sources'] == acceptance.sources() and manifest['policy'] == acceptance.POLICY
            and acceptance.suite(manifest['suite']) == manifest['suite'])
    parent = learned_models.select(plan['data_dir'], batch=batch['parent']['id'])
    candidate = manifest['selections']['candidate']
    expected = dict(schema='segmented_research_weights_v1', session=plan['session'], boundary=index['boundary'],
                    weights=new['weights'], parent=parent['fingerprint'],
                    short_report_sha256=batches.file_hash(short / 'review.json'))
    require(manifest['selections']['baseline'] == parent and candidate == {**expected, 'fingerprint': segments.digest(expected)})
    description = manifest['descriptions']['candidate']
    require(description['selection_fingerprint'] == candidate['fingerprint'])
    rows = []
    for phase in acceptance.PHASES:
        for cid, ctx in manifest['contexts'][phase].items():
            require(ctx['engine_selection'] == manifest['descriptions'][phase]
                    and render_prompt(ctx) == manifest['prompts'][phase][cid])
        rows.extend(read(full / (phase + '-responses.json')))
    reviewed = acceptance.assess(full, manifest, rows)
    require(reviewed['passed'] and reviewed == read(full / 'review.json'))
    return plan, batch, description, parent, new['weights']


def current(plan, batch, parent):
    batches.examples_current(learned_models.QueueReader(plan['data_dir']), batch)
    learned_models.check_context(parent)
