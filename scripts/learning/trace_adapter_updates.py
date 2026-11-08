"""Reproduce a verified adapter update and inspect predeclared intermediate weights."""
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

from scripts.learning import continue_verified_adapter as trial


PARENT = ROOT / 'data/learning/adapter-update-traces'
SCHEMA = 'adapter_update_trace_v1'
POINTS = (7, 14, 21)
PHASES = ('updates', *(f'step-{step}' for step in POINTS))
batches, gpu = trial.batches, trial.gpu
digest, read, save = trial.digest, trial.read, trial.save


def location(name):
    return PARENT / trial.location(name).name


def sources():
    path = Path(__file__).resolve()
    return {**trial.source_hashes(), path.relative_to(ROOT).as_posix(): batches.file_hash(path)}


def reference(name):
    directory = trial.location(name)
    plan = trial.checked_plan(directory)
    verified = trial.verify(name)
    for check in ('parent_weights', 'environment', 'parent_reproduced', 'weights_changed', 'reload_matches'):
        if not verified['checks'][check]:
            raise ValueError('來源訓練缺少可重現的權重或原答證據。')
    if len(plan['schedule']) != POINTS[-1]:
        raise ValueError('來源步數與預先指定的檢查點不符。')
    run_hash = digest(read(directory / 'run.json'))
    parent = trial.phase_record(directory, 'parent', run_hash)[0]
    trained = trial.phase_record(directory, 'trained', run_hash)[0]
    protocol = dict(schema=SCHEMA, sources=sources(), reference=name, reference_protocol=digest(plan),
                    reference_review=batches.file_hash(directory / 'review.json'), points=list(POINTS),
                    final_weights=trained['weights_after'])
    return protocol, plan, parent, trained


def prepare(name, source):
    protocol = reference(source)[0]
    folder = location(name)
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / 'protocol.json', protocol)
    with (folder / 'protocol.sha256').open('x', encoding='ascii') as stream:
        stream.write(digest(protocol))
    return protocol


def frozen(folder):
    protocol = read(folder / 'protocol.json')
    with (folder / 'protocol.sha256').open(encoding='ascii') as stream:
        pinned = stream.read(65)
    if protocol['schema'] != SCHEMA or pinned != digest(protocol) or protocol['sources'] != sources():
        raise ValueError('分段觀察規約或程式來源已變更。')
    return protocol


def checked(folder):
    protocol = frozen(folder)
    inputs = reference(protocol['reference'])
    if inputs[0] != protocol:
        raise ValueError('原始接續訓練證據已變更。')
    return inputs


def record_updates(model, tokenizer, plan, folder, check):
    import torch

    examples = [trial.encode(tokenizer, row) for row in plan['training']]
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
    model.enable_input_require_grads()
    model.train()
    trainable = [parameter for parameter in model.parameters() if parameter.requires_grad]
    optimizer = torch.optim.AdamW(trainable, lr=plan['learning_rate'], weight_decay=0.0)
    history, snapshots = [], []
    for step, batch in enumerate(plan['schedule'], 1):
        check()
        optimizer.zero_grad(set_to_none=True)
        losses = []
        for index in batch:
            check()
            tokens, labels = examples[index]
            inputs = torch.tensor([tokens], device='cuda')
            loss = model(input_ids=inputs, attention_mask=torch.ones_like(inputs),
                         labels=torch.tensor([labels], device='cuda'), use_cache=False).loss
            if not torch.isfinite(loss):
                raise ValueError('接續訓練損失不是有限值。')
            losses.append(float(loss.detach()))
            (loss / len(batch)).backward()
        torch.nn.utils.clip_grad_norm_(trainable, 1.0, error_if_nonfinite=True)
        optimizer.step()
        history.append(dict(samples=batch, loss=sum(losses) / len(losses)))
        if step in POINTS:
            check()
            target = folder / f'adapter-{step}'
            target.mkdir()
            with torch.random.fork_rng(devices=[0]):
                model.save_pretrained(target, safe_serialization=True)
            snapshots.append(dict(step=step, weights=trial.adapter_hash(model), files=batches.adapter_files(target)))
            save(folder / 'snapshots.partial.json', snapshots)
    return history, snapshots


def snapshots_for(folder, run_hash, plan, original):
    report = trial.phase_record(folder, 'updates', run_hash)[0]
    snapshots = report['snapshots']
    if (report['weights_before'] != plan['parent_weights'] or report['history'] != original['history']
            or [row['step'] for row in snapshots] != list(POINTS)
            or snapshots[-1]['weights'] != original['weights_after']):
        raise ValueError('取樣、損失歷程或最終權重未重現原始訓練。')
    for snapshot in snapshots:
        if snapshot['files'] != batches.adapter_files(folder / f'adapter-{snapshot["step"]}'):
            raise ValueError('保存的中途適配器已變更。')
    return snapshots


def worker(folder, phase, run_hash):
    protocol, run = frozen(folder), read(folder / 'run.json')
    if phase not in PHASES or run_hash != digest(run) or run['protocol_sha256'] != digest(protocol):
        raise ValueError('分段觀察的工作規約不符。')
    with (folder / (phase + '.started')).open('x', encoding='ascii') as marker:
        marker.write(str(os.getpid()))
    source = trial.location(protocol['reference'])
    plan = trial.frozen(source)
    if digest(plan) != protocol['reference_protocol']:
        raise ValueError('原始訓練規約已變更。')
    deadline = time.monotonic() + trial.LIMIT_SECONDS

    def check():
        gpu.input_gate(run['admitted'])
        if time.monotonic() >= deadline:
            raise TimeoutError('分段觀察階段達到六分鐘上限。')

    check()
    with trial.gpu_lease():
        trial.configure()
        environment = {name: importlib.metadata.version(name) for name in trial.EXPECTED_VERSIONS}
        if environment != trial.EXPECTED_VERSIONS or os.environ.get('CUBLAS_WORKSPACE_CONFIG') != ':4096:8':
            raise ValueError('分段觀察的執行環境不符。')
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from torch.nn.attention import sdpa_kernel, SDPBackend

        if torch.cuda.device_count() != 1 or not torch.cuda.is_bf16_supported():
            raise ValueError('需要單一支援 BF16 的 CUDA GPU。')
        torch.set_num_threads(8)
        torch.manual_seed(trial.SEED)
        torch.cuda.manual_seed_all(trial.SEED)
        torch.use_deterministic_algorithms(True)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.cuda.set_per_process_memory_fraction(0.85)
        base, implicit_adapter = trial.local_models.verify_assets(plan['model'])
        if implicit_adapter is not None:
            raise ValueError('底座不可隱式載入其他適配器。')
        tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
        for case in plan['cases']:
            trial.encode(tokenizer, case)
        if phase == 'updates':
            adapter = trial.short.assay_folder(plan['parent']) / 'adapter'
            expected, files = plan['parent_weights'], plan['parent_files']
        else:
            step = int(phase.removeprefix('step-'))
            original = trial.phase_record(source, 'trained', digest(read(source / 'run.json')))[0]
            snapshot = next(row for row in snapshots_for(folder, run_hash, plan, original) if row['step'] == step)
            adapter = folder / f'adapter-{step}'
            expected, files = snapshot['weights'], snapshot['files']
        if batches.adapter_files(adapter) != files:
            raise ValueError('載入的適配器檔案已變更。')
        model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False,
            use_safetensors=True, torch_dtype=torch.bfloat16, device_map={'': 0}, attn_implementation='sdpa')
        model = PeftModel.from_pretrained(model, adapter, is_trainable=phase == 'updates', local_files_only=True)
        before = trial.adapter_hash(model)
        if before != expected:
            raise ValueError('載入的權重與預期指紋不符。')
        report = dict(phase=phase, run_sha256=run_hash, pid=os.getpid(), environment=environment,
                      gpu=torch.cuda.get_device_name(0), weights_before=before)
        with sdpa_kernel(SDPBackend.MATH):
            if phase == 'updates':
                history, snapshots = record_updates(model, tokenizer, plan, folder, check)
                report.update(history=history, snapshots=snapshots)
            else:
                model.eval()
                inputs = torch.tensor([trial.prompt_ids(tokenizer, plan['cases'][0]['prompt'])], device='cuda')
                with torch.inference_mode():
                    logits = model(input_ids=inputs, use_cache=False).logits[0, -1].float()
                report.update(logits=gpu.ck.digest(gpu.ck.tensor_bytes(logits)),
                              rows=trial.short.evaluate_rows(model, tokenizer, plan['cases'], folder, phase, check))
        check()
        save(folder / (phase + '.json'), report)


def summarize(plan, reports):
    cases = plan['cases']
    scores = {step: trial.short.grades(cases, record['rows']) for step, record in reports.items()}
    initial = scores[0]
    protected = set(plan['protected']) | {case['id'] for case in cases if case['group'] != 'fit' and initial[case['id']]}
    first_lost, first_gained, points = {}, {}, {}
    for step in sorted(scores):
        grade = scores[step]
        groups = {}
        for case in cases:
            cid, group = case['id'], case['group']
            counts = groups.setdefault(group, dict(exact=0, count=0))
            counts['exact'] += grade[cid]
            counts['count'] += 1
            if cid in protected and initial[cid] and not grade[cid]:
                first_lost.setdefault(cid, step)
            if group != 'fit' and not initial[cid] and grade[cid]:
                first_gained.setdefault(cid, step)
        points[str(step)] = dict(groups=groups, missing_protected=sorted(cid for cid in protected if not grade[cid]))
    return dict(points=points, protected=len(protected), first_lost=first_lost, first_gained=first_gained,
                scope='historical_trajectory_diagnostic', automatic_activation=False)


def recompute(folder, inputs, run):
    protocol, plan, parent, original = inputs
    if (run['protocol_sha256'] != digest(protocol)
            or trial.short.segments.idle.validate_sample(run['admitted'])['idle_ms'] < gpu.IDLE_MS):
        raise ValueError('分段觀察的規約或原生閒置不符。')
    run_hash = digest(run)
    snapshots = snapshots_for(folder, run_hash, plan, original)
    record_hashes, exits = {}, {}
    reports = {0: parent}
    expected = {f'step-{row["step"]}': row['weights'] for row in snapshots}
    expected['updates'] = plan['parent_weights']
    for phase in PHASES:
        record, record_hashes[phase], exits[phase] = trial.phase_record(folder, phase, run_hash)
        if (record['environment'] != parent['environment'] or record['gpu'] != parent['gpu']
                or record['weights_before'] != expected[phase]):
            raise ValueError('分段觀察的裝置或載入權重不符。')
        if phase != 'updates':
            reports[int(phase.removeprefix('step-'))] = record
    if any(reports[POINTS[-1]][key] != original[key] for key in ('rows', 'logits')):
        raise ValueError('第 21 步未重現原始權重的原答及 logits。')
    return {**summarize(plan, reports), 'final_reproduced': True, 'run_sha256': run_hash,
            'report_sha256': record_hashes, 'exit_sha256': exits}


def collect(name):
    folder = location(name)
    with batches.owner(folder):
        if (folder / 'run.json').exists():
            raise ValueError('此規約已嘗試，請保留原結果。')
        inputs = checked(folder)
        protocol, plan, _, original = inputs
        admitted = trial.short.segments.idle.idle_sample()
        if admitted['idle_ms'] < gpu.IDLE_MS:
            raise ValueError('尚未達到原生閒置門檻。')
        run = dict(protocol_sha256=digest(protocol), admitted=admitted)
        save(folder / 'run.json', run)
        run_hash = digest(run)
        try:
            for phase in PHASES:
                deadline = time.monotonic() + trial.LIMIT_SECONDS

                def check():
                    gpu.input_gate(admitted)
                    if time.monotonic() >= deadline or read(folder / 'run.json') != run or frozen(folder) != protocol:
                        raise ValueError('分段觀察逾時或來源已變更。')

                check()
                command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                           str(Path(__file__).resolve()), '_worker', name, phase, run_hash]
                gpu.run_owned(command, folder, phase, lambda: gpu.input_gate(admitted), check)
                trial.phase_record(folder, phase, run_hash)
                if phase == 'updates':
                    snapshots_for(folder, run_hash, plan, original)
                print(phase, 'recorded', flush=True)
            if checked(folder) != inputs:
                raise ValueError('來源驗收證據已變更。')
            result = recompute(folder, inputs, run)
            save(folder / 'review.json', result)
            return result
        except BaseException as exc:
            save(folder / 'failure.json', dict(error=type(exc).__name__, reason=str(exc)[:1000]))
            raise


def verify(name):
    folder = location(name)
    result = recompute(folder, checked(folder), read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != result:
        raise ValueError('分段觀察報告與重算證據不符。')
    return result


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
        print(json.dumps(dict(protocol_sha256=digest(protocol), points=protocol['points'])))
    elif args.command == '_worker':
        worker(location(args.name), args.phase, args.run_sha256)
    else:
        result = (collect if args.command == 'run' else verify)(args.name)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
