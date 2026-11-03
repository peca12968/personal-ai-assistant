"""Explicit finite local training from confirmed examples; no idle scheduling."""
import argparse
import json
import sys
import sqlite3
from pathlib import Path

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from assistant import learning_batches as batches, local_models


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('check','list','prepare','run','status'),nargs='?',default='check')
    parser.add_argument('--data-dir',type=Path,default=batches.ROOT/'data')
    parser.add_argument('--item',action='append',default=[],help='明確選取項目ID:版本，可重複指定')
    parser.add_argument('--parent',help='沿用已驗證候選批次，並重播其全部確認例子')
    parser.add_argument('--checks',type=Path,help='額外保留問題與答案JSON清單；不加入訓練')
    parser.add_argument('--batch')
    parser.add_argument('--page',type=int,default=1)
    args=parser.parse_args()
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    try:
        if args.command=='check':
            print('Finite batch protocol available; no database opened, no ML imports or training.')
            return 0
        from assistant.store import Store
        if not (args.data_dir/'assistant.sqlite3').is_file(): raise ValueError('資料庫不存在，未建立空白個人資料庫。')
        store=Store(args.data_dir/'assistant.sqlite3')
        if args.command=='list':
            from assistant.learning_queue import page
            value=page(store,args.page)
            result={'status':'listed','page':value['page'],'pages':value['pages'],'total':value['total'],
                    'items':[{k:row[k] for k in ('id','revision','readiness','prompt')} for row in value['items']]}
        elif args.command=='prepare':
            selected=[]
            for raw in args.item:
                parts=raw.split(':')
                if len(parts)!=2 or not parts[1].isascii() or not parts[1].isdigit(): raise ValueError('--item需為ID:版本。')
                selected.append({'id':parts[0],'revision':int(parts[1])})
            checks=local_models.read_json(args.checks,256*1024) if args.checks else None
            result={'batch_id':batches.prepare(store,selected,checks=checks,parent=args.parent),'status':'prepared'}
        elif args.command=='run':
            result=batches.run(store,args.batch)
        else:
            path=batches.folder(args.data_dir,args.batch); manifest=batches.read_manifest(path)
            result=local_models.read_json(path/'receipt.json')
            try:
                batches.examples_current(store,manifest); result['examples_still_current']=True
            except ValueError:
                result['examples_still_current']=False
            result['recorded_status_is_not_liveness']=True
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 1 if result['status'] in {'failed','cancelled','stale','rejected'} else 0
    except (ValueError,OSError,RuntimeError,sqlite3.Error) as exc:
        parser.exit(1,'批次未完成：'+str(exc)+'\n')


if __name__=='__main__': raise SystemExit(main())
