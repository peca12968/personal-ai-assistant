"""Finite, frozen local training batches. No model imports or automatic activation."""
import json
import math
import os
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import learning_queue as queue, local_models
from .engine import CodexEngine
from scripts.learning.protocol import EXPECTED_VERSIONS, file_hash, make_suite

ROOT = Path(__file__).resolve().parents[1]
RECIPE = {'seed':20261104,'rank':16,'alpha':32,'dropout':0.05,'learning_rate':0.0002,
          'steps':128,'accumulation':4,'max_sequence_tokens':512,'max_output_tokens':128,
          'example_repeats':4,'system':'請直接回答問題，只輸出所要求的答案，不附說明。',
          'target_modules':['q_proj','k_proj','v_proj','o_proj','gate_proj','up_proj','down_proj']}
PHASES = ('baseline','train','reload')
TERMINAL = {'candidate','rejected','failed','cancelled','stale','already_satisfied'}
SOURCE_FILES = ('assistant/learning_batches.py','assistant/learning_queue.py','assistant/local_models.py',
                'scripts/learning/batch_worker.py','scripts/learning/batches.py',
                'scripts/learning/protocol.py','scripts/learning/experiment.py','scripts/learning/runtime.py',
                'scripts/learning/model.json','scripts/learning/requirements.txt','scripts/learning/fetch_model.py')


class Stale(ValueError):
    pass


class Stopped(RuntimeError):
    pass


def sources():
    return {name:file_hash(ROOT/name) for name in SOURCE_FILES}


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{32}',value):
        raise ValueError('批次／項目編號格式不符。')
    return value


def folder(data_dir, batch_id):
    return Path(data_dir).resolve()/'learning-batches'/identifier(batch_id)


def save(path, value):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(queue.encoded(value)+'\n',encoding='utf-8')
    os.replace(temporary,path)


def validate_manifest(value, *, live=False):
    expected={'schema','id','sources','model','parent','items','new_ids','checks','recipe'}
    if not isinstance(value,dict) or set(value)!=expected or value['schema']!='confirmed_learning_batch_v1':
        raise ValueError('學習批次規約格式不符。')
    identifier(value['id'])
    if value['recipe']!=RECIPE or value['model']!=local_models.select():
        raise ValueError('學習配方或固定模型不符。')
    if (not isinstance(value['sources'],dict) or set(value['sources'])!=set(SOURCE_FILES)
            or any(not isinstance(h,str) or not re.fullmatch('[0-9a-f]{64}',h) for h in value['sources'].values())):
        raise ValueError('學習來源指紋不符。')
    if live and value['sources']!=sources():
        raise Stale('程式已變更，請建立新批次；不重用舊量測。')
    if not isinstance(value['items'],list) or not 1<=len(value['items'])<=64:
        raise ValueError('每批含重播最多64個確認例子。')
    ids, prompts = set(), set()
    suite=make_suite()
    reserved={r['prompt'] for r in suite['training'] if r['group']=='anchor'} | {r['prompt'] for r in suite['evaluation'] if r['group']=='guard'}
    keys={'id','revision','status','prompt','answer','source','fingerprint','recorded_at'}
    for row in value['items']:
        if not isinstance(row,dict) or set(row)!=keys:
            raise ValueError('批次項目快照不完整。')
        queue.decode({**{k:v for k,v in row.items() if k!='source'},'source_json':queue.encoded(row['source'])})
        if row['status']!='confirmed' or row['id'] in ids or row['prompt'] in prompts or row['prompt'] in reserved:
            raise ValueError('批次含未確認、重複或衝突項目。')
        ids.add(row['id']); prompts.add(row['prompt'])
    if (not isinstance(value['new_ids'],list) or not value['new_ids']
            or any(not isinstance(i,str) for i in value['new_ids'])
            or len(set(value['new_ids']))!=len(value['new_ids']) or not set(value['new_ids'])<=ids):
        raise ValueError('批次新例子清單不符。')
    checks=value['checks']
    if not isinstance(checks,list) or len(checks)>16:
        raise ValueError('額外保留問答最多16題。')
    for row in checks:
        if not isinstance(row,dict) or set(row)!={'prompt','answer'}:
            raise ValueError('保留問答只接受問題與完整答案。')
        queue.text(row['prompt'],2000,'保留問題'); queue.text(row['answer'],2000,'保留答案')
        if row['prompt'] in prompts or row['prompt'] in reserved:
            raise ValueError('保留問題不能與訓練問題相同。')
        prompts.add(row['prompt'])
    parent=value['parent']
    if parent is not None:
        if not isinstance(parent,dict) or set(parent)!={'id','manifest_sha256','receipt_sha256'}:
            raise ValueError('前代參數來源不符。')
        identifier(parent['id'])
        if parent['id']==value['id'] or any(not isinstance(parent[k],str) or not re.fullmatch('[0-9a-f]{64}',parent[k]) for k in ('manifest_sha256','receipt_sha256')):
            raise ValueError('前代來源指紋不符。')
    return value


def read_manifest(path, *, live=False):
    return validate_manifest(local_models.read_json(path/'manifest.json',8*1024*1024),live=live)


def examples_current(store, manifest):
    with store.connect() as db:
        db.execute('BEGIN')
        for row in manifest['items']:
            current=queue.latest(db,row['id'])
            if current!=row or not queue.source_current(store,db,current['source']):
                raise Stale('確認例子、回饋來源或撤回狀態已變更；批次不能繼續。')


def evaluation_cases(manifest):
    rows=[{'id':'example-'+r['id'],'group':'new' if r['id'] in manifest['new_ids'] else 'replay',
           'prompt':r['prompt'],'answer':r['answer']} for r in manifest['items']]
    rows += [{'id':'heldout-'+str(i),'group':'heldout',**r} for i,r in enumerate(manifest['checks'])]
    rows += [r for r in make_suite()['evaluation'] if r['group']=='guard']
    return rows


def training_rows(manifest):
    rows=[{'prompt':r['prompt'],'answer':r['answer']} for r in manifest['items']]*RECIPE['example_repeats']
    rows += [{'prompt':r['prompt'],'answer':r['answer']} for r in make_suite()['training'] if r['group']=='anchor']
    return rows


def grades(manifest, report, phase):
    cases=evaluation_cases(manifest)
    if (not isinstance(report,dict) or report.get('schema')!='confirmed_learning_phase_v1'
            or report.get('phase')!=phase or report.get('status')!='complete'
            or report.get('manifest_sha256')!=queue.digest(manifest)
            or report.get('environment')!=EXPECTED_VERSIONS
            or not isinstance(report.get('rows'),list) or len(report['rows'])!=len(cases)):
        raise ValueError('批次實際評估紀錄不完整。')
    result={}
    for case,row in zip(cases,report['rows']):
        if (not isinstance(row,dict) or set(row)!={'id','output','tokens','ended_with_eos'} or row['id']!=case['id']
                or not isinstance(row['output'],str) or len(row['output'])>16000
                or type(row['tokens']) is not int or not 1<=row['tokens']<=RECIPE['max_output_tokens']
                or type(row['ended_with_eos']) is not bool):
            raise ValueError('批次實際回答順序或格式不符。')
        result[case['id']]=row['ended_with_eos'] and row['output']==case['answer']
    return result


def assess(manifest, reports):
    scores={phase:grades(manifest,reports[phase],phase) for phase in PHASES}
    baseline, trained, reloaded=(reports[p] for p in PHASES)
    for phase in ('train','reload'):
        for key in ('weights_before','weights_after'):
            if not isinstance(reports[phase].get(key),str) or not re.fullmatch('[0-9a-f]{64}',reports[phase][key]):
                raise ValueError('參數內容指紋缺漏。')
    targets=[c['id'] for c in evaluation_cases(manifest) if c['group']!='guard']
    guards=[c['id'] for c in evaluation_cases(manifest) if c['group']=='guard']
    checks={
        'baseline_has_valid_controls':sum(scores['baseline'][i] for i in guards)>=6,
        'all_confirmed_and_heldout_correct':all(scores['train'][i] for i in targets),
        'baseline_guards_retained':all(scores['train'][i] for i in guards if scores['baseline'][i]),
        'at_least_one_requested_case_improved':any(not scores['baseline'][i] and scores['train'][i]
            for i in ['example-'+i for i in manifest['new_ids']]+['heldout-'+str(i) for i in range(len(manifest['checks']))]),
        'weights_changed':trained['weights_before']!=trained['weights_after'],
        'parent_weights_continued':manifest['parent'] is None or baseline.get('weights_after')==trained['weights_before'],
        'reloaded_weights_match':trained['weights_after']==reloaded['weights_before']==reloaded['weights_after'],
        'reloaded_answers_match':trained['rows']==reloaded['rows'],
        'training_data_matches':trained.get('training_rows_sha256')==queue.digest(training_rows(manifest)),
    }
    history=trained.get('training')
    if (not isinstance(history,list) or len(history)!=RECIPE['steps']
            or any(not isinstance(row,dict) or set(row)!={'step','loss'} or row['step']!=i+1
                   or type(row['loss']) not in (int,float) or not math.isfinite(row['loss']) for i,row in enumerate(history))):
        raise ValueError('有限訓練步驟紀錄不完整。')
    return {'passed':all(checks.values()),'checks':checks,'scores':scores,
            'heldout_count':len(manifest['checks']),'training_prompt_fit_is_not_generalization':True}


def adapter_files(path):
    expected={'README.md','adapter_config.json','adapter_model.safetensors'}
    if {p.name for p in path.iterdir()}!=expected:
        raise ValueError('候選參數檔案清單不符。')
    if any(not (path/name).is_file() or (path/name).stat().st_size>256*1024*1024 for name in expected):
        raise ValueError('候選參數檔案大小不符。')
    return {name:file_hash(path/name) for name in sorted(expected)}


def candidate(data_dir, batch_id):
    path=folder(data_dir,batch_id); manifest=read_manifest(path,live=True)
    receipt=local_models.read_json(path/'receipt.json')
    if receipt.get('status')!='candidate' or receipt.get('manifest_sha256')!=queue.digest(manifest):
        raise ValueError('前代必須是完整驗證通過的候選。')
    reports={phase:local_models.read_json(path/(phase+'.json')) for phase in PHASES}
    actual={phase:file_hash(path/(phase+'.json')) for phase in PHASES}
    if receipt.get('report_files')!=actual or receipt.get('assessment')!=assess(manifest,reports) or not receipt['assessment']['passed']:
        raise ValueError('前代驗收或原始證據已變更。')
    if adapter_files(path/'adapter')!=reports['train'].get('adapter_files'):
        raise ValueError('前代參數已變更。')
    return manifest,receipt,reports


def prepare(store, selected, *, checks=None, parent=None):
    """selected is explicit [{id, revision}], never an implicit scan of feedback."""
    if (not isinstance(selected,list) or not 1<=len(selected)<=64
            or any(not isinstance(r,dict) or set(r)!={'id','revision'} or type(r['revision']) is not int or r['revision']<1 for r in selected)
            or any(not isinstance(r['id'],str) for r in selected) or len({r['id'] for r in selected})!=len(selected)):
        raise ValueError('請明確選取1至64個項目及觀察版本，不可重複。')
    old_items=[]; old_checks=[]; origin=None
    if parent is not None:
        before,receipt,reports=candidate(store.path.parent,parent)
        examples_current(store,before)
        old_items=before['items']
        old_checks=before['checks']
        origin={'id':parent,'manifest_sha256':queue.digest(before),'receipt_sha256':file_hash(folder(store.path.parent,parent)/'receipt.json')}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        items={r['id']:r for r in old_items}
        for frozen in old_items:
            now=queue.latest(db,frozen['id'])
            if now!=frozen or not queue.source_current(store,db,now['source']):
                raise Stale('前代例子已變更；需重新建立不沿用舊參數的批次。')
        for entry in selected:
            row=queue.latest(db,entry['id'])
            if row['revision']!=entry['revision'] or row['status']!='confirmed' or not queue.source_current(store,db,row['source']):
                raise Stale('選取例子已變更或不再有效，請重新核對。')
            items[row['id']]=row
        new_ids=[entry['id'] for entry in selected if entry['id'] not in {r['id'] for r in old_items}]
        extra=[] if checks is None else checks
        if not isinstance(extra,list): raise ValueError('保留問答需為清單。')
        combined=old_checks+[row for row in extra if row not in old_checks]
        manifest=validate_manifest({'schema':'confirmed_learning_batch_v1','id':uuid.uuid4().hex,
            'sources':sources(),'model':local_models.select(),'parent':origin,'items':list(items.values()),
            'new_ids':new_ids,'checks':combined,'recipe':RECIPE},live=True)
        # Filesystem and SQLite are not jointly atomic. The manifest is created
        # while writers are excluded; any later changes are checked on every run.
        path=folder(store.path.parent,manifest['id']); path.mkdir(parents=True,exist_ok=False)
        save(path/'manifest.json',manifest)
        save(path/'receipt.json',{'schema':'confirmed_learning_receipt_v1','status':'prepared',
                                 'manifest_sha256':queue.digest(manifest),'created':time.time()})
    return manifest['id']


@contextmanager
def owner(path):
    db=sqlite3.connect(path/'owner.sqlite3',timeout=0,isolation_level=None)
    try:
        db.execute('BEGIN EXCLUSIVE')
        yield
    finally:
        db.close()


def phase_command(path, phase):
    python=ROOT/'data/learning/runtime'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not python.is_file():
        raise ValueError('本機ML環境尚未安裝。')
    return [str(python),'-I',str(ROOT/'scripts/learning/batch_worker.py'),'--batch-dir',str(path),'--phase',phase]


class PhaseExit:
    """Wait for the actual Windows venv worker, not only its redirector.

    The phase creates its one-shot PID marker before importing ML or using CUDA.
    This observer has synchronization rights only; it never kills a marker PID.
    """
    def __init__(self, marker):
        self.marker=marker; self.pid=None; self.handle=None
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            self.ctypes=ctypes; self.api=ctypes.WinDLL('kernel32',use_last_error=True)
            self.api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
            self.api.OpenProcess.restype=wintypes.HANDLE
            self.api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
            self.api.WaitForSingleObject.restype=wintypes.DWORD
            self.api.CloseHandle.argtypes=[wintypes.HANDLE]; self.api.CloseHandle.restype=wintypes.BOOL

    def observe(self):
        if os.name!='nt' or self.pid is not None: return
        try:
            with self.marker.open('r',encoding='ascii') as stream: raw=stream.read(32)
        except FileNotFoundError: return
        if not raw: return  # The worker may still be writing its marker.
        if not raw.isascii() or not raw.isdigit() or not 0<int(raw)<2**32 or int(raw)==os.getpid():
            raise ValueError('實際階段程序識別不符。')
        pid=int(raw)
        handle=self.api.OpenProcess(0x00100000,False,pid)  # SYNCHRONIZE only
        if not handle:
            error=self.ctypes.get_last_error()
            if error!=87: raise self.ctypes.WinError(error)
            # A nonzero PID no longer exists. Never treat access errors as exit.
        self.pid=pid; self.handle=handle

    def wait(self):
        self.observe()
        if self.handle:
            result=self.api.WaitForSingleObject(self.handle,10000)
            if result==258: raise TimeoutError('無法確認實際階段程序已結束；不重試或啟用候選。')
            if result!=0: raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle); self.handle=None


def run(store, batch_id, *, cancel=None, timeout=1800):
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=3600:
        raise ValueError('批次時限需大於0且最多3600秒。')
    path=folder(store.path.parent,batch_id)
    if not path.is_dir(): raise ValueError('批次不存在。')
    deadline=time.monotonic()+timeout
    def stop_check():
        if cancel and cancel.is_set(): raise Stopped('已取消本機批次。')
        if time.monotonic()>=deadline: raise TimeoutError('批次超過有限執行時限。')
    with owner(path):
        manifest=read_manifest(path)
        receipt=local_models.read_json(path/'receipt.json')
        if receipt.get('status')!='prepared' or receipt.get('manifest_sha256')!=queue.digest(manifest):
            raise ValueError('批次只能執行一次；進行中紀錄不代表程序仍存活，不會自動重試。')
        receipt.update(status='running',started=time.time(),owner_pid=os.getpid())
        save(path/'receipt.json',receipt)
        reports={}
        try:
            for phase in PHASES:
                stop_check(); read_manifest(path,live=True); examples_current(store,manifest)
                if manifest['parent']:
                    origin=folder(store.path.parent,manifest['parent']['id'])
                    parent_manifest,_,_=candidate(store.path.parent,manifest['parent']['id'])
                    if queue.digest(parent_manifest)!=manifest['parent']['manifest_sha256'] or file_hash(origin/'receipt.json')!=manifest['parent']['receipt_sha256']:
                        raise Stale('前代證據已變更。')
                log=path/(phase+'.log')
                with log.open('xb') as stderr:
                    actual=PhaseExit(path/(phase+'.started'))
                    process=subprocess.Popen(phase_command(path,phase),stdin=subprocess.DEVNULL,stdout=stderr,stderr=stderr,
                        cwd=ROOT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    try:
                        receipt.update(phase=phase,worker_pid=process.pid,worker_exit_confirmed=False)
                        save(path/'receipt.json',receipt)
                        last_check=0
                        while process.poll() is None:
                            actual.observe()
                            stop_check()
                            if log.stat().st_size>4*1024*1024: raise ValueError('批次診斷輸出超限。')
                            if time.monotonic()-last_check>=2:
                                examples_current(store,manifest); last_check=time.monotonic()
                            time.sleep(0.1)
                        stop_check()
                        if log.stat().st_size>4*1024*1024: raise ValueError('批次診斷輸出超限。')
                        if process.returncode!=0: raise RuntimeError(f'{phase}未完成，原始紀錄保留。')
                    finally:
                        try:
                            try: actual.observe()
                            finally:
                                CodexEngine.stop(process); process.wait(timeout=10)
                            actual.wait()
                            receipt.update(worker_exit_confirmed=True,worker_returncode=process.returncode,
                                           actual_worker_pid=actual.pid)
                        finally: actual.close()
                report=local_models.read_json(path/(phase+'.json'))
                scores=grades(manifest,report,phase); reports[phase]=report
                examples_current(store,manifest); read_manifest(path,live=True)
                if phase=='baseline' and sum(scores[c['id']] for c in evaluation_cases(manifest) if c['group']=='guard')<6:
                    raise ValueError('底座基本控制題少於6/8，不進行本批訓練。')
                if phase=='baseline' and all(scores[c['id']] for c in evaluation_cases(manifest) if c['group']!='guard'):
                    receipt.update(status='already_satisfied',notice='確認例子與提供的保留題在原參數已全數精確回答；沒有訓練或新候選。')
                    break
            else:
                result=assess(manifest,reports)
                if adapter_files(path/'adapter')!=reports['train'].get('adapter_files'):
                    raise ValueError('候選參數檔案核對失敗。')
                examples_current(store,manifest); stop_check()
                receipt.update(status='candidate' if result['passed'] else 'rejected',assessment=result)
            receipt['report_files']={phase:file_hash(path/(phase+'.json')) for phase in reports}
        except Stale as exc:
            receipt.update(status='stale',error=str(exc))
        except Stopped as exc:
            receipt.update(status='cancelled',error=str(exc))
        except KeyboardInterrupt:
            receipt.update(status='cancelled',error='使用者中斷批次。')
        except Exception as exc:
            receipt.update(status='failed',error_type=type(exc).__name__,error=str(exc)[:2000])
        finally:
            receipt['ended']=time.time(); save(path/'receipt.json',receipt)
        return receipt
