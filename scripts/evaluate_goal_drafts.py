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
from scripts.evaluation_reports import (DRAFT_TRACE_MODE, DRAFT_CONTEXT_MARKER,
    MAX_JSON_BYTES, canonical_digest, strict_json, validate_report, validate_draft_trace, verify_suite)

VALIDATOR = ROOT/'scripts/evaluation_reports.py'


def signature(suite):
    return {**fingerprint(suite),
            'runner_sha256': fixture_builder.sha(Path(__file__).read_bytes()),
            'fixture_builder_sha256': fixture_builder.sha(fixture_builder.RUNNER_PATH.read_bytes()),
            'report_validation_sha256': fixture_builder.sha(VALIDATOR.read_bytes())}


def collect_case(store, sid, case, engine, unchanged, trace_path):
    fixture = json.loads(json.dumps(case, ensure_ascii=False, allow_nan=False))
    source = goal_drafts.capture(store, sid, fixture['prompt'])
    calls = []
    row = {'id': fixture['id'], 'category': fixture['category'], 'criteria': fixture['criteria'],
           'prompt': fixture['prompt'], 'fixture': fixture, 'fixture_sha256': canonical_digest(fixture),
           'source': source, 'captured_source_sha256': canonical_digest(source),
           'session_id': sid, 'calls': calls, 'engine_call_count': 0, 'human_grade': None}

    class Recorded:
        def answer(self, request, cancel=None):
            if calls:
                raise ValueError('單一草稿不能重複呼叫引擎。')
            unchanged()
            # Match the exact serialized source at the actual call boundary.
            # Never find a marker inside quoted user data or reconstruct a reply.
            body = json.dumps({**source, 'source_catalog': goal_drafts.catalog(source)}, ensure_ascii=False)
            offset = len(request) - len(body)
            if offset < 0 or request[offset:] != body or not request[:offset].endswith(DRAFT_CONTEXT_MARKER):
                raise ValueError('草稿實際請求與捕捉來源不同。')
            call = {'request': request, 'request_sha256': fixture_builder.sha(request.encode('utf-8')),
                    'request_context_offset': offset, 'status': 'calling'}
            calls.append(call)
            row['engine_call_count'] = len(calls)
            save_report(trace_path, row)
            try:
                raw = engine.answer(request, cancel)
            except (EngineError, ValueError) as exc:
                call.update(status='error', error=str(exc))
                save_report(trace_path, row)
                raise
            call.update(status='returned', engine_answer=raw,
                        engine_answer_sha256=fixture_builder.sha(raw.encode('utf-8')) if isinstance(raw, str) else None)
            save_report(trace_path, row)
            return raw

    unchanged()
    try:
        result = goal_drafts.generate(source, Recorded())
        row.update(draft=result, answer=result['objective_text'] or '\n'.join(result['proposal']['questions']))
    except (ValueError, EngineError) as exc:
        row.update(answer='', error=str(exc))
    # Keep the actual call evidence even if code/suite changed while waiting.
    save_report(trace_path, row)
    unchanged()
    validate_draft_trace(row)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, default=ROOT/'evals/goal_drafts.json')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        with args.suite.open('rb') as stream:
            suite_bytes = stream.read(MAX_JSON_BYTES + 1)
        if len(suite_bytes) > MAX_JSON_BYTES:
            raise ValueError('案例集單檔上限32 MiB。')
        cases = validate_cases(strict_json(suite_bytes))
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
        if frozen['suite_sha256'] != fixture_builder.sha(suite_bytes):
            raise ValueError('案例集在讀取後變更，未開始收集。')
        report = load_report(args.report, frozen, [case['id'] for case in cases], False, mode=DRAFT_TRACE_MODE)
        verify_suite(report, suite_bytes)
        def unchanged():
            if frozen != signature(args.suite):
                raise ValueError('來源、案例或追蹤程式變更；保留原始請求，但不混入舊指紋報告。')
        # Persist synthetic states for inspecting the exact generated source or
        # explicitly continuing a reviewed draft. Never reuse an old directory.
        folder = args.report.parent/(args.report.stem+'-state')
        folder.mkdir(parents=True, exist_ok=False)
        failed = False
        for index, case in enumerate(cases):
            unchanged()
            path = folder/f'{index}.sqlite3'
            store = Store(path); sid = prepare_case(store, case)
            row = collect_case(store, sid, case, CodexEngine(folder/f'engine-{index}'), unchanged,
                               folder/f'{index}.request.json')
            row['state_path'] = str(path)
            failed = failed or 'error' in row
            report['results'].append(row)
            validate_report(report)
            verify_suite(report, suite_bytes)
            save_report(args.report, report)
            print(case['id'] + (' failed; not retried' if 'error' in row else ' drafted; review pending'), flush=True)
        if failed:
            parser.exit(1, '部分草稿失敗，未重試；請查看報告。\n')
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc)+'\n')


if __name__ == '__main__':
    main()
