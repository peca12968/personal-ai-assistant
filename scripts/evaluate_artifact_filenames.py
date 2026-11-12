"""Check requested file names, actual clarification, and immutable draft bytes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import artifacts, autonomy, goal_guidance
from assistant.backup import validate_database
from assistant.engine import CodexEngine
from assistant.store import Store
from scripts.evaluate_plan_scope_cancellation import action
from scripts.evaluate_plan_scope_reply import reply
from scripts.evaluate_understanding import fingerprint, save_report

SUITE = ROOT / 'evals/goal_artifact_filenames.json'
DRAFT = '初始草稿。領用人林語安，門號待確認。'


def prepare(path, case):
    path.mkdir()
    store = Store(path / 'assistant.sqlite3')
    sid = store.new_session()
    gid = autonomy.create_goal(store, sid, case['objective'], file_delivery=True)
    actions = [action('write_artifact', filename=case[key], content=DRAFT)
               for key in ('draft', 'also_draft') if case.get(key)]
    parts = [{'question': '窗口是誰？', 'items': [1]}, {'question': '門號是多少？', 'items': [2]},
             {'question': '領用人是誰？', 'items': [3]}]
    tasks = ('安排窗口', '確認門號', '確認領用人')
    actions += [action('update_plan', verify_delivery=True, items=[
        {'id': number, 'task': task, 'state': 'pending', 'evidence_steps': [], 'note': ''}
        for number, task in enumerate(tasks, 1)]),
        action('ask', question='\n'.join(part['question'] for part in parts), plan_questions=parts)]
    setup = iter(actions)
    autonomy.run_goal(store, sid, gid, SimpleNamespace(answer=lambda *_: next(setup)), max_steps=len(actions))
    reply(store, sid, gid, '取消「安排窗口」。門號還沒確認。領用人是林語安。')
    if case.get('text_round'):
        reply(store, sid, gid, '8 號。領用人改為宋一珊或周雅文。')
        reply(store, sid, gid, '改成文字回覆。')
        reply(store, sid, gid, '用宋一珊，改回交檔。')
    else:
        reply(store, sid, gid, '8 號。領用人改為宋一珊。')
    if case.get('guidance'):
        view = goal_guidance.snapshot(store, sid, gid)
        goal_guidance.save(store, sid, gid, case['guidance'], expected_fingerprint=view['fingerprint'])
    return store, sid, gid


def collect(path, case, frozen, *, live):
    store, sid, gid = prepare(path, case)
    initial = autonomy.get_goal(store, sid, gid)
    trace = path / 'trace.json'
    row = {'id': case['id'], 'initial': initial, 'calls': []}
    engine = CodexEngine(path / 'engine') if live else None
    name = case['chosen'] or 'summary.md'
    default = iter([action('write_artifact', filename=name, content='門號 8 號，領用人宋一珊。'),
                    action('deliver', report='已交付', filenames=[name])])

    class Recorded:
        def answer(self, prompt, cancel=None):
            if fingerprint(SUITE) != frozen:
                raise ValueError('題庫或程式已變更。')
            call = {'prompt': prompt, 'status': 'calling'}
            row['calls'].append(call)
            if live:
                save_report(trace, row)
            call['raw'] = engine.answer(prompt, cancel) if live else next(default)
            call['status'] = 'returned'
            if live:
                save_report(trace, row)
            return call['raw']

    state = row['first'] = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=6 if live else 2)
    asked = state['status'] == 'needs_input' and bool(state['steps'][-1]['observation'].get('artifact_filename_question'))
    checks = {'clarification': asked == case['ask']}
    if asked:
        checks['before_model'] = not row['calls'] and state['artifacts'] == initial['artifacts']
        saved = row['answer'] = reply(store, sid, gid, f'定稿用「{name}」。')
        checks['verified_choice'] = (saved['reply_sources'][-1]['question'] == state['question']
                                     and saved['work_plan']['delivery_review']['ready'])
        state = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=6 if live else 2)
    elif case['ask']:
        checks['verified_choice'] = False
    row['final'] = state
    files = row['files'] = []
    if state['status'] == 'report_ready' and state['steps'][-1]['action']['tool'] == 'deliver':
        for receipt in state['steps'][-1]['observation']['artifacts']:
            data = (artifacts.directory(store, gid) / artifacts.filename(receipt['name'])).read_bytes()
            assert hashlib.sha256(data).hexdigest() == receipt['sha256']
            files.append({**receipt, 'content': data.decode('utf-8')})
    checks['file'] = (len(files) == 1 and (case['chosen'] is None or files[0]['name'] == case['chosen'])
                      and all(part.replace(' ', '') in files[0]['content'].replace(' ', '') for part in case['must_include'])
                      and all(part.replace(' ', '') not in files[0]['content'].replace(' ', '') for part in case['must_omit']))
    checks['historical_bytes'] = all((artifacts.directory(store, gid) / case[key]).read_text(encoding='utf-8') == DRAFT
                                     for key in ('draft', 'also_draft') if case.get(key))
    row['checks'] = checks
    validate_database(store.path)
    if live:
        save_report(trace, row)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    cases = json.loads(SUITE.read_text(encoding='utf-8'))
    frozen = fingerprint(SUITE)
    if args.output is None:
        with TemporaryDirectory() as scratch:
            for index, case in enumerate(cases):
                row = collect(Path(scratch) / str(index), case, frozen, live=False)
                if not all(row['checks'].values()):
                    raise ValueError(f"{case['id']} {row['checks']}")
        print(f'Validated {len(cases)} artifact filename scenarios without model calls.')
        return
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / 'data/evaluations').resolve()) or output.exists():
        parser.error('實測資料須保存在 data/evaluations 內未使用的資料夾。')
    output.mkdir(parents=True)
    report = {'suite': frozen, 'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'cases': [], 'completed': False}
    for index, case in enumerate(cases):
        row = collect(output / str(index), case, frozen, live=True)
        report['cases'].append(row)
        save_report(output / 'report.json', report)
        print(f"{row['id']} {row['final']['status']} {all(row['checks'].values())} ({len(row['calls'])} model calls)", flush=True)
    report['completed'] = True
    save_report(output / 'report.json', report)


if __name__ == '__main__':
    main()
