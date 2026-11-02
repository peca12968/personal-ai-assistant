"""Collect real answers about verified workflow outputs for rubric review."""
import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from assistant.engine import Assistant,CodexEngine
from assistant.store import Store
from assistant.tasks import execute_task
from assistant.workflows import plan_workflow


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if not args.live:
        print('Three workflow interpretation cases available; use --live with a new local report.')
        return
    if not args.report or args.report.exists() or not args.report.resolve().is_relative_to((ROOT/'data/evaluations').resolve()):
        parser.error('Use a new --report path under data/evaluations.')
    suite=json.loads((ROOT/'evals/workflows.json').read_text(encoding='utf-8'))
    cases={case['id']:case for case in suite['cases']}
    questions=[
        ('paid_top','這個工作符合條件共有幾筆、總額多少？前兩組共多少？為什麼兩者不同？原始檔案總額也是這個數字嗎？',
         '符合條件5筆總額11；前兩組B7、A5共12；可推算未列兩組合計-1，但不能捏造未提供的組名與個別值。不將11當原始7筆全部總額，該值未提供。'),
        ('no_match','結果是零，是否代表工作失敗，或原始 CSV 根本沒有資料？',
         '完成但沒有符合條件的資料；原始7筆全部排除，不能說原檔空白或工作失敗。'),
        ('combined','說明這個工作各步篩選後有幾筆，以及全部符合條件總額與列出前兩組總額。',
         '來源7筆；paid後5筆、日期文字開頭後4筆、north後3筆。全部總額7，前兩組B7與A2共9。')]
    engine=CodexEngine(ROOT/'data')
    report={'completed':False,'fingerprints':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
             ('assistant/workflows.py','assistant/tasks.py','assistant/engine.py','prompts/system.txt')},'results':[]}
    for cid,question,rubric in questions:
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'input.csv'
            source.write_text(suite['source'],encoding='utf-8',newline='')
            store=Store(root/'state.sqlite3');sid=store.new_session()
            plan=plan_workflow(store,sid,source,cases[cid]['spec'])
            result=execute_task(store,sid,plan['id'])
            if result['status']!='succeeded': raise RuntimeError(result['error'])
            answer=Assistant(store,engine).ask(sid,question)
            report['results'].append({'id':cid,'question':question,'rubric':rubric,'answer':answer['content'],
                                      'context':store.answer_context(answer['id'])['task_runs'],'agent_review':None,'human_grade':None})
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(cid,'answered; review pending',flush=True)
    report['completed']=True
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
