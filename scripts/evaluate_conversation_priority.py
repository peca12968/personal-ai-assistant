"""Evaluate user requirements retained amid long assistant replies."""
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
from scripts import evaluate_understanding as fixture_builder


def signature(suite):
    return {**fingerprint(suite),'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'fixture_builder_sha256':fixture_builder.sha(fixture_builder.RUNNER_PATH.read_bytes())}


def prepare(store, case):
    if 'history' in case:
        sid=prepare_case(store,case)
        return sid,autonomy.create_goal(store,sid,case['prompt'],from_chat=True)
    sid = store.new_session()
    store.add_message(sid, 'user', case['message'])
    for i in range(8):
        store.add_message(sid, 'assistant', case['assistant_text']*150)
        if i < 7:
            text = case.get('revision') if i == 0 and case.get('revision') else '條件沒有變，繼續說明。'
            store.add_message(sid, 'user', text)
    return sid, autonomy.create_goal(store, sid, case['prompt'], from_chat=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--suite',type=Path,default=ROOT/'evals/conversation_priority.json')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    try:
        cases=validate_cases(json.loads(args.suite.read_text(encoding='utf-8')))
        if not args.live:
            if args.report:raise ValueError('--report需搭配--live。')
            with tempfile.TemporaryDirectory() as tmp:
                for i,case in enumerate(cases):
                    store=Store(Path(tmp)/f'{i}.sqlite3')
                    prepare(store,case)
            print(f'Validated {len(cases)} conversation-priority goal fixtures; no model answers graded.')
            return
        if not args.report:raise ValueError('需指定新的本機報告路徑。')
        frozen=signature(args.suite)
        report=load_report(args.report,frozen,[c['id'] for c in cases],False)
        folder=args.report.parent/(args.report.stem+'-state');folder.mkdir(parents=True,exist_ok=False)
        failed=False
        def unchanged():
            if signature(args.suite)!=frozen:raise ValueError('來源、提示、腳本或案例已變更。')
        for i,case in enumerate(cases):
            unchanged();store=Store(folder/f'{i}.sqlite3');sid,gid=prepare(store,case)
            row={'id':case['id'],'category':case['category'],'criteria':case['criteria'],'human_grade':None,
                 'goal_id':gid,'session_id':sid,'state_path':str(store.path),'calls':[]}
            engine=CodexEngine(folder/f'engine-{i}')
            class Recorded:
                def answer(self,prompt,cancel=None):
                    unchanged();raw=engine.answer(prompt,cancel);unchanged()
                    row['calls'].append({'prompt':prompt,'raw':raw});return raw
            try:goal=autonomy.run_goal(store,sid,gid,Recorded(),max_steps=8)
            except (ValueError,EngineError) as exc:
                failed=True;row['error']=str(exc);goal=autonomy.get_goal(store,sid,gid)
            unchanged();row.update(goal=goal,answer=goal['result'] or goal['question'] or goal['notice'])
            report['results'].append(row);save_report(args.report,report)
            print(case['id']+': '+goal['status']+'; review pending',flush=True)
        if failed:parser.exit(1,'部分流程失敗，未自動重試。\n')
    except (ValueError,OSError) as exc:parser.exit(1,str(exc)+'\n')


if __name__=='__main__':main()
