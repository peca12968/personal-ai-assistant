"""Separate fact judgments from code assignment using completed replay evidence."""
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

from scripts.learning import replay_label_transfer as transfer

prior, short, gpu, batches = transfer.prior, transfer.short, transfer.gpu, transfer.batches
read, save, digest = transfer.read, transfer.save, transfer.digest
PARENT = ROOT / 'data/learning/label-reasoning-diagnostics'
SCHEMA = 'label_reasoning_diagnostic_v1'
PHASES = ('parent', 'candidate')
LIMIT_SECONDS = 540
SEMANTIC = dict(supported='可行', refuted='不能', undetermined='待查')
DIRECTIVE = '只回「可行」、「不能」或「待查」其中一詞。'
RULE_HINT = ('依當次證據判斷。任一項確定不成立時回不能，兩項都確定成立時回可行。'
             '沒有確定不成立的條件、但還有未核對項目時回待查。舊輪紀錄不能當成當輪確認。')


def location(name):
    return PARENT / transfer.location(name).name


def build_cases(heldout, records):
    historical = {row['id']: row for row in records if row['group'] == 'label_holdout'}
    result, direct_ids = [], []
    for scene in heldout:
        for fact in scene['observations']:
            key = f'{scene["id"]}-{fact["states"]}'
            expected = transfer.logic.decide('all', fact['states'])
            source = scene['rule'] + fact['fact']
            for group, instruction in (('semantic', DIRECTIVE), ('semantic_rule', RULE_HINT + DIRECTIVE)):
                result.append(dict(id=f'{group}-{key}', group=group, fact=key, verdict=expected,
                                   prompt=source + instruction, answer=SEMANTIC[expected]))
            for rotation in range(1, 4):
                item = historical[f'{key}-{rotation}']
                if item['verdict'] != expected or not item['prompt'].endswith(source):
                    raise ValueError('診斷須維持原有的規則、事實與代碼對應。')
                mapping = item['code_map']
                text = '，'.join(f'{label}回 {mapping[state]}' for state, label in SEMANTIC.items())
                result.append(dict(id='mapping-' + item['id'], group='mapping', fact=key, verdict=expected,
                                   prompt=f'已核對的結論是「{SEMANTIC[expected]}」。{text}。只輸出一個代碼。',
                                   answer=mapping[expected], code_map=mapping))
                if rotation == 1:
                    result.append(dict(item, group='direct'))
                    direct_ids.append(item['id'])
    if len(result) != 36 or len({row['prompt'] for row in result}) != len(result):
        raise ValueError('分離診斷的題數或唯一性不符。')
    return result, direct_ids


def sources():
    path = Path(__file__).resolve()
    return {**transfer.sources(), path.relative_to(ROOT).as_posix(): batches.file_hash(path)}


def assemble(reference):
    source = transfer.location(reference)
    plan = transfer.checked(source)
    run = read(source / 'run.json')
    if (source / 'failure.json').exists() or transfer.result(source, plan, run) != read(source / 'review.json'):
        raise ValueError('參考的獨立重載與成績不符。')
    run_hash = digest(run)
    parents = {}
    cases, direct_ids = build_cases(read(transfer.FIXTURE)['holdout'], plan['cases'])
    for label, phase in (('parent', 'parent'), ('candidate', 'reload')):
        record, raw, exited = prior.phase_record(source, phase, run_hash)
        recorded = {row['id']: row for row in record['rows']}
        parents[label] = dict(weights=record['weights_after'], files=record['adapter_files'], logits=record['logits'],
                              raw_sha=raw, exit_sha=exited, gpu=record['gpu'], environment=record['environment'],
                              direct=[recorded[cid] for cid in direct_ids],
                              all_coded=[recorded[c['id']] for c in plan['cases'] if c['group'] == 'label_holdout'])
    return dict(schema=SCHEMA, reference=reference, reference_order=plan['reference'],
                reference_protocol=digest(plan), sources=sources(), models=parents, model=plan['model'],
                seed=plan['seed'], logits_prompt=plan['cases'][0]['prompt'], cases=cases)


def prepare(name, reference):
    plan = assemble(reference)
    folder = location(name)
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / 'protocol.json', plan)
    (folder / 'protocol.sha256').write_text(digest(plan), encoding='ascii')
    return plan


def frozen(folder):
    plan = read(folder / 'protocol.json')
    if (plan['schema'] != SCHEMA or plan['sources'] != sources()
            or (folder / 'protocol.sha256').read_text(encoding='ascii') != digest(plan)):
        raise ValueError('分離診斷規約或來源已變更。')
    return plan


def checked(folder):
    plan = frozen(folder)
    if plan != assemble(plan['reference']):
        raise ValueError('分離診斷的參考證據已變更。')
    return plan


def adapter(plan, phase):
    if phase == 'candidate':
        return transfer.location(plan['reference']) / 'adapter'
    return transfer.order.location(plan['reference_order']) / ('adapter-' + transfer.SOURCE_PHASE)


def worker(folder, phase, run_hash):
    plan, run = frozen(folder), read(folder / 'run.json')
    if phase not in PHASES or run_hash != digest(run) or run['protocol_sha256'] != digest(plan):
        raise ValueError('分離診斷工作規約不符。')
    with (folder / (phase + '.started')).open('x', encoding='ascii') as marker:
        marker.write(str(os.getpid()))
    deadline = time.monotonic() + LIMIT_SECONDS

    def check():
        gpu.input_gate(run['admitted'])
        if time.monotonic() >= deadline:
            raise TimeoutError('分離診斷已達本階段時限。')

    check()
    with prior.gpu_lease():
        prior.configure()
        environment = {key: importlib.metadata.version(key) for key in prior.EXPECTED_VERSIONS}
        reference = plan['models'][phase]
        if environment != reference['environment'] or os.environ.get('CUBLAS_WORKSPACE_CONFIG') != ':4096:8':
            raise ValueError('分離診斷環境不符。')
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
        origin = adapter(plan, phase)
        if implicit is not None or batches.adapter_files(origin) != reference['files']:
            raise ValueError('分離診斷的適配器來源不符。')
        tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
        for case in plan['cases']:
            prior.encode(tokenizer, case)
        model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False,
            use_safetensors=True, torch_dtype=torch.bfloat16, device_map={'': 0}, attn_implementation='sdpa')
        model = PeftModel.from_pretrained(model, origin, is_trainable=False, local_files_only=True)
        model.eval()
        weights = prior.adapter_hash(model)
        if weights != reference['weights']:
            raise ValueError('分離診斷載入的權重不符。')
        with sdpa_kernel(SDPBackend.MATH):
            ids = torch.tensor([prior.prompt_ids(tokenizer, plan['logits_prompt'])], device='cuda')
            with torch.inference_mode():
                logits = model(input_ids=ids, use_cache=False).logits[0, -1].float()
            rows = short.evaluate_rows(model, tokenizer, plan['cases'], folder, phase, check)
        check()
        save(folder / (phase + '.json'), dict(phase=phase, run_sha256=run_hash, pid=os.getpid(), weights=weights,
             logits=gpu.ck.digest(gpu.ck.tensor_bytes(logits)), environment=environment,
             gpu=torch.cuda.get_device_name(0), rows=rows))


def summarize(cases, rows):
    exact = short.grades(cases, rows)
    counts, detail, composed = {}, {}, {}
    reverse = {label: key for key, label in SEMANTIC.items()}
    for case, row in zip(cases, rows, strict=True):
        group = case['group']
        decoded = None
        if row['ended_with_eos']:
            if group in ('semantic', 'semantic_rule'):
                decoded = reverse.get(row['output'])
            else:
                decoded = next((meaning for meaning, code in case['code_map'].items() if code == row['output']), None)
        score = counts.setdefault(group, dict(exact=0, count=0))
        score['exact'] += exact[case['id']]
        score['count'] += 1
        detail[case['id']] = dict(expected=case['verdict'], raw=row['output'], decoded=decoded,
                                  exact=exact[case['id']])
        if group in ('semantic', 'semantic_rule'):
            via = composed.setdefault(group, dict(exact=0, count=0, missing=0))
            via['exact'] += 3 * exact[case['id']]
            via['count'] += 3
            via['missing'] += 3 * (decoded is None)
    return dict(groups=counts, details=detail, deterministic_mapping=composed)


def result(folder, plan, run):
    if (run['protocol_sha256'] != digest(plan)
            or short.segments.idle.validate_sample(run['admitted'])['idle_ms'] < gpu.IDLE_MS):
        raise ValueError('分離診斷規約或原生閒置不符。')
    output = {}
    for phase in PHASES:
        record, raw, exited = prior.phase_record(folder, phase, digest(run))
        reference = plan['models'][phase]
        direct = [row for c, row in zip(plan['cases'], record['rows'], strict=True) if c['group'] == 'direct']
        if (any(record[key] != reference[key] for key in ('gpu', 'environment', 'weights', 'logits'))
                or direct != reference['direct'] or batches.adapter_files(adapter(plan, phase)) != reference['files']):
            raise ValueError('分離診斷未重現原版作答或權重。')
        output[phase] = dict(**summarize(plan['cases'], record['rows']), direct_reference=reference['all_coded'],
                             report_sha=raw, exit_sha=exited)
    return dict(scope=SCHEMA, arms=output, automatic_activation=False)


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
                deadline = time.monotonic() + LIMIT_SECONDS

                def check():
                    gpu.input_gate(admitted)
                    if time.monotonic() >= deadline:
                        raise TimeoutError('分離診斷已達本階段時限。')
                    if read(folder / 'run.json') != run or frozen(folder) != plan:
                        raise ValueError('分離診斷來源已變更。')

                command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                           str(Path(__file__).resolve()), '_worker', name, phase, digest(run)]
                check()
                gpu.run_owned(command, folder, phase, lambda: gpu.input_gate(admitted), check)
                prior.phase_record(folder, phase, digest(run))
                print(phase, 'recorded', flush=True)
            if checked(folder) != plan:
                raise ValueError('分離診斷參考證據已變更。')
            output = result(folder, plan, run)
            save(folder / 'review.json', output)
            return output
        except BaseException as exc:
            save(folder / 'failure.json', dict(error=type(exc).__name__, reason=str(exc)[:1000]))
            raise


def verify(name):
    folder = location(name)
    report = result(folder, checked(folder), read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != report:
        raise ValueError('分離診斷報告與重算證據不符。')
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
        plan = prepare(args.name, args.reference)
        print(json.dumps(dict(protocol_sha256=digest(plan), cases=len(plan['cases']))))
    elif args.command == '_worker':
        worker(location(args.name), args.phase, args.run_sha256)
    else:
        outcome = (collect if args.command == 'run' else verify)(args.name)
        print(json.dumps({key: row['groups'] for key, row in outcome['arms'].items()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
