"""Explicit checkpointed segments for a fresh, unadapted confirmed-example batch."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.learning import idle_segments as legacy
from scripts.learning import base_segment_worker as training
from scripts.learning.runtime import gpu_lease
from assistant import idle_learning as idle, learning_batches as batches, local_models

PARENT = ROOT/'data/learning/base-idle-segments'
read = local_models.read_json
digest = batches.queue.digest
gpu = legacy.gpu
identifier, checksum = legacy.identifier, legacy.checksum
ledger, stored_plan = legacy.ledger, legacy.stored_plan
head, follows, pin_blob = legacy.head, legacy.follows, legacy.pin_blob


def sources():
    paths = [Path(__file__), Path(training.__file__), Path(legacy.__file__),
             Path(gpu.__file__), Path(gpu.ck.__file__)]
    return {p.relative_to(ROOT).as_posix(): batches.file_hash(p) for p in paths}


def reference(data_dir, job_id):
    path = idle.folder(data_dir, job_id)
    job = idle.read_manifest(path, live=True)
    receipt = read(path/'receipt.json')
    if (receipt.get('schema') != 'idle_learning_receipt_v1' or receipt.get('status') != 'prepared'
            or receipt.get('manifest_sha256') != digest(job)):
        raise ValueError('參考工作必須尚未開始；不接管已執行或取消的工作。')
    manifest, batch_receipt = idle.prepared_batch(data_dir, job['batch_id'])
    if (manifest['parent'] is not None or not manifest['checks']
            or digest(manifest) != job['batch_manifest_sha256']
            or batch_receipt != job['batch_receipt_sha256']):
        raise ValueError('此入口只支援有保留題、尚未執行的原始底座批次。')
    return job, manifest, batches.file_hash(path/'receipt.json'), batch_receipt


def prepare(data_dir, job_id, steps):
    data_dir = str(Path(data_dir).resolve())
    job_id = identifier(job_id)
    job, manifest, job_receipt, batch_receipt = reference(data_dir, job_id)
    if type(steps) is not int or not 1 <= steps <= manifest['recipe']['steps']:
        raise ValueError('總步數必須在固定配方範圍內。')
    session = uuid.uuid4().hex
    plan = dict(schema='base_idle_segments_v1', session=session, training_steps=steps,
                data_dir=data_dir, reference_job=job_id, job_digest=digest(job),
                batch_manifest_sha256=digest(manifest), job_receipt_sha256=job_receipt,
                batch_receipt_sha256=batch_receipt, sources=sources(),
                idle_ms=max(gpu.IDLE_MS, job['idle_seconds']*1000))
    folder = PARENT/session
    folder.mkdir(parents=True, exist_ok=False)
    for name, sha in plan['sources'].items():
        target = folder/'sources'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, target)
        if batches.file_hash(target) != sha:
            raise batches.Stale('準備來源副本時程式已變更。')
    if sources() != plan['sources'] or reference(data_dir, job_id) != (job, manifest, job_receipt, batch_receipt):
        raise batches.Stale('準備期間的程式或參考工作已變更。')
    with closing(sqlite3.connect(folder/'ledger.sqlite3')) as db, db:
        db.execute('CREATE TABLE boundaries (step INTEGER PRIMARY KEY, sha TEXT UNIQUE NOT NULL, body TEXT NOT NULL)')
        db.execute('CREATE TABLE preparation (sha TEXT NOT NULL)')
        db.execute('INSERT INTO preparation VALUES(?)', (digest(plan),))
    gpu.save(folder/'plan.json', plan)
    return session


def verify_plan(folder, plan):
    if (plan.get('schema') != 'base_idle_segments_v1' or folder.name != plan['session']
            or stored_plan(folder) != plan or plan['sources'] != sources()):
        raise batches.Stale('底座分段規約或程式來源已變更。')
    if any(batches.file_hash(folder/'sources'/name) != sha for name, sha in plan['sources'].items()):
        raise batches.Stale('底座分段的來源副本已變更。')
    job, manifest, job_receipt, batch_receipt = reference(plan['data_dir'], plan['reference_job'])
    if (digest(job) != plan['job_digest'] or digest(manifest) != plan['batch_manifest_sha256']
            or job_receipt != plan['job_receipt_sha256'] or batch_receipt != plan['batch_receipt_sha256']
            or type(plan['training_steps']) is not int
            or not 1 <= plan['training_steps'] <= manifest['recipe']['steps']
            or plan['idle_ms'] != max(gpu.IDLE_MS, job['idle_seconds']*1000)):
        raise batches.Stale('確認資料、參考工作或起始批次已變更。')
    return manifest


def segment(session, operation, updates=None, receipt=None):
    folder = PARENT/identifier(session)
    plan = stored_plan(folder)
    with batches.owner(folder), closing(ledger(folder)) as db:
        verify_plan(folder, plan)
        before = head(db, folder, plan)
        if operation == 'start':
            if before or receipt is not None:
                raise ValueError('已有提交進度，請使用最新交接指紋明確接續。')
        elif operation in ('continue', 'control'):
            if not before or checksum(receipt) != digest(before):
                raise ValueError('請指定最新已提交的交接指紋。')
            pin_blob(folder, before['blob'])
        else:
            raise ValueError('未知工作。')
        control = operation == 'control'
        if control and (plan['training_steps'] != 4 or before['end'] != 4):
            raise ValueError('連續對照僅限完成四步的規約。')
        start = before['end'] if before and not control else 0
        if not control and (type(updates) is not int or updates < 1):
            raise ValueError('本段步數必須是正整數。')
        end = plan['training_steps'] if control else start+updates
        if not start < end <= plan['training_steps']:
            raise ValueError('本段超過剩餘步數。')
        admitted = idle.idle_sample()
        if admitted['idle_ms'] < plan['idle_ms']:
            raise batches.Stopped('尚未達到原生閒置門檻。')
        with gpu_lease():
            pass
        jid = uuid.uuid4().hex
        job = folder/'jobs'/jid
        job.mkdir(parents=True, exist_ok=False)
        if control:
            with (folder/'control-attempt').open('x', encoding='ascii') as marker:
                marker.write(jid)
        phase = 'continuous' if control else 'resumed' if start else 'first'
        request = dict(plan=digest(plan), job=jid, phase=phase, admitted=admitted, start=start, end=end,
                       from_blob=before['blob'] if start else None, from_receipt=receipt)
        gpu.save(job/'request.json', request)
        deadline = time.monotonic()+360
        def check():
            gpu.input_gate(admitted)
            if time.monotonic() >= deadline:
                raise TimeoutError('本段達到六分鐘時限。')
            if read(job/'request.json') != request:
                raise batches.Stale('本段請求已變更。')
            verify_plan(folder, plan)
        try:
            check()
            command = [str(ROOT/'data/learning/runtime/Scripts/python.exe'), '-I', str(Path(__file__).resolve()),
                       '_worker', session, jid, digest(request)]
            gpu.run_owned(command, job, phase, lambda: gpu.input_gate(admitted), check)
            row = legacy.evidence(folder, plan, jid)
            pin_blob(folder, row['blob'])
            check()
            if control:
                previous = read(folder/'jobs'/before['job']/(before['phase']+'.json'))
                actual = read(job/'continuous.json')
                equal = {k: previous[k] == actual[k] for k in
                         ('sha', 'binding', 'weights', 'history', 'generated', 'logits_sha256', 'rows')}
                passed = all(equal.values()) and actual['weights'] != actual['binding']['initial_adapter_sha256']
                result = dict(job=jid, status='verified' if passed else 'mismatch', equal=equal)
                gpu.save(job/'control-result.json', result)
                return result
            with db:
                db.execute('BEGIN IMMEDIATE')
                if head(db, folder, plan) != before or not follows(row, before):
                    raise ValueError('交接起點已變更。')
                check()
                db.execute('INSERT INTO boundaries VALUES(?,?,?)', (end, digest(row), batches.queue.encoded(row)))
            return dict(job=jid, status='committed', step=end, receipt=digest(row), automatic_activation=False)
        except BaseException as exc:
            gpu.save(job/'failure.json', dict(type=type(exc).__name__, reason=str(exc)))
            raise


def worker(session, jid, request_sha):
    folder = PARENT/identifier(session)
    job = folder/'jobs'/identifier(jid)
    request, plan = read(job/'request.json'), stored_plan(folder)
    phase = request['phase']
    if (digest(request) != checksum(request_sha) or request['plan'] != digest(plan)
            or request['job'] != jid or phase not in gpu.PHASES):
        raise ValueError('工作請求指紋不符。')
    with (job/(phase+'.started')).open('x', encoding='ascii') as marker:
        marker.write(str(os.getpid()))
    manifest = verify_plan(folder, plan)
    gpu.input_gate(request['admitted'])
    with gpu_lease():
        training.run(manifest, plan, request, folder, job, lambda: verify_plan(folder, plan))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('--data-dir', required=True, type=Path)
    prep.add_argument('--reference-job', required=True)
    prep.add_argument('--steps', required=True, type=int)
    for name in ('start', 'continue', 'control', 'status'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--session', required=True)
        if name in ('start', 'continue'):
            cmd.add_argument('--updates', required=True, type=int)
        if name in ('continue', 'control'):
            cmd.add_argument('--receipt', required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        result = dict(session=prepare(args.data_dir, args.reference_job, args.steps), model_started=False)
    elif args.command == 'status':
        folder = PARENT/identifier(args.session)
        plan = stored_plan(folder)
        verify_plan(folder, plan)
        with closing(ledger(folder, readonly=True)) as db:
            current = head(db, folder, plan)
        result = dict(step=current['end'] if current else 0, receipt=digest(current) if current else None,
                      scope='historical_committed_boundary', automatic_activation=False)
    else:
        result = segment(args.session, args.command, getattr(args, 'updates', None), getattr(args, 'receipt', None))
    print(json.dumps(result, ensure_ascii=False))
    return int(result.get('status') == 'mismatch')


if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['_worker'] and len(sys.argv) == 5:
            worker(*sys.argv[2:])
        else:
            raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, sqlite3.Error, KeyError, TypeError) as exc:
        raise SystemExit('底座分段工作未完成：'+str(exc))
