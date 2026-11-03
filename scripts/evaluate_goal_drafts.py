"""Fixed goal-drafting scenarios; live proposals require explicit rubric review."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import goal_drafts
from assistant.engine import CodexEngine, EngineError
from assistant.store import Store
from scripts.evaluate_understanding import validate_cases, prepare_case, fingerprint, load_report, save_report
from scripts import evaluate_understanding as fixture_builder


def signature(suite):
    return {**fingerprint(suite),
            'runner_sha256': fixture_builder.sha(Path(__file__).read_bytes()),
            'fixture_builder_sha256': fixture_builder.sha(fixture_builder.RUNNER_PATH.read_bytes())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, default=ROOT/'evals/goal_drafts.json')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        cases = validate_cases(json.loads(args.suite.read_text(encoding='utf-8')))
        if not args.live:
            if args.report:
                raise ValueError('--report需搭配--live。')
            with tempfile.TemporaryDirectory() as temp:
                for index, case in enumerate(cases):
                    store = Store(Path(temp)/f'{index}.sqlite3')
                    sid = prepare_case(store, case)
                    source = goal_drafts.capture(store, sid, case['prompt'])
                    assert goal_drafts.catalog(source)
            print(f'Validated {len(cases)} goal-draft sources; no model proposals or semantic grades.')
            return
        if not args.report:
            raise ValueError('真實整理需指定新報告路徑，請使用本機data/evaluations。')
        frozen = signature(args.suite)
        report = load_report(args.report, frozen, [case['id'] for case in cases], False)
        report['mode'] = 'goal_draft'
        # Persist synthetic states for inspecting the exact generated source or
        # explicitly continuing a reviewed draft. Never reuse an old directory.
        folder = args.report.parent/(args.report.stem+'-state')
        folder.mkdir(parents=True, exist_ok=False)
        failed = False
        for index, case in enumerate(cases):
            if frozen != signature(args.suite):
                raise ValueError('來源或案例變更，停止收集。')
            path = folder/f'{index}.sqlite3'
            store = Store(path); sid = prepare_case(store, case)
            source = goal_drafts.capture(store, sid, case['prompt'])
            row = {'id': case['id'], 'category': case['category'], 'criteria': case['criteria'],
                   'human_grade': None, 'state_path': str(path), 'session_id': sid}
            try:
                result = goal_drafts.generate(source, CodexEngine(folder/f'engine-{index}'))
                row.update(draft=result, answer=result['objective_text'] or '\n'.join(result['proposal']['questions']))
            except (ValueError, EngineError) as exc:
                failed = True
                row.update(answer='', error=str(exc), source=source)
            if frozen != signature(args.suite):
                raise ValueError('來源或案例變更，本次結果未存入舊指紋。')
            report['results'].append(row)
            save_report(args.report, report)
            print(case['id'] + (' failed; not retried' if 'error' in row else ' drafted; review pending'), flush=True)
        if failed:
            parser.exit(1, '部分草稿失敗，未重試；請查看報告。\n')
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc)+'\n')


if __name__ == '__main__':
    main()
