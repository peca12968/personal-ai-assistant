"""Explicit local batch selection, revisioned choices and read-only live checks."""
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import local_models, learning_batches as batches
from scripts.learning.protocol import digest, file_hash

SCHEMA='confirmed_batch_model_selection_v1'
FILES=('assistant/learned_models.py','assistant/local_engine.py','assistant/local_worker.py')
MAX_CHOICES=1000


class QueueReader:
    def __init__(self,data_dir): self.path=Path(data_dir).resolve()/'assistant.sqlite3'

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=1)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            yield db
        finally: db.close()


def choice_path(data_dir): return Path(data_dir).resolve()/'local-model-choice.sqlite3'


def initial(): return {'revision':0,'batch_id':None,'evidence_fingerprint':None,'created_at':None}


def decode_choice(row):
    value=dict(row)
    if (set(value)!={'revision','batch_id','evidence_fingerprint','created_at'}
            or type(value['revision']) is not int or not 1<=value['revision']<=MAX_CHOICES+1
            or not isinstance(value['created_at'],str) or not 1<=len(value['created_at'])<=50):
        raise ValueError('保存的模型選擇格式不符。')
    if value['batch_id'] is None:
        if value['evidence_fingerprint'] is not None: raise ValueError('原始模型選擇不能含候選指紋。')
    else:
        batches.identifier(value['batch_id'])
        if not isinstance(value['evidence_fingerprint'],str) or not re.fullmatch('[0-9a-f]{64}',value['evidence_fingerprint']):
            raise ValueError('候選來源指紋不符。')
    return value


def latest(db):
    row=db.execute('SELECT * FROM choices ORDER BY revision DESC LIMIT 1').fetchone()
    return decode_choice(row) if row is not None else initial()


def choice(data_dir):
    path=choice_path(data_dir)
    if not path.exists(): return initial()
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1); db.row_factory=sqlite3.Row
    try: return latest(db)
    finally: db.close()


def history(data_dir,before=None):
    if before is not None and (type(before) is not int or not 1<=before<=MAX_CHOICES+2):
        raise ValueError('歷史分頁版本不符。')
    path=choice_path(data_dir)
    if not path.exists(): return []
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1); db.row_factory=sqlite3.Row
    try:
        rows=db.execute('SELECT * FROM choices WHERE revision<? ORDER BY revision DESC LIMIT 20',
                        (before if before is not None else MAX_CHOICES+2,)).fetchall()
        return [decode_choice(row) for row in rows]
    finally: db.close()


def evidence(data_dir,batch_id):
    manifest,receipt,reports=batches.candidate(data_dir,batch_id)
    batches.examples_current(QueueReader(data_dir),manifest)
    path=batches.folder(data_dir,batch_id)
    return {'id':batch_id,'manifest_sha256':digest(manifest),
            'receipt_sha256':file_hash(path/'receipt.json'),'report_files':receipt['report_files'],
            'adapter_files':reports['train']['adapter_files'],'weights_sha256':reports['train']['weights_after']}


def choose(data_dir,batch_id,expected_revision):
    """Save an explicit choice; never switch a running engine or launch ML."""
    if type(expected_revision) is not int or not 0<=expected_revision<=MAX_CHOICES+1:
        raise ValueError('請提供讀取時的模型選擇版本。')
    data_dir=Path(data_dir).resolve()
    if not (data_dir/'assistant.sqlite3').is_file(): raise ValueError('助理資料庫不存在。')
    selected=evidence(data_dir,batch_id) if batch_id is not None else None
    fingerprint=digest(selected) if selected is not None else None
    db=sqlite3.connect(choice_path(data_dir),timeout=2); db.row_factory=sqlite3.Row
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS choices (revision INTEGER PRIMARY KEY,batch_id TEXT,evidence_fingerprint TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            old=latest(db)
            if old['revision']!=expected_revision: raise ValueError('模型選擇已變更，請重新核對版本。')
            if old['batch_id']==batch_id and old['evidence_fingerprint']==fingerprint: return old
            if old['revision']>=MAX_CHOICES and batch_id is not None:
                raise ValueError('模型選擇歷史已達上限；仍可回復原始模型。')
            if selected is not None and evidence(data_dir,batch_id)!=selected:
                raise ValueError('候選或確認來源已變更。')
            db.execute('INSERT INTO choices(revision,batch_id,evidence_fingerprint) VALUES (?,?,?)',
                       (old['revision']+1,batch_id,fingerprint))
            return latest(db)
    finally: db.close()


def select(data_dir,*,experiment=None,batch=None,active=False):
    if sum((experiment is not None,batch is not None,bool(active)))>1:
        raise ValueError('研究參數、確認批次及保存選擇不能同時選用。')
    if batch is None and not active: return local_models.select(experiment)
    data_dir=Path(data_dir).resolve()
    saved=choice(data_dir) if active else None
    if saved is not None: batch=saved['batch_id']
    selected=evidence(data_dir,batch) if batch is not None else None
    if saved is not None and saved['evidence_fingerprint']!=(digest(selected) if selected is not None else None):
        raise ValueError('保存的候選證據已變更；請重新核對或回復原始模型。')
    value={'schema':SCHEMA,'base':local_models.select(),'data_dir':str(data_dir),'batch':selected,'choice':saved,
           'inference_sources':{name:file_hash(local_models.ROOT/name) for name in FILES}}
    value['fingerprint']=digest(value)
    return value


def check_context(selection):
    """Cheap checks while a child runs; full file validation also surrounds it."""
    if selection.get('schema')!=SCHEMA: return
    if selection['inference_sources']!={name:file_hash(local_models.ROOT/name) for name in FILES}:
        raise ValueError('本機推理程式已變更。')
    if selection['choice'] is not None and choice(selection['data_dir'])!=selection['choice']:
        raise ValueError('保存的模型選擇已變更，請重新開啟引擎。')
    if selection['batch'] is not None:
        path=batches.folder(selection['data_dir'],selection['batch']['id'])
        manifest=batches.read_manifest(path,live=True)
        if digest(manifest)!=selection['batch']['manifest_sha256']: raise ValueError('候選批次已變更。')
        batches.examples_current(QueueReader(selection['data_dir']),manifest)


def validate(selection):
    if not isinstance(selection,dict): raise ValueError('模型選擇格式不符。')
    if selection.get('schema')!=SCHEMA:
        if local_models.select(selection.get('experiment'))!=selection: raise ValueError('原模型選擇已變更。')
        return
    if set(selection)!={'schema','base','data_dir','batch','choice','inference_sources','fingerprint'}:
        raise ValueError('模型選擇欄位不符。')
    if not isinstance(selection['data_dir'],str) or not Path(selection['data_dir']).is_absolute():
        raise ValueError('本機候選需要固定資料位置。')
    expected=select(selection['data_dir'],batch=selection['batch']['id'] if selection['batch'] and selection['choice'] is None else None,
                    active=selection['choice'] is not None)
    if expected!=selection: raise ValueError('選用參數、來源或版本已變更。')


def description(selection):
    if selection.get('schema')!=SCHEMA: return local_models.description(selection)
    result=local_models.description(selection['base'])
    result.update(selection_fingerprint=selection['fingerprint'],confirmed_batch=selection['batch'] is not None,
                  parameter_version='batch-'+selection['batch']['id'] if selection['batch'] else 'unadapted_base',
                  saved_choice_revision=selection['choice']['revision'] if selection['choice'] is not None else None)
    return result


def verify_assets(selection):
    validate(selection)
    if selection.get('schema')!=SCHEMA: return local_models.verify_assets(selection)
    base,_=local_models.verify_assets(selection['base']); adapter=None
    if selection['batch'] is not None:
        adapter=batches.folder(selection['data_dir'],selection['batch']['id'])/'adapter'
        if batches.adapter_files(adapter)!=selection['batch']['adapter_files']: raise ValueError('候選參數檔案已變更。')
        config=local_models.read_json(adapter/'adapter_config.json',65536)
        if config.get('peft_type')!='LORA' or config.get('task_type')!='CAUSAL_LM': raise ValueError('候選架構不符。')
    return base,adapter
