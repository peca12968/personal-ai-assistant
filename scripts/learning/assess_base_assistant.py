"""Compare a short-assayed base checkpoint through the ordinary Assistant, offline."""
import argparse
from contextlib import closing
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from types import SimpleNamespace
import unicodedata
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from assistant import learned_models, local_models, local_worker, parameter_acceptance as acceptance
from assistant.engine import Assistant, render_prompt
from assistant.store import Store
from scripts.learning import assess_base_checkpoint as short
from scripts.learning.experiment import configure
from scripts.learning.protocol import EXPECTED_VERSIONS

segments, batches, gpu = short.segments, short.batches, short.gpu
ROOT = short.ROOT
PARENT = ROOT / 'data/learning/base-assistant-assays'
SCHEMA = 'base_assistant_assay_v1'
CANDIDATE = 'base_assistant_research_selection_v1'
PHASES = acceptance.PHASES
digest, save = short.digest, short.save
read = local_models.read_json


def location(name):
    return PARENT / short.assay_folder(name).name


def sources():
    return {**acceptance.sources(), Path(__file__).relative_to(ROOT).as_posix(): batches.file_hash(Path(__file__))}


def evidence(name):
    directory = short.assay_folder(name)
    run = read(directory / 'run.json')
    protocol, series, plan, batch, cases, head, record = short.inputs(directory, run['receipt'])
    review_path = directory / 'review.json'
    review = read(review_path)
    if (run['protocol_sha256'] != digest(protocol)
            or segments.idle.validate_sample(run['admitted'])['idle_ms'] < plan['idle_ms']):
        raise ValueError('短題驗收的規約或閒置收據不符。')
    reports, report_hashes, exit_hashes = {}, {}, {}
    for phase in short.PHASES:
        report_path, exit_path = directory / (phase + '.json'), directory / (phase + '.exit.json')
        report, exited = read(report_path), read(exit_path)
        if (report['phase'] != phase or report['run_sha256'] != digest(run) or type(report['pid']) is not int
                or report['pid'] <= 0 or exited['worker'] != report['pid']
                or exited['exit_confirmed'] is not True or exited['returncode'] != 0):
            raise ValueError('短題工作程序證據不符。')
        reports[phase] = report
        report_hashes[phase], exit_hashes[phase] = batches.file_hash(report_path), batches.file_hash(exit_path)
    adapter = directory / 'adapter'
    reviewed = short.assess(cases, reports, record, batches.adapter_files(adapter))
    if not reviewed['passed'] or review != {**reviewed, 'run_sha256': digest(run),
                                            'report_sha256': report_hashes, 'exit_sha256': exit_hashes}:
        raise ValueError('短題驗收結果未通過或已變更。')
    base = local_models.select()
    if batch['model'] != base:
        raise ValueError('驗收必須使用同一份未微調底座。')
    candidate = dict(schema=CANDIDATE, base=base, session=protocol['session'], boundary=digest(head),
                     assay=name, review_sha256=batches.file_hash(review_path),
                     batch={'weights_sha256': record['weights']})
    return SimpleNamespace(selections={'baseline': base, 'candidate': {**candidate, 'fingerprint': digest(candidate)}},
                           series=series, head=head, idle_ms=plan['idle_ms'], adapter=adapter,
                           old_prompts={r['prompt'] for r in cases + batches.training_rows(batch)})


def description(selection):
    if selection.get('schema') != CANDIDATE:
        return learned_models.description(selection)
    return {**learned_models.description(selection['base']), 'research_adapter': True,
            'selection_fingerprint': selection['fingerprint'], 'parameter_version': 'segment-' + selection['session']}


def raw_assistant(store, engine):
    return Assistant(store, engine, allow_local_calculation=False, allow_local_transcription=False,
                     allow_local_evidence=False)


class Described:
    def __init__(self, selection):
        self.selection = selection

    def describe(self):
        return description(self.selection)


def prepare(name, assay, suite):
    state = evidence(assay)
    segments.pin_blob(state.series, state.head['blob'])
    suite = acceptance.suite(suite)
    def normalized(text):
        return ' '.join(unicodedata.normalize('NFKC', text).split())

    used = {normalized(prompt) for prompt in state.old_prompts}
    for case in suite['cases']:
        key = normalized(case['question'])
        if key in used:
            raise ValueError('完整助理題目與訓練、短題或其他驗收題重複。')
        used.add(key)
    folder = location(name)
    folder.mkdir(parents=True, exist_ok=False)
    source_hashes = sources()
    fixture = Store(folder / 'fixture.sqlite3')
    sessions = {}
    for case in suite['cases']:
        sid = sessions[case['id']] = fixture.new_session('acceptance ' + case['id'])
        for message in case['history']:
            fixture.add_message(sid, message['role'], message['content'])
    meta = dict(schema=SCHEMA, assay=assay, suite=suite, policy=acceptance.POLICY, sources=source_hashes,
                selections=state.selections, sessions=sessions, prompts={}, contexts={}, database_files={})
    for phase in PHASES:
        database = folder / (phase + '.sqlite3')
        with fixture.connect() as source, closing(sqlite3.connect(database)) as target:
            source.backup(target)
        assistant = raw_assistant(Store(database), Described(state.selections[phase]))
        contexts = {c['id']: assistant.prepare_context(sessions[c['id']], c['question'].strip())
                    for c in suite['cases']}
        meta['contexts'][phase] = contexts
        meta['prompts'][phase] = {key: render_prompt(ctx) for key, ctx in contexts.items()}
        meta['database_files'][phase] = batches.file_hash(database)
    for cid, baseline in meta['contexts']['baseline'].items():
        candidate = meta['contexts']['candidate'][cid]
        if baseline != {**candidate, 'engine_selection': baseline['engine_selection']}:
            raise ValueError('兩組的對話內容或參考資料不同。')
    for name, sha in source_hashes.items():
        target = folder / 'sources' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
        if batches.file_hash(target) != sha:
            raise ValueError('程式來源副本已變更。')
    if sources() != source_hashes:
        raise ValueError('準備期間的程式已變更。')
    save(folder / 'manifest.json', meta)
    (folder / 'manifest.sha256').write_text(digest(meta), encoding='ascii')
    return meta


def frozen(folder):
    meta = read(folder / 'manifest.json', 32 * 1024 * 1024)
    with (folder / 'manifest.sha256').open(encoding='ascii') as receipt:
        pinned = receipt.read(65)
    if (meta['schema'] != SCHEMA or digest(meta) != pinned or meta['sources'] != sources()
            or meta['policy'] != acceptance.POLICY or acceptance.suite(meta['suite']) != meta['suite']):
        raise ValueError('完整助理驗收規約已變更。')
    state = evidence(meta['assay'])
    if state.selections != meta['selections']:
        raise ValueError('驗收選用的模型證據已變更。')
    return meta, state


def wire(prompt, selection):
    return {'schema': 'local_inference_request_v1', 'prompt': prompt, 'selection': selection}


def preflight(folder):
    configure()
    from transformers import AutoTokenizer

    meta, state = frozen(folder)
    base, _ = local_models.verify_assets(state.selections['baseline'])
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
    counts = {}
    with patch.object(local_worker, 'learned_models', SimpleNamespace(description=description)):
        for phase in PHASES:
            counts[phase] = {}
            for cid, prompt in meta['prompts'][phase].items():
                request = wire(prompt, meta['selections'][phase])
                counts[phase][cid] = len(local_worker.encode_request(tokenizer, request, local_worker.request_messages(request)))
    if frozen(folder)[0] != meta:
        raise ValueError('預檢期間的驗收規約已變更。')
    with (folder / 'preflight.json').open('x', encoding='utf-8') as stream:
        json.dump({'manifest_sha256': digest(meta), 'input_tokens': counts}, stream, ensure_ascii=False)
    return counts


def worker(folder, phase, cid, run_hash):
    meta, state = frozen(folder)
    run = read(folder / 'run.json')
    if (phase not in PHASES or cid not in meta['sessions'] or digest(run) != run_hash
            or run['manifest_sha256'] != digest(meta)
            or run['preflight_sha256'] != batches.file_hash(folder / 'preflight.json')):
        raise ValueError('完整助理工作與預備規約不符。')
    gpu.input_gate(run['admitted'])
    case_dir = folder / phase / cid
    output = case_dir / 'worker'
    with (output / 'inference.started').open('x', encoding='ascii') as marker:
        marker.write(str(os.getpid()))
    payload = read(case_dir / 'request.json', 4 * 1024 * 1024)
    selection = meta['selections'][phase]
    if payload != {'prompt': meta['prompts'][phase][cid], 'selection': selection}:
        raise ValueError('工作程序收到的提示或參數與規約不同。')

    def validate(selected):
        gpu.input_gate(run['admitted'])
        if (selected != selection or frozen(folder)[0] != meta or read(folder / 'run.json') != run
                or run['preflight_sha256'] != batches.file_hash(folder / 'preflight.json')):
            raise ValueError('研究參數或完整提示的規約已變更。')

    def assets(selected):
        validate(selected)
        base, _ = local_models.verify_assets(state.selections['baseline'])
        return base, state.adapter if phase == 'candidate' else None

    # Only this owned research child uses the scoped selection provider. The
    # production worker verifies loaded adapter weights and uses the same inference path.
    provider = SimpleNamespace(SCHEMA=CANDIDATE, sqlite3=sqlite3, description=description,
                               validate=validate, verify_assets=assets)
    try:
        with patch.object(local_worker, 'learned_models', provider):
            result = local_worker.infer(wire(**payload))
        validate(selection)
    except local_worker.Failure as exc:
        save(output / 'result.json', {'schema': 'local_inference_result_v1', 'status': 'failed', 'code': exc.code})
        raise
    save(output / 'result.json', result)


class Observed(Described):
    def __init__(self, folder, phase, cid, meta, run, check):
        super().__init__(meta['selections'][phase])
        self.folder, self.phase, self.cid = folder, phase, cid
        self.prompt, self.run = meta['prompts'][phase][cid], run
        self.check = check

    def answer(self, prompt, cancel=None):
        if prompt != self.prompt:
            raise ValueError('實際助理提示與凍結規約不同。')
        path = self.folder / self.phase / self.cid
        output = path / 'worker'
        output.mkdir(parents=True, exist_ok=False)
        save(path / 'request.json', {'prompt': prompt, 'selection': self.selection})
        python = ROOT / 'data/learning/runtime' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        command = [str(python), '-I', str(Path(__file__).resolve()),
                   '_worker', self.folder.name, self.phase, self.cid, digest(self.run)]
        gpu.run_owned(command, output, 'inference', lambda: gpu.input_gate(self.run['admitted']), self.check)
        self.check()
        result = read(output / 'result.json')
        preflight = read(self.folder / 'preflight.json')
        if (result['selection_fingerprint'] != self.selection['fingerprint'] or result['status'] != 'succeeded'
                or result['input_tokens'] != preflight['input_tokens'][self.phase][self.cid]):
            raise ValueError('實際輸入或模型與預檢不符。')
        return result['answer']


def review(folder, meta, run, rows):
    preflight = folder / 'preflight.json'
    if (run['manifest_sha256'] != digest(meta)
            or run['preflight_sha256'] != batches.file_hash(preflight)
            or segments.idle.validate_sample(run['admitted'])['idle_ms'] < gpu.IDLE_MS):
        raise ValueError('保存的規約、預檢與工作收據不符。')
    result = acceptance.assess(folder, meta, rows)
    exit_hashes = {}
    counts = read(preflight)['input_tokens']
    for phase in PHASES:
        for cid in meta['sessions']:
            root = folder / phase / cid / 'worker'
            path = root / 'inference.exit.json'
            exit_record = read(path)
            with (root / 'inference.started').open(encoding='ascii') as stream:
                pid = int(stream.read(32))
            if (pid <= 0 or exit_record['worker'] != pid or exit_record['exit_confirmed'] is not True
                    or exit_record['returncode'] != 0 or read(root / 'result.json')['input_tokens'] != counts[phase][cid]):
                raise ValueError('保存的推論程序或實際輸入長度不符。')
            exit_hashes[path.relative_to(folder).as_posix()] = batches.file_hash(path)
    return {**result, 'run_sha256': digest(run), 'exit_files': exit_hashes,
            'responses_sha256': batches.file_hash(folder / 'responses.json')}


def compare(folder):
    with batches.owner(folder):
        meta, state = frozen(folder)
        if (folder / 'run.json').exists():
            raise ValueError('這份規約已執行，請保留結果。')
        prep = read(folder / 'preflight.json')
        ids = set(meta['sessions'])
        counts = prep['input_tokens']
        if (prep['manifest_sha256'] != digest(meta) or set(counts) != set(PHASES)
                or any(set(phase) != ids or any(type(t) is not int or not 0 < t <= local_models.MAX_INPUT_TOKENS
                       for t in phase.values()) for phase in counts.values())):
            raise ValueError('需要完整且未超限的預檢。')
        for phase in PHASES:
            if batches.file_hash(folder / (phase + '.sqlite3')) != meta['database_files'][phase]:
                raise ValueError('初始驗收會話已變更。')
        admitted = segments.idle.idle_sample()
        if admitted['idle_ms'] < state.idle_ms:
            raise ValueError('尚未達到原生閒置門檻。')
        segments.pin_blob(state.series, state.head['blob'])
        run = dict(manifest_sha256=digest(meta), admitted=admitted,
                   preflight_sha256=batches.file_hash(folder / 'preflight.json'))
        save(folder / 'run.json', run)
        rows, deadline = [], time.monotonic() + 1200

        def check():
            gpu.input_gate(admitted)
            if (time.monotonic() >= deadline or read(folder / 'run.json') != run or frozen(folder)[0] != meta
                    or batches.file_hash(folder / 'preflight.json') != run['preflight_sha256']):
                raise ValueError('完整助理工作逾時或規約已變更。')

        try:
            for phase in PHASES:
                store = Store(folder / (phase + '.sqlite3'))
                for case in meta['suite']['cases']:
                    check()
                    cid = case['id']
                    sid = meta['sessions'][cid]
                    app = raw_assistant(store, Observed(folder, phase, cid, meta, run, check))
                    if app.prepare_context(sid, case['question'].strip()) != meta['contexts'][phase][cid]:
                        raise ValueError('驗收會話背景已變更。')
                    row = dict(phase=phase, id=cid)
                    try:
                        reply = app.ask(sid, case['question'])
                        row.update(status='complete', answer=reply['content'], message_id=reply['id'], run_id=reply['run_id'])
                    except Exception as exc:
                        row.update(status='failed', error_type=type(exc).__name__, error=str(exc)[:2000])
                        raise
                    finally:
                        rows.append(row)
                        save(folder / 'responses.json', rows)
                    print(phase, cid, 'recorded', flush=True)
            check()
            result = review(folder, meta, run, rows)
            segments.pin_blob(state.series, state.head['blob'])
            check()
            save(folder / 'review.json', result)
            return result
        except BaseException as exc:
            save(folder / 'failure.json', {'type': type(exc).__name__, 'reason': str(exc)[:2000]})
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='command', required=True)
    prep = modes.add_parser('prepare')
    prep.add_argument('name')
    prep.add_argument('--assay', required=True)
    prep.add_argument('--suite', type=Path, required=True)
    for key in ('preflight', 'run', 'verify'):
        modes.add_parser(key).add_argument('name')
    child = modes.add_parser('_worker')
    child.add_argument('name')
    child.add_argument('phase', choices=PHASES)
    child.add_argument('cid')
    child.add_argument('run_hash')
    args = parser.parse_args()
    if args.command == 'prepare':
        print(digest(prepare(args.name, args.assay, read(args.suite))))
    elif args.command == 'preflight':
        print(preflight(location(args.name)))
    elif args.command == 'run':
        result = compare(location(args.name))
        print({'passed': result['passed'], 'checks': result['checks'], 'scores': result['scores']})
    elif args.command == 'verify':
        folder = location(args.name)
        meta, state = frozen(folder)
        if any(batches.file_hash(folder / 'sources' / name) != sha for name, sha in meta['sources'].items()):
            raise ValueError('驗收來源副本已變更。')
        segments.pin_blob(state.series, state.head['blob'])
        result = review(folder, meta, read(folder / 'run.json'), read(folder / 'responses.json', 8 * 1024 * 1024))
        if read(folder / 'review.json') != result:
            raise ValueError('保存的完整助理驗收與證據不符。')
        print({'passed': result['passed'], 'scores': result['scores']})
    else:
        worker(location(args.name), args.phase, args.cid, args.run_hash)


if __name__ == '__main__':
    main()
