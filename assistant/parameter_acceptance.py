"""Frozen, finite full-assistant comparisons. Never train or activate parameters."""
import json
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing
from pathlib import Path

from . import learned_models as models, learning_batches as batches, learning_queue as queue
from .engine import Assistant, EngineError, render_prompt
from .documents import check_citations
from .local_engine import LocalEngine
from .store import Store

ROOT=models.local_models.ROOT
PHASES=('baseline','candidate')
POLICY={'comparison':'exact_visible_answer_v1','all_candidate_cases_required':True,
        'minimum_baseline_controls_correct':1,'at_least_one_target_improvement':True,
        'automatic_activation':False}


def sources():
    paths=list((ROOT/'assistant').glob('*.py'))+list((ROOT/'prompts').glob('*.txt'))
    names={str(p.relative_to(ROOT)).replace('\\','/') for p in paths}
    names.update(batches.SOURCE_FILES); names.add('scripts/learning/acceptance.py')
    return {name:models.file_hash(ROOT/name) for name in sorted(names)}


def suite(value):
    if not isinstance(value,dict) or set(value)!={'schema','cases'} or value['schema']!='full_assistant_cases_v1':
        raise ValueError('完整助理驗收題庫格式不符。')
    if not isinstance(value['cases'],list) or not 3<=len(value['cases'])<=32:
        raise ValueError('題庫需3至32題，至少一題目標與兩題控制。')
    ids=set(); counts={'target':0,'control':0}; rows=[]
    for row in value['cases']:
        if (not isinstance(row,dict) or not {'id','group','question','answer'}<=set(row)
                or set(row)-{'id','group','question','answer','history'}
                or not isinstance(row['id'],str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}',row['id'])
                or row['id'] in ids or not isinstance(row['group'],str) or row['group'] not in counts):
            raise ValueError('驗收案例編號、分類或欄位不符。')
        queue.text(row['question'],16000,'驗收問題'); queue.text(row['answer'],2000,'完整預期答案')
        history=row.get('history',[])
        if not isinstance(history,list) or len(history)>8: raise ValueError('每題前文最多8則。')
        for message in history:
            if not isinstance(message,dict) or set(message)!={'role','content'} or message['role'] not in ('user','assistant'):
                raise ValueError('驗收前文只能是使用者或助理原文。')
            queue.text(message['content'],2000,'驗收前文')
        ids.add(row['id']); counts[row['group']]+=1
        rows.append({**row,'history':history})
    if not counts['target'] or counts['control']<2: raise ValueError('至少需要一題目標及兩題控制。')
    return json.loads(queue.encoded({'schema':value['schema'],'cases':rows}))


def folder(data_dir,check_id):
    return Path(data_dir).resolve()/'learning-batches'/'_assistant_checks'/batches.identifier(check_id)


def make_engine(manifest,phase,cls=LocalEngine,**kwargs):
    selected=manifest['selections'][phase]
    batch=selected['batch']['id'] if selected.get('schema')==models.SCHEMA else None
    engine=cls(manifest['data_dir'],batch=batch,**kwargs)
    if engine.selection!=selected: raise batches.Stale('驗收選用的模型、候選或來源已變更。')
    return engine


def prepare(data_dir,candidate,cases,*,baseline=None):
    data_dir=Path(data_dir).resolve(); cases=suite(cases)
    candidate=models.select(data_dir,batch=batches.identifier(candidate))
    base=models.select(data_dir,batch=batches.identifier(baseline)) if baseline is not None else models.select(data_dir)
    if base==candidate: raise ValueError('原版與候選不能是同一參數。')
    check_id=uuid.uuid4().hex; path=folder(data_dir,check_id); path.mkdir(parents=True,exist_ok=False)
    source_hashes=sources()
    fixture=Store(path/'fixture.sqlite3'); sessions={}
    for case in cases['cases']:
        sid=fixture.new_session('acceptance '+case['id']); sessions[case['id']]=sid
        for message in case['history']: fixture.add_message(sid,message['role'],message['content'])
    manifest={'schema':'full_assistant_check_v1','id':check_id,'data_dir':str(data_dir),'suite':cases,
              'policy':POLICY,'sources':source_hashes,'selections':{'baseline':base,'candidate':candidate},
              'sessions':sessions,'contexts':{},'prompts':{},'database_files':{}}
    for phase in PHASES:
        target=path/(phase+'.sqlite3')
        with fixture.connect() as source,closing(sqlite3.connect(target)) as destination: source.backup(destination)
        store=Store(target); engine=make_engine(manifest,phase)
        contexts={case['id']:Assistant(store,engine,allow_local_calculation=False,allow_local_transcription=False, allow_local_evidence=False).prepare_context(sessions[case['id']],case['question'].strip()) for case in cases['cases']}
        manifest['contexts'][phase]=contexts
        manifest['prompts'][phase]={key:render_prompt(value) for key,value in contexts.items()}
        manifest['database_files'][phase]=models.file_hash(target)
    if sources()!=source_hashes: raise batches.Stale('準備期間程式已變更，沒有建立可執行驗收。')
    for name,h in source_hashes.items():
        target=path/'sources'/name; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(ROOT/name,target)
        if models.file_hash(target)!=h: raise batches.Stale('程式來源快照變更。')
    if len(queue.encoded(manifest).encode('utf-8'))>32*1024*1024: raise ValueError('完整驗收快照超過32MiB。')
    batches.save(path/'manifest.json',manifest)
    batches.save(path/'receipt.json',{'schema':'full_assistant_receipt_v1','status':'prepared',
                                    'manifest_sha256':queue.digest(manifest),'created':time.time()})
    return check_id


def read_manifest(path,*,live=False):
    value=models.local_models.read_json(path/'manifest.json',32*1024*1024)
    keys={'schema','id','data_dir','suite','policy','sources','selections','sessions','contexts','prompts','database_files'}
    if not isinstance(value,dict) or set(value)!=keys or value['schema']!='full_assistant_check_v1' or value['policy']!=POLICY:
        raise ValueError('完整助理驗收規約不符。')
    if value['id']!=path.name or value['data_dir']!=str(path.parents[2]): raise ValueError('驗收位置與凍結資料目錄不符。')
    if value['suite']!=suite(value['suite']): raise ValueError('驗收題庫不是完整規約。')
    ids={row['id'] for row in value['suite']['cases']}
    if (not isinstance(value['sessions'],dict) or set(value['sessions'])!=ids
            or any(not isinstance(sid,str) for sid in value['sessions'].values())
            or len(set(value['sessions'].values()))!=len(ids)): raise ValueError('驗收會話不完整。')
    for sid in value['sessions'].values(): batches.identifier(sid)
    for name in ('selections','contexts','prompts','database_files'):
        if not isinstance(value[name],dict) or set(value[name])!=set(PHASES): raise ValueError('驗收階段不完整。')
    if not isinstance(value['sources'],dict) or not value['sources'] or any(not isinstance(h,str) or not re.fullmatch('[0-9a-f]{64}',h) for h in value['sources'].values()):
        raise ValueError('驗收程式指紋不符。')
    for phase in PHASES:
        if (not isinstance(value['contexts'][phase],dict) or not isinstance(value['prompts'][phase],dict)
                or set(value['contexts'][phase])!=ids or set(value['prompts'][phase])!=ids):
            raise ValueError('驗收問題／完整提示不完整。')
        try: description=models.description(value['selections'][phase])
        except (KeyError,TypeError,AttributeError): raise ValueError('驗收模型選擇格式不符。') from None
        for row in value['suite']['cases']:
            ctx=value['contexts'][phase][row['id']]
            if not isinstance(ctx,dict) or ctx.get('current_request')!=row['question'].strip() or ctx.get('engine_selection')!=description:
                raise ValueError('驗收問題或引擎背景不一致。')
            if not isinstance(value['prompts'][phase][row['id']],str): raise ValueError('驗收提示需為文字。')
        if not isinstance(value['database_files'][phase],str) or not re.fullmatch('[0-9a-f]{64}',value['database_files'][phase]):
            raise ValueError('初始驗收資料庫指紋不符。')
    if live and value['sources']!=sources(): raise batches.Stale('程式已變更，需準備新的完整助理驗收。')
    return value


class TracedEngine(LocalEngine):
    def answer(self,prompt,cancel=None):
        if prompt!=self.expected_prompt: raise batches.Stale('實際助理提示與凍結提示不同，沒有送出。')
        batches.save(self.destination/'request.json',{'prompt':prompt,'selection':self.selection})
        return super().answer(prompt,cancel)

    @contextmanager
    def response_directory(self):
        with super().response_directory() as temporary:
            try: yield temporary
            finally: shutil.copytree(temporary,self.destination/'worker')


def run(data_dir,check_id,*,cancel=None,timeout=1800):
    if type(timeout) not in (int,float) or not 0<timeout<=3600: raise ValueError('完整驗收時限需大於0且最多3600秒。')
    path=folder(data_dir,check_id); deadline=time.monotonic()+timeout
    if not path.is_dir(): raise ValueError('驗收不存在。')
    with batches.owner(path):
        manifest=read_manifest(path); receipt=models.local_models.read_json(path/'receipt.json')
        if (not isinstance(receipt,dict) or receipt.get('schema')!='full_assistant_receipt_v1'
                or receipt.get('status')!='prepared' or receipt.get('manifest_sha256')!=queue.digest(manifest)):
            raise ValueError('驗收只能執行一次，不從歷史running紀錄推斷程序存活或自動重試。')
        receipt.update(status='running',started=time.time()); batches.save(path/'receipt.json',receipt)
        rows=[]
        def check():
            if cancel and cancel.is_set(): raise EngineError('已停止完整助理驗收。','cancelled')
            if time.monotonic()>=deadline: raise TimeoutError('完整助理驗收超過時限。')
            current=read_manifest(path,live=True)
            if current!=manifest: raise batches.Stale('驗收規約已變更。')
            for selected in manifest['selections'].values(): models.validate(selected)
        try:
            for phase in PHASES:
                check()
                database=path/(phase+'.sqlite3')
                if models.file_hash(database)!=manifest['database_files'][phase]: raise batches.Stale('初始驗收資料庫已變更。')
                store=Store(database)
                for case in manifest['suite']['cases']:
                    check(); engine=make_engine(manifest,phase,TracedEngine,timeout=min(240,deadline-time.monotonic()))
                    engine.destination=path/phase/case['id']; engine.destination.mkdir(parents=True,exist_ok=False)
                    engine.expected_prompt=manifest['prompts'][phase][case['id']]
                    context=Assistant(store,engine,allow_local_calculation=False,allow_local_transcription=False, allow_local_evidence=False).prepare_context(manifest['sessions'][case['id']],case['question'].strip())
                    if context!=manifest['contexts'][phase][case['id']]: raise batches.Stale('驗收資料背景已變更。')
                    row={'phase':phase,'id':case['id'],'started':time.time(),'status':'running'}
                    receipt.update(phase=phase,case_id=case['id']); batches.save(path/'receipt.json',receipt)
                    try:
                        answer=Assistant(store,engine,allow_local_calculation=False,allow_local_transcription=False, allow_local_evidence=False).ask(manifest['sessions'][case['id']],case['question'],cancel=cancel)
                        row.update(status='complete',answer=answer['content'],message_id=answer['id'],run_id=answer['run_id'])
                    except Exception as exc:
                        row.update(status='failed',error_type=type(exc).__name__,error_code=getattr(exc,'code',None),error=str(exc)[:2000])
                        raise
                    finally:
                        row['ended']=time.time(); rows.append(row); batches.save(path/'responses.json',rows)
                    check()
            receipt.update(status='complete',responses_sha256=models.file_hash(path/'responses.json'))
            batches.save(path/'receipt.json',receipt)
            receipt['assessment']=assess(path,manifest,rows)
        except batches.Stale as exc: receipt.update(status='stale',error=str(exc))
        except KeyboardInterrupt: receipt.update(status='cancelled',error='使用者中斷完整驗收。')
        except Exception as exc:
            receipt.update(status='cancelled' if getattr(exc,'code',None)=='cancelled' else 'failed',error_type=type(exc).__name__,error=str(exc)[:2000])
        finally:
            receipt['ended']=time.time(); batches.save(path/'receipt.json',receipt)
        return receipt


def assess(path,manifest,rows):
    cases=manifest['suite']['cases']; order=[(phase,case['id']) for phase in PHASES for case in cases]
    if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows) or [(r.get('phase'),r.get('id')) for r in rows]!=order:
        raise ValueError('完整助理實際回答順序或數量不符。')
    scores={phase:{} for phase in PHASES}
    artifacts={}
    for row in rows:
        phase=row['phase']; case=next(c for c in cases if c['id']==row['id'])
        if row.get('status')!='complete' or not isinstance(row.get('answer'),str) or len(row['answer'].encode('utf-8'))>2*1024*1024:
            raise ValueError('完整助理實際回答不完整。')
        relative=phase+'/'+case['id']; location=path/relative
        request=models.local_models.read_json(location/'request.json',4*1024*1024)
        raw=models.local_models.read_json(location/'worker/result.json')
        if request!={'prompt':manifest['prompts'][phase][case['id']],'selection':manifest['selections'][phase]}:
            raise ValueError('實際送出的完整提示或模型與規約不符。')
        if (not isinstance(raw,dict) or raw.get('schema')!='local_inference_result_v1'
                or raw.get('status')!='succeeded' or raw.get('selection_fingerprint')!=manifest['selections'][phase]['fingerprint']
                or not isinstance(raw.get('answer'),str) or not raw['answer'].strip()):
            raise ValueError('實際工作程序沒有完成指定參數回答。')
        visible=raw['answer'].strip()
        citations=check_citations(visible,manifest['contexts'][phase][case['id']]['documents']['excerpts'])
        if any(not citation['range_valid'] for citation in citations):
            visible+='\n\n來源檢查：有引用無法對照本次文件片段，請勿將該引用視為已驗證。'
        if visible!=row['answer']: raise ValueError('工作程序輸出與正式助理可見答案不一致。')
        db=sqlite3.connect((path/(phase+'.sqlite3')).as_uri()+'?mode=ro',uri=True)
        try:
            answer=db.execute('SELECT content FROM messages WHERE id=? AND session_id=? AND role=?',
                              (row.get('message_id'),manifest['sessions'][case['id']],'assistant')).fetchone()
            run=db.execute('SELECT status,context_json FROM answer_runs WHERE id=? AND session_id=?',
                           (row.get('run_id'),manifest['sessions'][case['id']])).fetchone()
            if answer is None or answer[0]!=row['answer'] or run is None or run[0]!='succeeded' or json.loads(run[1])!=manifest['contexts'][phase][case['id']]:
                raise ValueError('正式助理保存的回答／背景與驗收紀錄不符。')
        finally: db.close()
        for name in ('request.json','worker/result.json'):
            artifacts[relative+'/'+name]=models.file_hash(location/name)
        scores[phase][case['id']]=row['answer']==case['answer']
    controls=[c['id'] for c in cases if c['group']=='control']; targets=[c['id'] for c in cases if c['group']=='target']
    checks={'all_candidate_cases_passed':all(scores['candidate'].values()),
            'baseline_has_valid_control':sum(scores['baseline'][i] for i in controls)>=POLICY['minimum_baseline_controls_correct'],
            'baseline_correct_cases_retained':all(scores['candidate'][i] for i in scores['baseline'] if scores['baseline'][i]),
            'at_least_one_target_improved':any(not scores['baseline'][i] and scores['candidate'][i] for i in targets)}
    return {'passed':all(checks.values()),'checks':checks,'scores':scores,'artifact_files':artifacts,
            'scope':'Only these frozen cases, contexts, sources and parameters; not a general intelligence score.',
            'automatic_activation':False}


def report(data_dir,check_id):
    path=folder(data_dir,check_id); manifest=read_manifest(path)
    receipt=models.local_models.read_json(path/'receipt.json')
    if (not isinstance(receipt,dict) or receipt.get('schema')!='full_assistant_receipt_v1'
            or receipt.get('manifest_sha256')!=queue.digest(manifest)): raise ValueError('驗收規約已變更。')
    if receipt.get('status')=='complete':
        if models.file_hash(path/'responses.json')!=receipt.get('responses_sha256'): raise ValueError('原始回答已變更。')
        result=assess(path,manifest,models.local_models.read_json(path/'responses.json',8*1024*1024))
        if result!=receipt.get('assessment'): raise ValueError('實際證據或保存驗收已變更。')
    current=True; error=None
    try:
        read_manifest(path,live=True)
        for selection in manifest['selections'].values(): models.validate(selection)
    except (ValueError,OSError,KeyError,TypeError,sqlite3.Error) as exc: current=False; error=str(exc)
    return {'receipt':receipt,'sources_and_parameters_still_current':current,'current_error':error,
            'recorded_status_is_not_liveness':True,'automatic_activation':False}
