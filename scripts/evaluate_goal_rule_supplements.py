"""Compare late document supplements using a saved synthetic missing-rules report."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import autonomy, goal_guidance
from assistant.backup import restore_backup
from assistant.engine import CodexEngine
from assistant.store import Store
from scripts.evaluate_goal_replies import signature


def run_cases(suite, seed, output):
    frozen = signature(suite)
    cases = json.loads(suite.read_text(encoding='utf-8'))
    assert len({case['id'] for case in cases}) == len(cases)
    if seed is None:
        for case in cases:
            assert case['expected'] in {'resolved', 'partial', 'conflict', 'rejected'}
            assert all(len(file['content'].splitlines()) in range(1, 9) for file in case['files'])
            assert case['selected'] == sorted(set(case['selected'])) and case['selected']
            assert all(0 <= index < len(case['files']) for index in case['selected'])
        print(f'Validated {len(cases)} supplement fixtures without model calls.')
        return

    output.mkdir()
    result = {'fingerprint': frozen, 'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'seed_sha256': hashlib.sha256(seed.read_bytes()).hexdigest(), 'cases': []}

    def save(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

    for index, case in enumerate(cases):
        restore_backup(seed, output / str(index))
        store = Store(output / str(index) / 'assistant.sqlite3')
        with store.connect() as db:
            sid, gid = db.execute('SELECT session_id,id FROM autonomous_goals').fetchone()
        before = autonomy.get_goal(store, sid, gid)
        assert before['status'] == 'report_ready' and '沐潭 H5' in before['objective'] and '待查' in before['result']
        ranges = []
        imported = []
        for number, file in enumerate(case['files']):
            path = output / str(index) / file['name']
            path.write_text(file['content'], encoding='utf-8')
            did = store.attach_document(sid, path)
            imported.append(did)
            if number in case['selected']:
                ranges.append(f'[D{did}:L1-L{len(file["content"].splitlines())}]')
        content = '補件 ' + ' '.join(ranges)
        observed = goal_guidance.snapshot(store, sid, gid)
        row = {'id': case['id'], 'before': before, 'imported': imported, 'content': content, 'calls': []}
        trace = output / str(index) / 'trace.json'
        try:
            goal_guidance.save(store, sid, gid, content, expected_fingerprint=observed['fingerprint'])
        except ValueError as exc:
            row['rejected'] = str(exc)
            row['final'] = autonomy.get_goal(store, sid, gid)
            assert row['final'] == before
        else:
            row['after_save'] = autonomy.get_goal(store, sid, gid)
            assert row['after_save']['status'] == 'paused'
            assert row['after_save']['steps'][:len(before['steps'])] == before['steps']
            engine = CodexEngine(output / str(index) / 'engine')

            class Recorded:
                def answer(self, prompt, cancel=None):
                    assert signature(suite) == frozen
                    call = {'prompt': prompt}
                    row['calls'].append(call)
                    save(trace, row)
                    call['raw'] = engine.answer(prompt, cancel)
                    save(trace, row)
                    return call['raw']

            row['final'] = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=8)
        save(trace, row)
        result['cases'].append(row)
        save(output / 'comparison.json', result)
        print(f'{case["id"]} collected', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--seed-backup', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if bool(args.seed_backup) != bool(args.output):
        parser.error('For live comparison provide both --seed-backup and --output.')
    run_cases(args.suite, args.seed_backup, args.output)

