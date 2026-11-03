"""Controlled saved-goal fixtures followed by traced real ordinary-chat replies."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import autonomy
from assistant.engine import Assistant, CodexEngine
from assistant.store import Store
from scripts import evaluate_understanding as base
from scripts.evaluation_reports import TRACE_MODE, load_json, validate_report, verify_suite

SUITE = ROOT / 'evals/goal_status.json'


def prepare(store, case):
    sid = store.new_session()
    fixture = case['goal_fixture']
    if fixture['state'] not in ('question', 'source_changed', 'report', 'running'):
        raise ValueError('Unknown controlled goal state.')
    source = store.path.parent / 'synthetic-source.txt'
    source.write_text('合成條件：數量12。', encoding='utf-8')
    store.attach_document(sid, source)
    gid = autonomy.create_goal(store, sid, fixture['objective'])
    class Scripted:
        def answer(self, prompt, cancel=None):
            if fixture['state'] == 'question':
                tool, args = 'ask', {'question':fixture['question']}
            elif fixture['state'] == 'report':
                tool, args = 'finish', {'report':'合成舊報告正文：PRIVATE_REPORT_DO_NOT_REPLAY'}
            else:
                tool, args = 'calculate', {'operation':'sum', 'values':['12','3']}
            return json.dumps({'tool':tool, 'args':args, 'note':'合成前置工作'}, ensure_ascii=False)
    autonomy.run_goal(store, sid, gid, Scripted(), max_steps=1)
    if fixture['state'] == 'source_changed':
        source.write_text('合成條件：數量18。', encoding='utf-8')
        store.attach_document(sid, source)
    elif fixture['state'] == 'running':
        autonomy.update(store, gid, 'running')  # A record, not a live process.
    return sid, gid


def signature(suite):
    return {**base.live_fingerprint(suite),
            'runner_sha256':base.sha(Path(__file__).read_bytes()),
            'fixture_builder_sha256':base.sha(base.RUNNER_PATH.read_bytes())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, default=SUITE)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        cases = base.validate_cases(load_json(args.suite))
        if bool(args.report) != args.live:
            raise ValueError('--live requires a new --report; offline runs take no report.')
        frozen = signature(args.suite)
        suite_bytes = args.suite.read_bytes()
        if base.sha(suite_bytes) != frozen['suite_sha256']:
            raise ValueError('Suite changed while loading.')
        if args.live:
            report = base.load_report(args.report, frozen, [c['id'] for c in cases], False, mode=TRACE_MODE)
            verify_suite(report, suite_bytes)
            base.save_report(args.report, report)
        def unchanged():
            if signature(args.suite) != frozen:
                raise ValueError('Sources, collector or fixtures changed during collection.')
        for case in cases:
            unchanged()
            with tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp)/'state.sqlite3')
                sid, gid = prepare(store, case)
                before = autonomy.get_goal(store, sid, gid)
                if args.live:
                    row = base.collect_case(store, sid, case, CodexEngine(ROOT/'data'), unchanged)
                    if autonomy.get_goal(store, sid, gid) != before:
                        raise ValueError('Ordinary chat mutated the saved goal.')
                    report['results'].append(row)
                    validate_report(report)
                    verify_suite(report, suite_bytes)
                    base.save_report(args.report, report)
                    print(case['id'] + ': collected; review pending', flush=True)
                else:
                    context = Assistant(store, None).prepare_context(sid, case['prompt'])
                    assert context['current_request'] == case['prompt']
        print(f'{len(cases)} controlled goal-status cases; ' + ('actual replies await review.' if args.live else 'no model calls or grading.'))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc)+'\n')


if __name__ == '__main__':
    main()
