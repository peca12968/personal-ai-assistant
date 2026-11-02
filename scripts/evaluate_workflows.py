"""Verify actual workflow artifacts; optionally collect real language plans."""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant.engine import CodexEngine
from assistant.store import Store
from assistant.tasks import execute_task, export_task
from assistant.workflows import plan_workflow, propose_workflow


def artifact(store, sid, task, expected, source, root):
    original = source.read_bytes()
    result = execute_task(store, sid, task['id'])
    if result['status'] != 'succeeded':
        return {'passed':False, 'error':result['error']}
    output = root / 'result.json'
    exported = export_task(store, sid, task['id'], output)
    report = json.loads(output.read_text(encoding='utf-8'))
    actual = [(row['group'],row['count'],Decimal(row['sum'])) for row in report['groups']]
    wanted = [(row[0],row[1],Decimal(row[2])) for row in expected['groups']]
    passed = (actual == wanted and Decimal(report['total']) == Decimal(expected['total']) and
              report['row_count'] == expected['row_count'] and report['group_count'] == expected['group_count'] and
              report['returned_group_count'] == len(wanted) and
              Decimal(report['returned_total']) == sum((row[2] for row in wanted),Decimal(0)) and
              report['selection_limited'] == (len(wanted) < expected['group_count']) and
              report['row_count'] + report['excluded_row_count'] == report['input_row_count'] and
              report['source_sha256'] == hashlib.sha256(original).hexdigest() and source.read_bytes() == original and
              hashlib.sha256(output.read_bytes()).hexdigest() == exported['sha256'] and exported['verified'])
    return {'passed':passed, 'report':report, 'export_sha256':exported['sha256']}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    suite=json.loads((ROOT/'evals/workflows.json').read_text(encoding='utf-8'))
    if args.live and (not args.report or args.report.exists() or not args.report.resolve().is_relative_to((ROOT/'data/evaluations').resolve())):
        parser.error('Live reports require a new path under data/evaluations.')
    offline=[]
    for case in suite['cases']:
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'input.csv'
            source.write_text(suite['source'],encoding='utf-8',newline='')
            store=Store(root/'state.sqlite3'); sid=store.new_session()
            task=plan_workflow(store,sid,source,case['spec'])
            result=artifact(store,sid,task,case['expected'],source,root)
        offline.append({'id':case['id'],**result})
    passed=sum(row['passed'] for row in offline)
    print(f'Workflow artifacts: {passed}/{len(offline)}; actual CSV filters, sums, sorting and exported JSON.',flush=True)
    if passed != len(offline):
        print(json.dumps([row for row in offline if not row['passed']],ensure_ascii=False))
        sys.exit(1)
    if not args.live:
        return
    engine=CodexEngine(ROOT/'data')
    report={'completed':False,'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'fingerprints':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                            ('assistant/workflows.py','assistant/tasks.py','assistant/engine.py','evals/workflows.json')},
            'offline':offline,'live':[]}
    cases=[case for case in suite['cases'] if case.get('request')] + suite['live_extra']
    class Recorder:
        def answer(self,prompt,cancel=None):
            self.raw=engine.answer(prompt,cancel)
            return self.raw
    for case in cases:
        recorder=Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'input.csv'
            source.write_text(case.get('source',suite['source']),encoding='utf-8',newline='')
            store=Store(root/'state.sqlite3'); sid=store.new_session()
            row={'id':case['id'],'request':case['request'],'agent_review':None,'human_grade':None}
            try:
                response=propose_workflow(store,sid,source,case['request'],recorder)
                row['response_status']=response['status']
                row['raw_response']=recorder.raw
                if 'spec' in case:
                    if response['status']=='planned':
                        row['actual_spec']=response['task']['plan']['spec']
                        row['spec_matches']=row['actual_spec']==case['spec']
                        row['artifact']=artifact(store,sid,response['task'],case['expected'],source,root)
                        row['passed']=row['spec_matches'] and row['artifact']['passed']
                    else:
                        row['passed']=False
                else:
                    row['passed']=response['status']==case['status'] and not store.tasks(sid)
                    row['message']=response.get('question') or response.get('reason')
            except Exception as exc:
                row.update(passed=False,error=str(exc),raw_response=getattr(recorder,'raw',None))
        report['live'].append(row)
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(case['id'], 'artifact/status pass' if row['passed'] else 'needs review',flush=True)
    report['completed']=True
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__': main()
