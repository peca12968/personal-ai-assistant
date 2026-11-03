"""Real goal statistics over bounded synthetic JSON arrays."""
import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant import autonomy
from assistant.engine import CodexEngine, EngineError
from assistant.store import Store
from scripts.evaluate_understanding import validate_cases, fingerprint, load_report, save_report


def fixture(name):
    if name == 'large_records':
        rows=[]
        for i in range(300):
            value=(i*37 % 997)-400
            literal=('-' if value<0 else '')+str(abs(value)//10)+'.'+str(abs(value)%10)
            rows.append('{"id":"R%03d","label":"%s","metrics":{"cost":%s}}' % (i,'合成資料'*12,literal))
        return '{"records":['+','.join(rows)+']}'
    if name == 'precision':
        return '{"readings":[9007199254740993,0.1,0.2,-9007199254740993,1e-3]}'
    if name == 'missing':
        return '{"records":[{"value":10},{"value":null},{"value":20}]}'
    raise ValueError('未知JSON統計來源。')


def signature(suite):
    return {**fingerprint(suite), 'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--suite',type=Path,default=ROOT/'evals/json_statistics.json')
    parser.add_argument('--ids',nargs='+')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    try:
        cases=validate_cases(json.loads(args.suite.read_text(encoding='utf-8')))
        if args.ids:
            if set(args.ids)-{c['id'] for c in cases}:raise ValueError('未知案例ID。')
            cases=[c for c in cases if c['id'] in args.ids]
        for case in cases:fixture(case['fixture'])
        if not args.live:
            if args.report:raise ValueError('--report需搭配--live。')
            with tempfile.TemporaryDirectory() as tmp:
                for i,case in enumerate(cases):
                    store=Store(Path(tmp)/f'{i}.sqlite3');sid=store.new_session()
                    path=Path(tmp)/f'{i}.json';path.write_text(fixture(case['fixture']),encoding='utf-8')
                    store.attach_document(sid,path);autonomy.create_goal(store,sid,case['prompt'])
            print(f'Validated {len(cases)} JSON statistics scenarios; no model answers graded.')
            return
        if not args.report:raise ValueError('需指定新的本機報告路徑。')
        frozen=signature(args.suite)
        report=load_report(args.report,frozen,[c['id'] for c in cases],False)
        folder=args.report.parent/(args.report.stem+'-state');folder.mkdir(parents=True,exist_ok=False)
        failed=False
        def unchanged():
            if signature(args.suite)!=frozen:raise ValueError('來源、提示或案例已變更。')
        for i,case in enumerate(cases):
            unchanged();store=Store(folder/f'{i}.sqlite3');sid=store.new_session()
            path=folder/f'{i}.json';path.write_text(fixture(case['fixture']),encoding='utf-8')
            source_sha=hashlib.sha256(path.read_bytes()).hexdigest()
            store.attach_document(sid,path);gid=autonomy.create_goal(store,sid,case['prompt'])
            row={'id':case['id'],'category':case['category'],'criteria':case['criteria'],
                 'human_grade':None,'source_sha256':source_sha,'calls':[],'goal_id':gid,'session_id':sid,'state_path':str(store.path)}
            engine=CodexEngine(folder/f'engine-{i}')
            class Recorded:
                def answer(self,prompt,cancel=None):
                    unchanged();raw=engine.answer(prompt,cancel);unchanged()
                    row['calls'].append({'prompt':prompt,'raw':raw});return raw
            try:
                goal=autonomy.run_goal(store,sid,gid,Recorded(),max_steps=8)
            except (ValueError,EngineError) as exc:
                failed=True;row['error']=str(exc);goal=autonomy.get_goal(store,sid,gid)
            unchanged()
            if hashlib.sha256(path.read_bytes()).hexdigest()!=source_sha:raise ValueError('測試原檔被改動。')
            row.update(goal=goal,answer=goal['result'] or goal['question'] or goal['notice'])
            report['results'].append(row);save_report(args.report,report)
            print(case['id']+': '+goal['status']+'; review pending',flush=True)
        if failed:parser.exit(1,'部分流程失敗，未重試。\n')
    except (ValueError,OSError) as exc:parser.exit(1,str(exc)+'\n')


if __name__=='__main__':main()
