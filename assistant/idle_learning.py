"""Once-only idle admission of a frozen batch, followed by full-assistant checks."""
import ctypes
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from . import learning_batches as batches, learned_models as models, parameter_acceptance as checks
from scripts.learning.runtime import gpu_lease

ROOT=batches.ROOT
POLICY={'automatic_activation':False,'automatic_retry':False,'poll_seconds':1,'input_scope':'current_windows_session'}


def capabilities():
    return {'idle_training_implemented':True,'persistent_idle_scheduler_implemented':False,'desktop_idle_jobs_available':True,
            'idle_training_scope':'明確準備並啟動的一次Windows閒置工作；桌面入口為本機學習佇列 → 閒置學習工作。不是保存佇列後自動排程或開機常駐。',
            'manual_training':'scripts/learning/batches.py仍可手動準備／執行有限批次；另可用scripts/learning/idle.py prepare指定尚未執行的批次與完整助理驗收題庫，再run啟動一次等待。確認佇列不啟動工作。',
            'idle_training':'目前工作階段輸入閒置達門檻且共用GPU鎖可用後，訓練並驗收；輸入恢復、撤回、觀察失敗或超時就停止，不自動續跑。observe只觀察。',
            'idle_candidate':'通過才標為待選用候選，沒有自動啟用或改變保存選擇；需要另外明確選用。'}


def idle_sample():
    if os.name!='nt': raise OSError('閒置偵測目前只支援Windows工作階段。')
    from ctypes import wintypes
    class LASTINPUTINFO(ctypes.Structure):
        _fields_=[('cbSize',wintypes.UINT),('dwTime',wintypes.DWORD)]
    user=ctypes.WinDLL('user32',use_last_error=True); kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    get=user.GetLastInputInfo; get.argtypes=[ctypes.POINTER(LASTINPUTINFO)]; get.restype=wintypes.BOOL
    tick=kernel.GetTickCount64; tick.argtypes=[]; tick.restype=ctypes.c_ulonglong
    info=LASTINPUTINFO(); info.cbSize=ctypes.sizeof(info)
    if not get(ctypes.byref(info)): raise ctypes.WinError(ctypes.get_last_error())
    now=tick()
    return validate_sample({'ticks_ms':now,'last_input_tick':info.dwTime,'idle_ms':((now&0xffffffff)-info.dwTime)&0xffffffff})


def validate_sample(value):
    if (not isinstance(value,dict) or set(value)!={'ticks_ms','last_input_tick','idle_ms'}
            or any(type(v) is not int for v in value.values()) or not 0<=value['ticks_ms']<2**64
            or not 0<=value['last_input_tick']<2**32 or not 0<=value['idle_ms']<=86400000
            or value['idle_ms']!=((value['ticks_ms']&0xffffffff)-value['last_input_tick'])&0xffffffff):
        raise ValueError('無法可靠判讀閒置時間；異常或超過24小時的觀察不啟動工作。')
    return value


def sources():
    return {**checks.sources(),'scripts/learning/idle.py':models.file_hash(ROOT/'scripts/learning/idle.py')}


def limits(idle_seconds,wait_seconds,work_seconds):
    for value,lo,hi in ((idle_seconds,30,3600),(wait_seconds,1,3600),(work_seconds,1,3600)):
        if type(value) is not int or not lo<=value<=hi: raise ValueError('閒置門檻30至3600秒；等待與工作時限各1至3600秒，須為整數。')


def folder(data_dir,job_id):
    return Path(data_dir).resolve()/'learning-batches'/'_idle_jobs'/batches.identifier(job_id)


def prepared_batch(data_dir,batch_id):
    path=batches.folder(data_dir,batch_id); manifest=batches.read_manifest(path,live=True)
    receipt=models.local_models.read_json(path/'receipt.json')
    if receipt.get('status')!='prepared' or receipt.get('manifest_sha256')!=batches.queue.digest(manifest):
        raise ValueError('閒置工作只能選用尚未執行的已準備批次。')
    batches.examples_current(models.QueueReader(data_dir),manifest)
    return manifest,models.file_hash(path/'receipt.json')


def prepare(data_dir,batch_id,suite,*,idle_seconds=600,wait_seconds=3600,work_seconds=1800):
    limits(idle_seconds,wait_seconds,work_seconds)
    data_dir=Path(data_dir).resolve(); batch_id=batches.identifier(batch_id); suite=checks.suite(suite)
    manifest,receipt_hash=prepared_batch(data_dir,batch_id)
    job_id=uuid.uuid4().hex; path=folder(data_dir,job_id); path.mkdir(parents=True,exist_ok=False)
    value={'schema':'idle_learning_job_v1','id':job_id,'data_dir':str(data_dir),'batch_id':batch_id,
           'batch_manifest_sha256':batches.queue.digest(manifest),'batch_receipt_sha256':receipt_hash,
           'baseline':manifest['parent']['id'] if manifest['parent'] else None,'suite':suite,'policy':POLICY,
           'idle_seconds':idle_seconds,'wait_seconds':wait_seconds,'work_seconds':work_seconds,'sources':sources()}
    if len(batches.queue.encoded(value).encode('utf-8'))>2*1024*1024: raise ValueError('閒置工作規約超過2MiB。')
    for name,h in value['sources'].items():
        target=path/'sources'/name; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(ROOT/name,target)
        if models.file_hash(target)!=h: raise batches.Stale('閒置工作來源快照變更。')
    if sources()!=value['sources']: raise batches.Stale('準備期間程式變更。')
    batches.save(path/'manifest.json',value)
    batches.save(path/'receipt.json',{'schema':'idle_learning_receipt_v1','status':'prepared',
                                    'manifest_sha256':batches.queue.digest(value),'created':time.time()})
    return job_id


def read_manifest(path,*,live=False):
    value=models.local_models.read_json(path/'manifest.json',2*1024*1024)
    fields={'schema','id','data_dir','batch_id','batch_manifest_sha256','batch_receipt_sha256','baseline',
            'suite','policy','idle_seconds','wait_seconds','work_seconds','sources'}
    if (not isinstance(value,dict) or set(value)!=fields or value['schema']!='idle_learning_job_v1'
            or value['policy']!=POLICY or value['id']!=path.name or value['data_dir']!=str(path.parents[2])):
        raise ValueError('閒置工作規約或位置不符。')
    batches.identifier(value['id']); batches.identifier(value['batch_id'])
    if value['baseline'] is not None: batches.identifier(value['baseline'])
    limits(value['idle_seconds'],value['wait_seconds'],value['work_seconds'])
    if checks.suite(value['suite'])!=value['suite']: raise ValueError('閒置驗收題庫規約不符。')
    if (not isinstance(value['sources'],dict) or not value['sources']
            or any(not isinstance(h,str) or not re.fullmatch('[0-9a-f]{64}',h) for h in
                   [*value['sources'].values(),value['batch_manifest_sha256'],value['batch_receipt_sha256']])):
        raise ValueError('閒置工作來源指紋不符。')
    if live and value['sources']!=sources(): raise batches.Stale('程式已變更，不能沿用閒置工作。')
    return value


class Gate:
    """Polled by existing owners; no unowned watcher thread or training retry."""
    def __init__(self,path,manifest,cancel,sensor,clock):
        self.path,self.manifest,self.cancel,self.sensor,self.clock=path,manifest,cancel,sensor,clock
        self.reason=None; self.failure=None; self.admitted=None; self.sample=None; self.deadline=None
        self.next_poll=0

    def is_set(self):
        if self.reason: return True
        if self.cancel and self.cancel.is_set(): self.reason='cancelled'; return True
        if self.deadline is not None and self.clock()>=self.deadline: self.reason='work_timeout'; return True
        if self.clock()<self.next_poll: return False
        self.next_poll=self.clock()+POLICY['poll_seconds']
        try:
            current=read_manifest(self.path,live=True)
            if current!=self.manifest: raise batches.Stale('閒置工作規約已變更。')
            batch=batches.read_manifest(batches.folder(current['data_dir'],current['batch_id']),live=True)
            if batches.queue.digest(batch)!=current['batch_manifest_sha256']: raise batches.Stale('批次規約已變更。')
            batches.examples_current(models.QueueReader(current['data_dir']),batch)
            self.sample=validate_sample(self.sensor())
            if self.admitted is not None and (self.sample['last_input_tick']!=self.admitted['last_input_tick']
                    or self.sample['idle_ms']<current['idle_seconds']*1000 or self.sample['ticks_ms']<self.admitted['ticks_ms']):
                self.reason='input_resumed'
        except batches.Stale as exc: self.reason='stale'; self.failure=str(exc)
        except Exception as exc: self.reason='observation_failed'; self.failure=str(exc)[:2000]
        return self.reason is not None

    def check(self,*,fresh=False):
        if fresh: self.next_poll=0
        if self.is_set(): raise batches.Stopped(self.reason)


def run(data_dir,job_id,*,cancel=None,sensor=None,clock=time.monotonic,sleep=time.sleep,on_progress=None):
    path=folder(data_dir,job_id)
    if not path.is_dir(): raise ValueError('閒置工作不存在。')
    with batches.owner(path):
        manifest=read_manifest(path); receipt=models.local_models.read_json(path/'receipt.json')
        if (not isinstance(receipt,dict) or receipt.get('schema')!='idle_learning_receipt_v1' or receipt.get('status')!='prepared'
                or receipt.get('manifest_sha256')!=batches.queue.digest(manifest)):
            raise ValueError('閒置工作只執行一次，不接管running紀錄或自動重試。')
        gate=Gate(path,manifest,cancel,sensor or idle_sample,clock)
        receipt.update(status='waiting',started=time.time()); batches.save(path/'receipt.json',receipt)
        def stage(status):
            receipt.update(status=status,last_observed_input=gate.sample); batches.save(path/'receipt.json',receipt)
            if on_progress: on_progress({'status':status})
        try:
            if on_progress: on_progress({'status':'waiting'})
            current,receipt_hash=prepared_batch(manifest['data_dir'],manifest['batch_id'])
            if (batches.queue.digest(current)!=manifest['batch_manifest_sha256']
                    or receipt_hash!=manifest['batch_receipt_sha256']): raise batches.Stale('批次準備紀錄已變更。')
            wait_deadline=clock()+manifest['wait_seconds']
            while True:
                gate.check(fresh=True)
                if clock()>=wait_deadline: stage('wait_expired'); return receipt
                ready=gate.sample['idle_ms']>=manifest['idle_seconds']*1000
                if ready:
                    try:
                        with gpu_lease(): pass
                    except RuntimeError: ready=False
                if ready: break
                sleep(min(POLICY['poll_seconds'],max(0,wait_deadline-clock())))
            gate.admitted=gate.sample.copy(); gate.deadline=clock()+manifest['work_seconds']
            gate.check(fresh=True); stage('training')
            current,receipt_hash=prepared_batch(manifest['data_dir'],manifest['batch_id'])
            if receipt_hash!=manifest['batch_receipt_sha256']: raise batches.Stale('等待期間批次準備紀錄已變更。')
            trained=batches.run(models.QueueReader(manifest['data_dir']),manifest['batch_id'],cancel=gate,
                                timeout=max(.001,gate.deadline-clock()))
            receipt['training_status']=trained['status']; gate.check(fresh=True)
            if trained['status']!='candidate':
                stage('already_satisfied' if trained['status']=='already_satisfied' else 'training_'+trained['status'])
                return receipt
            stage('checking')
            check_id=checks.prepare(manifest['data_dir'],manifest['batch_id'],manifest['suite'],baseline=manifest['baseline'])
            receipt['check_id']=check_id; batches.save(path/'receipt.json',receipt); gate.check(fresh=True)
            result=checks.run(manifest['data_dir'],check_id,cancel=gate,timeout=max(.001,gate.deadline-clock()))
            receipt['check_status']=result['status']; gate.check(fresh=True)
            report=checks.report(manifest['data_dir'],check_id)
            passed=(result['status']=='complete' and report['sources_and_parameters_still_current']
                    and report['receipt'].get('assessment',{}).get('passed') is True)
            gate.check(fresh=True)
            stage('validated_candidate' if passed else 'validation_rejected')
        except batches.Stale as exc: receipt.update(status='stale',error=str(exc))
        except batches.Stopped:
            receipt.update(status='stale' if gate.reason=='stale' else 'failed' if gate.reason=='observation_failed' else 'cancelled',
                           stop_reason=gate.reason,error=gate.failure)
        except KeyboardInterrupt: receipt.update(status='cancelled',stop_reason='keyboard_interrupt')
        except Exception as exc: receipt.update(status='failed',error_type=type(exc).__name__,error=str(exc)[:2000])
        finally:
            receipt.update(ended=time.time(),last_observed_input=gate.sample,automatic_activation=False)
            batches.save(path/'receipt.json',receipt)
        return receipt


def report(data_dir,job_id):
    path=folder(data_dir,job_id); manifest=read_manifest(path); receipt=models.local_models.read_json(path/'receipt.json')
    if (not isinstance(receipt,dict) or receipt.get('schema')!='idle_learning_receipt_v1'
            or receipt.get('manifest_sha256')!=batches.queue.digest(manifest)): raise ValueError('閒置工作規約已變更。')
    current=True; error=None; validation=None
    try:
        read_manifest(path,live=True)
        batch=batches.read_manifest(batches.folder(data_dir,manifest['batch_id']),live=True)
        if batches.queue.digest(batch)!=manifest['batch_manifest_sha256']: raise batches.Stale('批次規約已變更。')
        batches.examples_current(models.QueueReader(data_dir),batch)
    except (ValueError,OSError,sqlite3.Error) as exc: current=False; error=str(exc)
    if receipt.get('check_id'):
        check_manifest=checks.read_manifest(checks.folder(data_dir,receipt['check_id']))
        selected=check_manifest['selections']
        baseline=selected['baseline'].get('batch',{}).get('id')
        if (check_manifest['suite']!=manifest['suite'] or selected['candidate'].get('batch',{}).get('id')!=manifest['batch_id']
                or baseline!=manifest['baseline']): raise ValueError('完整驗收不屬於這個閒置工作。')
        validation=checks.report(data_dir,receipt['check_id'])
        current=current and validation['sources_and_parameters_still_current']
    if receipt.get('status')=='validated_candidate' and (validation is None or validation['receipt'].get('assessment',{}).get('passed') is not True):
        raise ValueError('沒有相符的完整助理驗收，不接受歷史候選狀態。')
    return {'receipt':receipt,'sources_still_current':current,'current_error':error,'validation':validation,
            'recorded_status_is_not_liveness':True,'automatic_activation':False}
