"""Fixed ask/reply/resume evaluations. Offline checks never grade model quality."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import autonomy, goal_replies
from assistant.engine import CodexEngine, EngineError
from assistant.store import Store
from scripts.evaluate_understanding import validate_cases, fingerprint, load_report, save_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, default=ROOT/'evals/goal_replies.json')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        cases = validate_cases(json.loads(args.suite.read_text(encoding='utf-8')))
        for case in cases:
            autonomy.text(case['prompt'], 4000)
            autonomy.text(case.get('reply'), 4000)
        if not args.live:
            if args.report: raise ValueError('--report需搭配--live。')
            with tempfile.TemporaryDirectory() as temp:
                for index, case in enumerate(cases):
                    store = Store(Path(temp)/f'{index}.sqlite3'); sid = store.new_session()
                    gid = autonomy.create_goal(store, sid, case['prompt'])
                    assert goal_replies.snapshot(store, sid, gid)['goal']['status'] == 'pending'
            print(f'Validated {len(cases)} reply scenarios; no model calls or quality grades.')
            return
        if not args.report: raise ValueError('真實評估需指定新的本機報告路徑。')
        signature = fingerprint(args.suite)
        report = load_report(args.report, signature, [case['id'] for case in cases], False)
        report['mode'] = 'goal_reply'
        folder = args.report.parent/(args.report.stem+'-state')
        folder.mkdir(parents=True, exist_ok=False)
        failed = False
        def unchanged():
            if fingerprint(args.suite) != signature: raise ValueError('來源或案例已變，停止收集。')
        for index, case in enumerate(cases):
            unchanged()
            path = folder/f'{index}.sqlite3'; store = Store(path); sid = store.new_session()
            gid = autonomy.create_goal(store, sid, case['prompt'])
            row = {'id':case['id'], 'category':case['category'], 'criteria':case['criteria'],
                   'human_grade':None, 'state_path':str(path), 'session_id':sid, 'goal_id':gid, 'calls':[]}
            engine = CodexEngine(folder/f'engine-{index}')
            class Recorded:
                def answer(self, prompt, cancel=None):
                    unchanged(); raw = engine.answer(prompt, cancel); unchanged()
                    row['calls'].append({'prompt':prompt, 'raw':raw})
                    return raw
            try:
                row['asked'] = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=8)
                if row['asked']['status'] != 'needs_input':
                    raise ValueError('初始執行未停下提問；未提交預設回答。')
                observed = goal_replies.snapshot(store, sid, gid)
                before_calls = len(row['calls'])
                row['reply'] = case['reply']
                row['saved'] = goal_replies.save(store, sid, gid, case['reply'], expected_fingerprint=observed['fingerprint'])
                assert row['saved']['goal']['status'] == 'paused' and len(row['calls']) == before_calls
                row['final'] = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=8)
                row['answer'] = row['final']['result'] or row['final']['question'] or row['final']['notice']
            except (ValueError, EngineError) as exc:
                failed = True; row.update(answer='', error=str(exc), final=autonomy.get_goal(store, sid, gid))
            unchanged(); report['results'].append(row); save_report(args.report, report)
            print(case['id']+(' failed; not retried' if 'error' in row else ' collected; review pending'), flush=True)
        if failed: parser.exit(1, '部分流程失敗；保留證據且未重試。\n')
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc)+'\n')


if __name__ == '__main__': main()
