"""Evaluate a committed base LoRA checkpoint in isolated, native-idle GPU phases."""
import argparse
from contextlib import closing
import importlib.metadata
import os
from pathlib import Path
import random
import re
import sys
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from assistant import local_models
from scripts.learning import base_idle_segments as segments
from scripts.learning.batch_worker import encode, prompt_ids
from scripts.learning.experiment import adapter_hash, configure
from scripts.learning.protocol import EXPECTED_VERSIONS
from scripts.learning.runtime import gpu_lease


PARENT = ROOT / 'data/learning/base-checkpoint-assays'
TRANSFER = ROOT / 'evals/base_checkpoint_transfer.json'
PHASES = ('baseline', 'restored', 'reload')
batches, gpu = segments.batches, segments.gpu
read, save, digest = segments.read, gpu.save, segments.digest


def assay_folder(name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9_-]{1,64}', name):
        raise ValueError('請使用單純的研究名稱。')
    return PARENT / name


def cases_for(manifest, transfer):
    if (not isinstance(transfer, dict) or set(transfer) != {'schema', 'cases'}
            or transfer['schema'] != 'base_checkpoint_transfer_v1'
            or not isinstance(transfer['cases'], list) or not 1 <= len(transfer['cases']) <= 16):
        raise ValueError('轉移測驗格式不符。')
    def prompt_key(prompt):
        return ' '.join(unicodedata.normalize('NFKC', prompt).split())

    cases = batches.evaluation_cases(manifest)
    seen = {prompt_key(row['prompt']) for row in [*cases, *batches.training_rows(manifest)]}
    ids = set()
    for row in transfer['cases']:
        if (not isinstance(row, dict) or set(row) != {'id', 'prompt', 'answer'}
                or not isinstance(row['id'], str) or not re.fullmatch(r'[a-z0-9_-]{1,40}', row['id'])
                or row['id'] in ids):
            raise ValueError('轉移題編號或欄位不符。')
        prompt = batches.queue.text(row['prompt'], 2000, '轉移題')
        batches.queue.text(row['answer'], 120, '答案')
        key = prompt_key(prompt)
        if key in seen:
            raise ValueError('轉移題與既有訓練或驗收問題重複。')
        seen.add(key)
        ids.add(row['id'])
        cases.append({**row, 'id': 'transfer-' + row['id'], 'group': 'transfer'})
    return cases


def prepare(name, session, transfer_path):
    folder = assay_folder(name)
    source = segments.PARENT / segments.identifier(session)
    plan = segments.stored_plan(source)
    manifest = segments.verify_plan(source, plan)
    transfer = local_models.read_json(transfer_path, 64 * 1024)
    cases = cases_for(manifest, transfer)
    folder.mkdir(parents=True, exist_ok=False)
    protocol = dict(schema='base_checkpoint_assay_v1', session=session, plan_sha256=digest(plan),
                    manifest_sha256=digest(manifest), driver_sha256=batches.file_hash(Path(__file__)),
                    transfer=transfer, cases_sha256=digest(cases), automatic_activation=False)
    save(folder / 'protocol.json', protocol)
    with (folder / 'protocol.sha256').open('x', encoding='ascii') as stream:
        stream.write(digest(protocol))
    return protocol


def inputs(folder, receipt):
    protocol = read(folder / 'protocol.json')
    with (folder / 'protocol.sha256').open(encoding='ascii') as stream:
        if stream.read(65) != digest(protocol):
            raise ValueError('先前準備的驗收規約指紋不符。')
    series = segments.PARENT / segments.identifier(protocol['session'])
    plan = segments.stored_plan(series)
    manifest = segments.verify_plan(series, plan)
    cases = cases_for(manifest, protocol['transfer'])
    if (protocol['schema'] != 'base_checkpoint_assay_v1' or protocol['plan_sha256'] != digest(plan)
            or protocol['manifest_sha256'] != digest(manifest) or protocol['cases_sha256'] != digest(cases)
            or protocol['driver_sha256'] != batches.file_hash(Path(__file__))
            or protocol['automatic_activation'] is not False):
        raise ValueError('驗收規約或來源已變更。')
    with closing(segments.ledger(series, readonly=True)) as db:
        head = segments.head(db, series, plan)
    if not head or head['end'] != plan['training_steps'] or digest(head) != segments.checksum(receipt):
        raise ValueError('請指定已完成訓練的最後提交指紋。')
    record = read(series / 'jobs' / head['job'] / (head['phase'] + '.json'))
    return protocol, series, plan, manifest, cases, head, record


def grades(cases, rows):
    scores = {}
    for case, row in zip(cases, rows, strict=True):
        if (not isinstance(row, dict) or set(row) != {'id', 'output', 'tokens', 'ended_with_eos'}
                or row['id'] != case['id'] or not isinstance(row['output'], str) or len(row['output']) > 16000
                or type(row['tokens']) is not int or not 1 <= row['tokens'] <= batches.RECIPE['max_output_tokens']
                or type(row['ended_with_eos']) is not bool):
            raise ValueError('驗收回答格式或順序不符。')
        scores[case['id']] = row['ended_with_eos'] and row['output'] == case['answer']
    return scores


def assess(cases, reports, record, weights_on_disk):
    restored, reload = reports['restored'], reports['reload']
    scores = {phase: grades(cases, reports[phase]['rows']) for phase in PHASES}
    old, new = scores['baseline'], scores['reload']
    groups = {}
    for case in cases:
        key = case['id']
        group = groups.setdefault(case['group'], {'baseline': 0, 'reload': 0, 'count': 0})
        group['baseline'] += old[key]
        group['reload'] += new[key]
        group['count'] += 1
    checks = {
        'weights_changed': record['weights'] != record['binding']['initial_adapter_sha256'],
        'reload_weights_match': restored['weights'] == reload['weights'] == record['weights'],
        'reload_files_match': restored['adapter_files'] == reload['adapter_files'] == weights_on_disk,
        'checkpoint_logits_match': restored['logits'] == reload['logits'] == record['logits_sha256'],
        'reload_answers_match': restored['rows'] == reload['rows'],
        'checkpoint_short_answers_match': restored['rows'][:len(record['rows'])] == record['rows'],
        'baseline_guards_valid': groups['guard']['baseline'] >= 6,
        'confirmed_and_heldout_correct': all(new[c['id']] for c in cases if c['group'] in ('new', 'heldout')),
        'baseline_correct_cases_retained': all(new[key] for key in old if old[key]),
        'heldout_or_transfer_improved': any(new[c['id']] and not old[c['id']] for c in cases
                                            if c['group'] in ('heldout', 'transfer')),
    }
    return dict(passed=all(checks.values()), checks=checks, groups=groups, scores=scores,
                automatic_activation=False, scope='short_prompt_research')


def evaluate_rows(model, tokenizer, cases, folder, phase, check):
    import torch
    rows = []
    with torch.inference_mode():
        for case in cases:
            check()
            ids = prompt_ids(tokenizer, case['prompt'])
            tokens = torch.tensor([ids], device='cuda')
            output = model.generate(input_ids=tokens, attention_mask=torch.ones_like(tokens), do_sample=False,
                max_new_tokens=batches.RECIPE['max_output_tokens'], eos_token_id=tokenizer.eos_token_id,
                pad_token_id=tokenizer.eos_token_id, use_cache=True)
            tail = output[0, len(ids):]
            rows.append(dict(id=case['id'], output=tokenizer.decode(tail, skip_special_tokens=True),
                             tokens=len(tail), ended_with_eos=len(tail) > 0 and int(tail[-1]) == tokenizer.eos_token_id))
            save(folder / (phase + '-answers.partial.json'), rows)
    return rows


def worker(folder, phase, run_hash):
    run = read(folder / 'run.json')
    if phase not in PHASES or digest(run) != segments.checksum(run_hash):
        raise ValueError('驗收工作規約不符。')
    with (folder / (phase + '.started')).open('x', encoding='ascii') as marker:
        marker.write(str(os.getpid()))
    protocol, series, plan, manifest, cases, head, record = inputs(folder, run['receipt'])
    if run['protocol_sha256'] != digest(protocol):
        raise ValueError('驗收規約已變更。')
    segments.pin_blob(series, head['blob'])
    deadline = time.monotonic() + 360

    def check():
        gpu.input_gate(run['admitted'])
        if time.monotonic() >= deadline:
            raise TimeoutError('本段驗收已達六分鐘。')

    check()
    with gpu_lease():
        configure()
        if (os.environ.get('CUBLAS_WORKSPACE_CONFIG') != ':4096:8'
                or {name: importlib.metadata.version(name) for name in EXPECTED_VERSIONS} != EXPECTED_VERSIONS):
            raise ValueError('驗收環境版本不符。')
        import torch
        from peft import LoraConfig, PeftModel, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from torch.nn.attention import sdpa_kernel, SDPBackend

        if torch.cuda.device_count() != 1 or not torch.cuda.is_bf16_supported():
            raise ValueError('需要單一支援 BF16 的 CUDA GPU。')
        recipe = manifest['recipe']
        torch.set_num_threads(8)
        torch.manual_seed(recipe['seed'])
        torch.cuda.manual_seed_all(recipe['seed'])
        torch.use_deterministic_algorithms(True)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.cuda.set_per_process_memory_fraction(0.85)
        base, adapter = local_models.verify_assets(manifest['model'])
        if adapter is not None:
            raise ValueError('底座驗收不能隱式載入適配器。')
        tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
        for case in cases:
            encode(tokenizer, case)
        model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False,
            use_safetensors=True, torch_dtype=torch.bfloat16, device_map={'': 0}, attn_implementation='sdpa')
        target = folder / 'adapter'
        if phase == 'restored':
            model = get_peft_model(model, LoraConfig(r=recipe['rank'], lora_alpha=recipe['alpha'],
                lora_dropout=recipe['dropout'], target_modules=recipe['target_modules'], bias='none', task_type='CAUSAL_LM'))
            if adapter_hash(model) != record['binding']['initial_adapter_sha256']:
                raise ValueError('種子起始權重不符。')
            optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                                          lr=recipe['learning_rate'], weight_decay=0.0)
            _, _, history = gpu.ck.restore(series / 'checkpoints', head['blob'], record['binding'],
                                           model, optimizer, random.Random())
            if history != record['history'] or len(history) != head['end']:
                raise ValueError('重載後的訓練歷程不符。')
            del optimizer
            target.mkdir()
            model.save_pretrained(target, safe_serialization=True)
        elif phase == 'reload':
            if batches.adapter_files(target) != read(folder / 'restored.json')['adapter_files']:
                raise ValueError('匯出的適配器已變更。')
            model = PeftModel.from_pretrained(model, target, is_trainable=False, local_files_only=True)
        weights = None if phase == 'baseline' else adapter_hash(model)
        if phase != 'baseline' and weights != record['weights']:
            raise ValueError('驗收載入的權重與已提交工作不符。')
        model.eval()
        with sdpa_kernel(SDPBackend.MATH), torch.inference_mode():
            ids = torch.tensor([prompt_ids(tokenizer, manifest['checks'][0]['prompt'])], device='cuda')
            logits = model(input_ids=ids, use_cache=False).logits[0, -1].float()
            logits_sha = gpu.ck.digest(gpu.ck.tensor_bytes(logits))
            rows = evaluate_rows(model, tokenizer, cases, folder, phase, check)
        check()
        save(folder / (phase + '.json'), dict(phase=phase, run_sha256=run_hash, pid=os.getpid(), weights=weights,
             logits=logits_sha, rows=rows, adapter_files=batches.adapter_files(target) if phase != 'baseline' else None))


def collect(name, receipt):
    folder = assay_folder(name)
    with batches.owner(folder):
        if (folder / 'run.json').exists():
            raise ValueError('這份驗收已嘗試，請保留證據並另建規約。')
        protocol, series, plan, _, cases, head, record = inputs(folder, receipt)
        admitted = segments.idle.idle_sample()
        if admitted['idle_ms'] < plan['idle_ms']:
            raise ValueError('本次觀察尚未達到原生閒置門檻。')
        segments.pin_blob(series, head['blob'])
        run = dict(protocol_sha256=digest(protocol), receipt=receipt, admitted=admitted)
        save(folder / 'run.json', run)
        run_hash = digest(run)
        for phase in PHASES:
            deadline = time.monotonic() + 360

            def check():
                gpu.input_gate(admitted)
                if time.monotonic() >= deadline or read(folder / 'run.json') != run:
                    raise ValueError('驗收逾時或工作規約已變更。')
                if digest(inputs(folder, receipt)[0]) != run['protocol_sha256']:
                    raise ValueError('驗收規約已變更。')

            check()
            command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                       str(Path(__file__).resolve()), '_worker', name, phase, run_hash]
            gpu.run_owned(command, folder, phase, lambda: gpu.input_gate(admitted), check)
            check()
        reports = {phase: read(folder / (phase + '.json')) for phase in PHASES}
        for phase, report in reports.items():
            exited = read(folder / (phase + '.exit.json'))
            if (report['phase'] != phase or report['run_sha256'] != run_hash
                    or exited['exit_confirmed'] is not True or exited['returncode'] != 0
                    or type(report['pid']) is not int or report['pid'] <= 0 or exited['worker'] != report['pid']):
                raise ValueError('驗收回覆不屬於目前工作。')
        segments.pin_blob(series, head['blob'])
        review = assess(cases, reports, record, batches.adapter_files(folder / 'adapter'))
        review['run_sha256'] = run_hash
        review['report_sha256'] = {phase: batches.file_hash(folder / (phase + '.json')) for phase in PHASES}
        review['exit_sha256'] = {phase: batches.file_hash(folder / (phase + '.exit.json')) for phase in PHASES}
        check()
        save(folder / 'review.json', review)
        return review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='command', required=True)
    prep = modes.add_parser('prepare')
    prep.add_argument('--name', required=True)
    prep.add_argument('--session', required=True)
    prep.add_argument('--transfer-suite', type=Path, default=TRANSFER)
    run = modes.add_parser('run')
    run.add_argument('--name', required=True)
    run.add_argument('--receipt', required=True)
    process = modes.add_parser('_worker')
    process.add_argument('name')
    process.add_argument('phase', choices=PHASES)
    process.add_argument('run_hash')
    args = parser.parse_args()
    if args.command == 'prepare':
        print(digest(prepare(args.name, args.session, args.transfer_suite)))
    elif args.command == 'run':
        review = collect(args.name, args.receipt)
        print(batches.queue.encoded({'passed': review['passed'], 'groups': review['groups']}))
    else:
        worker(assay_folder(args.name), args.phase, args.run_hash)


if __name__ == '__main__':
    main()
