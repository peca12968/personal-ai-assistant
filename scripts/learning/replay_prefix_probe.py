"""Reproduce a failed three-update stage at its first two prefixes."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.learning import paired_adapter_replay as replay

prior, batches, gpu, short = replay.prior, replay.batches, replay.gpu, replay.short
digest, read, save = replay.digest, replay.read, replay.save
PARENT = ROOT / 'data/learning/replay-prefix-probes'
SCHEMA = 'paired_replay_prefix_probe_v1'
PHASES = ('parent', 'prefix-1', 'prefix-2', 'prefix-3')
LABELS = ('R', 'B', 'X')
STATES = ('supported', 'refuted', 'undetermined')


def location(name):
    return PARENT / replay.location(name).name


def relabeled(cases):
    original = {case['id']: case for case in cases}
    phrase = 'R 代表能、B 代表不能、X 代表未定'
    result = []
    for labels in (('B', 'X', 'R'), ('X', 'R', 'B')):
        mapping = dict(zip(LABELS, labels, strict=True))
        replacement = f'{labels[0]} 代表能、{labels[1]} 代表不能、{labels[2]} 代表未定'
        for state in ('ready', 'blocked', 'pending'):
            case = original[f'fresh-wash_{state}']
            if case['prompt'].count(phrase) != 1 or case['answer'] not in mapping:
                raise ValueError('原場景的代碼映射不符。')
            result.append(dict(id=f'contrast-wash-{state}-{labels[0]}', group='remapped',
                               prompt=case['prompt'].replace(phrase, replacement), answer=mapping[case['answer']],
                               meanings=dict(zip(labels, STATES, strict=True))))
    return result


def frozen_plan(reference):
    source = replay.location(reference)
    plan, outcome = replay.checked(source), replay.verify(reference)
    if len(outcome['stages']) != 1 or outcome['stages'][0]['lost'] != ['fresh-wash_pending']:
        raise ValueError('來源須為已確認待查退步的三步回放。')
    extra = relabeled(plan['cases'])
    used = {prior.normalized(c['prompt']) for c in [*plan['training'], *plan['cases']]}
    keys = {prior.normalized(c['prompt']) for c in extra}
    if len(keys) != len(extra) or keys & used:
        raise ValueError('重新映射的題目與來源題目或訓練資料重疊。')
    path = Path(__file__).resolve()
    return dict(schema=SCHEMA, reference=reference, reference_protocol=digest(plan),
                reference_review=batches.file_hash(source / 'review.json'),
                sources={**replay.sources(), path.relative_to(ROOT).as_posix(): batches.file_hash(path)},
                contrasts=extra, original_cases=len(plan['cases'])), plan


def prepare(name, reference):
    protocol, _ = frozen_plan(reference)
    folder = location(name)
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / 'protocol.json', protocol)
    (folder / 'protocol.sha256').write_text(digest(protocol), encoding='ascii')
    return protocol


def checked(folder):
    protocol = read(folder / 'protocol.json')
    expected, plan = frozen_plan(protocol['reference'])
    if (protocol != expected or (folder / 'protocol.sha256').read_text(encoding='ascii') != digest(protocol)):
        raise ValueError('分步探查的來源或規約已變更。')
    return protocol, plan


def worker(folder, phase, run_hash):
    protocol, plan = checked(folder)
    run = read(folder / 'run.json')
    if phase not in PHASES or run_hash != digest(run) or run['protocol_sha256'] != digest(protocol):
        raise ValueError('分步探查工作規約不符。')
    with (folder / (phase + '.started')).open('x', encoding='ascii') as marker:
        marker.write(str(os.getpid()))
    deadline = time.monotonic() + prior.LIMIT_SECONDS

    def check():
        gpu.input_gate(run['admitted'])
        if time.monotonic() >= deadline:
            raise TimeoutError('分步探查已達六分鐘時限。')

    check()
    with prior.gpu_lease():
        prior.configure()
        environment = {key: importlib.metadata.version(key) for key in prior.EXPECTED_VERSIONS}
        if environment != prior.EXPECTED_VERSIONS or os.environ.get('CUBLAS_WORKSPACE_CONFIG') != ':4096:8':
            raise ValueError('分步探查環境不符。')
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from torch.nn.attention import sdpa_kernel, SDPBackend

        if torch.cuda.device_count() != 1 or not torch.cuda.is_bf16_supported():
            raise ValueError('需要單一支援 BF16 的 CUDA GPU。')
        torch.set_num_threads(8)
        torch.manual_seed(plan['seed'])
        torch.cuda.manual_seed_all(plan['seed'])
        torch.use_deterministic_algorithms(True)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.cuda.set_per_process_memory_fraction(0.85)
        base, implicit = prior.local_models.verify_assets(plan['model'])
        origin = short.assay_folder(plan['parent']) / 'adapter'
        if implicit is not None or batches.adapter_files(origin) != plan['parent_files']:
            raise ValueError('分步探查父權重來源不符。')
        tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
        cases = [*plan['cases'], *protocol['contrasts']]
        for case in cases:
            prior.encode(tokenizer, case)
        count = 0 if phase == 'parent' else int(phase[-1])
        model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False,
            use_safetensors=True, torch_dtype=torch.bfloat16, device_map={'': 0}, attn_implementation='sdpa')
        model = PeftModel.from_pretrained(model, origin, is_trainable=bool(count), local_files_only=True)
        before = prior.adapter_hash(model)
        if before != plan['parent_weights']:
            raise ValueError('分步探查載入的父權重不符。')
        with sdpa_kernel(SDPBackend.MATH):
            history = []
            if count:
                recipe = dict(training=plan['training'], schedule=plan['schedule'][:count], learning_rate=plan['rate'])
                history = prior.train(model, tokenizer, recipe, check)
                check()
                origin = folder / f'adapter-{count}'
                origin.mkdir()
                model.save_pretrained(origin, safe_serialization=True)
            model.eval()
            ids = torch.tensor([prior.prompt_ids(tokenizer, cases[0]['prompt'])], device='cuda')
            with torch.inference_mode():
                logits = model(input_ids=ids, use_cache=False).logits[0, -1].float()
            rows = short.evaluate_rows(model, tokenizer, cases, folder, phase, check)
        check()
        save(folder / (phase + '.json'), dict(phase=phase, run_sha256=run_hash, pid=os.getpid(),
             weights_before=before, weights_after=prior.adapter_hash(model), logits=gpu.ck.digest(gpu.ck.tensor_bytes(logits)),
             history=history, environment=environment, gpu=torch.cuda.get_device_name(0), rows=rows,
             adapter_files=batches.adapter_files(origin)))


def summarize(cases, reports):
    scores = [short.grades(cases, report['rows']) for report in reports]
    points, first_lost = {}, {}
    for step, (graded, report) in enumerate(zip(scores, reports, strict=True)):
        counts, wash = {}, {}
        for case, raw in zip(cases, report['rows'], strict=True):
            cid = case['id']
            group = counts.setdefault(case['group'], dict(exact=0, total=0))
            group['total'] += 1
            group['exact'] += graded[cid]
            if case['group'] != 'new_fit' and scores[0][cid] and not graded[cid]:
                first_lost.setdefault(cid, step)
            if cid.startswith(('fresh-wash_', 'contrast-wash-')):
                meanings = case.get('meanings', dict(zip(LABELS, STATES, strict=True)))
                wash[cid] = dict(raw=raw['output'], expected=case['answer'], exact=graded[cid],
                                 decoded=meanings.get(raw['output']) if raw['ended_with_eos'] else None)
        points[str(step)] = dict(groups=counts, wash=wash)
    return dict(points=points, first_lost=first_lost, automatic_activation=False)


def result(folder, protocol, plan, run):
    if (run['protocol_sha256'] != digest(protocol)
            or short.segments.idle.validate_sample(run['admitted'])['idle_ms'] < gpu.IDLE_MS):
        raise ValueError('分步探查的規約或原生閒置不符。')
    source = replay.location(protocol['reference'])
    original_run = digest(read(source / 'run.json'))
    original_parent = prior.phase_record(source, 'parent', original_run)[0]
    original_trained = prior.phase_record(source, 'train-1', original_run)[0]
    reports, hashes = [], {}
    for step, phase in enumerate(PHASES):
        report, raw_hash, exit_hash = prior.phase_record(folder, phase, digest(run))
        expected_files = plan['parent_files'] if step == 0 else batches.adapter_files(folder / f'adapter-{step}')
        if (report['environment'] != original_parent['environment'] or report['gpu'] != original_parent['gpu']
                or report['weights_before'] != plan['parent_weights'] or report['adapter_files'] != expected_files
                or report['history'] != original_trained['history'][:step]
                or (report['weights_after'] == plan['parent_weights']) != (step == 0)):
            raise ValueError('分步探查的取樣、起始權重或檔案不符。')
        if step in (0, 3):
            reference = original_parent if step == 0 else original_trained
            if (report['weights_after'] != reference['weights_after'] or report['logits'] != reference['logits']
                    or report['rows'][:protocol['original_cases']] != reference['rows']):
                raise ValueError('首末兩點未重現既有權重、logits 或原答。')
        reports.append(report)
        hashes[phase] = dict(report=raw_hash, exit=exit_hash)
    return dict(**summarize([*plan['cases'], *protocol['contrasts']], reports),
                hashes=hashes, endpoints_reproduced=True, scope=SCHEMA)


def collect(name):
    folder = location(name)
    with batches.owner(folder):
        if (folder / 'run.json').exists():
            raise ValueError('此規約已嘗試，請保留原結果。')
        protocol, plan = checked(folder)
        admitted = short.segments.idle.idle_sample()
        if admitted['idle_ms'] < gpu.IDLE_MS:
            raise ValueError('尚未達到原生閒置門檻。')
        run = dict(protocol_sha256=digest(protocol), admitted=admitted)
        save(folder / 'run.json', run)
        try:
            for phase in PHASES:
                deadline = time.monotonic() + prior.LIMIT_SECONDS

                def check():
                    gpu.input_gate(admitted)
                    if (time.monotonic() >= deadline or read(folder / 'run.json') != run
                            or read(folder / 'protocol.json') != protocol
                            or any(batches.file_hash(ROOT / path) != sha for path, sha in protocol['sources'].items())):
                        raise ValueError('分步探查逾時或來源已變更。')

                command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                           str(Path(__file__).resolve()), '_worker', name, phase, digest(run)]
                check()
                gpu.run_owned(command, folder, phase, lambda: gpu.input_gate(admitted), check)
                prior.phase_record(folder, phase, digest(run))
                print(phase, 'recorded', flush=True)
            if checked(folder) != (protocol, plan):
                raise ValueError('分步探查參考證據已變更。')
            report = result(folder, protocol, plan, run)
            save(folder / 'review.json', report)
            return report
        except BaseException as exc:
            save(folder / 'failure.json', dict(error=type(exc).__name__, reason=str(exc)[:1000]))
            raise


def verify(name):
    folder = location(name)
    report = result(folder, *checked(folder), read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != report:
        raise ValueError('分步探查報告與重算證據不符。')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='command', required=True)
    prep = modes.add_parser('prepare')
    prep.add_argument('name')
    prep.add_argument('--reference', required=True)
    for mode in ('run', 'verify'):
        modes.add_parser(mode).add_argument('name')
    child = modes.add_parser('_worker')
    child.add_argument('name')
    child.add_argument('phase', choices=PHASES)
    child.add_argument('run_sha256')
    args = parser.parse_args()
    if args.command == 'prepare':
        protocol = prepare(args.name, args.reference)
        print(json.dumps(dict(protocol_sha256=digest(protocol), contrasts=len(protocol['contrasts']))))
    elif args.command == '_worker':
        worker(location(args.name), args.phase, args.run_sha256)
    else:
        report = (collect if args.command == 'run' else verify)(args.name)
        print(json.dumps(dict(first_lost=report['first_lost'], endpoints_reproduced=report['endpoints_reproduced'],
                              points={step: row['groups'] for step, row in report['points'].items()}), ensure_ascii=False))


if __name__ == '__main__':
    main()
