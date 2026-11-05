"""Real local text-copy/storage checks. Stub fallback routing is not model evaluation."""
import argparse
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if __package__ in (None,''):sys.path.insert(0,str(ROOT))
from assistant.engine import Assistant
from assistant.store import Store
from assistant import answer_runs, chat_transcription
from assistant.backup import create_backup, restore_backup


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,help='Fresh local-only report under data/')
    args=parser.parse_args()
    if args.report:
        args.report=args.report.resolve()
        if not args.report.is_relative_to((ROOT/'data').resolve()) or args.report.exists():
            parser.error('報告需使用data/下尚未存在的新路徑。')
    suite=ROOT/'evals/chat_transcription.json'
    cases=json.loads(suite.read_text(encoding='utf-8'))
    sources=['assistant/literal_request_v1.py','assistant/chat_transcription.py','assistant/engine.py',
             'assistant/answer_runs.py','assistant/store.py','assistant/backup.py',
             'scripts/daily_expenses.py','scripts/evaluate_chat_transcription.py','evals/chat_transcription.json']
    fingerprints={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}
    results=[]
    class NoModel:
        def answer(self,*args,**kwargs):raise AssertionError('Local transcription called model')
        def describe(self):raise AssertionError('Local transcription selected model')
    class RoutingStub:
        def __init__(self):self.calls=0
        def answer(self,prompt,cancel=None):
            self.calls+=1
            return 'routing-stub-only'
    with tempfile.TemporaryDirectory() as directory:
        store=Store(Path(directory)/'assistant.sqlite3');local=Assistant(store,NoModel())
        for case in cases:
            sid=store.new_session(case['id']);stub=RoutingStub()
            app=Assistant(store,stub) if case.get('fallback') else local
            result=app.ask(sid,case['request'])
            assert answer_runs.runs(store,sid)[0]['status']=='succeeded',case['id']
            receipt=chat_transcription.read(store,result['id'])
            if case.get('fallback'):
                assert stub.calls==1 and result['content']=='routing-stub-only' and receipt is None,case['id']
            else:
                assert stub.calls==0 and result['content']==case['answer'],case['id']
                assert receipt['output']==case['answer'] and receipt['model_called'] is False,case['id']
                assert result['citation_checks']==[] and result['memory_ids']==[] and result['document_count']==0,case['id']
            results.append({'id':case['id'],'message_id':result['id'],'session_id':sid,
                            'request':case['request'],'answer':result['content'],
                            'receipt':receipt,'stub_engine_calls':stub.calls})
        archive=create_backup(store.path)['path'];destination=Path(directory)/'restored'
        restore_backup(archive,destination);restored=Store(destination/'assistant.sqlite3')
        for row in results:
            assert chat_transcription.read(restored,row['message_id'])==row['receipt'],row['id']
            assert restored.messages(row['session_id'])[-1]['content']==row['answer'],row['id']
    assert fingerprints=={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}
    report={'schema':'local_transcription_regression_v1','created_at':datetime.now(timezone.utc).isoformat(),
            'scope':'Synthetic host operation, SQLite persistence, backup/restore and stub routing; no model-quality score.',
            'source_sha256':fingerprints,'real_model_calls':0,'passed':len(results),'results':results}
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        with args.report.open('x',encoding='utf-8',newline='\n') as stream:
            json.dump(report,stream,ensure_ascii=False,indent=2,allow_nan=False)
    local_count=sum(not case.get('fallback',False) for case in cases)
    print(f'Local transcription: {local_count} exact host operations, {len(cases)-local_count} stub routing controls; storage/backup verified. Zero model calls; not a reasoning or learning score.')


if __name__=='__main__':main()
