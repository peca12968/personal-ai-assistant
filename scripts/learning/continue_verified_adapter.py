"""Research-only LoRA continuation with a verified parent and retention comparison."""
import argparse
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import time
import unicodedata


ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from assistant import learning_batches as batches, local_models
from scripts.learning import assess_base_checkpoint as short, review_base_checkpoint as review
from scripts.learning.batch_worker import encode, prompt_ids
from scripts.learning.experiment import adapter_hash, configure
from scripts.learning.protocol import EXPECTED_VERSIONS
from scripts.learning.runtime import gpu_lease


PARENT = ROOT / 'data/learning/continued-adapter-assays'
FIXTURE = ROOT / 'evals/continued_evidence_cases.json'
SEALED_FULL = ROOT / 'evals/prerequisite_observation_language.json'
SCHEMA = 'verified_adapter_continuation_v1'
PHASES = ('parent', 'trained', 'reload')
SEED = 20261109
RATE = 0.00005
LIMIT_SECONDS = 360
digest, read, save = short.digest, short.read, short.save
gpu = short.gpu


def normalized(prompt):
    return ' '.join(unicodedata.normalize('NFKC', prompt).split())


def fixture():
    data = read(FIXTURE)
    if (not isinstance(data, dict) or set(data) != {'schema', 'training', 'checks'}
            or data['schema'] != 'continued_evidence_cases_v1'):
        raise ValueError('接續學習的合成資料格式不符。')
    ids, questions = set(), set()
    for split, count in (('training', 21), ('checks', 12)):
        rows = data[split]
        if not isinstance(rows, list) or len(rows) != count:
            raise ValueError('接續學習的合成題數不符。')
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'id', 'kind', 'prompt', 'answer'}
                    or not isinstance(row['id'], str) or not re.fullmatch(r'[a-z0-9_]{1,36}', row['id'])
                    or row['id'] in ids or row['kind'] not in ('veto', 'deadline', 'scope', 'json')):
                raise ValueError('合成案例的編號或欄位不符。')
            prompt = normalized(batches.queue.text(row['prompt'], 2000, '合成問題'))
            answer = batches.queue.text(row['answer'], 120, '合成答案')
            if prompt in questions:
                raise ValueError('合成問題重複。')
            if row['kind'] == 'json':
                value = json.loads(answer)
                if not isinstance(value, dict) or json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                                                              allow_nan=False) != answer:
                    raise ValueError('JSON 答案須為完整緊湊物件。')
            ids.add(row['id'])
            questions.add(prompt)
    return data


def source_hashes():
    paths = (Path(__file__), FIXTURE, SEALED_FULL, Path(short.__file__), Path(review.__file__),
             Path(gpu.__file__), Path(gpu.ck.__file__), Path(short.segments.idle.__file__))
    return {**batches.sources(), **{path.relative_to(ROOT).as_posix(): batches.file_hash(path) for path in paths}}


def location(name):
    return PARENT / short.assay_folder(name).name


def reference(name):
    folder = short.assay_folder(name)
    verified = review.verify(folder)
    run = read(folder / 'run.json')
    _, _, _, manifest, _, _, _ = short.inputs(folder, run['receipt'])
    return verified, manifest, batches.file_hash(folder / 'review.json')


def schedule(replay_count, fresh_count):
    if replay_count != 3 * fresh_count or fresh_count != 21:
        raise ValueError('接續配方需要每步一筆新例及三筆重播。')
    rng = random.Random(SEED)
    replay, fresh = list(range(replay_count)), list(range(replay_count, replay_count + fresh_count))
    rng.shuffle(replay)
    rng.shuffle(fresh)
    return [replay[i * 3:i * 3 + 3] + [row] for i, row in enumerate(fresh)]


def assemble(parent_name, recent_name):
    if parent_name == recent_name:
        raise ValueError('父模型與近期參考須為兩個已驗收的研究。')
    parent, parent_batch, parent_hash = reference(parent_name)
    recent, recent_batch, recent_hash = reference(recent_name)
    if not parent[2]['passed'] or parent_batch['model'] != recent_batch['model']:
        raise ValueError('父模型須通過既有短題門檻，兩次研究須用同一個底座。')
    review.retention(parent, recent)
    data = fixture()
    sealed = {normalized(row['question']) for row in read(SEALED_FULL)['full_transfer']['cases']}
    earlier = {normalized(case['prompt']) for verified in (parent, recent) for case in verified[0]}
    fresh = {normalized(row['prompt']) for split in ('training', 'checks') for row in data[split]}
    if fresh & (earlier | sealed):
        raise ValueError('新例與既有研究或完整助理保留題重複。')

    cases, protected, reproduce, used = [], set(), {}, {}
    for group, verified in (('parent_history', parent), ('recent_history', recent)):
        old_cases, old_reports, scores = verified
        for case, raw in zip(old_cases, old_reports['reload']['rows'], strict=True):
            if case['group'] not in review.RETENTION_GROUPS:
                continue
            key = normalized(case['prompt'])
            previous = used.get(key)
            if previous is None:
                previous = {**case, 'id': f'{group}-{case["id"]}', 'group': group}
                used[key] = previous
                cases.append(previous)
            elif any(previous[field] != case[field] for field in ('prompt', 'answer')):
                raise ValueError('跨研究題目的原文或答案不同。')
            if scores['scores']['reload'][case['id']]:
                protected.add(previous['id'])
            if group == 'parent_history':
                reproduce[previous['id']] = {key: value for key, value in raw.items() if key != 'id'}
    if len(reproduce) != 32 or len(cases) != 40:
        raise ValueError('先前兩輪共 40 道非訓練題必須完整保留。')

    replay = [{key: row[key] for key in ('prompt', 'answer')} for row in recent_batch['items']]
    replay_keys = {normalized(row['prompt']) for row in replay}
    if len(replay_keys) != len(replay) or replay_keys & (used.keys() | sealed | fresh):
        raise ValueError('重播來源與保留題或新例重疊。')
    training = [*replay, *data['training']]
    for group, split in (('fresh', 'checks'), ('fit', 'training')):
        cases.extend(dict(id=f'{group}-{row["id"]}', group=group, prompt=row['prompt'], answer=row['answer'])
                     for row in data[split])
    parent_report = parent[1]['reload']
    return dict(schema=SCHEMA, sources=source_hashes(), model=parent_batch['model'],
                parent=parent_name, parent_review_sha256=parent_hash, parent_weights=parent_report['weights'],
                parent_files=parent_report['adapter_files'], recent=recent_name, recent_review_sha256=recent_hash,
                training=training, schedule=schedule(len(replay), len(data['training'])),
                learning_rate=RATE, cases=cases, protected=sorted(protected), reproduce=reproduce)


def prepare(name, parent_name, recent_name):
    plan = assemble(parent_name, recent_name)
    folder = location(name)
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / 'protocol.json', plan)
    with (folder / 'protocol.sha256').open('x', encoding='ascii') as stream:
        stream.write(digest(plan))
    return plan


def frozen(folder):
    plan = read(folder / 'protocol.json')
    with (folder / 'protocol.sha256').open(encoding='ascii') as stream:
        pinned = stream.read(65)
    if plan['schema'] != SCHEMA or pinned != digest(plan) or plan['sources'] != source_hashes():
        raise ValueError('接續訓練的來源或規約已變更。')
    return plan


def checked_plan(folder):
    plan = frozen(folder)
    if plan != assemble(plan['parent'], plan['recent']):
        raise ValueError('接續訓練的父模型或題目證據已變更。')
    return plan


def train(model, tokenizer, plan, check):
    import torch

    examples = [encode(tokenizer, row) for row in plan['training']]
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
    model.enable_input_require_grads()
    model.train()
    trainable = [parameter for parameter in model.parameters() if parameter.requires_grad]
    optimizer = torch.optim.AdamW(trainable, lr=plan['learning_rate'], weight_decay=0.0)
    history = []
    for batch in plan['schedule']:
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
    model.gradient_checkpointing_disable()
    return history


def worker(folder, phase, run_hash):
    plan, run = frozen(folder), read(folder / 'run.json')
    if phase not in PHASES or digest(run) != run_hash or run['protocol_sha256'] != digest(plan):
        raise ValueError('接續工作規約不符。')
    with (folder / (phase + '.started')).open('x', encoding='ascii') as marker:
        marker.write(str(os.getpid()))
    deadline = time.monotonic() + LIMIT_SECONDS

    def check():
        gpu.input_gate(run['admitted'])
        if time.monotonic() >= deadline:
            raise TimeoutError('接續訓練階段已達六分鐘上限。')

    check()
    with gpu_lease():
        configure()
        environment = {name: importlib.metadata.version(name) for name in EXPECTED_VERSIONS}
        if (os.environ.get('CUBLAS_WORKSPACE_CONFIG') != ':4096:8'
                or environment != EXPECTED_VERSIONS):
            raise ValueError('接續訓練執行環境不符。')
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from torch.nn.attention import sdpa_kernel, SDPBackend

        if torch.cuda.device_count() != 1 or not torch.cuda.is_bf16_supported():
            raise ValueError('需要單一支援 BF16 的 CUDA GPU。')
        torch.set_num_threads(8)
        torch.manual_seed(SEED)
        torch.cuda.manual_seed_all(SEED)
        torch.use_deterministic_algorithms(True)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.cuda.set_per_process_memory_fraction(0.85)
        base, implicit_adapter = local_models.verify_assets(plan['model'])
        if implicit_adapter is not None:
            raise ValueError('底座不可隱式載入其他適配器。')
        tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
        for case in plan['cases']:
            encode(tokenizer, case)
        target = folder / 'adapter'
        if phase == 'reload':
            earlier = phase_record(folder, 'trained', run_hash)[0]
            origin, expected, files = target, earlier['weights_after'], earlier['adapter_files']
        else:
            origin = short.assay_folder(plan['parent']) / 'adapter'
            expected, files = plan['parent_weights'], plan['parent_files']
        if batches.adapter_files(origin) != files:
            raise ValueError('接續來源適配器檔案已變更。')
        model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False,
            use_safetensors=True, torch_dtype=torch.bfloat16, device_map={'': 0}, attn_implementation='sdpa')
        model = PeftModel.from_pretrained(model, origin, is_trainable=phase == 'trained', local_files_only=True)
        before = adapter_hash(model)
        if before != expected:
            raise ValueError('載入的起始權重與接續來源不符。')
        history = []
        with sdpa_kernel(SDPBackend.MATH):
            if phase == 'trained':
                history = train(model, tokenizer, plan, check)
                check()
                target.mkdir()
                model.save_pretrained(target, safe_serialization=True)
            model.eval()
            inputs = torch.tensor([prompt_ids(tokenizer, plan['cases'][0]['prompt'])], device='cuda')
            with torch.inference_mode():
                logits = model(input_ids=inputs, use_cache=False).logits[0, -1].float()
                logits_sha = gpu.ck.digest(gpu.ck.tensor_bytes(logits))
            rows = short.evaluate_rows(model, tokenizer, plan['cases'], folder, phase, check)
        check()
        save(folder / (phase + '.json'), dict(phase=phase, run_sha256=run_hash, pid=os.getpid(),
             weights_before=before, weights_after=adapter_hash(model), logits=logits_sha, history=history,
             environment=environment, gpu=torch.cuda.get_device_name(0),
             adapter_files=batches.adapter_files(origin if phase == 'parent' else target), rows=rows))


def phase_record(folder, phase, run_hash):
    record_path, exit_path = folder / (phase + '.json'), folder / (phase + '.exit.json')
    report, report_sha = review.read_phase(record_path)
    exited, exit_sha = review.read_phase(exit_path)
    with (folder / (phase + '.started')).open(encoding='ascii') as stream:
        marker = stream.read(32)
    pid = report['pid']
    if (report['phase'] != phase or report['run_sha256'] != run_hash or type(pid) is not int or pid <= 0
            or marker != str(pid) or type(exited['worker']) is not int or exited['worker'] != pid
            or exited['exit_confirmed'] is not True or type(exited['returncode']) is not int or exited['returncode'] != 0):
        raise ValueError('接續工作程序的啟動、退出或規約不符。')
    return report, report_sha, exit_sha


def assess(plan, reports, files):
    cases = plan['cases']
    parent, trained, reload = (reports[phase] for phase in PHASES)
    scores = {phase: short.grades(cases, reports[phase]['rows']) for phase in PHASES}
    before, after = scores['parent'], scores['reload']
    observed = {row['id']: {key: value for key, value in row.items() if key != 'id'} for row in parent['rows']}
    history = trained['history']
    if (any(set(row) != {'samples', 'loss'} or type(row['loss']) is not float or not math.isfinite(row['loss'])
            for row in history) or [row['samples'] for row in history] != plan['schedule']
            or parent['history'] or reload['history']):
        raise ValueError('接續訓練的取樣或損失紀錄不符。')
    research = [case['id'] for case in cases if case['group'] != 'fit']
    protected = set(plan['protected']) | {cid for cid in research if before[cid]}
    regressed = sorted(cid for cid in protected if not after[cid])
    gained = [cid for cid in research if after[cid] and not before[cid]]
    checks = dict(parent_weights=parent['weights_before'] == parent['weights_after'] == trained['weights_before']
                  == plan['parent_weights'] and parent['adapter_files'] == plan['parent_files'],
                  environment=all(record['environment'] == EXPECTED_VERSIONS and record['gpu'] == parent['gpu']
                                  for record in reports.values()),
                  parent_reproduced=all(observed[cid] == row for cid, row in plan['reproduce'].items()),
                  weights_changed=trained['weights_before'] != trained['weights_after'],
                  reload_matches=trained['weights_after'] == reload['weights_before'] == reload['weights_after']
                  and trained['adapter_files'] == reload['adapter_files'] == files
                  and trained['logits'] == reload['logits'] and trained['rows'] == reload['rows'],
                  fit_correct=all(after[case['id']] for case in cases if case['group'] == 'fit'),
                  fresh_correct=all(after[case['id']] for case in cases if case['group'] == 'fresh'),
                  retained=not regressed, gained=bool(gained))
    groups = {}
    for case in cases:
        cid = case['id']
        group = groups.setdefault(case['group'], dict(count=0, parent=0, reload=0))
        group['count'] += 1
        group['parent'] += before[cid]
        group['reload'] += after[cid]
    return dict(passed=all(checks.values()), checks=checks, groups=groups, scores=scores, updates=len(history),
                protected=len(protected), regressed=regressed, gained=gained, automatic_activation=False)


def recompute(folder, plan, run):
    run_hash = digest(run)
    if (run['protocol_sha256'] != digest(plan)
            or short.segments.idle.validate_sample(run['admitted'])['idle_ms'] < gpu.IDLE_MS):
        raise ValueError('接續工作規約或閒置證據不符。')
    reports, report_hashes, exits = {}, {}, {}
    for phase in PHASES:
        reports[phase], report_hashes[phase], exits[phase] = phase_record(folder, phase, run_hash)
    result = assess(plan, reports, batches.adapter_files(folder / 'adapter'))
    return {**result, 'run_sha256': run_hash, 'report_sha256': report_hashes, 'exit_sha256': exits}


def collect(name):
    folder = location(name)
    with batches.owner(folder):
        if (folder / 'run.json').exists():
            raise ValueError('此規約已嘗試，請保留原結果。')
        plan = checked_plan(folder)
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
                    if time.monotonic() >= deadline or read(folder / 'run.json') != run or frozen(folder) != plan:
                        raise ValueError('接續工作逾時或規約已變更。')

                check()
                command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                           str(Path(__file__).resolve()), '_worker', name, phase, digest(run)]
                gpu.run_owned(command, folder, phase, lambda: gpu.input_gate(admitted), check)
                phase_record(folder, phase, digest(run))
                print(phase, 'recorded', flush=True)
            if checked_plan(folder) != plan:
                raise ValueError('接續訓練來源已變更。')
            result = recompute(folder, plan, run)
            save(folder / 'review.json', result)
            return result
        except BaseException as exc:
            save(folder / 'failure.json', dict(error=type(exc).__name__, reason=str(exc)[:1000]))
            raise


def verify(name):
    folder = location(name)
    plan = checked_plan(folder)
    result = recompute(folder, plan, read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != result:
        raise ValueError('保存的接續驗收結果與證據不符。')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='command', required=True)
    modes.add_parser('check')
    prep = modes.add_parser('prepare')
    prep.add_argument('name')
    prep.add_argument('--parent', required=True)
    prep.add_argument('--recent', required=True)
    for mode in ('run', 'verify'):
        modes.add_parser(mode).add_argument('name')
    child = modes.add_parser('_worker')
    child.add_argument('name')
    child.add_argument('phase', choices=PHASES)
    child.add_argument('run_sha256')
    args = parser.parse_args()
    if args.command == 'check':
        data = fixture()
        print(f'{len(data["training"])} new training cases and {len(data["checks"])} independent checks')
    elif args.command == 'prepare':
        plan = prepare(args.name, args.parent, args.recent)
        print(json.dumps(dict(protocol_sha256=digest(plan), updates=len(plan['schedule']),
                              cases=len(plan['cases']), protected=len(plan['protected']))))
    elif args.command == '_worker':
        worker(location(args.name), args.phase, args.run_sha256)
    else:
        result = (collect if args.command == 'run' else verify)(args.name)
        print(json.dumps({key: result[key] for key in ('passed', 'checks', 'groups', 'regressed', 'gained')},
                         ensure_ascii=False))


if __name__ == '__main__':
    main()
