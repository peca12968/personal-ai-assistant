"""Explicit, separately admitted GPU training segments with a durable checkpoint ledger."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.learning import check_gpu_checkpoint as gpu
from assistant import idle_learning as idle, learning_batches as batches, local_models
from scripts.learning.runtime import gpu_lease

PARENT = ROOT/'data/learning/idle-segments'
read = local_models.read_json
digest = batches.queue.digest


def identifier(value):
    return batches.identifier(value)


def checksum(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('無效的 SHA-256。')
    return value


def prepare(data_dir, job_id, steps):
    data_dir = str(Path(data_dir).resolve())
    job = idle.read_manifest(idle.folder(data_dir, job_id), live=True)
    session = uuid.uuid4().hex
    plan = {'schema':'idle_segments_v1', 'session':session, 'training_steps':steps, 'data_dir':data_dir,
            'reference_job':job_id, 'job_digest':digest(job), 'sources':gpu.sources(),
            'controller':batches.file_hash(Path(__file__)), 'idle_ms':max(gpu.IDLE_MS,job['idle_seconds']*1000)}
    manifest = gpu.inputs(plan)
    if type(steps) is not int or not 1 <= steps <= manifest['recipe']['steps'] or not manifest['checks']:
        raise ValueError('步數必須在原配方的範圍內，並至少需要一道保留題。')
    gpu.parent_evidence(plan,manifest)
    folder = PARENT/session
    folder.mkdir(parents=True,exist_ok=False)
    with closing(sqlite3.connect(folder/'ledger.sqlite3')) as db, db:
        db.execute('CREATE TABLE boundaries (step INTEGER PRIMARY KEY, sha TEXT UNIQUE NOT NULL, body TEXT NOT NULL)')
        db.execute('CREATE TABLE preparation (sha TEXT NOT NULL)')
        db.execute('INSERT INTO preparation VALUES(?)',(digest(plan),))
    gpu.save(folder/'plan.json',plan)
    return session


def stored_plan(folder):
    plan = read(folder/'plan.json')
    with closing(ledger(folder, readonly=True)) as db:
        if db.execute('SELECT sha FROM preparation').fetchall() != [(digest(plan),)]:
            raise batches.Stale('原先準備的訓練規約指紋不符。')
    return plan


def verify_plan(folder, plan):
    if (plan.get('schema') != 'idle_segments_v1' or folder.name != plan['session']
            or plan['controller'] != batches.file_hash(Path(__file__)) or stored_plan(folder) != plan):
        raise batches.Stale('訓練規約或控制程式已變更。')
    return gpu.inputs(plan)


def ledger(folder, *, readonly=False):
    mode = 'ro' if readonly else 'rw'
    return sqlite3.connect((folder/'ledger.sqlite3').resolve().as_uri()+'?mode='+mode,uri=True,timeout=0)


def evidence(folder, plan, job_id):
    job = folder/'jobs'/identifier(job_id)
    request = read(job/'request.json')
    phase = request['phase']
    if phase not in gpu.PHASES or request['plan'] != digest(plan) or request['job'] != job_id:
        raise ValueError('工作來源與訓練規約不符。')
    record = read(job/(phase+'.json'))
    exit_record = read(job/(phase+'.exit.json'))
    if (exit_record['exit_confirmed'] is not True or exit_record['returncode'] != 0
            or exit_record['worker'] != record['pid'] or record['phase'] != phase
            or record['binding']['experiment'] != request['plan'] or record['consumed'] != request['from_blob']
            or record['step'] != request['end'] or len(record['history']) != request['end']):
        raise ValueError('工作結果、步數、來源或程序退出紀錄不符。')
    if request['end'] == plan['training_steps']:
        if batches.file_hash(job/(phase+'.logits')) != record['logits_sha256']:
            raise ValueError('保留輸入的實際輸出檔案不符。')
    return {'plan':request['plan'], 'job':job_id, 'phase':phase, 'start':request['start'], 'end':request['end'],
            'parent':request['from_receipt'], 'from_blob':request['from_blob'], 'blob':checksum(record['sha']),
            'request_digest':digest(request), 'result_digest':digest(record), 'exit_digest':digest(exit_record)}


def head(db, folder, plan):
    previous = None
    for step, sha, body in db.execute('SELECT step,sha,body FROM boundaries ORDER BY step'):
        row = json.loads(body)
        if (digest(row) != sha or row != evidence(folder,plan,row['job'])
                or not follows(row, previous) or row['end'] != step or step > plan['training_steps']):
            raise ValueError('已提交的交接鏈或來源紀錄不符。')
        previous = row
    return previous


def follows(row, previous):
    return (row['phase'] == ('resumed' if previous else 'first')
            and row['parent'] == (digest(previous) if previous else None)
            and row['from_blob'] == (previous['blob'] if previous else None)
            and row['start'] == (previous['end'] if previous else 0)
            and row['end'] > row['start'])


def pin_blob(folder, sha):
    if batches.file_hash(folder/'checkpoints'/(checksum(sha)+'.safetensors')) != sha:
        raise ValueError('檢查點實體檔案指紋不符。')


def segment(session, operation, updates, receipt):
    folder = PARENT/identifier(session)
    plan = stored_plan(folder)
    with batches.owner(folder), closing(ledger(folder)) as db:
        manifest = verify_plan(folder,plan)
        before = head(db,folder,plan)
        if operation == 'start':
            if before or receipt is not None:
                raise ValueError('此訓練已有進度或起點無效。')
        elif operation in ('continue','control'):
            if not before or checksum(receipt) != digest(before):
                raise ValueError('請指定目前已提交的交接紀錄指紋。')
            pin_blob(folder,before['blob'])
        else:
            raise ValueError('未知的工作。')
        control = operation == 'control'
        if control and (plan['training_steps'] != 4 or before['end'] != 4):
            raise ValueError('連續對照限於已完成的四步實驗。')
        start = before['end'] if before and not control else 0
        if not control and type(updates) is not int:
            raise ValueError('本次更新步數必須是整數。')
        end = plan['training_steps'] if control else start + updates
        if not start < end <= plan['training_steps']:
            raise ValueError('本次工作步數超過剩餘範圍。')
        gpu.parent_evidence(plan,manifest)
        admitted = idle.idle_sample()
        if admitted['idle_ms'] < plan['idle_ms']:
            raise batches.Stopped('尚未達到本次閒置門檻。')
        with gpu_lease():
            pass
        jid = uuid.uuid4().hex
        job = folder/'jobs'/jid
        job.mkdir(parents=True,exist_ok=False)
        if control:
            with (folder/'control-attempt').open('x') as stream:
                stream.write(jid)
        phase = 'continuous' if control else 'resumed' if start else 'first'
        request = {'plan':digest(plan),'job':jid,'phase':phase,'admitted':admitted,'start':start,'end':end,
                   'from_blob':before['blob'] if start else None,'from_receipt':receipt}
        gpu.save(job/'request.json',request)
        deadline = time.monotonic() + 360
        def check():
            gpu.input_gate(admitted)
            if time.monotonic() >= deadline:
                raise TimeoutError('本次工作已達六分鐘。')
            if read(job/'request.json') != request:
                raise batches.Stale('本次工作規約已變更。')
            verify_plan(folder,plan)
        try:
            check()
            command = [str(ROOT/'data/learning/runtime/Scripts/python.exe'),'-I',str(Path(__file__).resolve()),
                       '_worker',session,jid,digest(request)]
            gpu.run_owned(command,job,phase,lambda:gpu.input_gate(admitted),check)
            row = evidence(folder,plan,jid)
            pin_blob(folder,row['blob'])
            gpu.parent_evidence(plan,verify_plan(folder,plan))
            check()
            if control:
                prior = read(folder/'jobs'/before['job']/(before['phase']+'.json'))
                actual = read(job/'continuous.json')
                equal = {key:prior[key]==actual[key] for key in ('sha','binding','weights','history','generated','logits_sha256')}
                result = {'job':jid,'status':'verified' if all(equal.values()) and actual['weights']!=actual['binding']['initial_adapter_sha256'] else 'mismatch','equal':equal}
                gpu.save(job/'control-result.json',result)
                return result
            with db:
                db.execute('BEGIN IMMEDIATE')
                if head(db,folder,plan) != before or not follows(row,before):
                    raise ValueError('交接起點已變更。')
                check()
                db.execute('INSERT INTO boundaries VALUES(?,?,?)',(end,digest(row),batches.queue.encoded(row)))
            return {'job':jid,'status':'committed','step':end,'receipt':digest(row)}
        except BaseException as exc:
            gpu.save(job/'failure.json',{'type':type(exc).__name__,'reason':str(exc)})
            raise


def worker(session,jid,request_sha):
    folder=PARENT/identifier(session)
    job=folder/'jobs'/identifier(jid)
    request=read(job/'request.json')
    plan=read(folder/'plan.json')
    phase=request['phase']
    if (phase not in gpu.PHASES or digest(request)!=checksum(request_sha)
            or request['plan']!=digest(plan) or request['job']!=jid):
        raise ValueError('工作規約不符。')
    with (job/(phase+'.started')).open('x',encoding='ascii') as marker:
        marker.write(str(os.getpid()))
    verify_plan(folder,plan)
    gpu.input_gate(request['admitted'])
    with gpu_lease():
        gpu.worker(job,phase,plan,request['from_blob'],admitted=request['admitted'],
                   checkpoints=folder/'checkpoints',start=request['start'],end=request['end'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    prep=commands.add_parser('prepare')
    prep.add_argument('--data-dir',required=True,type=Path)
    prep.add_argument('--reference-job',required=True)
    prep.add_argument('--steps',required=True,type=int)
    for name in ('start','continue','control','status'):
        cmd=commands.add_parser(name)
        cmd.add_argument('--session',required=True)
        if name in ('start','continue'):
            cmd.add_argument('--updates',required=True,type=int)
        if name in ('continue','control'):
            cmd.add_argument('--receipt',required=True)
    args=parser.parse_args()
    if args.command=='prepare':
        result={'session':prepare(args.data_dir,args.reference_job,args.steps)}
    elif args.command=='status':
        folder=PARENT/identifier(args.session)
        with closing(ledger(folder,readonly=True)) as db:
            latest=head(db,folder,stored_plan(folder))
        result={'step':latest['end'] if latest else 0,'receipt':digest(latest) if latest else None,
                'scope':'historical_committed_boundary'}
    else:
        result=segment(args.session,args.command,getattr(args,'updates',None),getattr(args,'receipt',None))
    print(json.dumps(result,ensure_ascii=False))
    return int(result.get('status')=='mismatch')


if __name__=='__main__':
    if sys.argv[1:2]==['_worker'] and len(sys.argv)==5:
        worker(*sys.argv[2:])
    else:
        try:
            raise SystemExit(main())
        except (ValueError,OSError,RuntimeError,sqlite3.Error) as exc:
            raise SystemExit('工作未完成：'+str(exc))
