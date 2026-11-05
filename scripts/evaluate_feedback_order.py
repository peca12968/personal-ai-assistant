"""Controlled timestamp collisions, real feedback writes, and actual chat replies."""
import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant.engine import Assistant, CodexEngine, EngineError, render_prompt
from assistant.store import Store
from scripts.evaluate_understanding import validate_cases, fingerprint, load_report, save_report


def signature(suite):
    return {**fingerprint(suite), 'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def prepare(store, case):
    sid = store.new_session()
    ids = []
    for text in case['corrections']:
        mid = store.add_message(sid, 'assistant', '這是待你核對的舊建議。')
        store.rate(mid, -1, text)
        ids.append(mid)
    store.rate(ids[0], -1, case['latest'])
    # These are controlled clock observations, not simulated feedback writes.
    with store.connect() as db:
        db.execute("UPDATE feedback SET updated_at='2026-11-03 10:00:00'")
        if case['clock'] == 'rollback':
            db.execute("UPDATE feedback SET updated_at='2026-11-03 09:00:00' WHERE message_id=?", (ids[0],))
    return sid, ids


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, default=ROOT/'evals/feedback_order.json')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        cases = validate_cases(json.loads(args.suite.read_text(encoding='utf-8')))
        if not args.live:
            if args.report: raise ValueError('--report需搭配--live。')
            with tempfile.TemporaryDirectory() as tmp:
                for i, case in enumerate(cases):
                    prepare(Store(Path(tmp)/f'{i}.sqlite3'), case)
            print(f'Validated {len(cases)} feedback-order fixtures; no model answers graded.')
            return
        if not args.report: raise ValueError('需指定新的本機報告路徑。')
        frozen = signature(args.suite)
        report = load_report(args.report, frozen, [c['id'] for c in cases], False)
        folder = args.report.parent/(args.report.stem+'-state')
        folder.mkdir(parents=True, exist_ok=False)
        failed = False
        for i, case in enumerate(cases):
            if signature(args.suite) != frozen: raise ValueError('來源、提示、腳本或案例已變更。')
            store = Store(folder/f'{i}.sqlite3')
            sid, ids = prepare(store, case)
            app = Assistant(store, CodexEngine(folder/f'engine-{i}'), allow_local_transcription=False)
            context = app.prepare_context(sid, case['prompt'])
            row = {'id': case['id'], 'category': case['category'], 'criteria': case['criteria'],
                   'human_grade': None, 'session_id': sid, 'state_path': str(store.path),
                   'update_order': ids + [ids[0]], 'context': context,
                   'request_sha256': hashlib.sha256(render_prompt(context).encode('utf-8')).hexdigest()}
            try: row['answer'] = app.ask(sid, case['prompt'])['content']
            except EngineError as exc:
                failed = True
                row.update(answer='', error=str(exc))
            if signature(args.suite) != frozen: raise ValueError('來源、提示、腳本或案例已變更。')
            report['results'].append(row); save_report(args.report, report)
            print(case['id'] + ': collected; review pending', flush=True)
        if failed: parser.exit(1, '部分流程失敗，未自動重試。\n')
    except (ValueError, OSError) as exc: parser.exit(1, str(exc)+'\n')


if __name__ == '__main__': main()
