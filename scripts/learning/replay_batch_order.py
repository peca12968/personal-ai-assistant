"""Compare a deadline batch alone and before the equipment batch."""
import argparse
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.learning import replay_prefix_probe as prefix

replay, prior, short, gpu, batches = prefix.replay, prefix.prior, prefix.short, prefix.gpu, prefix.batches
read, save, digest = prefix.read, prefix.save, prefix.digest
PARENT = ROOT / 'data/learning/replay-batch-orders'
SCHEMA = 'paired_replay_batch_order_v1'
ORDERS = {'deadline': (1,), 'deadline-equipment': (1, 0)}
REFERENCE_PHASES = {'parent': 'parent', 'equipment': 'prefix-1', 'equipment-deadline': 'prefix-2'}


def location(name):
    return PARENT / prefix.location(name).name


def sources():
    entries = (Path(__file__).resolve(), Path(prefix.__file__).resolve())
    return {**replay.sources(), **{path.relative_to(ROOT).as_posix(): batches.file_hash(path) for path in entries}}


def assemble(reference):
    folder = prefix.location(reference)
    protocol, plan = prefix.checked(folder)
    outcome = prefix.verify(reference)
    if outcome['first_lost'].get('fresh-wash_pending') != 2:
        raise ValueError('參考須重現第二次更新的待查退步。')
    if (len(plan['schedule']) != 6 or any(len(batch) != 6 for batch in plan['schedule'][:2])
            or set(plan['schedule'][0]) & set(plan['schedule'][1])):
        raise ValueError('參考的設備與期限批次不符。')
    schedules = {phase: [plan['schedule'][index] for index in order] for phase, order in ORDERS.items()}
    return dict(schema=SCHEMA, reference=reference, prefix_protocol=digest(protocol),
                prefix_review=batches.file_hash(folder / 'review.json'), sources=sources(),
                replay_reference=protocol['reference'], replay_protocol=digest(plan),
                contrasts=protocol['contrasts'], schedules=schedules)


def prepare(name, reference):
    protocol = assemble(reference)
    folder = location(name)
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / 'protocol.json', protocol)
    (folder / 'protocol.sha256').write_text(digest(protocol), encoding='ascii')
    return protocol


def frozen(folder):
    protocol = read(folder / 'protocol.json')
    if (protocol['schema'] != SCHEMA or protocol['sources'] != sources()
            or (folder / 'protocol.sha256').read_text(encoding='ascii') != digest(protocol)):
        raise ValueError('次序對照的規約或來源已變更。')
    return protocol


def checked(folder):
    protocol = frozen(folder)
    if protocol != assemble(protocol['reference']):
        raise ValueError('次序對照參考證據已變更。')
    return protocol


def replay_plan(protocol):
    plan = replay.frozen(replay.location(protocol['replay_reference']))
    if digest(plan) != protocol['replay_protocol']:
        raise ValueError('回放的最佳化器資料或父模型已變更。')
    return plan


def worker(folder, phase, run_hash):
    protocol, run = frozen(folder), read(folder / 'run.json')
    if phase not in ORDERS or run_hash != digest(run) or run['protocol_sha256'] != digest(protocol):
        raise ValueError('次序對照工作規約不符。')
    with (folder / (phase + '.started')).open('x', encoding='ascii') as marker:
        marker.write(str(os.getpid()))
    deadline = time.monotonic() + prior.LIMIT_SECONDS

    def check():
        gpu.input_gate(run['admitted'])
        if time.monotonic() >= deadline:
            raise TimeoutError('次序對照已達六分鐘時限。')

    check()
    plan = replay_plan(protocol)
    with prior.gpu_lease():
        prior.configure()
        environment = {key: importlib.metadata.version(key) for key in prior.EXPECTED_VERSIONS}
        if environment != prior.EXPECTED_VERSIONS or os.environ.get('CUBLAS_WORKSPACE_CONFIG') != ':4096:8':
            raise ValueError('次序對照環境不符。')
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
            raise ValueError('次序對照父權重來源不符。')
        tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
        cases = [*plan['cases'], *protocol['contrasts']]
        for case in cases:
            prior.encode(tokenizer, case)
        model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False,
            use_safetensors=True, torch_dtype=torch.bfloat16, device_map={'': 0}, attn_implementation='sdpa')
        model = PeftModel.from_pretrained(model, origin, is_trainable=True, local_files_only=True)
        before = prior.adapter_hash(model)
        if before != plan['parent_weights']:
            raise ValueError('次序對照實際載入的父權重不符。')
        target = folder / ('adapter-' + phase)
        recipe = dict(training=plan['training'], schedule=protocol['schedules'][phase], learning_rate=plan['rate'])
        with sdpa_kernel(SDPBackend.MATH):
            history = prior.train(model, tokenizer, recipe, check)
            check()
            target.mkdir()
            model.save_pretrained(target, safe_serialization=True)
            model.eval()
            ids = torch.tensor([prior.prompt_ids(tokenizer, cases[0]['prompt'])], device='cuda')
            with torch.inference_mode():
                logits = model(input_ids=ids, use_cache=False).logits[0, -1].float()
            rows = short.evaluate_rows(model, tokenizer, cases, folder, phase, check)
        check()
        save(folder / (phase + '.json'), dict(phase=phase, run_sha256=run_hash, pid=os.getpid(),
             weights_before=before, weights_after=prior.adapter_hash(model), logits=gpu.ck.digest(gpu.ck.tensor_bytes(logits)),
             history=history, environment=environment, gpu=torch.cuda.get_device_name(0), rows=rows,
             adapter_files=batches.adapter_files(target)))


def check_arm(report, schedule, parent, expected_weights, expected_files):
    history = report['history']
    if (report['environment'] != parent['environment'] or report['gpu'] != parent['gpu']
            or report['weights_before'] != expected_weights or report['weights_after'] == expected_weights
            or report['adapter_files'] != expected_files
            or any(set(row) != {'samples', 'loss'} or type(row['loss']) is not float or not math.isfinite(row['loss'])
                   for row in history)
            or [row['samples'] for row in history] != schedule):
        raise ValueError('次序對照的取樣、環境或權重證據不符。')


def compare(cases, reports):
    by_phase = {}
    before = short.grades(cases, reports['parent']['rows'])
    for name, report in reports.items():
        after = short.grades(cases, report['rows'])
        loss = [cid for cid in before if before[cid] and not after[cid]]
        gain = [cid for cid in before if not before[cid] and after[cid]]
        detail = prefix.summarize(cases, [report])['points']['0']
        by_phase[name] = dict(**detail, lost=loss, gained=gain)
    return by_phase


def result(folder, protocol, run):
    if (run['protocol_sha256'] != digest(protocol)
            or short.segments.idle.validate_sample(run['admitted'])['idle_ms'] < gpu.IDLE_MS):
        raise ValueError('次序對照規約或原生閒置不符。')
    plan = replay_plan(protocol)
    ref = prefix.location(protocol['reference'])
    ref_run = digest(read(ref / 'run.json'))
    reports, hashes = {}, {}
    for name, phase in REFERENCE_PHASES.items():
        reports[name], raw, exited = prior.phase_record(ref, phase, ref_run)
        hashes[name] = dict(raw=raw, exit=exited)
    for phase in ORDERS:
        report, raw, exited = prior.phase_record(folder, phase, digest(run))
        check_arm(report, protocol['schedules'][phase], reports['parent'], plan['parent_weights'],
                  batches.adapter_files(folder / ('adapter-' + phase)))
        reports[phase] = report
        hashes[phase] = dict(raw=raw, exit=exited)
    cases = [*plan['cases'], *protocol['contrasts']]
    return dict(scope=SCHEMA, arms=compare(cases, reports), hashes=hashes, automatic_activation=False)


def collect(name):
    folder = location(name)
    with batches.owner(folder):
        if (folder / 'run.json').exists():
            raise ValueError('此規約已嘗試，請保留原結果。')
        protocol = checked(folder)
        admitted = short.segments.idle.idle_sample()
        if admitted['idle_ms'] < gpu.IDLE_MS:
            raise ValueError('尚未達到原生閒置門檻。')
        run = dict(protocol_sha256=digest(protocol), admitted=admitted)
        save(folder / 'run.json', run)
        try:
            for phase in ORDERS:
                deadline = time.monotonic() + prior.LIMIT_SECONDS

                def check():
                    gpu.input_gate(admitted)
                    if time.monotonic() >= deadline or read(folder / 'run.json') != run or frozen(folder) != protocol:
                        raise ValueError('次序對照逾時或來源已變更。')

                command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                           str(Path(__file__).resolve()), '_worker', name, phase, digest(run)]
                check()
                gpu.run_owned(command, folder, phase, lambda: gpu.input_gate(admitted), check)
                prior.phase_record(folder, phase, digest(run))
                print(phase, 'recorded', flush=True)
            if checked(folder) != protocol:
                raise ValueError('次序對照來源已變更。')
            outcome = result(folder, protocol, run)
            save(folder / 'review.json', outcome)
            return outcome
        except BaseException as exc:
            save(folder / 'failure.json', dict(error=type(exc).__name__, reason=str(exc)[:1000]))
            raise


def verify(name):
    folder = location(name)
    outcome = result(folder, checked(folder), read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != outcome:
        raise ValueError('次序對照報告與重算證據不符。')
    return outcome


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
    child.add_argument('phase', choices=ORDERS)
    child.add_argument('run_sha256')
    args = parser.parse_args()
    if args.command == 'prepare':
        protocol = prepare(args.name, args.reference)
        print(json.dumps(dict(protocol_sha256=digest(protocol), arms=list(ORDERS))))
    elif args.command == '_worker':
        worker(location(args.name), args.phase, args.run_sha256)
    else:
        outcome = (collect if args.command == 'run' else verify)(args.name)
        print(json.dumps({name: dict(lost=row['lost'], gained=row['gained'],
                                     wash_pending=row['wash']['fresh-wash_pending'])
                          for name, row in outcome['arms'].items()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
