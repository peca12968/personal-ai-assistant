"""Compare mixed work-plan replies through the saved question and delivery entry points."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import artifacts, autonomy, goal_guidance, goal_replies
from assistant.backup import validate_database
from assistant.engine import CodexEngine, EngineError
from scripts.evaluate_plan_scope_cancellation import action, prepare
from scripts.evaluate_understanding import fingerprint, save_report

SUITE = ROOT / 'evals/goal_plan_scope_reply.json'


def reply(store, sid, gid, answer):
    view = goal_replies.snapshot(store, sid, gid)
    goal_replies.save(store, sid, gid, answer, expected_fingerprint=view['fingerprint'])
    return autonomy.get_goal(store, sid, gid)


def collect(path, case, frozen, *, live=False, suite=SUITE):
    store, sid, gid, _ = prepare(path, case)
    parts = case['parts']
    question = '\n'.join(part['question'] for part in parts)
    first = action('ask', question=question, plan_questions=parts)
    before = autonomy.run_goal(store, sid, gid, SimpleNamespace(answer=lambda *_: first), max_steps=1)
    saved = reply(store, sid, gid, case['reply'])
    plan = saved['work_plan']
    proof = plan.get('scope_reply', {})
    latest_source = saved['reply_sources'][-1]
    checks = {
        'reply_source': latest_source['question'] == question and latest_source['answer'] == case['reply'],
        'cancelled': plan.get('scope_cancelled', []) == case['cancelled'],
        'completion': proof.get('completed_items', []) == case.get('completed', []),
        'waiting': saved['question'] == case.get('remaining', ''),
    }
    if case.get('scope_receipt', bool(case['cancelled'])):
        checks['receipt'] = (plan['conditions_current'] and proof.get('citation') == latest_source['citation']
                             and proof.get('source_sha256') == latest_source['sha256'])
        checks['retained'] = all(item['state'] == 'done' for item in plan['items'][:len(case['done'])])
    else:
        checks['no_inferred_cancellation'] = saved['steps'] == before['steps']
    if 'part_states' in case:
        checks['part_states'] = [part['state'] for part in proof.get('parts', ())] == case['part_states']
    if 'scope_candidates' in case:
        unresolved = proof.get('language', {}).get('cancellation') or {}
        checks['scope_candidates'] = unresolved.get('items', []) == case['scope_candidates']
    if 'remaining_items' in case:
        successor = proof.get('successor') or {}
        checks['successor_items'] = [number for part in successor.get('plan_questions', ())
                                     for number in part['items']] == case['remaining_items']
    state = saved
    row = {'id': case['id'], 'before': before, 'after_save': saved, 'checks': checks, 'calls': []}
    if 'followup' in case:
        if saved['status'] == 'needs_input' and checks['waiting']:
            state = row['after_followup'] = reply(store, sid, gid, case['followup'])
        checks['followup'] = ('after_followup' in row and state['question'] == case.get('followup_remaining', '')
                              and state['work_plan']['conditions_current']
                              and state['work_plan'].get('scope_reply', {}).get('completed_items') == case['followup_completed']
                              and state['reply_sources'][-1]['question'] == case['remaining'])
        if 'followup_cancelled' in case:
            checks['followup_cancelled'] = state['work_plan'].get('scope_cancelled', []) == case['followup_cancelled']
        if 'followup_revisions' in case:
            proof = state['work_plan'].get('scope_reply', {})
            checks['revisions'] = {row['field']: row['value'] for row in proof.get('revisions', ())} == case['followup_revisions']
        if case.get('followup_fallback'):
            proof = state['work_plan'].get('scope_reply', {})
            previous = saved['work_plan']['items']
            safe = (state['steps'] == saved['steps'] or (not proof.get('revisions')
                    and state['work_plan'].get('scope_cancelled', []) == saved['work_plan'].get('scope_cancelled', [])
                    and all(old == new for old, new in zip(previous, state['work_plan']['items'], strict=True)
                            if old['state'] == 'done')))
            checks['followup'] = ('after_followup' in row and safe
                                  and state['reply_sources'][-1]['answer'] == case['followup'])
        if 'pending_values' in case:
            language = state['work_plan'].get('scope_reply', {}).get('language', {})
            checks['pending_values'] = [[option['value'] for option in pending['options']]
                                        for pending in language.get('pending_revisions', ())] == case['pending_values']
        if 'declined_count' in case:
            language = state['work_plan'].get('scope_reply', {}).get('language', {})
            checks['declined'] = len(language.get('declined_proposals', ())) == case['declined_count']
    if 'resolution_reply' in case:
        if state['status'] == 'needs_input' and state['question'] == case['followup_remaining']:
            state = row['after_resolution'] = reply(store, sid, gid, case['resolution_reply'])
        result = state['work_plan'].get('scope_reply', {})
        checks['resolution'] = ('after_resolution' in row and state['question'] == case.get('resolution_remaining', '')
                                and state['work_plan']['conditions_current']
                                and {rev['field']: rev['value'] for rev in result.get('revisions', ())} == case['resolution_revisions']
                                and state['reply_sources'][-1]['question'] == case['followup_remaining'])
    ready_to_measure = 'followup' not in case or checks['followup'] or (
        case.get('measure_fallback_delivery') and 'after_followup' in row)
    if 'resolution_reply' in case:
        ready_to_measure = checks['resolution']
    if turns := case.get('delivery_turns'):
        row['delivery_turns'] = []
        for turn in turns:
            if state['status'] != 'needs_input':
                break
            before_turn = state
            entry = {'before': state, 'source': turn}
            try:
                if 'guidance' in turn:
                    view = goal_guidance.snapshot(store, sid, gid)
                    goal_guidance.save(store, sid, gid, turn['guidance'], expected_fingerprint=view['fingerprint'])
                    state = autonomy.get_goal(store, sid, gid)
                else:
                    state = reply(store, sid, gid, turn['answer'])
            except ValueError as exc:
                entry['error'] = str(exc)
                state = autonomy.get_goal(store, sid, gid)
            proof = state['work_plan'].get('scope_reply', {})
            entry['after'] = state
            entry['passed'] = (state == before_turn and 'error' in entry if turn.get('reject') else
                               ('error' not in entry and state['file_delivery'] == turn['file_delivery']
                                and state['question'] == turn.get('remaining', '') and state['work_plan']['conditions_current']
                                and {rev['field']: rev['value'] for rev in proof.get('revisions', ())} == turn.get('revisions', {})
                                and state.get('delivery_mode_change', {}).get('quote') == turn.get('mode_quote')))
            row['delivery_turns'].append(entry)
        checks['delivery_turns'] = len(row['delivery_turns']) == len(turns) and all(row['passed'] for row in row['delivery_turns'])
        ready_to_measure = checks['delivery_turns'] or (case.get('measure_fallback_delivery') and not state['file_delivery'])
    if live and 'must_include' in case and state['status'] == 'paused' and ready_to_measure:
        engine = CodexEngine(path / 'engine')

        class Recorded:
            def answer(self, prompt, cancel=None):
                if fingerprint(suite) != frozen:
                    raise ValueError('題庫已變更，停止測量。')
                call = {'prompt': prompt, 'status': 'calling'}
                row['calls'].append(call)
                save_report(path / 'trace.json', row)
                try:
                    call['raw'] = engine.answer(prompt, cancel)
                except (EngineError, ValueError, OSError) as exc:
                    call.update(status='error', error=str(exc))
                    save_report(path / 'trace.json', row)
                    raise
                call['status'] = 'returned'
                save_report(path / 'trace.json', row)
                return call['raw']

        state = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=6)
        report = state.get('result', '')
        delivered = state['steps'][-1]['observation'].get('artifacts', ()) if state['status'] == 'report_ready' else ()
        if delivered:
            row['delivered_files'] = []
            for receipt in delivered:
                data = (artifacts.directory(store, gid) / artifacts.filename(receipt['name'])).read_bytes()
                if hashlib.sha256(data).hexdigest() != receipt['sha256']:
                    raise ValueError('已交付文件與實際保存的內容不一致。')
                row['delivered_files'].append({**receipt, 'content': data.decode('utf-8')})
            report += '\n' + '\n'.join(file['content'] for file in row['delivered_files'])
        report = report.replace(' ', '')
        checks['delivery'] = (state['status'] == 'report_ready'
                              and all(text.replace(' ', '') in report for text in case['must_include'])
                              and all(text.replace(' ', '') not in report for text in case['must_omit'])
                              and (bool(delivered) == case['expect_artifact'] if 'expect_artifact' in case else True)
                              and (case.get('expect_artifact', True) or not state['artifacts']))
    elif live and 'must_include' in case:
        checks['delivery'] = False
    row['final'] = state
    validate_database(store.path)
    if live:
        save_report(path / 'trace.json', row)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, default=SUITE)
    parser.add_argument('--output', type=Path, help='新建本機實測資料夾；省略時不呼叫模型。')
    parser.add_argument('--save-only', action='store_true', help='只測保存及還原，並在指定資料夾保留證據。')
    args = parser.parse_args()
    cases = json.loads(args.suite.read_text(encoding='utf-8'))
    if len({case['id'] for case in cases}) != len(cases):
        raise ValueError('合成案例編號重複。')
    frozen = fingerprint(args.suite)
    if not args.output:
        with TemporaryDirectory() as scratch:
            for index, case in enumerate(cases):
                row = collect(Path(scratch) / str(index), case, frozen)
                if not all(row['checks'].values()):
                    raise ValueError(f"{case['id']}: {row['checks']}")
        print(f'Validated {len(cases)} mixed work-plan replies without model calls.')
        return
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / 'data/evaluations').resolve()) or output.exists():
        parser.error('實測資料須保存在 data/evaluations 內未使用的資料夾。')
    output.mkdir(parents=True)
    report = {'suite': frozen, 'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'cases': [], 'completed': False}
    for index, case in enumerate(cases):
        row = collect(output / str(index), case, frozen, live=not args.save_only, suite=args.suite)
        report['cases'].append(row)
        save_report(output / 'report.json', report)
        print(f"{row['id']} {row['final']['status']} {all(row['checks'].values())} ({len(row['calls'])} model calls)", flush=True)
    report['completed'] = True
    save_report(output / 'report.json', report)


if __name__ == '__main__':
    main()
