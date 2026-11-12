"""Collect bounded goal-scope changes from a reproducible public-plan state."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import autonomy, goal_guidance, goal_replies
from assistant.engine import CodexEngine, EngineError
from assistant.store import Store
from scripts.evaluate_understanding import fingerprint, save_report

SUITE = ROOT / 'evals/goal_plan_scope_cancellation.json'


def action(tool, **args):
    return json.dumps({'tool': tool, 'args': args, 'note': '建立固定工作清單起點'}, ensure_ascii=False)


def prepare(path, case):
    path.mkdir()
    store = Store(path / 'assistant.sqlite3')
    sid = store.new_session()
    source = path / 'input.txt'
    source.write_text(case['document'], encoding='utf-8', newline='\n')
    did = store.attach_document(sid, source)
    gid = autonomy.create_goal(store, sid, case['prompt'])
    tasks = case['done'] + case['pending']
    items = [{'id': i, 'task': task, 'state': 'done' if i <= len(case['done']) else 'pending',
              'evidence_steps': [1] if i <= len(case['done']) else [], 'note': ''}
             for i, task in enumerate(tasks, 1)]
    script = [action('read', document_id=did, start_line=1, end_line=len(case['document'].splitlines())),
              action('update_plan', verify_delivery=True, items=items)]
    if case.get('question'):
        script.append(action('ask', question=case['question']))
    planned = iter(script)
    state = autonomy.run_goal(store, sid, gid, SimpleNamespace(answer=lambda *_: next(planned)), max_steps=len(script))
    assert state['work_plan']['items'] == items
    return store, sid, gid, state


def collect(path, case, frozen):
    store, sid, gid, initial = prepare(path, case)
    engine = CodexEngine(path / 'engine')
    trace = path / 'trace.json'
    row = {'id': case['id'], 'fixture': case, 'initial': initial, 'calls': []}

    class Recorded:
        def answer(self, prompt, cancel=None):
            if fingerprint(SUITE) != frozen:
                raise ValueError('來源或題庫已變更，停止測量。')
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

    if case.get('ask_live'):
        initial = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=4)
    row['before'] = initial
    view = goal_guidance.snapshot(store, sid, gid)
    goal_guidance.save(store, sid, gid, case['correction'], expected_fingerprint=view['fingerprint'])
    state = row['after_save'] = autonomy.get_goal(store, sid, gid)
    if (case.get('reply') and state['status'] == 'needs_input' and case['reply_question'] in state['question']
            and not (case['expected'] == 'partial' and '收貨窗口' in state['question'])):
        view = goal_replies.snapshot(store, sid, gid)
        goal_replies.save(store, sid, gid, case['reply'], expected_fingerprint=view['fingerprint'])
        state = row['after_reply'] = autonomy.get_goal(store, sid, gid)
    if state['status'] == 'paused':
        state = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=6)
    row['final'] = state
    save_report(trace, row)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, help='新建本機評測資料夾；省略時只建立並核對合成起點。')
    args = parser.parse_args()
    cases = json.loads(SUITE.read_text(encoding='utf-8'))
    if len({case['id'] for case in cases}) != len(cases):
        raise ValueError('合成案例編號重複。')
    if not args.output:
        with TemporaryDirectory() as temp:
            for index, case in enumerate(cases):
                prepare(Path(temp) / str(index), case)
        print(f'Validated {len(cases)} plan scope fixtures without model calls.')
        return
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / 'data/evaluations').resolve()) or output.exists():
        parser.error('實測資料須保存在 data/evaluations 內未使用的資料夾。')
    output.mkdir(parents=True)
    frozen = fingerprint(SUITE)
    report = {'fingerprint': frozen, 'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'cases': [], 'completed': False}
    for index, case in enumerate(cases):
        row = collect(output / str(index), case, frozen)
        report['cases'].append(row)
        save_report(output / 'report.json', report)
        print(f"{row['id']} {row['final']['status']} ({len(row['calls'])} model calls)", flush=True)
    report['completed'] = True
    save_report(output / 'report.json', report)


if __name__ == '__main__':
    main()
