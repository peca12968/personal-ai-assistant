"""Offline protocol validation or an explicit local-model device component experiment."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import device_reasoning as reasoning
from scripts.evaluate_daily_conversations import fingerprint, check_cases
from scripts.learning.check_gpu_checkpoint import save

SUITE=ROOT/'evals/daily_backup_devices.json'
LABELS={
 'ready':['能','資料不足','能'],
 'uncertain':['能','資料不足','不能'],
 'down':['能','不能','不能'],
 'common':['不能','資料不足','能'],
}


def cases():
    rows=json.loads(SUITE.read_text(encoding='utf8'))
    check_cases(rows)
    for row in rows:
        group,order=row['id'].rsplit('-',1)
        row['component_answers']=LABELS[group].copy()
        if order=='2':
            row['component_answers'][1:]=reversed(row['component_answers'][1:])
        if not reasoning.plan(row['question']) or reasoning.combine(row['component_answers'])!=row['answer']:
            raise ValueError('題目無法完整拆解或參考判斷與最終答案不一致。')
    return rows


def stamp():
    sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path(__file__).resolve(),Path(reasoning.__file__))}
    sources['daily']=fingerprint(SUITE)
    return hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',choices=('original','status','isolated'),default='original')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--out')
    args=parser.parse_args()
    rows=cases()
    if not args.live:
        if args.out: parser.error('--out requires --live')
        print(f'{len(rows)} component protocols validated; no model inference')
        return
    if not args.out or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',args.out):
        parser.error('--live requires a new --out run name')
    from assistant.local_engine import LocalEngine
    folder=ROOT/'data/evaluations'/args.out
    folder.mkdir(parents=True,exist_ok=False)
    engine=LocalEngine(folder)
    selection=engine.describe()
    if selection.get('parameter_version')!='unadapted_base':
        raise ValueError('This experiment requires the original local model.')
    report=dict(schema='device_components_v1',profile=args.profile,fingerprint=stamp(),engine=selection,cases=rows,results=[])
    save(folder/'report.json',report)
    for case in rows:
        build = reasoning.status_prompt if args.profile == 'status' else reasoning.prompt
        plan=reasoning.plan(case['question'])
        request=build(plan)
        result=dict(id=case['id'],status='running')
        if args.profile != 'isolated':
            result.update(prompt=request,prompt_sha256=hashlib.sha256(request.encode()).hexdigest())
        report['results'].append(result)
        save(folder/'report.json',report)
        try:
            if args.profile == 'isolated':
                traces = []
                result['component_calls'] = traces
                for text in reasoning.isolated_prompts(plan):
                    call = dict(prompt=text, prompt_sha256=hashlib.sha256(text.encode()).hexdigest())
                    traces.append(call)
                    save(folder/'report.json',report)
                    call['raw_answer'] = engine.answer(text)
                    save(folder/'report.json',report)
                    if stamp()!=report['fingerprint'] or engine.describe()!=selection:
                        raise ValueError('Sources or engine changed during isolated collection.')
                values = [reasoning.decode_isolated(call['raw_answer']) for call in traces]
                if any(label is None for label in values):
                    values = None
            else:
                raw=engine.answer(request)
                result['raw_answer']=raw
                parse = reasoning.decode_status if args.profile == 'status' else reasoning.decode
                values=parse(raw)
            result['component_matches'] = [a==b for a,b in zip(values,case['component_answers'],strict=True)] if values else [False]*3
            result.update(labels=values,answer=reasoning.combine(values) if values else None,
                          components_correct=values==case['component_answers'])
            result['status']='pass' if values==case['component_answers'] and result['answer']==case['answer'] else 'wrong_answer'
            if stamp()!=report['fingerprint'] or engine.describe()!=selection:
                raise ValueError('Experiment sources or selected engine changed during collection.')
        except Exception as exc:
            result.update(status='error',error=str(exc))
            save(folder/'report.json',report)
            raise
        save(folder/'report.json',report)
        print(case['id'],result['status'],flush=True)
    report['summary'] = dict(
        complete_correct=sum(r['status']=='pass' for r in report['results']),
        final_correct=sum(r['answer']==c['answer'] for r,c in zip(report['results'],rows,strict=True)),
        component_correct=[sum(r['component_matches'][i] for r in report['results']) for i in range(3)],
        count=len(rows))
    save(folder/'report.json',report)
    print('Common, first and second component correct counts:', report['summary']['component_correct'])
    print(f"{sum(r['status']=='pass' for r in report['results'])}/{len(rows)} complete component judgments matched")


if __name__=='__main__':
    main()
