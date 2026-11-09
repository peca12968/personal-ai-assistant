"""Train on fixed facts paired with three answer-code assignments."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.learning import decision_code_study as logic
from scripts.learning import replay_batch_order as order

prior, short, gpu, batches = order.prior, order.short, order.gpu, order.batches
read, save, digest = order.read, order.save, order.digest
PARENT = ROOT / 'data/learning/replay-label-transfers'
FIXTURE = ROOT / 'evals/replay_label_transfer.json'
SCHEMA = 'replay_label_transfer_v1'
SOURCE_PHASE = 'deadline-equipment'
PHASES = ('parent', 'trained', 'reload')
PHASE_LIMITS = (360, 540)


def location(name):
    return PARENT / order.location(name).name


def fixture():
    spec = read(FIXTURE)
    if set(spec) != {'schema', 'training', 'holdout'} or spec['schema'] != SCHEMA:
        raise ValueError('代碼轉移題庫格式不符。')
    result, ids, used_codes = {}, set(), set()
    for split in ('training', 'holdout'):
        groups = []
        if not isinstance(spec[split], list) or len(spec[split]) != 2:
            raise ValueError('訓練及保留各須兩個獨立場景。')
        for scene in spec[split]:
            sid, codes = scene['id'], scene['codes']
            if (set(scene) != {'id', 'rule', 'codes', 'observations'}
                    or not isinstance(sid, str) or not re.fullmatch('[a-z_]{2,30}', sid) or sid in ids
                    or not isinstance(codes, list) or len(codes) != 3
                    or any(not isinstance(code, str) or not re.fullmatch('[A-Z][0-9]', code) for code in codes)
                    or len(set(codes)) != 3 or used_codes.intersection(codes)
                    or not isinstance(scene['observations'], list) or len(scene['observations']) != 3):
                raise ValueError('場景、觀察或代碼不符。')
            ids.add(sid)
            used_codes.update(codes)
            rule = batches.queue.text(scene['rule'], 160, '規則')
            verdicts = []
            for row in scene['observations']:
                if set(row) != {'states', 'fact'} or not re.fullmatch('[TFU]{2}', row['states']):
                    raise ValueError('觀察須明列兩項條件的三值狀態。')
                fact = batches.queue.text(row['fact'], 160, '觀察')
                verdict = logic.decide('all', row['states'])
                verdicts.append(verdict)
                group = []
                for rotation in range(3):
                    assigned = codes[rotation:] + codes[:rotation]
                    yes, no, unknown = assigned
                    if split == 'training':
                        prompt = rule + fact + f'只輸出代碼。確定能回 {yes}，確定不能回 {no}，未定回 {unknown}。'
                    else:
                        prompt = (f'請只交付一個代碼。資料尚不足用「{unknown}」，確定不可行用「{no}」，'
                                  f'確定可行用「{yes}」。' + rule + fact)
                    mapping = dict(zip(logic.VERDICTS, assigned, strict=True))
                    group.append(dict(id=f'{sid}-{row["states"]}-{rotation + 1}', contrast=f'{sid}-{row["states"]}',
                                      group='label_fit' if split == 'training' else 'label_holdout',
                                      prompt=prompt, answer=mapping[verdict], verdict=verdict, code_map=mapping))
                groups.append(group)
            if sorted(verdicts) != sorted(logic.VERDICTS):
                raise ValueError('每個場景須各有成立、不成立及待查。')
        result[split] = groups
    return result


def sources():
    paths = (Path(__file__).resolve(), Path(order.__file__).resolve(), Path(logic.__file__).resolve(), FIXTURE)
    return {**order.sources(), **{p.relative_to(ROOT).as_posix(): batches.file_hash(p) for p in paths}}


def assemble(reference, phase_seconds):
    if type(phase_seconds) is not int or phase_seconds not in PHASE_LIMITS:
        raise ValueError('每階段時限須為六或九分鐘。')
    source = order.location(reference)
    protocol = order.checked(source)
    outcome = order.verify(reference)
    if outcome['arms'][SOURCE_PHASE]['lost']:
        raise ValueError('來源版本須保留其父版本已答對的題目。')
    original = order.replay_plan(protocol)
    source_run = digest(read(source / 'run.json'))
    parent, report_sha, exit_sha = prior.phase_record(source, SOURCE_PHASE, source_run)
    expected = batches.adapter_files(source / ('adapter-' + SOURCE_PHASE))
    if parent['adapter_files'] != expected:
        raise ValueError('反序父適配器已變更。')
    data = fixture()
    historical = [*original['cases'], *protocol['contrasts']]
    old_training = original['training']
    previous_prompts = {prior.normalized(c['prompt']) for c in [*historical, *old_training]}
    sealed = {prior.normalized(c['question']) for c in read(prior.SEALED_FULL)['full_transfer']['cases']}
    new_prompts = [prior.normalized(c['prompt']) for groups in data.values() for group in groups for c in group]
    if len(set(new_prompts)) != 36 or set(new_prompts) & (previous_prompts | sealed):
        raise ValueError('新場景和既有資料或封存保留題重複。')
    training, schedule = [], []
    for old_indices, group in zip(original['schedule'], data['training'], strict=True):
        start = len(training)
        training.extend(old_training[i] for i in old_indices[:3])
        training.extend(group)
        schedule.append(list(range(start, len(training))))
    new_cases = [case for key in ('holdout', 'training') for group in data[key] for case in group]
    cases = [*historical, *new_cases]
    if len({c['id'] for c in cases}) != len(cases) or len(historical) != 94:
        raise ValueError('案例編號或既有驗收範圍不符。')
    nonfit = {prior.normalized(c['prompt']) for c in cases if c['group'] not in ('label_fit', 'new_fit')}
    if nonfit & {prior.normalized(row['prompt']) for row in training}:
        raise ValueError('保留題出現在最佳化器資料。')
    return dict(schema=SCHEMA, sources=sources(), reference=reference, reference_protocol=digest(protocol),
                reference_review=batches.file_hash(source / 'review.json'), parent_report=report_sha, parent_exit=exit_sha,
                parent_weights=parent['weights_after'], parent_files=expected, parent_logits=parent['logits'],
                parent_rows=parent['rows'], model=original['model'], rate=original['rate'], seed=original['seed'],
                training=training, schedule=schedule, cases=cases, phase_seconds=phase_seconds)


def prepare(name, reference, phase_seconds):
    plan = assemble(reference, phase_seconds)
    folder = location(name)
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / 'protocol.json', plan)
    (folder / 'protocol.sha256').write_text(digest(plan), encoding='ascii')
    return plan


def frozen(folder):
    plan = read(folder / 'protocol.json')
    if (plan['schema'] != SCHEMA or plan['sources'] != sources()
            or (folder / 'protocol.sha256').read_text(encoding='ascii') != digest(plan)):
        raise ValueError('代碼轉移規約或程式來源已變更。')
    return plan


def checked(folder):
    plan = frozen(folder)
    if plan != assemble(plan['reference'], plan['phase_seconds']):
        raise ValueError('代碼轉移的參考證據已變更。')
    return plan


def worker(folder, phase, run_hash):
    plan, run = frozen(folder), read(folder / 'run.json')
    if phase not in PHASES or run_hash != digest(run) or run['protocol_sha256'] != digest(plan):
        raise ValueError('代碼轉移工作規約不符。')
    with (folder / (phase + '.started')).open('x', encoding='ascii') as marker:
        marker.write(str(os.getpid()))
    deadline = time.monotonic() + plan['phase_seconds']

    def check():
        gpu.input_gate(run['admitted'])
        if time.monotonic() >= deadline:
            raise TimeoutError('代碼轉移已達本階段時限。')

    check()
    with prior.gpu_lease():
        prior.configure()
        environment = {key: importlib.metadata.version(key) for key in prior.EXPECTED_VERSIONS}
        if environment != prior.EXPECTED_VERSIONS or os.environ.get('CUBLAS_WORKSPACE_CONFIG') != ':4096:8':
            raise ValueError('代碼轉移環境不符。')
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
        target = folder / 'adapter'
        if phase == 'reload':
            trained = prior.phase_record(folder, 'trained', run_hash)[0]
            origin, expected, files = target, trained['weights_after'], trained['adapter_files']
        else:
            origin = order.location(plan['reference']) / ('adapter-' + SOURCE_PHASE)
            expected, files = plan['parent_weights'], plan['parent_files']
        if implicit is not None or batches.adapter_files(origin) != files:
            raise ValueError('代碼轉移的適配器來源不符。')
        tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
        for case in plan['cases']:
            prior.encode(tokenizer, case)
        model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False,
            use_safetensors=True, torch_dtype=torch.bfloat16, device_map={'': 0}, attn_implementation='sdpa')
        model = PeftModel.from_pretrained(model, origin, is_trainable=phase == 'trained', local_files_only=True)
        before = prior.adapter_hash(model)
        if before != expected:
            raise ValueError('代碼轉移載入的權重不符。')
        history = []
        with sdpa_kernel(SDPBackend.MATH):
            if phase == 'trained':
                recipe = dict(training=plan['training'], schedule=plan['schedule'], learning_rate=plan['rate'])
                history = prior.train(model, tokenizer, recipe, check)
                check()
                target.mkdir()
                model.save_pretrained(target, safe_serialization=True)
                origin = target
            model.eval()
            ids = torch.tensor([prior.prompt_ids(tokenizer, plan['cases'][0]['prompt'])], device='cuda')
            with torch.inference_mode():
                logits = model(input_ids=ids, use_cache=False).logits[0, -1].float()
            rows = short.evaluate_rows(model, tokenizer, plan['cases'], folder, phase, check)
        check()
        save(folder / (phase + '.json'), dict(phase=phase, run_sha256=run_hash, pid=os.getpid(),
             weights_before=before, weights_after=prior.adapter_hash(model), logits=gpu.ck.digest(gpu.ck.tensor_bytes(logits)),
             history=history, environment=environment, gpu=torch.cuda.get_device_name(0), rows=rows,
             adapter_files=batches.adapter_files(origin)))


def compare(cases, parent, candidate):
    before, after = (short.grades(cases, report['rows']) for report in (parent, candidate))
    groups, contrasts, target = {}, {}, {}
    lost = []
    for case, original, new in zip(cases, parent['rows'], candidate['rows'], strict=True):
        cid, group = case['id'], case['group']
        totals = groups.setdefault(group, dict(total=0, parent=0, candidate=0))
        totals['total'] += 1
        totals['parent'] += before[cid]
        totals['candidate'] += after[cid]
        if group not in ('new_fit', 'label_fit') and before[cid] and not after[cid]:
            lost.append(cid)
        if group == 'label_holdout':
            block = contrasts.setdefault(case['contrast'], dict(total=0, parent=0, candidate=0))
            block['total'] += 1
            block['parent'] += before[cid]
            block['candidate'] += after[cid]
            target[cid] = dict(expected=case['verdict'], answer=case['answer'], mapping=case['code_map'],
                               parent=original['output'], candidate=new['output'])
    gained = [key for key, item in contrasts.items() if item['parent'] < item['total'] == item['candidate']]
    return dict(groups=groups, contrasts=contrasts, target=target, lost=lost, gained_complete_contrasts=gained,
                passed=not lost and bool(gained), automatic_activation=False)


def result(folder, plan, run):
    if (run['protocol_sha256'] != digest(plan)
            or short.segments.idle.validate_sample(run['admitted'])['idle_ms'] < gpu.IDLE_MS):
        raise ValueError('代碼轉移規約或原生閒置不符。')
    reports, hashes = {}, {}
    for phase in PHASES:
        reports[phase], raw, exited = prior.phase_record(folder, phase, digest(run))
        hashes[phase] = dict(raw=raw, exit=exited)
    parent, trained, reloaded = (reports[p] for p in PHASES)
    files = batches.adapter_files(folder / 'adapter')
    order.check_arm(trained, plan['schedule'], parent, plan['parent_weights'], files)
    if (parent['history'] or reloaded['history'] or parent['environment'] != prior.EXPECTED_VERSIONS
            or reloaded['environment'] != parent['environment'] or reloaded['gpu'] != parent['gpu']
            or parent['weights_before'] != plan['parent_weights'] or parent['weights_after'] != plan['parent_weights']
            or parent['adapter_files'] != plan['parent_files'] or parent['logits'] != plan['parent_logits']
            or parent['rows'][:len(plan['parent_rows'])] != plan['parent_rows']
            or reloaded['weights_before'] != trained['weights_after'] or reloaded['weights_after'] != trained['weights_after']
            or reloaded['adapter_files'] != files or reloaded['logits'] != trained['logits']
            or reloaded['rows'] != trained['rows']):
        raise ValueError('父版本或獨立重載的權重、原答或 logits 未重現。')
    return dict(**compare(plan['cases'], parent, reloaded), scope=SCHEMA, updates=len(trained['history']), hashes=hashes)


def collect(name):
    folder = location(name)
    with batches.owner(folder):
        if (folder / 'run.json').exists():
            raise ValueError('此規約已嘗試，請保留原結果。')
        plan = checked(folder)
        admitted = short.segments.idle.idle_sample()
        if admitted['idle_ms'] < gpu.IDLE_MS:
            raise ValueError('尚未達到原生閒置門檻。')
        run = dict(protocol_sha256=digest(plan), admitted=admitted)
        save(folder / 'run.json', run)
        try:
            for phase in PHASES:
                deadline = time.monotonic() + plan['phase_seconds']

                def check():
                    gpu.input_gate(admitted)
                    if time.monotonic() >= deadline:
                        raise TimeoutError('代碼轉移已達本階段時限。')
                    if read(folder / 'run.json') != run or frozen(folder) != plan:
                        raise ValueError('代碼轉移來源已變更。')

                command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                           str(Path(__file__).resolve()), '_worker', name, phase, digest(run)]
                check()
                gpu.run_owned(command, folder, phase, lambda: gpu.input_gate(admitted), check)
                report = prior.phase_record(folder, phase, digest(run))[0]
                if phase == 'parent' and (report['rows'][:len(plan['parent_rows'])] != plan['parent_rows']
                                          or report['logits'] != plan['parent_logits']):
                    raise ValueError('父版本未重現原答，停止訓練。')
                print(phase, 'recorded', flush=True)
            if checked(folder) != plan:
                raise ValueError('代碼轉移來源已變更。')
            outcome = result(folder, plan, run)
            save(folder / 'review.json', outcome)
            return outcome
        except BaseException as exc:
            save(folder / 'failure.json', dict(error=type(exc).__name__, reason=str(exc)[:1000]))
            raise


def verify(name):
    folder = location(name)
    outcome = result(folder, checked(folder), read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != outcome:
        raise ValueError('代碼轉移報告與重算證據不符。')
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='command', required=True)
    modes.add_parser('check')
    prep = modes.add_parser('prepare')
    prep.add_argument('name')
    prep.add_argument('--reference', required=True)
    prep.add_argument('--phase-seconds', type=int, choices=PHASE_LIMITS, default=PHASE_LIMITS[0])
    for mode in ('run', 'verify'):
        modes.add_parser(mode).add_argument('name')
    child = modes.add_parser('_worker')
    child.add_argument('name')
    child.add_argument('phase', choices=PHASES)
    child.add_argument('run_sha256')
    args = parser.parse_args()
    if args.command == 'check':
        print(json.dumps({key: sum(map(len, groups)) for key, groups in fixture().items()}))
    elif args.command == 'prepare':
        plan = prepare(args.name, args.reference, args.phase_seconds)
        print(json.dumps(dict(protocol_sha256=digest(plan), cases=len(plan['cases']), updates=len(plan['schedule']))))
    elif args.command == '_worker':
        worker(location(args.name), args.phase, args.run_sha256)
    else:
        outcome = (collect if args.command == 'run' else verify)(args.name)
        print(json.dumps({key: value for key, value in outcome.items() if key != 'target'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
