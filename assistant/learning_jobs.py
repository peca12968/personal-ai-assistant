"""Desktop preparation and read-only previews of finite idle learning jobs."""
import json
from pathlib import Path
from . import idle_learning as idle,learning_batches as batches,learning_queue as examples

LABELS={'prepared':'已準備，尚未開始','waiting':'等待閒置','training':'訓練與重載檢查','checking':'完整助理驗收',
        'wait_expired':'等待期限已到','already_satisfied':'原參數已符合，未訓練','validated_candidate':'驗收通過，尚未啟用',
        'validation_rejected':'完整驗收未通過','stale':'來源已變更','cancelled':'已停止','failed':'工作失敗',
        'training_rejected':'訓練驗收未通過','training_failed':'訓練失敗','training_cancelled':'訓練已停止','training_stale':'訓練來源已變更'}


def picked_id(data_dir,path,*,job=False):
    path=Path(path).resolve(); base=Path(data_dir).resolve()/'learning-batches'
    if job: base=base/'_idle_jobs'
    ident=batches.identifier(path.parent.name)
    if path.name.lower()!='manifest.json' or path.parent.parent!=base:
        raise ValueError('請選擇這份助理資料目錄內對應的manifest.json，不能使用其他資料庫的工作。')
    return ident


def prepare(store,selected,suite,settings,*,parent=None,existing=None,cancel=None):
    # Validate every field before creating a training batch. Never infer targets.
    suite=idle.checks.suite(suite); idle.limits(**settings)
    result={'batch_id':existing,'job_id':None,'error':None}
    try:
        if cancel and cancel.is_set(): raise ValueError('已停止準備，沒有啟動訓練。')
        if existing is None: result['batch_id']=batches.prepare(store,selected,parent=parent)
        else: idle.prepared_batch(store.path.parent,existing)
        if cancel and cancel.is_set(): raise ValueError('已停止；已準備批次保留，沒有啟動訓練。')
        result['job_id']=idle.prepare(store.path.parent,result['batch_id'],suite,**settings)
        result['preview']=inspect(store.path.parent,result['job_id'])
    except Exception as exc: result['error']=str(exc)[:2000]
    return result


def inspect(data_dir,job_id):
    path=idle.folder(data_dir,job_id); manifest=idle.read_manifest(path)
    report=idle.report(data_dir,job_id); batch=batches.read_manifest(batches.folder(data_dir,manifest['batch_id']))
    status=report['receipt'].get('status')
    if status not in LABELS: raise ValueError('工作紀錄狀態不完整或不受支援，不能開始。')
    startable=status=='prepared' and report['sources_still_current']
    if startable:
        _,receipt_hash=idle.prepared_batch(data_dir,manifest['batch_id'])
        startable=receipt_hash==manifest['batch_receipt_sha256']
    lines=[f"工作：{job_id}",f"批次：{manifest['batch_id']}",f"歷史狀態：{LABELS[status]}",
           '來源目前有效：'+('是' if report['sources_still_current'] else '否'),
           '歷史狀態不是程序正在執行的證據；此處不接管或重跑舊工作。',
           f"閒置門檻{manifest['idle_seconds']}秒；等待最多{manifest['wait_seconds']}秒；工作最多{manifest['work_seconds']}秒。",
           '不自動啟用候選。\n\n已凍結的訓練項目（包括前代重播）：']
    for row in batch['items']: lines.append(f"版本{row['revision']} · {row['id']}\n問題：{row['prompt']}\n答案：{row['answer']}\n")
    lines.append('\n完整助理驗收（不作訓練資料）：')
    for row in manifest['suite']['cases']:
        history='\n'.join(('使用者' if h['role']=='user' else '助理')+'：'+h['content'] for h in row['history']) or '無'
        lines.append(f"{'目標' if row['group']=='target' else '控制'} · {row['id']}\n問題：{row['question']}\n完整預期答案：{row['answer']}\n前文："+history)
    receipt=report['receipt']
    if receipt.get('error'): lines.append('原因：'+str(receipt['error']))
    if receipt.get('stop_reason'): lines.append('停止原因：'+receipt['stop_reason'])
    if report['current_error']: lines.append('目前來源：'+report['current_error'])
    if report['validation']:
        vr=report['validation']['receipt']; lines.append('完整驗收歷史：'+vr['status'])
        if vr.get('assessment'): lines.append(json.dumps(vr['assessment']['checks'],ensure_ascii=False,indent=2))
    text='\n'.join(lines)
    if len(text)>200000: text=text[:200000]+f'\n\n顯示前200000字，完整規約請用「開啟本機規約」查看：{path}'
    return {'id':job_id,'batch_id':manifest['batch_id'],'startable':bool(startable),'status':status,
            'current':report['sources_still_current'],'text':text,'manifest_path':str(path/'manifest.json')}
