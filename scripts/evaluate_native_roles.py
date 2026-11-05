"""Once-only local experiment using the real worker and frozen role messages."""
import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from assistant import evidence_logic, learned_models, local_models
from assistant.engine import Assistant, EngineError, render_prompt_parts
from assistant.local_engine import LocalEngine
from assistant.local_worker import Failure, encode_request, request_messages
from assistant.store import Store
from scripts import evaluate_understanding as understanding
from scripts.evaluation_attempts import exclusive_json, report_lease
from scripts.evaluation_reports import canonical_digest, load_json
from scripts.learning.protocol import EXPECTED_VERSIONS

EXTRACTORS = {
    'evidence': (evidence_logic.EXTRACTION_GUIDE, evidence_logic.analyse),
    'evidence-names': (evidence_logic.NAMED_GUIDE, evidence_logic.analyse_named),
    'evidence-indexed': (evidence_logic.INDEXED_GUIDE, evidence_logic.analyse_indexed),
}
VARIANTS = ('flat', 'split', *EXTRACTORS)


def source_paths(suite):
    return [*sorted((ROOT / 'assistant').glob('*.py')),
            ROOT / 'prompts/system.txt', ROOT / 'prompts/communication.txt', suite,
            understanding.RUNNER_PATH, understanding.VALIDATOR_PATH, understanding.ATTEMPTS_PATH,
            ROOT / 'scripts/learning/protocol.py', ROOT / 'scripts/learning/runtime.py',
            Path(__file__).resolve()]


def sources(suite):
    result = {}
    for path in source_paths(suite):
        with path.open('rb') as stream:
            result[str(path)] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


def wire(variant, selection, context):
    guide, data = render_prompt_parts(context)
    if variant in EXTRACTORS:
        guide = EXTRACTORS[variant][0]
    if variant == 'evidence-indexed':
        segments = evidence_logic.source_segments(context['current_request'])
        data = '本輪原文片段（資料）\n' + json.dumps(segments, ensure_ascii=False) + data
    if variant != 'flat':
        return {'schema': 'local_inference_request_v2', 'guide': guide, 'data': data, 'selection': selection}
    return {'schema': 'local_inference_request_v1', 'prompt': guide + data, 'selection': selection}


def prepare(folder, suite, ids, variant):
    before = sources(suite)
    cases = understanding.validate_cases(load_json(suite))
    known = {case['id'] for case in cases}
    if not 1 <= len(ids) <= 32 or len(ids) != len(set(ids)) or not set(ids) <= known:
        raise ValueError('案例選取包含未知或重複編號。')
    engine = LocalEngine(ROOT / 'data')
    rows = []
    for case in cases:
        if case['id'] not in ids:
            continue
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'fixture.sqlite3')
            sid = understanding.prepare_case(store, case)
            context = Assistant(store, engine, allow_local_transcription=False).prepare_context(sid, case['prompt'])
            payload = wire(variant, engine.selection, context)
            rows.append({'case': case, 'context': context, 'wire': payload, 'messages': request_messages(payload)})
    if sources(suite) != before:
        raise ValueError('準備期間來源有變更。')
    folder.mkdir(parents=True)
    for index, (filename, expected) in enumerate(before.items()):
        content = Path(filename).read_bytes()
        if understanding.sha(content) != expected:
            raise ValueError('保存來源副本時檔案已變更。')
        with (folder / f'source-{index}.bin').open('xb') as stream:
            stream.write(content)
    exclusive_json(folder / 'plan.json', {'schema': 'native_roles_plan_v1', 'variant': variant,
                                         'suite': str(suite), 'sources': before, 'rows': rows})
    print(f'Frozen {len(rows)} complete requests for {variant}.')


def case_ids(plan):
    if plan['schema'] != 'native_roles_plan_v1' or plan['variant'] not in VARIANTS:
        raise ValueError('原生訊息實驗計畫格式無效。')
    ids = [row['case']['id'] for row in plan['rows']]
    if not 1 <= len(ids) <= 32 or len(ids) != len(set(ids)):
        raise ValueError('實驗案例選取無效。')


def check_plan(plan):
    case_ids(plan)
    suite = Path(plan['suite'])
    if sources(suite) != plan['sources']:
        raise ValueError('來源已變更，請使用新實驗目錄。')
    fixtures = {case['id']: case for case in understanding.validate_cases(load_json(suite))}
    selection = plan['rows'][0]['wire']['selection']
    for row in plan['rows']:
        case = row['case']
        if (fixtures[case['id']] != case or row['context']['current_request'] != case['prompt']
                or wire(plan['variant'], selection, row['context']) != row['wire']
                or request_messages(row['wire']) != row['messages']):
            raise ValueError('固定案例或實際訊息與來源不符。')
    return selection


def preflight(folder, plan):
    selection = check_plan(plan)
    if {name: importlib.metadata.version(name) for name in EXPECTED_VERSIONS} != EXPECTED_VERSIONS:
        raise ValueError('請用已驗證的本機模型環境執行 preflight。')
    os.environ.update(HF_HOME=str(ROOT / 'data/learning/huggingface'), HF_HUB_OFFLINE='1',
                      HF_HUB_DISABLE_IMPLICIT_TOKEN='1', HF_HUB_DISABLE_TELEMETRY='1',
                      TOKENIZERS_PARALLELISM='false')
    from transformers import AutoTokenizer
    base, _ = learned_models.verify_assets(selection)
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
    rows = []
    for row in plan['rows']:
        result = {'id': row['case']['id'], 'wire_sha256': canonical_digest(row['wire'])}
        try:
            wire_bytes = json.dumps(row['wire'], ensure_ascii=False).encode('utf-8')
            if len(wire_bytes) > local_models.MAX_REQUEST_BYTES:
                raise Failure('invalid_request')
            ids = encode_request(tokenizer, row['wire'], row['messages'])
        except Failure as exc:
            result['error'] = exc.code
            if exc.code == 'input_too_large':
                result['input_tokens'] = len(tokenizer.apply_chat_template(
                    row['messages'], tokenize=True, add_generation_prompt=True, enable_thinking=False))
        else:
            result['input_tokens'] = len(ids)
        rows.append(result)
    check_plan(plan)
    exclusive_json(folder / 'preflight.json', {'plan_sha256': canonical_digest(plan), 'rows': rows})
    for row in rows:
        print(row, flush=True)


def check_preflight(plan, record):
    if record['plan_sha256'] != canonical_digest(plan) or len(record['rows']) != len(plan['rows']):
        raise ValueError('預檢屬於不同實驗計畫。')
    for row, check in zip(plan['rows'], record['rows'], strict=True):
        if check['id'] != row['case']['id'] or check['wire_sha256'] != canonical_digest(row['wire']):
            raise ValueError('預檢與固定請求不符。')
        if 'error' not in check and (type(check.get('input_tokens')) is not int
                                     or not 1 <= check['input_tokens'] <= local_models.MAX_INPUT_TOKENS):
            raise ValueError('預檢缺少有效的輸入 token 數。')


def run(folder, plan):
    selection = check_plan(plan)
    preflight_record = load_json(folder / 'preflight.json')
    check_preflight(plan, preflight_record)
    if any('error' in check for check in preflight_record['rows']):
        raise ValueError('至少一題預檢失敗，未開始推論。')
    plan_hash = canonical_digest(plan)
    engine = LocalEngine(ROOT / 'data')
    if engine.selection != selection:
        raise ValueError('本機參數選用已變更。')
    exclusive_json(folder / 'run.json', {'plan_sha256': plan_hash})
    for index, row in enumerate(plan['rows']):
        check_plan(plan)
        attempt = {'plan_sha256': plan_hash, 'index': index, **row}
        exclusive_json(folder / f'{index}.request.json', attempt)
        request = row['wire']
        try:
            if request['schema'] == 'local_inference_request_v2':
                answer = engine.answer_roles(request['guide'], request['data'])
            else:
                answer = engine.answer(request['prompt'])
        except BaseException as exc:
            exclusive_json(folder / f'{index}.outcome.json', {
                'request_sha256': canonical_digest(attempt), 'error': getattr(exc, 'code', type(exc).__name__),
                'detail': str(exc)[:4096]})
            raise
        exclusive_json(folder / f'{index}.outcome.json', {
            'request_sha256': canonical_digest(attempt), 'answer': answer,
            'answer_sha256': understanding.sha(answer.encode('utf-8'))})
        check_plan(plan)
        print(row['case']['id'], 'answered; review pending', flush=True)
    exclusive_json(folder / 'collected.json', {'plan_sha256': plan_hash, 'count': len(plan['rows'])})


def audit(folder, plan):
    """Check historical evidence links using the saved source copies."""
    case_ids(plan)
    for index, expected in enumerate(plan['sources'].values()):
        with (folder / f'source-{index}.bin').open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                raise ValueError('凍結來源副本指紋不符。')
    plan_hash = canonical_digest(plan)
    preflight_record = load_json(folder / 'preflight.json')
    check_preflight(plan, preflight_record)
    started = folder / 'run.json'
    if started.exists() and load_json(started) != {'plan_sha256': plan_hash}:
        raise ValueError('執行紀錄屬於不同計畫。')
    rows = []
    can_continue = started.exists()
    for index, row in enumerate(plan['rows']):
        request_path = folder / f'{index}.request.json'
        outcome_path = folder / f'{index}.outcome.json'
        status = 'not_attempted'
        if request_path.exists():
            expected = {'plan_sha256': plan_hash, 'index': index, **row}
            if not can_continue or load_json(request_path) != expected:
                raise ValueError('實際請求與計畫或執行順序不符。')
            status = 'attempt_without_outcome'
            if outcome_path.exists():
                outcome = load_json(outcome_path)
                if outcome['request_sha256'] != canonical_digest(expected):
                    raise ValueError('原答或錯誤連結到不同請求。')
                if 'answer' in outcome:
                    answer_hash = understanding.sha(outcome['answer'].encode('utf-8'))
                    if outcome['answer_sha256'] != answer_hash or 'error' in outcome:
                        raise ValueError('原答與指紋不符。')
                    status = 'answered'
                else:
                    if not isinstance(outcome.get('error'), str):
                        raise ValueError('缺少引擎錯誤紀錄。')
                    status = 'error'
        elif outcome_path.exists():
            raise ValueError('有回傳結果但缺少實際請求。')
        rows.append({'id': row['case']['id'], 'status': status,
                     'preflight': preflight_record['rows'][index]})
        can_continue = status == 'answered'
    completion = folder / 'collected.json'
    if completion.exists():
        if not can_continue or load_json(completion) != {'plan_sha256': plan_hash, 'count': len(rows)}:
            raise ValueError('完成紀錄與實際收集不符。')
    return {'plan_sha256': plan_hash, 'collected': completion.exists(), 'rows': rows}


def analyse_evidence(folder, plan):
    if plan['variant'] not in EXTRACTORS:
        raise ValueError('條件核對需要 evidence 實驗計畫。')
    checked = audit(folder, plan)
    for name in ('evidence_logic.py', 'acceptance.py'):
        source = ROOT / 'assistant' / name
        if understanding.sha(source.read_bytes()) != plan['sources'].get(str(source)):
            raise ValueError('條件核對程式已變更，請保留原實驗邊界。')
    analyse = EXTRACTORS[plan['variant']][1]
    rows = []
    for index, row in enumerate(checked['rows']):
        result = {'id': row['id'], 'status': row['status']}
        if row['status'] == 'answered':
            outcome = load_json(folder / f'{index}.outcome.json')
            result['outcome_sha256'] = canonical_digest(outcome)
            try:
                result['analysis'] = analyse(plan['rows'][index]['case']['prompt'], outcome['answer'])
            except ValueError as exc:
                result.update(status='invalid_extraction', detail=str(exc))
            else:
                result['status'] = result['analysis']['status']
        rows.append(result)
    record = {'schema': 'native_evidence_analysis_v1', 'plan_sha256': checked['plan_sha256'],
              'collected': checked['collected'], 'rows': rows}
    exclusive_json(folder / 'analysis.json', record)
    print(json.dumps([{key: row[key] for key in ('id', 'status')} for row in rows], ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare', 'preflight', 'run', 'audit', 'analyse'))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--suite', type=Path, default=ROOT / 'evals/work_comprehension.json')
    parser.add_argument('--ids', nargs='+')
    parser.add_argument('--variant', choices=VARIANTS, default='split')
    args = parser.parse_args()
    folder = args.out.resolve()
    private_root = ROOT / 'data/evaluations'
    if not folder.is_relative_to(private_root) or folder == private_root:
        parser.error('--out 需位於本機 data/evaluations 子目錄。')
    try:
        if args.phase == 'prepare':
            if not args.ids:
                parser.error('準備需要明確指定 --ids。')
            prepare(folder, args.suite.resolve(), args.ids, args.variant)
        else:
            if not folder.is_dir():
                raise ValueError('請先準備實驗。')
            with report_lease(folder / 'native-roles'):
                plan = load_json(folder / 'plan.json')
                if args.phase == 'preflight':
                    preflight(folder, plan)
                elif args.phase == 'audit':
                    print(json.dumps(audit(folder, plan), ensure_ascii=False, indent=2))
                elif args.phase == 'analyse':
                    analyse_evidence(folder, plan)
                else:
                    run(folder, plan)
    except (ValueError, OSError, KeyError, IndexError, TypeError, EngineError, Failure) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
