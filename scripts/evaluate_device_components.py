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
    report=dict(schema='device_components_v1',fingerprint=stamp(),engine=selection,cases=rows,results=[])
    save(folder/'report.json',report)
    for case in rows:
        request=reasoning.prompt(reasoning.plan(case['question']))
        result=dict(id=case['id'],prompt=request,prompt_sha256=hashlib.sha256(request.encode()).hexdigest(),status='running')
        report['results'].append(result)
        save(folder/'report.json',report)
        try:
            raw=engine.answer(request)
            result['raw_answer']=raw
            values=reasoning.decode(raw)
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
    print(f"{sum(r['status']=='pass' for r in report['results'])}/{len(rows)} complete component judgments matched")


if __name__=='__main__':
    main()
