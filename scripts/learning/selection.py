"""Save or inspect an explicit local model choice; never load a model or train."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

if __package__ in (None,''): sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from assistant import learned_models as models, local_models


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('status','use','history'),nargs='?',default='status')
    parser.add_argument('--data-dir',type=Path,default=local_models.ROOT/'data')
    target=parser.add_mutually_exclusive_group(); target.add_argument('--batch'); target.add_argument('--base',action='store_true')
    parser.add_argument('--revision',type=int); parser.add_argument('--before',type=int)
    args=parser.parse_args()
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    if args.command=='use' and (not (args.batch or args.base) or args.revision is None): parser.error('use需要--batch或--base，及--revision。')
    if args.command!='use' and (args.batch or args.base or args.revision is not None): parser.error('選擇與版本只能搭配use。')
    if args.before is not None and args.command!='history': parser.error('--before只能搭配history。')
    try:
        if args.command=='use':
            result={'status':'saved','choice':models.choose(args.data_dir,args.batch,args.revision),
                    'loaded_into_running_engine':False,'next_launch':'--engine local --local-active'}
        elif args.command=='history': result={'status':'history','items':models.history(args.data_dir,args.before)}
        else:
            saved=models.choice(args.data_dir)
            result={'status':'observed','choice':saved,'loaded_into_running_engine':False,'assets_and_gpu_checked':False}
            try:
                selected=models.select(args.data_dir,active=True)
                if selected['choice']!=saved: raise ValueError('查詢期間模型選擇已變更，請重新讀取。')
                result.update(eligible=True,model=models.description(selected))
            except (ValueError,OSError,sqlite3.Error,KeyError,TypeError) as exc:
                result.update(eligible=False,error=str(exc))
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError) as exc: parser.exit(1,'模型選擇未完成：'+str(exc)+'\n')


if __name__=='__main__': raise SystemExit(main())
