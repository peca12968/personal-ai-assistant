"""Compare direct codes and split delivery through a complete Assistant context."""
import argparse
from contextlib import closing
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from assistant import local_models, local_worker, parameter_acceptance
from assistant.engine import Assistant, render_prompt_parts
from assistant.store import Store
from scripts.learning import diagnose_label_reasoning as diagnosis

prior, short, gpu, batches = diagnosis.prior, diagnosis.short, diagnosis.gpu, diagnosis.batches
read, save, digest = diagnosis.read, diagnosis.save, diagnosis.digest
PARENT = ROOT / 'data/learning/full-semantic-delivery'
FIXTURE = ROOT / 'evals/full_semantic_delivery.json'
SCHEMA = 'full_semantic_delivery_v1'
PHASES = ('direct', 'split')
LIMIT_SECONDS = 900


def location(name):
    return PARENT / diagnosis.location(name).name


def cases():
    fixture = read(FIXTURE)
    if set(fixture) != {'schema', 'fresh', 'historical', 'controls'} or fixture['schema'] != SCHEMA:
        raise ValueError('完整語意對照題庫格式不符。')
    if [len(fixture[key]) for key in ('fresh', 'historical', 'controls')] != [6, 2, 2]:
        raise ValueError('須保留六道新題、兩道歷史對照及兩道一般控制。')
    historical = {scene['id']: scene for scene in read(diagnosis.transfer.FIXTURE)['holdout']}
    compiled = []
    for group in ('fresh', 'historical', 'controls'):
        for row in fixture[group]:
            if group == 'controls':
                compiled.append(dict(id=row['id'], group=group, question=row['question'], answer=row['answer'],
                                     semantic_question=row['question'], history=[], mapping={}))
                continue
            pattern = row['states']
            if not re.fullmatch('[TFU]{2}', pattern):
                raise ValueError('案例須明列兩項條件的三值狀態。')
            if group == 'historical':
                scene = historical[row['scene']]
                body = scene['rule'] + next(item['fact'] for item in scene['observations'] if item['states'] == pattern)
                history = []
            else:
                body = row['body']
                history = [dict(role=role, content=content) for role, content in row['history']]
            codes = row['codes']
            if (len(codes) != 3 or len(set(codes)) != 3
                    or any(not re.fullmatch('[A-Z][0-9]{1,2}', code) for code in codes)):
                raise ValueError('三種結論須有唯一代碼。')
            verdict = diagnosis.transfer.logic.decide('all', pattern)
            mapping = dict(zip(diagnosis.SEMANTIC.values(), codes, strict=True))
            yes, no, unknown = codes
            code_instruction = f'只輸出指定代碼。確定可行回 {yes}，確定不可行回 {no}，尚無法判定回 {unknown}。'
            compiled.append(dict(id=row['id'], group=group, question=body + code_instruction,
                                 semantic_question=body + diagnosis.DIRECTIVE, mapping=mapping,
                                 answer=mapping[diagnosis.SEMANTIC[verdict]], verdict=verdict, history=history))
    standard = [dict(id=c['id'], group='control' if not c['mapping'] else 'target',
                     question=c['question'], answer=c['answer'], history=c['history']) for c in compiled]
    parameter_acceptance.suite(dict(schema='full_assistant_cases_v1', cases=standard))
    if len({c['question'] for c in compiled}) != 10:
        raise ValueError('完整語意對照題目重複。')
    return compiled


def sources():
    paths = (Path(__file__).resolve(), FIXTURE)
    return {**diagnosis.sources(), **parameter_acceptance.sources(),
            **{p.relative_to(ROOT).as_posix(): batches.file_hash(p) for p in paths}}


def reference(name):
    folder = diagnosis.location(name)
    plan = diagnosis.checked(folder)
    run = read(folder / 'run.json')
    if (folder / 'failure.json').exists() or diagnosis.result(folder, plan, run) != read(folder / 'review.json'):
        raise ValueError('先前的語意診斷未完成或證據不符。')
    weights = plan['models']['parent']['weights']
    selection = dict(schema='full_semantic_research_selection_v1', weights=weights, reference=name)
    description = dict(**local_models.description(plan['model']))
    description.update(parameter_version='semantic-study-parent-' + name, research_adapter=True,
                       selection_fingerprint=digest(selection))
    return plan, selection, description


def assistant(store, engine):
    return Assistant(store, engine, allow_local_calculation=False, allow_local_transcription=False,
                     allow_local_evidence=False)


def prompt_pair(context, case):
    if context['current_request'] != case['question']:
        raise ValueError('完整上下文的原問不符。')
    direct, raw = render_prompt_parts(context)
    semantic, changed = render_prompt_parts({**context, 'current_request': case['semantic_question']})
    if direct != semantic:
        raise ValueError('拆分更動了當前問題以外的提示。')
    return {'direct': direct + raw, 'split': semantic + changed}


def prepare(name, source):
    reference_plan, selection, description = reference(source)
    fixture = cases()
    old_transfer = diagnosis.transfer.frozen(diagnosis.transfer.location(reference_plan['reference']))
    previous = {prior.normalized(row['prompt']) for row in [*old_transfer['cases'], *old_transfer['training']]}
    held = {prior.normalized(row['question']) for row in read(prior.SEALED_FULL)['full_transfer']['cases']}
    if any(prior.normalized(c['question']) in (previous | held) for c in fixture if c['group'] == 'fresh'):
        raise ValueError('新場景與已用的最佳化器或驗收題目重疊。')
    folder = location(name)
    folder.mkdir(parents=True, exist_ok=False)
    source_hashes = sources()
    db = Store(folder / 'fixture.sqlite3')
    app = assistant(db, SimpleNamespace(describe=lambda: description))
    sessions, contexts, prompts = {}, {}, {}
    for case in fixture:
        cid = case['id']
        sessions[cid] = db.new_session('semantic delivery ' + cid)
        for row in case['history']:
            db.add_message(sessions[cid], row['role'], row['content'])
        context = app.prepare_context(sessions[cid], case['question'])
        contexts[cid] = context
        prompts[cid] = {key: digest(value) for key, value in prompt_pair(context, case).items()}
    if sources() != source_hashes:
        raise ValueError('準備期間的來源已變更。')
    plan = dict(schema=SCHEMA, sources=source_hashes, reference=source, reference_protocol=digest(reference_plan),
                model=reference_plan['model'], files=reference_plan['models']['parent']['files'],
                selection=selection, description=description, cases=fixture, sessions=sessions,
                contexts=contexts, prompts=prompts, fixture_sha=batches.file_hash(folder / 'fixture.sqlite3'))
    save(folder / 'protocol.json', plan)
    (folder / 'protocol.sha256').write_text(digest(plan), encoding='ascii')
    return plan


def frozen(folder):
    plan = read(folder / 'protocol.json', 32 * 1024 * 1024)
    if (plan['schema'] != SCHEMA or plan['sources'] != sources() or plan['cases'] != cases()
            or (folder / 'protocol.sha256').read_text(encoding='ascii') != digest(plan)
            or batches.file_hash(folder / 'fixture.sqlite3') != plan['fixture_sha']):
        raise ValueError('完整語意對照規約或來源已變更。')
    return plan


def checked(folder):
    plan = frozen(folder)
    ref, selection, description = reference(plan['reference'])
    if (digest(ref) != plan['reference_protocol'] or selection != plan['selection'] or description != plan['description']
            or plan['model'] != ref['model'] or plan['files'] != ref['models']['parent']['files']):
        raise ValueError('完整語意對照的模型來源已變更。')
    return plan


def deliver(case, phase, raw, ended):
    answer = raw.strip()
    if phase == 'split' and case['mapping'] and ended and answer in case['mapping']:
        return case['mapping'][answer], True
    return answer, False


class Observed:
    def __init__(self, plan, case, phase, infer):
        self.plan, self.case, self.phase, self.infer = plan, case, phase, infer
        self.record = None

    def describe(self):
        return self.plan['description']

    def answer(self, prompt, cancel=None):
        if self.record is not None:
            raise ValueError('同一案例只能推論一次。')
        cid = self.case['id']
        pair = prompt_pair(self.plan['contexts'][cid], self.case)
        if prompt != pair['direct'] or {p: digest(x) for p, x in pair.items()} != self.plan['prompts'][cid]:
            raise ValueError('實際助理提示與凍結內容不符。')
        raw, ended, tokens = self.infer(pair[self.phase], cancel)
        delivered, mapped = deliver(self.case, self.phase, raw, ended)
        self.record = dict(id=cid, raw=raw, ended=ended, input_tokens=tokens, mapped=mapped, delivered=delivered,
                           wire_sha=digest(pair[self.phase]), app_prompt_sha=digest(prompt))
        return delivered


def worker(folder, phase, run_hash):
    plan, run = frozen(folder), read(folder / 'run.json')
    if phase not in PHASES or run_hash != digest(run) or run['protocol_sha256'] != digest(plan):
        raise ValueError('完整語意對照工作規約不符。')
    with (folder / (phase + '.started')).open('x', encoding='ascii') as marker:
        marker.write(str(os.getpid()))
    deadline = time.monotonic() + LIMIT_SECONDS

    def check():
        gpu.input_gate(run['admitted'])
        if time.monotonic() >= deadline:
            raise TimeoutError('完整語意對照已達時限。')

    check()
    database = folder / (phase + '.sqlite3')
    shutil.copyfile(folder / 'fixture.sqlite3', database)
    if batches.file_hash(database) != plan['fixture_sha']:
        raise ValueError('完整語意對照的資料庫副本不符。')
    store = Store(database)
    with prior.gpu_lease():
        prior.configure()
        environment = {key: importlib.metadata.version(key) for key in prior.EXPECTED_VERSIONS}
        if environment != prior.EXPECTED_VERSIONS:
            raise ValueError('完整語意對照環境不符。')
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer, StaticCache

        if torch.cuda.device_count() != 1 or not torch.cuda.is_bf16_supported():
            raise ValueError('需要單一支援 BF16 的 CUDA GPU。')
        torch.set_num_threads(8)
        torch.manual_seed(20261104)
        torch.cuda.set_per_process_memory_fraction(0.85)
        base, implicit = local_models.verify_assets(plan['model'])
        origin = diagnosis.adapter(read(diagnosis.location(plan['reference']) / 'protocol.json'), 'parent')
        if implicit is not None or batches.adapter_files(origin) != plan['files']:
            raise ValueError('完整語意對照的適配器來源不符。')
        tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)

        def encode(prompt):
            wire = dict(schema='local_inference_request_v1', prompt=prompt, selection=plan['selection'])
            provider = SimpleNamespace(description=lambda _: plan['description'])
            with patch.object(local_worker, 'learned_models', provider):
                messages = local_worker.request_messages(wire)
                local_worker._check_role_content(tokenizer, messages)
                return local_worker.encode_request(tokenizer, wire, messages)

        counts = {}
        for case in plan['cases']:
            check()
            cid = case['id']
            counts[cid] = len(encode(prompt_pair(plan['contexts'][cid], case)[phase]))
        save(folder / (phase + '-preflight.json'), counts)
        model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False,
            use_safetensors=True, torch_dtype=torch.bfloat16, device_map={'': 0}, attn_implementation='sdpa')
        model = PeftModel.from_pretrained(model, origin, is_trainable=False, local_files_only=True)
        model.eval()
        weights = prior.adapter_hash(model)
        if weights != plan['selection']['weights']:
            raise ValueError('完整語意對照載入的權重不符。')

        def infer(prompt, cancel):
            check()
            ids = encode(prompt)
            tokens = torch.tensor([ids], device='cuda')
            with torch.inference_mode():
                cache = StaticCache(config=model.config, max_cache_len=len(ids) + local_models.MAX_OUTPUT_TOKENS)
                for start in range(0, len(ids) - 1, 256):
                    check()
                    end = min(start + 256, len(ids) - 1)
                    output = model(input_ids=tokens[:, start:end], attention_mask=torch.ones((1, end), device='cuda'),
                                   past_key_values=cache, use_cache=True, logits_to_keep=1)
                    cache = output.past_key_values
                    if cache.get_seq_length() != end:
                        raise ValueError('完整語意對照的前綴長度不符。')
                    del output
                output = model.generate(input_ids=tokens, attention_mask=torch.ones_like(tokens), past_key_values=cache,
                    max_new_tokens=local_models.MAX_OUTPUT_TOKENS, disable_compile=True, do_sample=False,
                    pad_token_id=tokenizer.eos_token_id, eos_token_id=tokenizer.eos_token_id, logits_to_keep=1,
                    use_cache=True)
            tail = output[0, len(ids):]
            raw = tokenizer.decode(tail, skip_special_tokens=True)
            return raw, len(tail) > 0 and int(tail[-1]) == tokenizer.eos_token_id, len(ids)

        rows = []
        for case in plan['cases']:
            check()
            cid, sid = case['id'], plan['sessions'][case['id']]
            engine = Observed(plan, case, phase, infer)
            app = assistant(store, engine)
            if app.prepare_context(sid, case['question']) != plan['contexts'][cid]:
                raise ValueError('完整語意對照的會話背景已變更。')
            reply = app.ask(sid, case['question'])
            row = dict(engine.record, message_id=reply['id'], user_id=reply['user_message_id'], run_id=reply['run_id'])
            if reply['content'] != row['delivered'] or row['input_tokens'] != counts[cid]:
                raise ValueError('完整語意對照的實際回答或 token 與紀錄不符。')
            rows.append(row)
            save(folder / (phase + '-partial.json'), rows)
            print(phase, cid, row['input_tokens'], flush=True)
        check()
        save(folder / (phase + '.json'), dict(phase=phase, run_sha256=run_hash, pid=os.getpid(), weights=weights,
             environment=environment, gpu=torch.cuda.get_device_name(0), rows=rows, db_sha=batches.file_hash(database)))


def score(cases, reports):
    rows = {phase: report['rows'] for phase, report in reports.items()}
    groups, losses, gains = {}, [], []
    for case, direct, split in zip(cases, rows['direct'], rows['split'], strict=True):
        if case['id'] != direct['id'] or case['id'] != split['id']:
            raise ValueError('兩組作答的案例順序不符。')
        initial = direct['ended'] and direct['delivered'] == case['answer']
        revised = split['ended'] and split['delivered'] == case['answer']
        if case['group'] == 'controls' and any(direct[k] != split[k] for k in
                                               ('raw', 'ended', 'input_tokens', 'wire_sha', 'delivered')):
            raise ValueError('控制題未重現相同模型輸出。')
        bucket = groups.setdefault(case['group'], dict(count=0, direct=0, split=0, unmapped=0))
        bucket['count'] += 1
        bucket['direct'] += initial
        bucket['split'] += revised
        bucket['unmapped'] += bool(case['mapping']) and not split['mapped']
        if initial and not revised:
            losses.append(case['id'])
        if revised and not initial:
            gains.append(case['id'])
    return dict(groups=groups, lost=losses, gained=gains, automatic_activation=False)


def result(folder, plan, run):
    if (run['protocol_sha256'] != digest(plan)
            or short.segments.idle.validate_sample(run['admitted'])['idle_ms'] < gpu.IDLE_MS):
        raise ValueError('完整語意對照規約或原生閒置不符。')
    reports, hashes = {}, {}
    for phase in PHASES:
        record, raw, exited = prior.phase_record(folder, phase, digest(run))
        database = folder / (phase + '.sqlite3')
        if (record['weights'] != plan['selection']['weights'] or record['environment'] != prior.EXPECTED_VERSIONS
                or record['db_sha'] != batches.file_hash(database)):
            raise ValueError('完整語意對照的權重、環境或會話已變更。')
        counts = read(folder / (phase + '-preflight.json'))
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            for case, row in zip(plan['cases'], record['rows'], strict=True):
                cid = case['id']
                expected = deliver(case, phase, row['raw'], row['ended'])
                messages = db.execute('SELECT id,role,content FROM messages WHERE session_id=? ORDER BY id',
                                      (plan['sessions'][cid],)).fetchall()
                context = db.execute('SELECT payload FROM answer_contexts WHERE message_id=?',
                                     (row['message_id'],)).fetchone()
                if (row['id'] != cid or row['wire_sha'] != plan['prompts'][cid][phase]
                        or row['app_prompt_sha'] != plan['prompts'][cid]['direct']
                        or row['input_tokens'] != counts[cid] or type(row['ended']) is not bool
                        or (row['delivered'], row['mapped']) != expected
                        or context is None or json.loads(context[0]) != plan['contexts'][cid]
                        or [x[1:] for x in messages] != [(x['role'], x['content']) for x in case['history']] +
                            [('user', case['question']), ('assistant', row['delivered'])]
                        or [x[0] for x in messages[-2:]] != [row['user_id'], row['message_id']]):
                    raise ValueError('完整語意對照的請求、原答或會話證據不符。')
                if db.execute('SELECT 1 FROM answer_runs WHERE id=? AND user_message_id=? '
                              'AND answer_message_id=? AND status=?',
                              (row['run_id'], row['user_id'], row['message_id'], 'succeeded')).fetchone() is None:
                    raise ValueError('完整語意對照缺少已完成的回答流程。')
        reports[phase] = record
        hashes[phase] = dict(raw=raw, exit=exited)
    if reports['direct']['gpu'] != reports['split']['gpu']:
        raise ValueError('完整語意對照使用的裝置不同。')
    return dict(**score(plan['cases'], reports), scope=SCHEMA, hashes=hashes)


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
                        raise TimeoutError('完整語意對照已達時限。')
                    if read(folder / 'run.json') != run or frozen(folder) != plan:
                        raise ValueError('完整語意對照來源已變更。')

                command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                           str(Path(__file__).resolve()), '_worker', name, phase, digest(run)]
                check()
                gpu.run_owned(command, folder, phase, lambda: gpu.input_gate(admitted), check)
                prior.phase_record(folder, phase, digest(run))
                print(phase, 'recorded', flush=True)
            if checked(folder) != plan:
                raise ValueError('完整語意對照來源已變更。')
            report = result(folder, plan, run)
            save(folder / 'review.json', report)
            return report
        except BaseException as exc:
            save(folder / 'failure.json', dict(error=type(exc).__name__, reason=str(exc)[:1000]))
            raise


def verify(name):
    folder = location(name)
    report = result(folder, checked(folder), read(folder / 'run.json'))
    if (folder / 'failure.json').exists() or read(folder / 'review.json') != report:
        raise ValueError('完整語意對照報告與重算證據不符。')
    return report


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
        print(len(cases()))
    elif args.command == 'prepare':
        plan = prepare(args.name, args.reference)
        print(json.dumps(dict(protocol_sha256=digest(plan), cases=len(plan['cases']))))
    elif args.command == '_worker':
        worker(location(args.name), args.phase, args.run_sha256)
    else:
        print(json.dumps((collect if args.command == 'run' else verify)(args.name), ensure_ascii=False))


if __name__ == '__main__':
    main()
