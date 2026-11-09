"""Short, retention-gated paired replay from a verified adapter."""
import argparse
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from scripts.learning import continue_verified_adapter as prior
from scripts.learning import trace_adapter_updates as trace

SCHEMA = 'paired_adapter_replay_v1'
FIXTURE = ROOT / 'evals/paired_adapter_replay.json'
PARENT = ROOT / 'data/learning/paired-adapter-replays'
RATE = 0.000025
STEPS_PER_STAGE = 3
TRAIN_PHASES = ('train-1', 'train-2')
PHASES = ('parent', 'train-1', 'reload-1', 'train-2', 'reload-2')
KINDS = ('veto', 'deadline', 'json') * 2
REPLAY = (('press_ready', 'press_blocked', 'press_unchecked'),
          ('window_ready', 'window_blocked', 'window_unchecked'),
          ('json_single_true', 'json_single_false', 'json_single_null'),
          ('lift_ready', 'lift_blocked', 'lift_unchecked'),
          ('shelf_ready', 'shelf_blocked', 'shelf_unchecked'),
          ('json_nested_true', 'json_nested_false', 'json_nested_null'))
batches, gpu, short = prior.batches, prior.gpu, prior.short
digest, read, save = prior.digest, prior.read, prior.save


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def variants(scene, reverse):
    if scene['kind'] == 'json':
        if set(scene) != {'id', 'kind', 'name', 'key', 'value'} or not re.fullmatch('[a-z_]{2,24}', scene['key']):
            raise ValueError('陣列情境的欄位不符。')
        name = batches.queue.text(scene['name'], 40, '清單名稱')
        value = batches.queue.text(scene['value'], 40, '陣列值')
        observations = (f'當次清點確認，{name}共有零筆。',
                        f'當次清點確認，{name}只有一筆，該筆確實是一個空字串。',
                        f'當次清點確認，{name}只有一筆，原值為「{value}」。')
        output = f'只回一個緊湊 JSON 物件，唯一鍵為 {scene["key"]}，值是{name}的字串陣列。保留實際筆數。'
        prompts = [output + fact if reverse else fact + output for fact in observations]
        answers = [compact({scene['key']: items}) for items in ([], [''], [value])]
    else:
        if (set(scene) != {'id', 'kind', 'rule', 'observations', 'codes'}
                or not isinstance(scene['codes'], list) or len(scene['codes']) != 3
                or any(not isinstance(code, str) or not re.fullmatch('[A-Z][0-9]', code) for code in scene['codes'])
                or len(set(scene['codes'])) != 3
                or not isinstance(scene['observations'], list) or len(scene['observations']) != 3):
            raise ValueError('判斷情境須有三種觀察及不重複代碼。')
        rule = batches.queue.text(scene['rule'], 180, '規則')
        facts = [batches.queue.text(value, 120, '觀察') for value in scene['observations']]
        answers = scene['codes']
        labels = list(zip(('確定可行', '確定不可行', '尚無法判定'), answers, strict=True))
        if reverse:
            labels.reverse()
        output = '只輸出指定代碼。' + '，'.join(f'{meaning}回 {code}' for meaning, code in labels) + '。'
        prompts = [output + rule + fact if reverse else rule + fact + output for fact in facts]
    return [dict(id=f'{scene["id"]}-{i}', prompt=prompt, answer=answer)
            for i, (prompt, answer) in enumerate(zip(prompts, answers, strict=True), 1)]


def fixture():
    spec = read(FIXTURE)
    if set(spec) != {'schema', 'training', 'holdout'} or spec['schema'] != SCHEMA:
        raise ValueError('成組回放資料格式不符。')
    result, scene_ids, seen = {}, set(), set()
    for split in ('training', 'holdout'):
        scenes = spec[split]
        if not isinstance(scenes, list) or tuple(row['kind'] for row in scenes) != KINDS:
            raise ValueError('每組須涵蓋必要條件、期限和陣列。')
        groups = []
        for index, scene in enumerate(scenes):
            sid = scene['id']
            if not isinstance(sid, str) or not re.fullmatch('[a-z_]{2,25}', sid) or sid in scene_ids:
                raise ValueError('情境編號重複或無效。')
            scene_ids.add(sid)
            group = variants(scene, reverse=(index >= 3) != (split == 'holdout'))
            keys = {prior.normalized(row['prompt']) for row in group}
            if len(keys) != 3 or keys & seen:
                raise ValueError('配對問法或保留題重複。')
            seen |= keys
            groups.append(group)
        result[split] = groups
    return result


def location(name):
    return PARENT / prior.location(name).name


def sources():
    return {**trace.sources(), **{path.relative_to(ROOT).as_posix(): batches.file_hash(path)
                                  for path in (Path(__file__), FIXTURE)}}


def assemble(reference):
    study = trace.location(reference)
    protocol, original, parent, _ = trace.checked(study)
    review = trace.verify(reference)
    if not review['final_reproduced']:
        raise ValueError('分段對照沒有可用的原始父權重證據。')
    spec = fixture()
    old_training = original['training']
    previous = {row['id']: row for row in old_training if 'id' in row}
    sealed = {prior.normalized(row['question']) for row in read(prior.SEALED_FULL)['full_transfer']['cases']}
    seen = {prior.normalized(row['prompt']) for row in [*old_training, *original['cases']]}
    new = {prior.normalized(row['prompt']) for groups in spec.values() for group in groups for row in group}
    if new & (seen | sealed):
        raise ValueError('新例與已用題目或完整助理保留題重疊。')
    training, schedule = [], []
    for group, replay_ids in zip(spec['training'], REPLAY, strict=True):
        start = len(training)
        training.extend(previous[cid] for cid in replay_ids)
        training.extend(group)
        schedule.append(list(range(start, len(training))))
    history = [row for row in original['cases'] if row['group'] != 'fit']
    cases = [*history, *(dict(row, group='new_holdout') for group in spec['holdout'] for row in group),
             *(dict(row, group='new_fit') for group in spec['training'] for row in group)]
    eval_keys = {prior.normalized(row['prompt']) for row in cases if row['group'] != 'new_fit'}
    if len({row['id'] for row in cases}) != len(cases) or eval_keys & {prior.normalized(row['prompt']) for row in training}:
        raise ValueError('保留題、測試編號或最佳化器資料衝突。')
    historical_ids = {case['id'] for case in history}
    reproduce = {row['id']: row for row in parent['rows'] if row['id'] in historical_ids}
    if len(reproduce) != 52:
        raise ValueError('須保存完整 52 道既有非訓練題。')
    return dict(schema=SCHEMA, sources=sources(), reference=reference, reference_protocol=digest(protocol),
                reference_review=batches.file_hash(study / 'review.json'), model=original['model'],
                parent=original['parent'], parent_weights=original['parent_weights'],
                parent_files=original['parent_files'], parent_logits=parent['logits'],
                cases=cases, reproduce=reproduce, training=training, schedule=schedule,
                protected=original['protected'], rate=RATE, seed=prior.SEED, reset_optimizer_each_stage=True)


def prepare(name, reference):
    plan = assemble(reference)
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
    if plan['schema'] != SCHEMA or pinned != digest(plan) or plan['sources'] != sources():
        raise ValueError('成組回放規約或程式來源已變更。')
    return plan


def checked(folder):
    plan = frozen(folder)
    if plan != assemble(plan['reference']):
        raise ValueError('成組回放的參考證據已變更。')
    return plan


def stage_schedule(plan, phase):
    start = (int(phase[-1]) - 1) * STEPS_PER_STAGE
    return plan['schedule'][start:start + STEPS_PER_STAGE]


def adapter_path(folder, phase):
    return folder / f'adapter-{phase[-1]}'


def load_source(folder, plan, phase, run_hash):
    if phase in ('parent', 'train-1'):
        return short.assay_folder(plan['parent']) / 'adapter', plan['parent_weights'], plan['parent_files']
    trained = 'train-1' if phase == 'train-2' else f'train-{phase[-1]}'
    report = prior.phase_record(folder, trained, run_hash)[0]
    return adapter_path(folder, trained), report['weights_after'], report['adapter_files']


def worker(folder, phase, run_hash):
    plan, run = frozen(folder), read(folder / 'run.json')
    if phase not in PHASES or digest(run) != run_hash or run['protocol_sha256'] != digest(plan):
        raise ValueError('成組回放的工作規約不符。')
    with (folder / (phase + '.started')).open('x', encoding='ascii') as marker:
        marker.write(str(os.getpid()))
    deadline = time.monotonic() + prior.LIMIT_SECONDS

    def check():
        gpu.input_gate(run['admitted'])
        if time.monotonic() >= deadline:
            raise TimeoutError('成組回放階段已達六分鐘上限。')

    check()
    with prior.gpu_lease():
        prior.configure()
        environment = {name: importlib.metadata.version(name) for name in prior.EXPECTED_VERSIONS}
        if environment != prior.EXPECTED_VERSIONS or os.environ.get('CUBLAS_WORKSPACE_CONFIG') != ':4096:8':
            raise ValueError('成組回放的執行環境不符。')
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
        base, implicit_adapter = prior.local_models.verify_assets(plan['model'])
        if implicit_adapter is not None:
            raise ValueError('底座不可隱式載入其他適配器。')
        tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
        for case in plan['cases']:
            prior.encode(tokenizer, case)
        origin, expected, files = load_source(folder, plan, phase, run_hash)
        if batches.adapter_files(origin) != files:
            raise ValueError('成組回放起始適配器已變更。')
        training = phase in TRAIN_PHASES
        model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False,
            use_safetensors=True, torch_dtype=torch.bfloat16, device_map={'': 0}, attn_implementation='sdpa')
        model = PeftModel.from_pretrained(model, origin, is_trainable=training, local_files_only=True)
        before = prior.adapter_hash(model)
        if before != expected:
            raise ValueError('成組回放實際載入的權重不符。')
        history = []
        with sdpa_kernel(SDPBackend.MATH):
            if training:
                recipe = dict(training=plan['training'], schedule=stage_schedule(plan, phase), learning_rate=plan['rate'])
                history = prior.train(model, tokenizer, recipe, check)
                check()
                origin = adapter_path(folder, phase)
                origin.mkdir()
                model.save_pretrained(origin, safe_serialization=True)
            model.eval()
            inputs = torch.tensor([prior.prompt_ids(tokenizer, plan['cases'][0]['prompt'])], device='cuda')
            with torch.inference_mode():
                logits = model(input_ids=inputs, use_cache=False).logits[0, -1].float()
            rows = short.evaluate_rows(model, tokenizer, plan['cases'], folder, phase, check)
        check()
        save(folder / (phase + '.json'), dict(phase=phase, run_sha256=run_hash, pid=os.getpid(),
             weights_before=before, weights_after=prior.adapter_hash(model), logits=gpu.ck.digest(gpu.ck.tensor_bytes(logits)),
             history=history, environment=environment, gpu=torch.cuda.get_device_name(0),
             adapter_files=batches.adapter_files(origin), rows=rows))


def parent_scores(plan, report):
    if (report['weights_before'] != plan['parent_weights'] or report['weights_after'] != plan['parent_weights']
            or report['adapter_files'] != plan['parent_files'] or report['logits'] != plan['parent_logits']
            or report['environment'] != prior.EXPECTED_VERSIONS or report['history']):
        raise ValueError('父權重、環境或 logits 不符。')
    scores = short.grades(plan['cases'], report['rows'])
    found = {row['id']: row for row in report['rows']}
    if any(found[cid] != row for cid, row in plan['reproduce'].items()):
        raise ValueError('既有保留題的父版本原答未重現。')
    return scores


def stage_result(plan, trained, reloaded, before, previous, parent, files, number, expected_weights):
    history = trained['history']
    if (any(set(row) != {'samples', 'loss'} or type(row['loss']) is not float or not math.isfinite(row['loss'])
            for row in history) or [row['samples'] for row in history] != stage_schedule(plan, f'train-{number}')
            or reloaded['history'] or trained['weights_before'] != expected_weights
            or trained['weights_after'] == expected_weights
            or trained['weights_after'] != reloaded['weights_before'] or trained['weights_after'] != reloaded['weights_after']
            or trained['adapter_files'] != files or reloaded['adapter_files'] != files
            or trained['logits'] != reloaded['logits'] or trained['rows'] != reloaded['rows']
            or any(row['environment'] != prior.EXPECTED_VERSIONS or row['gpu'] != parent['gpu']
                   for row in (trained, reloaded))):
        raise ValueError('本段取樣、權重、原答、環境或獨立重載證據不符。')
    after = short.grades(plan['cases'], reloaded['rows'])
    protected = {case['id'] for case in plan['cases'] if case['group'] != 'new_fit'
                 and (before[case['id']] or previous[case['id']])}
    lost = sorted(cid for cid in protected if not after[cid])
    missing_history = sorted(cid for cid in plan['protected'] if not after[cid])
    gained = [case['id'] for case in plan['cases'] if case['group'] == 'new_holdout'
              and after[case['id']] and not before[case['id']]]
    groups = {}
    for case in plan['cases']:
        cid = case['id']
        group = groups.setdefault(case['group'], dict(count=0, parent=0, stage=0))
        group['count'] += 1
        group['parent'] += before[cid]
        group['stage'] += after[cid]
    return dict(updates=number * STEPS_PER_STAGE, groups=groups, lost=lost, missing_history=missing_history,
                gained_holdout=gained, advance=not lost, passed=not lost and not missing_history and bool(gained)), after


def stage_evidence(folder, plan, run_hash, parent, before, previous, number, expected_weights):
    train, reload = f'train-{number}', f'reload-{number}'
    trained, train_sha, train_exit = prior.phase_record(folder, train, run_hash)
    reloaded, reload_sha, reload_exit = prior.phase_record(folder, reload, run_hash)
    files = batches.adapter_files(adapter_path(folder, train))
    outcome, after = stage_result(plan, trained, reloaded, before, previous, parent, files, number, expected_weights)
    outcome.update(reports={train: train_sha, reload: reload_sha}, exits={train: train_exit, reload: reload_exit},
                   weights=trained['weights_after'])
    return outcome, after


def result(folder, plan, run, launch=None):
    run_hash = digest(run)
    if (run['protocol_sha256'] != digest(plan)
            or short.segments.idle.validate_sample(run['admitted'])['idle_ms'] < gpu.IDLE_MS):
        raise ValueError('成組回放的規約或原生閒置不符。')
    if launch:
        launch('parent')
    parent, parent_sha, parent_exit = prior.phase_record(folder, 'parent', run_hash)
    before = previous = parent_scores(plan, parent)
    expected = plan['parent_weights']
    stages = []
    for number in (1, 2):
        if launch:
            launch(f'train-{number}')
            launch(f'reload-{number}')
        outcome, previous = stage_evidence(folder, plan, run_hash, parent, before, previous, number, expected)
        stages.append(outcome)
        expected = outcome['weights']
        if not outcome['advance']:
            if number == 1 and (any(folder.glob('train-2*')) or any(folder.glob('reload-2*'))
                                or adapter_path(folder, 'train-2').exists()):
                raise ValueError('第一段失去保留能力後仍啟動第二段。')
            break
    return dict(scope='paired_replay_short_prompt_research', run_sha256=run_hash, parent_report=parent_sha,
                parent_exit=parent_exit, stages=stages, complete_schedule=len(stages) == 2,
                passed=len(stages) == 2 and stages[-1]['passed'], automatic_activation=False)


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

        def launch(phase):
            deadline = time.monotonic() + prior.LIMIT_SECONDS

            def check():
                gpu.input_gate(admitted)
                if time.monotonic() >= deadline or read(folder / 'run.json') != run or frozen(folder) != plan:
                    raise ValueError('成組回放逾時或來源已變更。')

            command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                       str(Path(__file__).resolve()), '_worker', name, phase, digest(run)]
            check()
            gpu.run_owned(command, folder, phase, lambda: gpu.input_gate(admitted), check)
            print(phase, 'recorded', flush=True)

        try:
            outcome = result(folder, plan, run, launch)
            if checked(folder) != plan or result(folder, plan, run) != outcome:
                raise ValueError('成組回放的規約或結果已變更。')
            save(folder / 'review.json', outcome)
            return outcome
        except BaseException as exc:
            save(folder / 'failure.json', dict(error=type(exc).__name__, reason=str(exc)[:1000]))
            raise


def verify(name):
    folder = location(name)
    outcome = result(folder, checked(folder), read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != outcome:
        raise ValueError('成組回放報告與重算證據不符。')
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='command', required=True)
    modes.add_parser('check')
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
    if args.command == 'check':
        print(json.dumps({split: sum(map(len, groups)) for split, groups in fixture().items()}))
    elif args.command == 'prepare':
        plan = prepare(args.name, args.reference)
        print(json.dumps(dict(protocol_sha256=digest(plan), updates=len(plan['schedule']), cases=len(plan['cases']))))
    elif args.command == '_worker':
        worker(location(args.name), args.phase, args.run_sha256)
    else:
        print(json.dumps((collect if args.command == 'run' else verify)(args.name), ensure_ascii=False))


if __name__ == '__main__':
    main()
