"""Explicit prepare/run/report of frozen full-assistant local candidate checks."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

if __package__ in (None,''): sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from assistant import parameter_acceptance as checks


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('check','prepare','run','report'),nargs='?',default='check')
    parser.add_argument('--data-dir',type=Path,default=checks.ROOT/'data')
    parser.add_argument('--candidate'); parser.add_argument('--baseline',help='原版批次ID；省略則使用原始底座')
    parser.add_argument('--suite',type=Path); parser.add_argument('--id')
    args=parser.parse_args()
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    if args.command=='prepare' and (args.candidate is None or args.suite is None): parser.error('prepare需要--candidate與--suite。')
    if args.command in ('run','report') and args.id is None: parser.error('run/report需要--id。')
    if args.command!='prepare' and any(value is not None for value in (args.candidate,args.baseline,args.suite)):
        parser.error('--candidate、--baseline與--suite只可搭配prepare。')
    if args.id is not None and args.command not in ('run','report'): parser.error('--id只可搭配run/report。')
    try:
        if args.command=='check':
            print('Full-assistant validation available; no database opened, no model loaded, no training or activation. Model-only comparison: direct local arithmetic routing is disabled.')
            return 0
        if args.command=='prepare':
            data=checks.models.local_models.read_json(args.suite,1024*1024)
            result={'id':checks.prepare(args.data_dir,args.candidate,data,baseline=args.baseline),'status':'prepared'}
        elif args.command=='run': result=checks.run(args.data_dir,args.id)
        else: result=checks.report(args.data_dir,args.id)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        receipt=result.get('receipt',result)
        return 1 if (receipt.get('status') in ('failed','stale','cancelled') or receipt.get('assessment',{}).get('passed') is False
                     or result.get('sources_and_parameters_still_current') is False) else 0
    except (ValueError,OSError,RuntimeError,KeyError,TypeError,sqlite3.Error) as exc: parser.exit(1,'完整驗收未完成：'+str(exc)+'\n')


if __name__=='__main__': raise SystemExit(main())
