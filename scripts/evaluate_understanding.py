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
from assistant.store import Store

SUITE = ROOT / 'evals/understanding.json'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_cases(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError('案例集不能為空。')
    ids = set()
    for case in cases:
        if not isinstance(case.get('id'), str) or not case['id'] or case['id'] in ids:
            raise ValueError('案例編號無效或重複。')
        ids.add(case['id'])
        if not case.get('category') or not isinstance(case.get('prompt'), str) or not case['prompt'].strip():
            raise ValueError('案例缺少分類或問題。')
        criteria = case.get('criteria')
        if not isinstance(criteria, list) or not criteria or not all(isinstance(c, str) and c for c in criteria):
            raise ValueError('每題需要明確評讀標準。')
        if case.get('check') not in (None, 'three_bullets'):
            raise ValueError('未知的確定性檢查。')
    return cases


def prepare_case(store, case):
    sid = store.new_session()
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
    return sid


def mechanical_check(case, answer):
    if case.get('check') == 'three_bullets':
        lines = answer.strip().splitlines()
        return {'three_bullets': len(lines) == 3 and all(line.startswith('- ') and line[2:].strip() for line in lines)}
    return {}


def fingerprint():
    source = b''.join(path.name.encode() + path.read_bytes() for path in sorted((ROOT / 'assistant').glob('*.py')))
    return {'suite_sha256': sha(SUITE.read_bytes()),
            'prompt_sha256': sha((ROOT / 'prompts/system.txt').read_bytes()), 'source_sha256': sha(source)}


def load_report(path, signature, ids, resume):
    if path.exists():
        if not resume:
            raise ValueError('報告已存在；請使用新路徑或 --resume，不會覆寫結果。')
        report = json.loads(path.read_text(encoding='utf-8'))
        if report.get('fingerprint') != signature or report.get('case_ids') != ids:
            raise ValueError('來源、提示或案例選取已改變，不能混入同一份報告。')
        seen = [row['id'] for row in report['results']]
        if len(set(seen)) != len(seen) or any(item not in ids for item in seen):
            raise ValueError('報告案例紀錄無效。')
        return report
    if resume:
        raise ValueError('找不到要接續的報告。')
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
    return {'format_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
            'git_head': commit.stdout.strip(), 'fingerprint': signature, 'case_ids': ids,
            'review_note': 'Requires actual-answer rubric review; no automated overall correctness score.', 'results': []}


def save_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='呼叫真實 Codex；使用帳號額度')
    parser.add_argument('--ids', nargs='+')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    try:
        cases = validate_cases(json.loads(SUITE.read_text(encoding='utf-8')))
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
            print(f'Validated {len(cases)} understanding scenarios; no model answers graded.')
            return
        if not args.report:
            raise ValueError('--live 需指定 --report，建議使用 data/evaluations 下的新路徑。')
        signature = fingerprint()
        report = load_report(args.report, signature, [case['id'] for case in cases], args.resume)
        save_report(args.report, report)
        engine = CodexEngine(ROOT / 'data')
        finished = {row['id'] for row in report['results']}
        for case in cases:
            if case['id'] in finished:
                continue
            if fingerprint() != signature:
                raise ValueError('評估期間程式、提示或案例已改變，已停止以免混用版本。')
            with tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / 'state.sqlite3')
                sid = prepare_case(store, case)
                app = Assistant(store, engine)
                context = app.prepare_context(sid, case['prompt'])
                answer = app.ask(sid, case['prompt'])
                report['results'].append({'id': case['id'], 'category': case['category'],
                    'prompt': case['prompt'], 'criteria': case['criteria'],
                    'context': context, 'request_sha256': sha(render_prompt(context).encode('utf-8')),
                    'answer': answer['content'], 'mechanical_checks': mechanical_check(case, answer['content']),
                    'human_grade': None})
            save_report(args.report, report)
            print(case['id'], 'answered; rubric review pending', flush=True)
        print(f'Collected {len(report["results"])}/{len(cases)} actual answers; review required.')
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
