"""Fixed saved-report transfer and actual draft-submitting chat evaluation."""
import argparse
import json
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import autonomy, chat_drafts, goal_discussion
from assistant.engine import Assistant, CodexEngine, render_prompt
from assistant.store import Store
from scripts import evaluate_understanding as base
from scripts.evaluation_reports import (TRACE_MODE, TRACE_CONTEXT_MARKER, canonical_digest,
                                        load_json, validate_report, verify_suite)

SUITE = ROOT/'evals/goal_discussion.json'


def prepare(store, case):
    sid = store.new_session()
    fixture = case['discussion_fixture']
    source = store.path.parent/'synthetic-source.txt'
    if fixture.get('old_source'):
        source.write_text(fixture['old_source'],encoding='utf-8')
        store.attach_document(sid,source)
    with patch.object(autonomy.uuid,'uuid4',return_value=uuid.UUID(fixture['goal_id'])):
        gid = autonomy.create_goal(store,sid,fixture['objective'])
    with store.connect() as db:
        db.execute('UPDATE autonomous_goals SET notes=? WHERE id=?',(fixture['notes'],gid))
    class Scripted:
        calls = 0
        def answer(self,prompt,cancel=None):
            self.calls += 1
            if fixture.get('old_source') and self.calls == 1:
                tool,args = 'read',{'document_id':1,'start_line':1,'end_line':1}
            else:
                tool,args = 'finish',{'report':fixture['report']}
            return json.dumps({'tool':tool,'args':args,'note':'合成前置工作'},ensure_ascii=False)
    autonomy.run_goal(store,sid,gid,Scripted(),max_steps=2)
    if autonomy.get_goal(store,sid,gid)['status'] != 'report_ready':
        raise ValueError('Controlled report was not saved.')
    if fixture.get('new_source'):
        source.write_text(fixture['new_source'],encoding='utf-8')
        store.attach_document(sid,source)
    with store.connect() as db:
        db.execute('UPDATE autonomous_goals SET updated_at=? WHERE id=?',('2026-11-03 12:00:00',gid))
    draft = chat_drafts.save(store,sid,fixture['question'],0)
    saved = goal_discussion.stage(store,sid,gid,expected_revision=draft['revision'])
    if saved['content'] != case['prompt']:
        raise ValueError('Actual transfer differs from the frozen public prompt.')
    return sid,gid,saved


def signature(suite):
    return {**base.live_fingerprint(suite), 'runner_sha256':base.sha(Path(__file__).read_bytes()),
            'fixture_builder_sha256':base.sha(base.RUNNER_PATH.read_bytes())}


def collect(store,sid,case,saved,engine,unchanged):
    calls=[]
    class Recorded:
        def answer(self,prompt,cancel=None):
            if calls:
                raise ValueError('Unexpected second engine call.')
            unchanged()
            calls.append({'request':prompt})
            result=engine.answer(prompt,cancel)
            calls[0]['engine_answer']=result
            return result
    answer=Assistant(store,Recorded(),allow_local_transcription=False, allow_local_evidence=False).ask(sid,case['prompt'],draft=chat_drafts.submission(saved))
    unchanged()
    if len(calls)!=1 or chat_drafts.get(store,sid)['content']:
        raise ValueError('Actual draft submission did not complete.')
    context=store.answer_context(answer['id'])
    request=calls[0]['request']
    if request!=render_prompt(context):
        raise ValueError('Request/context mismatch.')
    body=json.dumps(context,ensure_ascii=False)
    offset=len(request)-len(body)
    if request[offset:]!=body or not request[:offset].endswith(TRACE_CONTEXT_MARKER):
        raise ValueError('Invalid context boundary.')
    fixture=json.loads(json.dumps(case,ensure_ascii=False,allow_nan=False))
    return {'id':case['id'],'category':case['category'],'prompt':case['prompt'],'criteria':case['criteria'],
            'fixture':fixture,'fixture_sha256':canonical_digest(fixture),'context':context,
            'request':request,'request_sha256':base.sha(request.encode('utf-8')),
            'request_context_offset':offset,'engine_call_count':1,'engine_answer':calls[0]['engine_answer'],
            'answer':answer['content'],'human_grade':None,'mechanical_checks':{},'draft_consumed':True}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--suite',type=Path,default=SUITE)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    try:
        cases=base.validate_cases(load_json(args.suite))
        if bool(args.report)!=args.live:
            raise ValueError('--live requires a new --report; offline takes no report.')
        frozen=signature(args.suite)
        suite_bytes=args.suite.read_bytes()
        if frozen['suite_sha256']!=base.sha(suite_bytes):
            raise ValueError('Suite changed while loading.')
        if args.live:
            report=base.load_report(args.report,frozen,[c['id'] for c in cases],False,mode=TRACE_MODE)
            verify_suite(report,suite_bytes)
            base.save_report(args.report,report)
        def unchanged():
            if signature(args.suite)!=frozen:
                raise ValueError('Source or fixtures changed during collection.')
        for case in cases:
            unchanged()
            with tempfile.TemporaryDirectory() as tmp:
                store=Store(Path(tmp)/'state.sqlite3')
                sid,gid,saved=prepare(store,case)
                if args.live:
                    before=autonomy.get_goal(store,sid,gid)
                    row=collect(store,sid,case,saved,CodexEngine(ROOT/'data'),unchanged)
                    if autonomy.get_goal(store,sid,gid)!=before:
                        raise ValueError('Chat modified the original goal.')
                    with store.connect() as db:
                        if db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]:
                            raise ValueError('Chat unexpectedly saved memory.')
                    report['results'].append(row)
                    validate_report(report)
                    verify_suite(report,suite_bytes)
                    base.save_report(args.report,report)
                    print(case['id']+': collected; review pending',flush=True)
        print(f'{len(cases)} controlled report-discussion cases; '+('actual replies await review.' if args.live else 'no model calls or grading.'))
    except (ValueError,OSError) as exc:
        parser.exit(1,str(exc)+'\n')


if __name__=='__main__': main()
