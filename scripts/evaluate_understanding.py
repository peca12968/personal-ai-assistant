"""Real-answer understanding suite with fingerprints, explicit review, and resume."""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant.engine import Assistant, CodexEngine, render_prompt
from scripts.evaluation_reports import (index_report, load_json, strict_json, validate_report,
    verify_suite, canonical_digest, TRACE_MODE, TRACE_CONTEXT_MARKER, MAX_JSON_BYTES)
from assistant.store import Store
from assistant.companion_profile import get_profile, reset_profile, save_profile, validate as validate_profile

SUITE = ROOT / 'evals/understanding.json'
RUNNER_PATH = Path(__file__).resolve()
VALIDATOR_PATH = RUNNER_PATH.with_name('evaluation_reports.py')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_cases(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError('案例集不能為空。')
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get('id'), str) or not case['id'].strip() or case['id'] in ids:
            raise ValueError('案例編號無效或重複。')
        ids.add(case['id'])
        if not isinstance(case.get('category'), str) or not case['category'].strip() or not isinstance(case.get('prompt'), str) or not case['prompt'].strip():
            raise ValueError('案例缺少分類或問題。')
        criteria = case.get('criteria')
        if not isinstance(criteria, list) or not criteria or not all(isinstance(c, str) and c.strip() for c in criteria):
            raise ValueError('每題需要明確評讀標準。')
        if case.get('check') not in (None, 'three_bullets'):
            raise ValueError('未知的確定性檢查。')
        for key in ('companion_profile', 'prior_companion_profile'):
            if key in case:
                validate_profile(case[key])
        if 'reset_companion_profile' in case and (type(case['reset_companion_profile']) is not bool or 'companion_profile' in case):
            raise ValueError('重設夥伴案例格式無效。')
    return cases


def prepare_case(store, case):
    sid = store.new_session()
    if 'prior_companion_profile' in case:
        save_profile(store, case['prior_companion_profile'], expected_revision=get_profile(store)['revision'])
    for memory in case.get('memories', []):
        store.remember(**memory)
    if case.get('brief'):
        store.save_session_brief(sid, **case['brief'])
    for row in case.get('history', []):
        store.add_message(sid, row['role'], row['content'])
    for number in range(case.get('filler_pairs', 0)):
        store.add_message(sid, 'user', f'一般背景討論第 {number+1} 回。')
        store.add_message(sid, 'assistant', '收到這項背景。')
    if case.get('long_message'):
        text = '長文開始。\n' + '一般背景。' * 1000 + '\n復原代號：不可見-947。\n' + '其他背景。' * 1000 + '\n長文結束。'
        store.add_message(sid, 'user', text)
    if 'companion_profile' in case:
        save_profile(store, case['companion_profile'], expected_revision=get_profile(store)['revision'])
    elif case.get('reset_companion_profile'):
        reset_profile(store, expected_revision=get_profile(store)['revision'])
    return sid


def mechanical_check(case, answer):
    if case.get('check') == 'three_bullets':
        lines = answer.strip().splitlines()
        return {'three_bullets': len(lines) == 3 and all(line.startswith('- ') and line[2:].strip() for line in lines)}
    return {}


def fingerprint(suite_path=None):
    source = b''.join(path.name.encode() + path.read_bytes() for path in sorted((ROOT / 'assistant').glob('*.py')))
    return {'suite_sha256': sha((suite_path or SUITE).read_bytes()),
            'prompt_sha256': sha((ROOT / 'prompts/system.txt').read_bytes()),
            'communication_prompt_sha256': sha((ROOT / 'prompts/communication.txt').read_bytes()),
            'source_sha256': sha(source)}


def live_fingerprint(suite_path):
    # Keep fingerprint() compatible with the other dedicated goal runners;
    # this protocol specifically fingerprints its collector and validator.
    return {**fingerprint(suite_path), 'runner_sha256': sha(RUNNER_PATH.read_bytes()),
            'report_validation_sha256': sha(VALIDATOR_PATH.read_bytes())}


def load_report(path, signature, ids, resume, *, mode=None):
    index_report({'case_ids': ids, 'results': []})
    if path.exists():
        if not resume:
            raise ValueError('報告已存在；請使用新路徑或 --resume，不會覆寫結果。')
        report = load_json(path)
        validate_report(report)
        if report.get('fingerprint') != signature or report.get('case_ids') != ids:
            raise ValueError('來源、提示或案例選取已改變，不能混入同一份報告。')
        if mode is not None and report.get('mode') != mode:
            raise ValueError('評估紀錄模式不同，不能補造舊報告的實際請求。')
        seen = [row['id'] for row in report['results']]
        if len(set(seen)) != len(seen) or any(item not in ids for item in seen):
            raise ValueError('報告案例紀錄無效。')
        return report
    if resume:
        raise ValueError('找不到要接續的報告。')
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
    result = {'format_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
            'git_head': commit.stdout.strip(), 'fingerprint': signature, 'case_ids': ids,
            'review_note': 'Requires actual-answer rubric review; no automated overall correctness score.', 'results': []}
    if mode is not None:
        result['mode'] = mode
    return result


def collect_case(store, sid, case, engine, unchanged):
    calls = []
    class Recorded:
        def answer(self, prompt, cancel=None):
            if calls:
                raise ValueError('本理解案例意外呼叫引擎多次，未將它視為單次作答。')
            unchanged()
            calls.append({'request': prompt})
            result = engine.answer(prompt, cancel)
            calls[0]['engine_answer'] = result
            return result
    answer = Assistant(store, Recorded()).ask(sid, case['prompt'])
    unchanged()
    if len(calls) != 1:
        raise ValueError('此案例未呼叫模型（可能是本機命令），不保存為模型回答；請使用對應功能評估器。')
    context = store.answer_context(answer['id'])
    request = calls[0]['request']
    if not isinstance(context, dict) or request != render_prompt(context):
        raise ValueError('實際引擎請求與已保存作答脈絡不一致，未加入報告。')
    body = json.dumps(context, ensure_ascii=False)
    offset = len(request)-len(body)
    if not request[offset:] == body or not request[:offset].endswith(TRACE_CONTEXT_MARKER):
        raise ValueError('無法核對實際請求的脈絡位置。')
    fixture = json.loads(json.dumps(case, ensure_ascii=False, allow_nan=False))
    return {'id':case['id'], 'category':case['category'], 'prompt':case['prompt'],
            'criteria':case['criteria'], 'fixture':fixture, 'fixture_sha256':canonical_digest(fixture),
            'context':context, 'request':request, 'request_sha256':sha(request.encode('utf-8')),
            'request_context_offset':offset, 'engine_call_count':len(calls),
            'engine_answer':calls[0]['engine_answer'], 'answer':answer['content'],
            'mechanical_checks':mechanical_check(case, answer['content']), 'human_grade':None}


def save_report(path, report):
    try:
        raw = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('評估報告不能序列化為有效JSON。') from exc
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('評估報告超過32 MiB，保留先前已保存內容；請減少每次案例選取。')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(raw)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='呼叫真實 Codex；使用帳號額度')
    parser.add_argument('--suite', type=Path, default=SUITE, help='指定另一組同格式的固定案例')
    parser.add_argument('--ids', nargs='+')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    try:
        with args.suite.open('rb') as stream:
            suite_bytes = stream.read(MAX_JSON_BYTES+1)
        if len(suite_bytes) > MAX_JSON_BYTES:
            raise ValueError('案例集單檔上限32 MiB。')
        cases = validate_cases(strict_json(suite_bytes))
        if args.ids:
            if set(args.ids) - {case['id'] for case in cases}:
                raise ValueError('有未知案例編號。')
            cases = [case for case in cases if case['id'] in args.ids]
        if not args.live:
            if args.resume or args.report:
                raise ValueError('--resume 與 --report 需搭配 --live。')
            for case in cases:
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(Path(tmp) / 'state.sqlite3')
                    sid = prepare_case(store, case)
                    context = Assistant(store, None).prepare_context(sid, case['prompt'])
                    assert context['current_request'] == case['prompt']
                    if 'companion_profile' in case:
                        assert context['companion_profile']['settings'] == validate_profile(case['companion_profile'])
            print(f'Validated {len(cases)} understanding scenarios; no model answers graded.')
            return
        if not args.report:
            raise ValueError('--live 需指定 --report，建議使用 data/evaluations 下的新路徑。')
        signature = live_fingerprint(args.suite)
        if signature['suite_sha256'] != sha(suite_bytes):
            raise ValueError('案例集在讀取後已變更，未開始收集。')
        report = load_report(args.report, signature, [case['id'] for case in cases], args.resume, mode=TRACE_MODE)
        verify_suite(report, suite_bytes)
        save_report(args.report, report)
        engine = CodexEngine(ROOT / 'data')
        def unchanged():
            if live_fingerprint(args.suite) != signature:
                raise ValueError('評估期間程式、提示、案例、測試器或驗證器已改變，停止以免混用版本。')
        finished = {row['id'] for row in report['results']}
        for case in cases:
            if case['id'] in finished:
                continue
            unchanged()
            with tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / 'state.sqlite3')
                sid = prepare_case(store, case)
                row = collect_case(store, sid, case, engine, unchanged)
                report['results'].append(row)
                validate_report(report)
                verify_suite(report, suite_bytes)
            save_report(args.report, report)
            print(case['id'], 'answered; rubric review pending', flush=True)
        print(f'Collected {len(report["results"])}/{len(cases)} actual answers; review required.')
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
