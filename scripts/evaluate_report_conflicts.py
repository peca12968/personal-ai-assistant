"""Compare a formal choice of a cited rule after a delivered conflict report."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import autonomy, goal_guidance, goal_replies
from assistant.backup import restore_backup
from assistant.engine import CodexEngine
from assistant.store import Store
from scripts.evaluate_goal_replies import save_report, signature

FIELD_QUESTION = '第一筆改值指哪個欄位？請回完整欄名「採樣班別」、「核驗班別」。'


def run(suite, seeds, output):
    cases = json.loads(suite.read_text(encoding='utf-8'))
    assert len({case['id'] for case in cases}) == len(cases)
    for case in cases:
        assert re.fullmatch(r'[a-z][a-z0-9_-]{0,32}', case['seed'])
        assert case['expected'] in {'resolved', 'field', 'partial', 'conflict', 'rejected'}
        assert bool(case.get('reply')) == (case['expected'] == 'field')
        if 'reply_question' in case:
            assert case['expected'] == 'field'
            autonomy.text(case['reply_question'], 800)
        if 'clarification_reply' in case or 'clarification_question' in case:
            autonomy.text(case['clarification_reply'], 4000)
            autonomy.text(case['clarification_question'], 800)
        if 'clarifications' in case:
            assert case['expected'] != 'rejected' and 'clarification_question' not in case
            assert 1 <= len(case['clarifications']) <= 2
            for clarification in case['clarifications']:
                assert clarification.keys() == {'question', 'reply'}
                autonomy.text(clarification['question'], 800)
                autonomy.text(clarification['reply'], 4000)
            autonomy.text(case['destination'], 100)
            assert isinstance(case['pending'], list) and all(isinstance(item, str) for item in case['pending'])
            assert re.fullmatch(r'\[D[1-9][0-9]*:L[1-9][0-9]*\]', case['active_rule'])
        autonomy.text(case['correction'], 4000)
    if seeds is None:
        print(f'Validated {len(cases)} report conflict fixtures without model calls.')
        return

    output.mkdir()
    frozen = signature(suite)
    result = {'fingerprint': frozen, 'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'seeds': {name: hashlib.sha256((seeds / (name + '.paibackup')).read_bytes()).hexdigest()
                        for name in sorted({case['seed'] for case in cases})}, 'cases': []}
    for index, case in enumerate(cases):
        folder = output / str(index)
        restore_backup(seeds / (case['seed'] + '.paibackup'), folder)
        store = Store(folder / 'assistant.sqlite3')
        with store.connect() as db:
            sid, gid = db.execute('SELECT session_id,id FROM autonomous_goals').fetchone()
        before = autonomy.get_goal(store, sid, gid)
        assert before['status'] == 'report_ready'
        correction = case['correction']
        snapshot = goal_guidance.snapshot(store, sid, gid)
        row = {'id': case['id'], 'before': before, 'content': correction, 'calls': []}
        trace = folder / 'trace.json'
        try:
            goal_guidance.save(store, sid, gid, correction, expected_fingerprint=snapshot['fingerprint'])
        except ValueError as exc:
            row['rejected'] = str(exc)
            row['final'] = autonomy.get_goal(store, sid, gid)
            assert row['final'] == before
        else:
            state = row['after_save'] = autonomy.get_goal(store, sid, gid)
            assert state['status'] in {'paused', 'needs_input'}
            assert state['steps'][:-1] == before['steps']
            engine = CodexEngine(folder / 'engine')

            class Recorded:
                def answer(self, prompt, cancel=None):
                    assert signature(suite) == frozen
                    call = {'prompt': prompt}
                    row['calls'].append(call)
                    save_report(trace, row)
                    call['raw'] = engine.answer(prompt, cancel)
                    save_report(trace, row)
                    return call['raw']

            if (state['status'] == 'needs_input' and state['question'] == case.get('clarification_question')
                    and 'clarification_reply' in case):
                observed = goal_replies.snapshot(store, sid, gid)
                goal_replies.save(store, sid, gid, case['clarification_reply'], expected_fingerprint=observed['fingerprint'])
                state = row['after_clarification'] = autonomy.get_goal(store, sid, gid)
            if 'clarifications' in case:
                row['clarifications'] = []
                for clarification in case['clarifications']:
                    if state['status'] != 'needs_input' or state['question'] != clarification['question']:
                        break
                    observed = goal_replies.snapshot(store, sid, gid)
                    goal_replies.save(store, sid, gid, clarification['reply'], expected_fingerprint=observed['fingerprint'])
                    state = autonomy.get_goal(store, sid, gid)
                    row['clarifications'].append(state)
            if state['status'] == 'paused':
                state = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=6)
            if 'reply' in case:
                row['asked'] = state
                expected_question = case.get('reply_question', FIELD_QUESTION)
                if state['status'] == 'needs_input' and state['question'] == expected_question:
                    observed = goal_replies.snapshot(store, sid, gid)
                    goal_replies.save(store, sid, gid, case['reply'], expected_fingerprint=observed['fingerprint'])
                    state = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=6)
            row['final'] = state
        save_report(trace, row)
        result['cases'].append(row)
        save_report(output / 'comparison.json', result)
        print(f'{case["id"]} collected', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--seeds', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if bool(args.seeds) != bool(args.output):
        parser.error('For live comparison provide both --seeds and --output.')
    run(args.suite, args.seeds, args.output)

