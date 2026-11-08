"""Collect a frozen full-assistant study one case pair at a time."""
import argparse
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from assistant.store import Store
from scripts.learning import assess_base_assistant as assay

SCHEMA = 'base_assistant_pair_study_v1'
PAIR_TIMEOUT_SECONDS = 1200
PHASES = assay.PHASES
digest, save, read = assay.digest, assay.save, assay.read
file_hash = assay.batches.file_hash


def master(folder):
    meta, state = assay.frozen(folder)
    if (folder / 'run.json').exists():
        raise ValueError('逐題研究需要尚未整批執行的原始驗收規約。')
    prep = read(folder / 'preflight.json')
    ids = set(meta['sessions'])
    counts = prep['input_tokens']
    if (prep['manifest_sha256'] != digest(meta) or set(counts) != set(PHASES)
            or any(set(tokens) != ids or any(type(n) is not int or not 0 < n <= assay.local_models.MAX_INPUT_TOKENS
                                             for n in tokens.values()) for tokens in counts.values())):
        raise ValueError('逐題研究需要完整且未超限的原始預檢。')
    if any(file_hash(folder / f'{phase}.sqlite3') != meta['database_files'][phase] for phase in PHASES):
        raise ValueError('逐題研究的原始會話已變更。')
    if any(file_hash(folder / 'sources' / name) != sha for name, sha in meta['sources'].items()):
        raise ValueError('逐題研究的原始來源副本已變更。')
    return meta, state


def protocol(folder, meta):
    return dict(schema=SCHEMA, name=folder.name, master_sha256=digest(meta),
                preflight_sha256=file_hash(folder / 'preflight.json'),
                collector_sha256=file_hash(Path(__file__)), pair_timeout_seconds=PAIR_TIMEOUT_SECONDS,
                order=[case['id'] for case in meta['suite']['cases']])


def study(folder):
    meta, state = master(folder)
    plan = read(folder / 'pair-study.json')
    if plan != protocol(folder, meta):
        raise ValueError('逐題研究規約已變更。')
    return meta, state, plan


def pair_folder(plan, case):
    identity = digest(dict(study=digest(plan), case=case['id']))
    return assay.location('pair-' + identity[:48])


def review_pair(path, meta, plan, case):
    if (read(path / 'manifest.json', 32 * 1024 * 1024) != meta
            or (path / 'manifest.sha256').read_text(encoding='ascii') != plan['master_sha256']):
        raise ValueError('逐題推論使用的規約副本已變更。')
    prep_path = path / 'preflight.json'
    run = read(path / 'run.json')
    if (run['manifest_sha256'] != plan['master_sha256']
            or file_hash(prep_path) != run['preflight_sha256'] or run['preflight_sha256'] != plan['preflight_sha256']
            or assay.segments.idle.validate_sample(run['admitted'])['idle_ms'] < assay.gpu.IDLE_MS):
        raise ValueError('逐題推論的預檢或原生閒置紀錄不符。')
    rows = read(path / 'responses.json', 8 * 1024 * 1024)
    view = {**meta, 'suite': {**meta['suite'], 'cases': [case]}}
    result = assay.acceptance.assess(path, view, rows)
    cid = case['id']
    counts = read(prep_path)['input_tokens']
    exits = {}
    for phase in PHASES:
        worker = path / phase / cid / 'worker'
        receipt_path = worker / 'inference.exit.json'
        receipt = read(receipt_path)
        pid = int((worker / 'inference.started').read_text(encoding='ascii'))
        if (pid <= 0 or receipt['worker'] != pid or receipt['exit_confirmed'] is not True
                or receipt['returncode'] != 0 or read(worker / 'result.json')['input_tokens'] != counts[phase][cid]):
            raise ValueError('逐題推論的程序退出或輸入長度不符。')
        exits[phase] = file_hash(receipt_path)
    return dict(schema=SCHEMA, case=cid, study_sha256=digest(plan), run_sha256=digest(run),
                responses_sha256=file_hash(path / 'responses.json'), exits=exits,
                artifact_files=result['artifact_files'],
                exact={phase: result['scores'][phase][cid] for phase in PHASES})


def run_pair(folder, meta, state, plan, case):
    admitted = assay.segments.idle.idle_sample()
    if admitted['idle_ms'] < max(state.idle_ms, assay.gpu.IDLE_MS):
        raise ValueError('尚未達到原生閒置門檻。')
    assay.segments.pin_blob(state.series, state.head['blob'])
    path = pair_folder(plan, case)
    path.mkdir(exist_ok=False)
    rows = []
    try:
        for name in ('manifest.json', 'manifest.sha256', 'preflight.json', *(f'{p}.sqlite3' for p in PHASES)):
            shutil.copyfile(folder / name, path / name)
        if any(file_hash(path / f'{phase}.sqlite3') != meta['database_files'][phase] for phase in PHASES):
            raise ValueError('逐題推論的會話副本與原始規約不符。')
        run = dict(manifest_sha256=plan['master_sha256'], preflight_sha256=plan['preflight_sha256'],
                   admitted=admitted)
        save(path / 'run.json', run)
        deadline = time.monotonic() + plan['pair_timeout_seconds']

        def check():
            assay.gpu.input_gate(admitted)
            if (time.monotonic() >= deadline or assay.frozen(path)[0] != meta
                    or read(path / 'run.json') != run
                    or read(folder / 'pair-study.json') != plan
                    or file_hash(path / 'preflight.json') != plan['preflight_sha256']
                    or file_hash(Path(__file__)) != plan['collector_sha256']):
                raise ValueError('逐題推論逾時或規約已變更。')

        cid = case['id']
        sid = meta['sessions'][cid]
        for phase in PHASES:
            check()
            store = Store(path / f'{phase}.sqlite3')
            app = assay.raw_assistant(store, assay.Observed(path, phase, cid, meta, run, check))
            if app.prepare_context(sid, case['question'].strip()) != meta['contexts'][phase][cid]:
                raise ValueError('逐題推論的會話背景已變更。')
            row = dict(phase=phase, id=cid)
            try:
                reply = app.ask(sid, case['question'])
                row.update(status='complete', answer=reply['content'],
                           message_id=reply['id'], run_id=reply['run_id'])
            except Exception as exc:
                row.update(status='failed', error_type=type(exc).__name__, error=str(exc)[:2000])
                raise
            finally:
                rows.append(row)
                save(path / 'responses.json', rows)
            print(phase, cid, 'recorded', flush=True)
        check()
        result = review_pair(path, meta, plan, case)
        if study(folder)[0] != meta:
            raise ValueError('逐題研究的原始規約已變更。')
        assay.segments.pin_blob(state.series, state.head['blob'])
        check()
        save(path / 'pair-review.json', result)
        return result['exact']
    except BaseException as exc:
        save(path / 'pair-failure.json', {'type': type(exc).__name__, 'reason': str(exc)[:2000]})
        raise


def report(meta, plan):
    cases = {}
    for case in meta['suite']['cases']:
        cid = case['id']
        path = pair_folder(plan, case)
        saved = path / 'pair-review.json'
        if saved.is_file():
            result = review_pair(path, meta, plan, case)
            if read(saved) != result or (path / 'pair-failure.json').exists():
                raise ValueError('逐題保存的比較與實際紀錄不符。')
            cases[cid] = dict(status='complete', exact=result['exact'])
        elif (path / 'pair-failure.json').is_file():
            cases[cid] = dict(status='failed')
        else:
            cases[cid] = dict(status='incomplete' if path.exists() else 'pending')
    completed = [item['exact'] for item in cases.values() if item['status'] == 'complete']
    groups = {}
    for group in ('target', 'control'):
        group_cases = [cases[case['id']] for case in meta['suite']['cases'] if case['group'] == group]
        paired = [item['exact'] for item in group_cases if item['status'] == 'complete']
        groups[group] = dict(planned=len(group_cases), completed=len(paired),
                             exact={phase: sum(item[phase] for item in paired) for phase in PHASES})
    return dict(study_sha256=digest(plan), planned_pairs=len(cases), completed_pairs=len(completed),
                exact={phase: sum(item[phase] for item in completed) for phase in PHASES},
                groups=groups, cases=cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for mode in ('prepare', 'run', 'report'):
        command = commands.add_parser(mode)
        command.add_argument('name')
        if mode == 'run':
            command.add_argument('--limit', type=int, default=1)
    args = parser.parse_args()
    folder = assay.location(args.name)
    with assay.batches.owner(folder):
        if args.command == 'prepare':
            plan_path = folder / 'pair-study.json'
            if plan_path.exists():
                raise ValueError('這份規約已有逐題研究。')
            plan = protocol(folder, master(folder)[0])
            save(plan_path, plan)
            print(digest(plan))
            return
        meta, state, plan = study(folder)
        if args.command == 'run':
            if not 1 <= args.limit <= len(plan['order']):
                parser.error('--limit must be between 1 and the number of cases')
            pending = [case for case in meta['suite']['cases'] if not pair_folder(plan, case).exists()]
            for case in pending[:args.limit]:
                print(case['id'], run_pair(folder, meta, state, plan, case), flush=True)
        print(report(meta, plan))


if __name__ == '__main__':
    main()
