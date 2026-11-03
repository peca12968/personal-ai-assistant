"""Trace scoped CSV analysis and file delivery using fixed synthetic inputs."""
import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from assistant import autonomy
from assistant.engine import CodexEngine, EngineError
from assistant.store import Store
from scripts.evaluate_understanding import validate_cases, prepare_case, fingerprint, load_report, save_report
from scripts.evaluation_reports import (GOAL_TRACE_MODE, GOAL_CONTEXT_MARKER, canonical_digest,
                                       strict_json, MAX_JSON_BYTES, validate_report, verify_suite)


def signature(suite):
    return {**fingerprint(suite),'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'report_validation_sha256':hashlib.sha256((ROOT/'scripts/evaluation_reports.py').read_bytes()).hexdigest(),
            'fixture_builder_sha256':hashlib.sha256((ROOT/'scripts/evaluate_understanding.py').read_bytes()).hexdigest()}


def prepare(store,case):
    sid=prepare_case(store,case)
    content=case.get('csv_content')
    if not isinstance(content,str) or not content or len(content)>200000:
        raise ValueError('CSV案例需提供1至200000字元的合成內容。')
    delivery=case.get('file_delivery',False)
    if type(delivery) is not bool:raise ValueError('交付設定需為布林值。')
    path=store.path.parent/(store.path.stem+'-source.csv')
    path.write_text(content,encoding='utf-8',newline='')
    did=store.attach_document(sid,path)
    return sid,autonomy.create_goal(store,sid,case['prompt'],csv_document_id=did,file_delivery=delivery)


def collect(store,case,engine,unchanged):
    # Detach the complete fixture before creating the synthetic source.
    fixture=strict_json(json.dumps(case,ensure_ascii=False,allow_nan=False))
    unchanged();sid,gid=prepare(store,fixture)
    row={key:fixture[key] for key in ('id','category','prompt','criteria')}
    row.update(fixture=fixture,fixture_sha256=canonical_digest(fixture),human_grade=None,
               goal_id=gid,session_id=sid,state_path=str(store.path),calls=[],csv_source_requested=True,
               file_delivery_requested=fixture.get('file_delivery',False))
    class Recorded:
        def answer(self,prompt,cancel=None):
            unchanged()
            position=prompt.rfind(GOAL_CONTEXT_MARKER)
            if position<0:raise ValueError('自主引擎請求缺少資料邊界。')
            offset=position+len(GOAL_CONTEXT_MARKER)
            call={'sequence':len(row['calls'])+1,'prompt':prompt,
                  'request_sha256':hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                  'request_context_offset':offset,'context_sha256':canonical_digest(strict_json(prompt[offset:])),
                  'status':'running'}
            row['calls'].append(call)
            try:
                raw=engine.answer(prompt,cancel)
            except (EngineError,ValueError,OSError) as exc:
                call.update(status='error',error=str(exc) or type(exc).__name__)
                raise
            call.update(status='returned',raw=raw,raw_sha256=hashlib.sha256(raw.encode('utf-8')).hexdigest())
            unchanged();return raw
    try:goal=autonomy.run_goal(store,sid,gid,Recorded(),max_steps=8)
    except (ValueError,EngineError,OSError) as exc:
        row['error']=str(exc) or type(exc).__name__;goal=autonomy.get_goal(store,sid,gid)
    unchanged()
    errors=[call['error'] for call in row['calls'] if call['status']=='error']
    if errors:row['error']=errors[-1]
    elif goal['status']=='failed':row.setdefault('error',goal['notice'] or '自主工作失敗。')
    row.update(goal=goal,engine_call_count=len(row['calls']),answer=goal['result'] or goal['question'] or goal['notice'])
    return row


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--suite',type=Path,default=ROOT/'evals/csv_membership.json')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    try:
        with args.suite.open('rb') as stream:suite_bytes=stream.read(MAX_JSON_BYTES+1)
        if len(suite_bytes)>MAX_JSON_BYTES:raise ValueError('案例集超過32 MiB。')
        cases=validate_cases(strict_json(suite_bytes))
        if not args.live:
            if args.report:raise ValueError('--report需搭配--live。')
            with tempfile.TemporaryDirectory() as tmp:
                for i,case in enumerate(cases):
                    store=Store(Path(tmp)/f'{i}.sqlite3')
                    prepare(store,case)
            print(f'Validated {len(cases)} CSV membership goal fixtures; no model answers graded.')
            return
        if not args.report:raise ValueError('需指定新的本機報告路徑。')
        frozen=signature(args.suite)
        if frozen['suite_sha256']!=hashlib.sha256(suite_bytes).hexdigest():raise ValueError('載入時案例集已變更。')
        report=load_report(args.report,frozen,[c['id'] for c in cases],False,mode=GOAL_TRACE_MODE)
        folder=args.report.parent/(args.report.stem+'-state');folder.mkdir(parents=True,exist_ok=False)
        failed=False
        def unchanged():
            if signature(args.suite)!=frozen:raise ValueError('來源、提示、腳本或案例已變更。')
        for i,case in enumerate(cases):
            unchanged();store=Store(folder/f'{i}.sqlite3')
            engine=CodexEngine(folder/f'engine-{i}')
            row=collect(store,case,engine,unchanged)
            failed=failed or 'error' in row
            report['results'].append(row)
            validate_report(report);verify_suite(report,suite_bytes)
            save_report(args.report,report)
            print(case['id']+': '+row['goal']['status']+'; review pending',flush=True)
        if failed:parser.exit(1,'部分流程失敗，未自動重試。\n')
    except (ValueError,OSError) as exc:parser.exit(1,str(exc)+'\n')


if __name__=='__main__':main()
