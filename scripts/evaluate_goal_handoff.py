"""Repeatable synthetic draft -> explicitly started goal measurement, offline by default."""
import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import autonomy, goal_drafts
from assistant.engine import CodexEngine, EngineError
from assistant.store import Store
from scripts import evaluate_goal_drafts as drafting
from scripts.evaluate_understanding import validate_cases, prepare_case, fingerprint, load_report, save_report
from scripts.evaluation_reports import (DRAFT_TRACE_MODE, GOAL_TRACE_MODE, GOAL_CONTEXT_MARKER,
    canonical_digest, strict_json, load_json, criteria, validate_report, verify_suite)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def signature(suite, checks):
    return {**fingerprint(suite), 'handoff_checks_sha256': sha(checks.read_bytes()),
            'runner_sha256': sha(Path(__file__).read_bytes()),
            'draft_collector_sha256': sha(Path(drafting.__file__).read_bytes()),
            'fixture_builder_sha256': sha((ROOT/'scripts/evaluate_understanding.py').read_bytes()),
            'report_validation_sha256': sha((ROOT/'scripts/evaluation_reports.py').read_bytes())}


def inputs(suite, checks):
    cases = validate_cases(load_json(suite))
    rubrics = load_json(checks)
    if not isinstance(rubrics, list):
        raise ValueError('接續評讀標準需為清單。')
    indexed = {}
    for row in rubrics:
        if (not isinstance(row, dict) or set(row) != {'id', 'criteria'}
                or not isinstance(row['id'], str) or row['id'] in indexed or not criteria(row['criteria'])):
            raise ValueError('接續評讀編號重複或欄位／標準無效。')
        indexed[row['id']] = row['criteria']
    if set(indexed) != {case['id'] for case in cases}:
        raise ValueError('接續評讀標準需精確涵蓋原案例，不能漏題或加入未知題。')
    return cases, indexed


def clone_state(source, destination):
    """Copy only a fresh synthetic state owned by this collector, never edit it."""
    if destination.exists():
        raise ValueError('接續狀態已存在，不覆寫或接續舊執行。')
    src = sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def collect_goal(store, fixture, draft, engine, unchanged, trace_path):
    unchanged()
    sid = draft['source']['context']['session_id']
    gid = autonomy.create_goal(store, sid, fixture['prompt'], goal_draft=draft)
    row = {key: fixture[key] for key in ('id', 'category', 'prompt', 'criteria')}
    row.update(fixture=fixture, fixture_sha256=canonical_digest(fixture), human_grade=None,
               goal_id=gid, session_id=sid, state_path=str(store.path), calls=[])

    class Recorded:
        def answer(self, prompt, cancel=None):
            unchanged()
            position = prompt.rfind(GOAL_CONTEXT_MARKER)
            if position < 0:
                raise ValueError('自主請求缺少資料邊界。')
            offset = position + len(GOAL_CONTEXT_MARKER)
            context = strict_json(prompt[offset:])
            if context.get('conversation_context') is not None or context.get('conversation_history') is not None:
                raise ValueError('接續測試意外帶入聊天或歷史，未呼叫模型。')
            call = {'sequence': len(row['calls'])+1, 'prompt': prompt,
                    'request_sha256': sha(prompt.encode('utf-8')), 'request_context_offset': offset,
                    'context_sha256': canonical_digest(context), 'status': 'running'}
            row['calls'].append(call)
            save_report(trace_path, row)
            try:
                raw = engine.answer(prompt, cancel)
            except (ValueError, EngineError, OSError) as exc:
                call.update(status='error', error=str(exc) or type(exc).__name__)
                save_report(trace_path, row)
                raise
            call.update(status='returned', raw=raw, raw_sha256=sha(raw.encode('utf-8')))
            save_report(trace_path, row)
            unchanged()
            return raw

    try:
        goal = autonomy.run_goal(store, sid, gid, Recorded(), max_steps=8)
    except (ValueError, EngineError, OSError) as exc:
        row['error'] = str(exc) or type(exc).__name__
        goal = autonomy.get_goal(store, sid, gid)
    errors = [call['error'] for call in row['calls'] if call['status'] == 'error']
    if errors:
        row['error'] = errors[-1]
    elif goal['status'] == 'failed':
        row.setdefault('error', goal['notice'] or '自主工作失敗。')
    row.update(goal=goal, engine_call_count=len(row['calls']),
               answer=goal['result'] or goal['question'] or goal['notice'])
    save_report(trace_path, row)
    unchanged()
    return row


def validate_links(draft_report, goal_report, rubrics):
    """Historical consistency only; no model call or current prompt reconstruction."""
    drafts = validate_report(draft_report)
    goals = validate_report(goal_report)
    if draft_report.get('mode') != DRAFT_TRACE_MODE or goal_report.get('mode') != GOAL_TRACE_MODE:
        raise ValueError('接續報告模式不同。')
    signature = goal_report.get('fingerprint', {})
    if (signature.get('draft_report_canonical_sha256') != canonical_digest(draft_report)
            or signature.get('handoff_checks_canonical_sha256') != canonical_digest(rubrics)):
        raise ValueError('接續報告與完整草稿／評讀標準指紋不符。')
    for key, value in draft_report.get('fingerprint', {}).items():
        target = 'input_suite_sha256' if key == 'suite_sha256' else key
        if signature.get(target) != value:
            raise ValueError('兩階段的產品、收集器或固定輸入指紋不同。')
    expected = [id for id in draft_report['case_ids'] if id in drafts and 'draft' in drafts[id]
                and bool(drafts[id]['draft']['objective_text']) and 'error' not in drafts[id]]
    if len(drafts) != len(draft_report['case_ids']) or goal_report['case_ids'] != expected:
        raise ValueError('草稿收集未完成或接續題目與可用草稿不符。')
    for id, row in goals.items():
        draft = drafts[id]
        if (row['fixture'].get('draft_row_sha256') != canonical_digest(draft)
                or row['fixture'].get('original_fixture_sha256') != draft['fixture_sha256']
                or row['prompt'] != draft['draft']['objective_text']
                or row['session_id'] != draft['session_id'] or row['criteria'] != rubrics.get(id)):
            raise ValueError('接續目標、草稿來源或固定評讀標準不符。')
        goal = row['goal']
        if (goal.get('conversation_context') is not None or goal.get('conversation_history') is not None
                or goal.get('file_delivery') is not False or goal.get('csv_source') is not None
                or goal.get('artifacts') or goal.get('sources')):
            raise ValueError('接續測試不能帶入額外背景、文件或執行權限。')


def run(suite, checks, out, engine_factory=CodexEngine):
    frozen = signature(suite, checks)
    cases, rubrics = inputs(suite, checks)
    def unchanged():
        if signature(suite, checks) != frozen:
            raise ValueError('產品、收集器、案例或評讀標準改變；保留原側錄，不混入舊指紋報告。')
    unchanged()
    out.mkdir(parents=True, exist_ok=False)
    # Immutable copies of the actual selected inputs, for later review.
    (out/'input-suite.json').write_bytes(suite.read_bytes())
    (out/'handoff-checks.json').write_bytes(checks.read_bytes())
    unchanged()
    manifest = {'format_version': 1, 'mode': 'goal_draft_handoff_v1', 'fingerprint': frozen,
                'collection_status': 'running', 'case_ids': [c['id'] for c in cases],
                'cases': {c['id']: {'stage': 'pending'} for c in cases},
                'note': 'Collection status is not task success. Review each stage; no IQ score.'}
    draft_path = out/'drafts.json'
    draft_report = load_report(draft_path, frozen, manifest['case_ids'], False, mode=DRAFT_TRACE_MODE)
    save_report(out/'manifest.json', manifest)
    save_report(draft_path, draft_report)
    failed = False
    try:
        for index, case in enumerate(cases):
            unchanged()
            manifest['cases'][case['id']]['stage'] = 'draft_running'
            save_report(out/'manifest.json', manifest)
            store = Store(out/f'draft-{index}.sqlite3')
            sid = prepare_case(store, case)
            row = drafting.collect_case(store, sid, case, engine_factory(out/f'draft-engine-{index}'),
                                        unchanged, out/f'draft-{index}.request.json')
            row['state_path'] = str(store.path)
            draft_report['results'].append(row)
            validate_report(draft_report); verify_suite(draft_report, (out/'input-suite.json').read_bytes())
            save_report(draft_path, draft_report)
            stage = 'draft_failed' if 'error' in row else ('draft_ready' if row['draft']['objective_text'] else 'draft_needs_input')
            manifest['cases'][case['id']]['stage'] = stage
            failed = failed or stage != 'draft_ready'
            save_report(out/'manifest.json', manifest)
            print(case['id']+': '+stage, flush=True)
        ready = [r for r in draft_report['results'] if manifest['cases'][r['id']]['stage'] == 'draft_ready']
        if ready:
            if load_json(draft_path) != draft_report:
                raise ValueError('第一階段報告已改變，不能接續。')
            goal_cases = [{'id': row['id'], 'category': 'draft_handoff', 'prompt': row['draft']['objective_text'],
                           'criteria': rubrics[row['id']], 'draft_row_sha256': canonical_digest(row),
                           'original_fixture_sha256': row['fixture_sha256']} for row in ready]
            goal_suite = out/'goals-suite.json'; save_report(goal_suite, goal_cases)
            goal_signature = {**frozen, 'input_suite_sha256': frozen['suite_sha256'],
                              'suite_sha256': sha(goal_suite.read_bytes()), 'draft_report_sha256': sha(draft_path.read_bytes()),
                              'draft_report_canonical_sha256': canonical_digest(draft_report),
                              'handoff_checks_canonical_sha256': canonical_digest(rubrics)}
            goal_path = out/'goals.json'
            goal_report = load_report(goal_path, goal_signature, [r['id'] for r in ready], False, mode=GOAL_TRACE_MODE)
            save_report(goal_path, goal_report)
            for index, (fixture, draft) in enumerate(zip(goal_cases, ready)):
                unchanged()
                if sha(draft_path.read_bytes()) != goal_signature['draft_report_sha256']:
                    raise ValueError('第一階段報告已改變，不能接續。')
                id = fixture['id']; manifest['cases'][id]['stage'] = 'goal_running'
                save_report(out/'manifest.json', manifest)
                clone_state(Path(draft['state_path']), out/f'goal-{index}.sqlite3')
                store = Store(out/f'goal-{index}.sqlite3')
                row = collect_goal(store, fixture, draft['draft'], engine_factory(out/f'goal-engine-{index}'),
                                   unchanged, out/f'goal-{index}.request.json')
                if sha(draft_path.read_bytes()) != goal_signature['draft_report_sha256']:
                    raise ValueError('第一階段報告在接續期間改變；保留側錄，不加入舊指紋報告。')
                goal_report['results'].append(row)
                validate_links(draft_report, goal_report, rubrics)
                verify_suite(goal_report, goal_suite.read_bytes())
                save_report(goal_path, goal_report)
                failed = failed or 'error' in row
                manifest['cases'][id].update(stage='goal_failed' if 'error' in row else 'goal_collected',
                                              goal_status=row['goal']['status'])
                save_report(out/'manifest.json', manifest)
                print(id+': '+row['goal']['status']+'; review pending', flush=True)
        unchanged()
        manifest['collection_status'] = 'incomplete' if failed else 'collected'
        save_report(out/'manifest.json', manifest)
        return not failed
    except (ValueError, EngineError, OSError) as exc:
        manifest.update(collection_status='failed', error=str(exc) or type(exc).__name__)
        save_report(out/'manifest.json', manifest)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', type=Path, default=ROOT/'evals/goal_draft_inputs.json')
    parser.add_argument('--checks', type=Path, default=ROOT/'evals/goal_draft_handoff_checks.json')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    try:
        if not args.live:
            if args.out:
                raise ValueError('--out需搭配--live。')
            cases, _ = inputs(args.suite, args.checks)
            with tempfile.TemporaryDirectory() as tmp:
                for index, case in enumerate(cases):
                    store = Store(Path(tmp)/f'{index}.sqlite3')
                    sid = prepare_case(store, case)
                    assert goal_drafts.catalog(goal_drafts.capture(store, sid, case['prompt']))
            print(f'Validated {len(cases)} draft/handoff fixtures; no model calls, goals or semantic grades.')
        else:
            if not args.out:
                raise ValueError('需指定新的本機輸出目錄，例如data/evaluations/handoff-run。')
            if not run(args.suite, args.checks, args.out):
                parser.exit(1, '部分草稿無法接續或引擎失敗；未自動重試，請查看manifest與分階段報告。\n')
    except (ValueError, EngineError, OSError) as exc:
        parser.exit(1, str(exc)+'\n')


if __name__ == '__main__':
    main()
