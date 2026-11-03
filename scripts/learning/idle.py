"""Prepare and explicitly run one idle-triggered batch plus full-assistant checks."""
import argparse,json,signal,sqlite3,sys,threading
from pathlib import Path
if __package__ in (None,''): sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from assistant import idle_learning as idle

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('check','observe','prepare','run','report'),nargs='?',default='check')
    parser.add_argument('--data-dir',type=Path,default=idle.ROOT/'data')
    parser.add_argument('--batch'); parser.add_argument('--suite',type=Path); parser.add_argument('--id')
    parser.add_argument('--idle-seconds',type=int); parser.add_argument('--wait-seconds',type=int); parser.add_argument('--work-seconds',type=int)
    args=parser.parse_args()
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    if args.command=='prepare' and (args.batch is None or args.suite is None): parser.error('prepare需要--batch與--suite。')
    if args.command in ('run','report') and args.id is None: parser.error('run/report需要--id。')
    if args.command!='prepare' and any(v is not None for v in (args.batch,args.suite,args.idle_seconds,args.wait_seconds,args.work_seconds)):
        parser.error('批次、題庫與時間設定只能搭配prepare。')
    if args.id is not None and args.command not in ('run','report'): parser.error('--id只可搭配run/report。')
    try:
        if args.command=='check':
            print('Once-only idle jobs available; not started, no database opened, no model loaded.'); return 0
        if args.command=='observe': result={'observation':idle.idle_sample(),'scope':'current_windows_session','not_a_start_request':True}
        elif args.command=='prepare':
            suite=idle.models.local_models.read_json(args.suite,1024*1024)
            options={k:getattr(args,k) for k in ('idle_seconds','wait_seconds','work_seconds') if getattr(args,k) is not None}
            result={'id':idle.prepare(args.data_dir,args.batch,suite,**options),'status':'prepared'}
        elif args.command=='run':
            cancel=threading.Event(); old=signal.signal(signal.SIGINT,lambda *_:cancel.set())
            try: result=idle.run(args.data_dir,args.id,cancel=cancel)
            finally: signal.signal(signal.SIGINT,old)
        else: result=idle.report(args.data_dir,args.id)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        status=result.get('receipt',result).get('status')
        return 0 if (status is None or status in ('prepared','validated_candidate','already_satisfied')) and result.get('sources_still_current') is not False else 1
    except (ValueError,OSError,RuntimeError,KeyError,TypeError,sqlite3.Error) as exc: parser.exit(1,'閒置工作未完成：'+str(exc)+'\n')

if __name__=='__main__': raise SystemExit(main())
