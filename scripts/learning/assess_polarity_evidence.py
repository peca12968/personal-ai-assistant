"""Measure current-evidence extraction and deterministic prerequisite decisions."""
import argparse
from contextlib import closing
import hashlib
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

from assistant.acceptance import strict_json
from assistant import local_models, local_worker, parameter_acceptance
from assistant.store import Store
from scripts.learning import assess_semantic_delivery as full

diagnosis = full.diagnosis
prior, short, gpu, batches = full.prior, full.short, full.gpu, full.batches
read, save, digest = full.read, full.save, full.digest
PARENT = ROOT / 'data/learning/polarity-evidence-delivery'
FIXTURE = ROOT / 'evals/polarity_evidence_delivery.json'
SCHEMA = 'polarity_evidence_delivery_v1'
PHASE = 'paired'
LIMIT_SECONDS = 1200
SLOTS = ('A', 'B')
EXTRACT = ('逐一核對 A、B 的當輪觀察。T 表示已確認成立，F 表示已確認不成立，U 表示當輪未核對。'
           '撤回或仍有阻擋要依該條件的肯定命題判斷。不要以舊批、其他物件或規則本身作觀察。'
           '每項只填對應觀察的原文編號。只回 JSON，例如 '
           '{"A":{"state":"T","source":2},"B":{"state":"U","source":3}}。例子的狀態與編號僅示意。')


def location(name):
    return PARENT / full.location(name).name


def conclusion(states):
    return diagnosis.SEMANTIC[diagnosis.transfer.logic.decide('all', states)]


def parse_facts(text, ended, count):
    if not ended or not 1 <= len(text) <= 2048:
        return None
    try:
        value = strict_json(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or set(value) != set(SLOTS):
        return None
    facts = []
    for key in SLOTS:
        row = value[key]
        if (not isinstance(row, dict) or set(row) != {'state', 'source'}
                or not isinstance(row['state'], str) or row['state'] not in ('T', 'F', 'U')
                or type(row['source']) is not int or not 2 <= row['source'] <= count):
            return None
        facts.append([row['state'], row['source']])
    return facts


def cases():
    data = read(FIXTURE)
    if set(data) != {'schema', 'cases'} or data['schema'] != SCHEMA:
        raise ValueError('反證擷取題庫格式不符。')
    output = []
    for row in data['cases']:
        cid, group = row['id'], row['group']
        if group == 'control':
            output.append(dict(id=cid, group=group, question=row['question'], semantic_question=row['question'],
                               answer=row['answer'], mapping={}))
            continue
        if (group not in ('fresh', 'historical') or len(row['claims']) != 2 or len(set(row['claims'])) != 2
                or not 3 <= len(row['clauses']) <= 6 or len(set(row['clauses'])) != len(row['clauses'])):
            raise ValueError('題目須有兩項不同條件與獨立觀察。')
        for claim in row['claims']:
            batches.queue.text(claim, 50, '條件')
        for clause in row['clauses']:
            batches.queue.text(clause, 100, '原文')
        gold = row['gold']
        if (len(gold) != 2 or any(not isinstance(item, list) or len(item) != 2 or item[0] not in ('T', 'F', 'U')
                                 or type(item[1]) is not int or not 2 <= item[1] <= len(row['clauses']) for item in gold)):
            raise ValueError('預期狀態或原文編號無效。')
        source = '。'.join(row['clauses']) + '。'
        question = source + diagnosis.DIRECTIVE
        indexed = '。'.join(f'[{i}] {text}' for i, text in enumerate(row['clauses'], 1)) + '。'
        semantic = (indexed + '待核對的肯定命題 A 為「' + row['claims'][0] + '」，B 為「' + row['claims'][1]
                    + '」。' + EXTRACT)
        output.append(dict(id=cid, group=group, question=question, semantic_question=semantic, mapping={},
                           gold=gold, claims=row['claims'], clauses=row['clauses'],
                           answer=conclusion([item[0] for item in gold])))
    if (len({row['id'] for row in output}) != 8
            or [sum(row['group'] == key for row in output) for key in ('fresh', 'historical', 'control')] != [6, 1, 1]
            or any(not re.fullmatch('[a-z_]{1,32}', row['id']) for row in output)):
        raise ValueError('反證擷取題組數量或編號不符。')
    return output


def sources():
    hashes = diagnosis.sources()
    paths = [ROOT / name for name in parameter_acceptance.sources()]
    paths.extend((Path(full.__file__), Path(__file__).resolve(), FIXTURE))
    for path in paths:
        name = path.relative_to(ROOT).as_posix()
        if name not in hashes:
            # The rendered text and Python code use universal newlines.
            hashes[name] = hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    return hashes


def prepare(name, reference):
    ref, selection, description = full.reference(reference)
    fixture = cases()
    earlier = diagnosis.transfer.frozen(diagnosis.transfer.location(ref['reference']))
    used = {prior.normalized(row['prompt']) for row in [*earlier['cases'], *earlier['training']]}
    if any(prior.normalized(case['question']) in used for case in fixture if case['group'] == 'fresh'):
        raise ValueError('新題與早期訓練或驗收重疊。')
    folder = location(name)
    folder.mkdir(parents=True, exist_ok=False)
    bound_sources = sources()
    store = Store(folder / 'fixture.sqlite3')
    app = full.assistant(store, SimpleNamespace(describe=lambda: description))
    sessions, contexts, prompts = {}, {}, {}
    for case in fixture:
        cid = case['id']
        sessions[cid] = store.new_session('polarity ' + cid)
        contexts[cid] = app.prepare_context(sessions[cid], case['question'])
        prompts[cid] = {arm: digest(prompt) for arm, prompt in full.prompt_pair(contexts[cid], case).items()}
    if sources() != bound_sources:
        raise ValueError('準備期間的來源已變更。')
    plan = dict(schema=SCHEMA, reference=reference, reference_protocol=digest(ref), selection=selection,
                description=description, files=ref['models']['parent']['files'], model=ref['model'],
                sources=bound_sources, cases=fixture, sessions=sessions, contexts=contexts, prompts=prompts,
                fixture_sha=batches.file_hash(folder / 'fixture.sqlite3'))
    save(folder / 'protocol.json', plan)
    (folder / 'protocol.sha256').write_text(digest(plan), encoding='ascii')
    return plan


def frozen(folder):
    plan = read(folder / 'protocol.json', 16 * 1024 * 1024)
    if (plan['schema'] != SCHEMA or plan['sources'] != sources() or plan['cases'] != cases()
            or (folder / 'protocol.sha256').read_text(encoding='ascii') != digest(plan)
            or batches.file_hash(folder / 'fixture.sqlite3') != plan['fixture_sha']):
        raise ValueError('反證擷取規約或來源已變更。')
    return plan


def checked(folder):
    plan = frozen(folder)
    ref, selection, description = full.reference(plan['reference'])
    if (digest(ref) != plan['reference_protocol'] or plan['selection'] != selection or plan['description'] != description
            or plan['files'] != ref['models']['parent']['files'] or plan['model'] != ref['model']):
        raise ValueError('反證擷取的權重來源已變更。')
    return plan


def worker(folder, run_hash):
    plan, run = frozen(folder), read(folder / 'run.json')
    if run_hash != digest(run) or run['protocol_sha256'] != digest(plan):
        raise ValueError('反證擷取工作規約不符。')
    with (folder / (PHASE + '.started')).open('x', encoding='ascii') as marker:
        marker.write(str(os.getpid()))
    deadline = time.monotonic() + LIMIT_SECONDS

    def check():
        gpu.input_gate(run['admitted'])
        if time.monotonic() >= deadline:
            raise TimeoutError('反證擷取已達時限。')

    stores = {}
    for arm in full.PHASES:
        path = folder / (arm + '.sqlite3')
        shutil.copyfile(folder / 'fixture.sqlite3', path)
        if batches.file_hash(path) != plan['fixture_sha']:
            raise ValueError('反證擷取的合成對話副本不符。')
        stores[arm] = Store(path)
    check()
    with prior.gpu_lease():
        prior.configure()
        environment = {key: importlib.metadata.version(key) for key in prior.EXPECTED_VERSIONS}
        if environment != prior.EXPECTED_VERSIONS:
            raise ValueError('反證擷取環境不符。')
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
            raise ValueError('反證擷取的適配器來源不符。')
        tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)

        def encode(prompt):
            wire = dict(schema='local_inference_request_v1', prompt=prompt, selection=plan['selection'])
            with patch.object(local_worker, 'learned_models', SimpleNamespace(description=lambda _: plan['description'])):
                messages = local_worker.request_messages(wire)
                local_worker._check_role_content(tokenizer, messages)
                return local_worker.encode_request(tokenizer, wire, messages)

        counts = {}
        for case in plan['cases']:
            check()
            cid = case['id']
            counts[cid] = {arm: len(encode(prompt)) for arm, prompt in
                           full.prompt_pair(plan['contexts'][cid], case).items()}
        save(folder / 'preflight.json', counts)
        model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, trust_remote_code=False,
            use_safetensors=True, torch_dtype=torch.bfloat16, device_map={'': 0}, attn_implementation='sdpa')
        model = PeftModel.from_pretrained(model, origin, is_trainable=False, local_files_only=True)
        model.eval()
        weights = prior.adapter_hash(model)
        if weights != plan['selection']['weights']:
            raise ValueError('反證擷取載入的權重不符。')

        def infer(prompt, cancel):
            check()
            ids = encode(prompt)
            tokens = torch.tensor([ids], device='cuda')
            with torch.inference_mode():
                cache = StaticCache(config=model.config, max_cache_len=len(ids) + local_models.MAX_OUTPUT_TOKENS)
                for start in range(0, len(ids) - 1, 256):
                    check()
                    end = min(start + 256, len(ids) - 1)
                    part = model(input_ids=tokens[:, start:end], attention_mask=torch.ones((1, end), device='cuda'),
                                 past_key_values=cache, use_cache=True, logits_to_keep=1)
                    cache = part.past_key_values
                    if cache.get_seq_length() != end:
                        raise ValueError('反證擷取的前綴長度不符。')
                    del part
                output = model.generate(input_ids=tokens, attention_mask=torch.ones_like(tokens), past_key_values=cache,
                    max_new_tokens=local_models.MAX_OUTPUT_TOKENS, disable_compile=True, do_sample=False,
                    pad_token_id=tokenizer.eos_token_id, eos_token_id=tokenizer.eos_token_id, logits_to_keep=1,
                    use_cache=True)
            tail = output[0, len(ids):]
            return (tokenizer.decode(tail, skip_special_tokens=True),
                    len(tail) > 0 and int(tail[-1]) == tokenizer.eos_token_id, len(ids))

        rows = {arm: [] for arm in full.PHASES}
        for index, case in enumerate(plan['cases']):
            cid, sid = case['id'], plan['sessions'][case['id']]
            for arm in full.PHASES if index % 2 == 0 else reversed(full.PHASES):
                check()
                engine = full.Observed(plan, case, arm, infer)
                app = full.assistant(stores[arm], engine)
                if app.prepare_context(sid, case['question']) != plan['contexts'][cid]:
                    raise ValueError('反證擷取的完整上下文已變更。')
                reply = app.ask(sid, case['question'])
                row = dict(engine.record, message_id=reply['id'], user_id=reply['user_message_id'], run_id=reply['run_id'])
                if reply['content'] != row['delivered'] or row['input_tokens'] != counts[cid][arm]:
                    raise ValueError('反證擷取的回覆或 token 與預檢不符。')
                rows[arm].append(row)
                save(folder / 'partial.json', rows)
                print(cid, arm, row['input_tokens'], flush=True)
        check()
        save(folder / (PHASE + '.json'), dict(phase=PHASE, run_sha256=run_hash, pid=os.getpid(), weights=weights,
             environment=environment, gpu=torch.cuda.get_device_name(0), rows=rows,
             db_sha={arm: batches.file_hash(store.path) for arm, store in stores.items()}))


def summarize(cases, rows):
    groups, details = {}, []
    for case, direct, extracted in zip(cases, rows['direct'], rows['split'], strict=True):
        if case['id'] != direct['id'] or case['id'] != extracted['id']:
            raise ValueError('反證擷取案例順序不符。')
        target = case['group'] != 'control'
        if target:
            facts = parse_facts(extracted['raw'], extracted['ended'], len(case['clauses']))
            solved = conclusion([item[0] for item in facts]) if facts else None
            exact_facts = sum(a == b for a, b in zip(facts or [None, None], case['gold'], strict=True))
        else:
            if any(direct[k] != extracted[k] for k in ('raw', 'ended', 'input_tokens', 'wire_sha', 'delivered')):
                raise ValueError('一般控制題的提示或原答不同。')
            facts, solved, exact_facts = None, extracted['delivered'] if extracted['ended'] else None, 0
        direct_ok = direct['ended'] and direct['delivered'] == case['answer']
        solved_ok = solved == case['answer']
        group = groups.setdefault(case['group'], dict(count=0, direct=0, solved=0, valid=0, exact_facts=0))
        group['count'] += 1
        group['direct'] += direct_ok
        group['solved'] += solved_ok
        group['valid'] += facts is not None
        group['exact_facts'] += exact_facts
        details.append(dict(id=case['id'], direct=direct['delivered'], facts=facts, solved=solved, expected=case['answer'],
                            direct_ok=direct_ok, solved_ok=solved_ok, exact_facts=exact_facts))
    return dict(groups=groups, details=details, automatic_activation=False)


def result(folder, plan, run):
    if (run['protocol_sha256'] != digest(plan)
            or short.segments.idle.validate_sample(run['admitted'])['idle_ms'] < gpu.IDLE_MS):
        raise ValueError('反證擷取規約或原生閒置不符。')
    record, raw, exited = prior.phase_record(folder, PHASE, digest(run))
    if record['weights'] != plan['selection']['weights'] or record['environment'] != prior.EXPECTED_VERSIONS:
        raise ValueError('反證擷取的模型或環境不符。')
    counts = read(folder / 'preflight.json')
    for arm in full.PHASES:
        database = folder / (arm + '.sqlite3')
        if record['db_sha'][arm] != batches.file_hash(database):
            raise ValueError('反證擷取的會話副本已變更。')
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            for case, row in zip(plan['cases'], record['rows'][arm], strict=True):
                cid = case['id']
                saved = db.execute('SELECT payload FROM answer_contexts WHERE message_id=?',
                                   (row['message_id'],)).fetchone()
                rows = db.execute('SELECT id,role,content FROM messages WHERE session_id=? ORDER BY id',
                                  (plan['sessions'][cid],)).fetchall()
                if (row['id'] != cid or row['wire_sha'] != plan['prompts'][cid][arm]
                        or row['app_prompt_sha'] != plan['prompts'][cid]['direct']
                        or row['input_tokens'] != counts[cid][arm] or type(row['ended']) is not bool
                        or row['mapped'] is not False or row['delivered'] != row['raw'].strip()
                        or saved is None or json.loads(saved[0]) != plan['contexts'][cid]
                        or rows != [(row['user_id'], 'user', case['question']),
                                    (row['message_id'], 'assistant', row['delivered'])]
                        or db.execute('SELECT 1 FROM answer_runs WHERE id=? AND user_message_id=? '
                                      'AND answer_message_id=? AND status=?',
                                      (row['run_id'], row['user_id'], row['message_id'], 'succeeded')).fetchone() is None):
                    raise ValueError('反證擷取的提示、原答或已完成會話不符。')
    return dict(**summarize(plan['cases'], record['rows']), scope=SCHEMA, report_sha=raw, exit_sha=exited)


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
        deadline = time.monotonic() + LIMIT_SECONDS

        def check():
            gpu.input_gate(admitted)
            if time.monotonic() >= deadline:
                raise TimeoutError('反證擷取已達時限。')
            if read(folder / 'run.json') != run or frozen(folder) != plan:
                raise ValueError('反證擷取來源已變更。')

        try:
            command = [str(ROOT / 'data/learning/runtime/Scripts/python.exe'), '-I',
                       str(Path(__file__).resolve()), '_worker', name, digest(run)]
            check()
            gpu.run_owned(command, folder, PHASE, lambda: gpu.input_gate(admitted), check)
            if checked(folder) != plan:
                raise ValueError('反證擷取的參考證據已變更。')
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
        raise ValueError('反證擷取報告與重算證據不符。')
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
    child.add_argument('run_sha256')
    args = parser.parse_args()
    if args.command == 'check':
        print(len(cases()))
    elif args.command == 'prepare':
        plan = prepare(args.name, args.reference)
        print(json.dumps(dict(protocol_sha256=digest(plan), cases=len(plan['cases']))))
    elif args.command == '_worker':
        worker(location(args.name), args.run_sha256)
    else:
        print(json.dumps((collect if args.command == 'run' else verify)(args.name), ensure_ascii=False))


if __name__ == '__main__':
    main()
