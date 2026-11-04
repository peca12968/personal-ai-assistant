"""Cross-process daily CLI regression; optionally collect a separate real-model suite."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant.parameter_acceptance import sources
from scripts.learning.check_gpu_checkpoint import save

SUITE = ROOT / 'evals/daily_conversations.json'
GENERAL_SUITE = ROOT / 'evals/daily_general_conversations.json'
PAIR_SUITE = ROOT / 'evals/daily_topic_pairs.json'
MODEL_ROUTES = ('model', 'general_model')
SOURCE_KEY = {'approved_expense_calculation_v1': 'expense_source',
              'daily_meeting_calculation_v1': 'meeting_source', 'daily_option_selection_v1': 'option_source'}


def fingerprint(suite=SUITE):
    files = sources()
    for path in [suite, Path(__file__).resolve(), *(ROOT/'scripts').glob('daily_*.py')]:
        files[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def snapshot(folder):
    db = folder/'assistant.sqlite3'
    if not db.exists():
        return [], [], []
    with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro', uri=True)) as conn:
        return [conn.execute(sql).fetchall() for sql in (
            'SELECT id,session_id,role,content FROM messages ORDER BY id',
            'SELECT id,session_id,user_message_id,answer_message_id,status,context_json FROM answer_runs ORDER BY rowid',
            'SELECT message_id,payload FROM answer_contexts ORDER BY message_id')]


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def turn(folder, case, sid, refs, live):
    before = snapshot(folder)
    trace = folder / (case['id'] + '.trace.json')
    args = [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--observed-child',
            'live' if live else 'offline', str(trace)]
    args += ['--engine','local','--data-dir',str(folder),'--ask',case['question']]
    if sid:
        args += ['--session',sid]
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, encoding='utf8', timeout=None if live else 45)
    after = snapshot(folder)
    calls = json.loads(trace.read_text(encoding='utf8'))
    if not live:
        require(not calls, 'offline path attempted model inference')
    if case['route'] == 'reject':
        require(proc.returncode == 1 and case['error'] in proc.stderr and not proc.stdout and before == after,
                'expected a rejection with no recorded turn')
        return sid, {'status':'expected_rejection'}
    require(proc.returncode == 0, 'CLI exit ' + str(proc.returncode) + ' ' + proc.stderr[-400:])
    content, sep, actual_sid = proc.stdout.removesuffix('\n').rpartition('\n\nSession: ')
    require(sep and re.fullmatch(r'[0-9a-f]{32}',actual_sid) and (not sid or actual_sid == sid), 'CLI session mismatch')
    for previous, current, added in zip(before, after, (2,1,1), strict=True):
        require(len(current) == len(previous)+added and current[:-added] == previous,
                'unexpected new records or prior history changed')
    user, answer = after[0][-2:]
    run, ctx = after[1][-1], after[2][-1]
    require(user[1:] == (actual_sid,'user',case['question']) and answer[1:] == (actual_sid,'assistant',content)
            and run[1:5] == (actual_sid,user[0],answer[0],'succeeded') and ctx[0] == answer[0],
            'CLI output, messages and answer run disagree')
    initial, saved = json.loads(run[5]), json.loads(ctx[1])
    require(initial['current_request'] == saved['current_request'] == case['question'], 'wrong saved question')
    details = {}
    if live:
        engine = saved['engine_selection']
        require(saved.get('response_mode') != 'local_calculation' and engine == initial['engine_selection']
                and engine['kind'] == 'local_qwen' and engine['parameter_version'] == 'unadapted_base',
                'expected real base-model route')
        require(len(calls) == 1 and calls[0].get('engine') == engine and 'raw_answer' in calls[0],
                'real local model answer missing or selection changed')
        if case['route'] == 'general_model':
            from assistant.engine import render_prompt
            require(saved.get('response_mode') in (None, 'model') and 'inventory_check' not in saved
                    and 'calculation' not in saved, 'general scenario received a tool answer')
            digest = hashlib.sha256(render_prompt(initial).encode('utf8')).hexdigest()
            require(calls[0].get('prompt_sha256') == digest, 'model prompt differs from the saved request')
            details['delivery'] = 'general_model'
        if saved.get('response_mode') == 'verified_inventory':
            audit = saved.get('inventory_check', {})
            require('inventory_check' not in initial and audit.get('model_called') is True
                    and audit.get('raw_answer') == calls[0]['raw_answer'] and audit.get('delivered') == content
                    and audit.get('raw_exact') is (content == calls[0]['raw_answer']), 'inventory audit mismatch')
            details['delivery'] = 'verified_inventory'
        details.update(engine=engine['selection_fingerprint'], raw_answer=calls[0]['raw_answer'],
                       raw_exact=calls[0]['raw_answer'] == case['answer'])
    else:
        route = case['route']
        calc = saved.get('calculation', {})
        require(saved['response_mode'] == 'local_calculation' and calc.get('model_called') is False
                and calc.get('status') == 'complete' and calc['schema'] == route, 'wrong calculation route')
        parent = refs[case['source']] if 'source' in case else None
        require(saved.get(SOURCE_KEY[route]) == initial.get(SOURCE_KEY[route]) == parent, 'wrong prior answer source')
        refs[case['id']] = dict(answer_id=answer[0], sha256=hashlib.sha256(ctx[1].encode('utf8')).hexdigest())
    for field, expected in case.get('evidence', {}).items():
        observed = saved
        for part in field.split('.'):
            observed = observed[int(part)] if isinstance(observed, list) else observed[part]
        require(type(observed) is type(expected) and observed == expected, 'wrong evidence at ' + field)
    exact = content == case['answer'] if 'answer' in case else all(t in content for t in case['contains'])
    return actual_sid, dict(status='pass' if exact else 'wrong_answer', answer=content, run_id=run[0], **details)


def check_cases(cases):
    require(bool(cases), 'empty evaluation arm')
    sessions = {}
    for c in cases:
        require(c['id'] not in sessions and re.fullmatch(r'[a-z0-9_-]+', c['id']), 'duplicate or unsafe case ID')
        require(c['route'] in (*SOURCE_KEY, *MODEL_ROUTES, 'reject') and c['question'].strip(), 'invalid route or question')
        if 'source' in c:
            require(sessions.get(c['source']) == c['session'], 'parent must precede case in the same chat')
        require(c.get('error') if c['route'] == 'reject' else c.get('answer') or c.get('contains'),
                'case must have a grading criterion')
        sessions[c['id']] = c['session']


def evaluate(folder, live, suite=SUITE):
    stamp = fingerprint(suite)
    specification = json.loads(suite.read_text(encoding='utf-8-sig'))
    history = specification['synthetic_history'] if suite == PAIR_SUITE else []
    definitions = specification['cases'] if suite == PAIR_SUITE else specification
    cases = [c for c in definitions if (c['route'] in MODEL_ROUTES) == live]
    check_cases(cases)
    sessions, refs, results = {}, {}, []
    report = dict(schema='daily_cli_eval_v1', fingerprint=stamp, suite=suite.name, live_model=live, cases=cases, results=results)
    if suite == PAIR_SUITE:
        require(live and len(cases) == 2 and len({c['session'] for c in cases}) == 2 and
                len({c['answer'] for c in cases}) == 2 and all(c['route'] == 'general_model' for c in cases),
                'pair requires two independent general-model cases with different answers')
        require(history and all(isinstance(pair, list) and len(pair) == 2 and
                all(isinstance(x, str) and x.strip() for x in pair) for pair in history), 'invalid synthetic history')
        from assistant.store import Store
        store = Store(folder/'assistant.sqlite3')
        report['synthetic_history'] = history
        for case in cases:
            sid = store.new_session()
            sessions[case['session']] = sid
            for user, assistant in history:
                store.add_message(sid, 'user', user)
                store.add_message(sid, 'assistant', assistant)
            expected = [row for pair in history for row in zip(('user','assistant'), pair)]
            require([(r['role'],r['content']) for r in store.messages(sid, len(expected)+1)] == expected,
                    'seeded conversation differs from the fixed history')
        require(len(set(sessions.values())) == len(cases), 'paired sessions must be independent')
    save(folder/'report.json',report)
    for case in cases:
        thread = case['session']
        try:
            sid, result = turn(folder,case,sessions.get(thread),refs,live)
            if thread not in sessions:
                require(sid not in sessions.values(), 'independent chats must use different sessions')
        except (ValueError, KeyError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
            results.append(dict(id=case['id'],status='error',reason=str(exc)))
            save(folder/'report.json',report)
            raise
        sessions[thread] = sid
        results.append(dict(id=case['id'],**result))
        save(folder/'report.json',report)
        print(case['id'],result['status'],flush=True)
    require(stamp == fingerprint(suite), 'evaluation inputs changed during collection')
    passed = sum(r['status'] in ('pass','expected_rejection') for r in results)
    if live:
        print(f"{sum(r['raw_exact'] for r in results)}/{len(cases)} raw model outputs matched exactly")
    print(f'{passed}/{len(cases)} ' + ('visible answers with live model; exact output' if live else 'offline CLI outcomes; model calls blocked'))
    return passed == len(cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--topic-pair', action='store_true', help='Test contrasting current actors with identical synthetic history')
    group.add_argument('--general', action='store_true', help='Use the separate general dialogue scenarios with --live')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--out', help='New run name under local data/evaluations (required for live runs)')
    args = parser.parse_args()
    if (args.general or args.topic_pair) and not args.live:
        parser.error('dialogue suites require --live')
    if args.live and not args.out:
        parser.error('--live requires --out')
    if args.out:
        if not re.fullmatch('[a-zA-Z0-9_-]{1,64}',args.out):
            parser.error('--out must be a simple run name')
        folder = ROOT/'data/evaluations'/args.out
        folder.mkdir(parents=True, exist_ok=False)
        suite = PAIR_SUITE if args.topic_pair else GENERAL_SUITE if args.general else SUITE
        return evaluate(folder, args.live, suite)
    with tempfile.TemporaryDirectory() as tmp:
        return evaluate(Path(tmp),False)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--observed-child']:
        from unittest.mock import patch
        from assistant.local_engine import LocalEngine
        from scripts import daily_chat
        mode, trace = sys.argv[2], Path(sys.argv[3])
        del sys.argv[1:4]
        calls = []
        original = LocalEngine.answer

        def observe(engine, prompt, cancel=None):
            call = dict(engine=engine.describe(), prompt_sha256=hashlib.sha256(prompt.encode('utf8')).hexdigest())
            calls.append(call)
            save(trace,calls)
            if mode != 'live':
                raise AssertionError('offline case reached model inference')
            call['raw_answer'] = original(engine,prompt,cancel)
            save(trace,calls)
            return call['raw_answer']

        save(trace,calls)
        with patch.object(LocalEngine,'answer',observe):
            daily_chat.main()
    else:
        sys.exit(0 if main() else 1)
