"""Exercise actual delivery and clarification on frozen synthetic goal requests."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import artifacts, autonomy, goal_replies
from assistant.acceptance import strict_json
from assistant.engine import CodexEngine, EngineError
from assistant.store import Store
from scripts.evaluate_understanding import fingerprint, save_report

SUITE = ROOT / 'evals/goal_cross_task_boundaries.json'


def prepare(path, case):
    path.mkdir()
    store = Store(path / 'assistant.sqlite3')
    sid = store.new_session()
    documents = case.get('documents', [])
    csv = case.get('csv')
    for item in documents + ([csv] if csv else []):
        name = item['name']
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,48}\.(?:txt|csv)', name):
            raise ValueError('合成文件名稱不符。')
        source = path / name
        with source.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(item['content'])
        document_id = store.attach_document(sid, source)
    gid = autonomy.create_goal(store, sid, case['prompt'], file_delivery=case.get('file_delivery', False),
                               csv_document_id=document_id if csv else None)
    return store, sid, gid


def collect(path, case, frozen):
    store, sid, gid = prepare(path, case)
    engine = CodexEngine(path / 'engine')
    trace = path / 'trace.json'
    row = {'id': case['id'], 'calls': [], 'fixture': case, 'goal_id': gid, 'session_id': sid}

    class Recorded:
        def answer(self, prompt, cancel=None):
            if fingerprint(SUITE) != frozen:
                raise ValueError('來源或題庫已變更，停止本次測量。')
            call = {'prompt': prompt, 'status': 'calling'}
            row['calls'].append(call)
            save_report(trace, row)
            try:
                call['raw'] = engine.answer(prompt, cancel)
            except (EngineError, ValueError, OSError) as exc:
                call.update(status='error', error=str(exc))
                save_report(trace, row)
                raise
            call['status'] = 'returned'
            save_report(trace, row)
            return call['raw']

    state = row['first'] = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=8)
    if case.get('reply') and state['status'] == 'needs_input' and any(
            phrase in state['question'] for phrase in case['reply_when']):
        view = goal_replies.snapshot(store, sid, gid)
        goal_replies.save(store, sid, gid, case['reply'], expected_fingerprint=view['fingerprint'])
        state = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=8)
    if state['status'] == 'paused':
        state = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=6)
    row['final'] = state
    if spec := case.get('artifact'):
        file = path / 'autonomy-artifacts' / gid / spec['name']
        if file.exists():
            try:
                row['actual_file'] = strict_json(file.read_text(encoding='utf-8'))
            except (ValueError, OSError) as exc:
                row['artifact_error'] = str(exc)
            row['file_sha256'] = hashlib.sha256(file.read_bytes()).hexdigest()
    save_report(trace, row)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, help='新建本機評測資料夾；省略時只核對題庫，不呼叫模型。')
    args = parser.parse_args()
    cases = json.loads(SUITE.read_text(encoding='utf-8'))
    ids = [case['id'] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError('合成案例編號重複。')
    for case in cases:
        autonomy.text(case['prompt'], 4000)
        autonomy.text(case['expected'], 4000)
        if 'reply' in case:
            autonomy.text(case['reply'], 4000)
            if not case.get('reply_when'):
                raise ValueError('合成答覆須有可核對的提問用詞。')
        if 'artifact' in case:
            artifacts.filename(case['artifact']['name'])
    if not args.output:
        with TemporaryDirectory() as temporary:
            for index, case in enumerate(cases):
                store, sid, gid = prepare(Path(temporary) / str(index), case)
                assert autonomy.get_goal(store, sid, gid)['status'] == 'pending'
        print(f'Validated {len(cases)} cross-task boundary fixtures without model calls.')
        return
    path = args.output.resolve()
    if not path.is_relative_to((ROOT / 'data/evaluations').resolve()) or path.exists():
        parser.error('實測資料須保存在 data/evaluations 內尚未使用的資料夾。')
    path.mkdir(parents=True)
    frozen = fingerprint(SUITE)
    report = {'fingerprint': frozen, 'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'cases': [], 'completed': False}
    for index, case in enumerate(cases):
        row = collect(path / str(index), case, frozen)
        report['cases'].append(row)
        save_report(path / 'report.json', report)
        print(f"{row['id']} {row['final']['status']} ({len(row['calls'])} model calls)", flush=True)
    report['completed'] = True
    save_report(path / 'report.json', report)


if __name__ == '__main__':
    main()
