"""Explicit, cancellable per-file imports with receipts for committed files."""
from pathlib import Path

from .documents import MAX_DOCUMENTS


def freeze(items):
    if not isinstance(items,(list,tuple)) or not 1 <= len(items) <= MAX_DOCUMENTS:
        raise ValueError('每次請選取1到8個文件。')
    frozen=[]
    for item in items:
        if not isinstance(item,dict) or set(item)-{'path','scope','expected_sha256'} or 'path' not in item:
            raise ValueError('匯入選取格式不符。')
        path,scope,digest=item['path'],item.get('scope'),item.get('expected_sha256')
        if not isinstance(path,(str,Path)) or not str(path):raise ValueError('文件路徑無效。')
        if scope is not None and not isinstance(scope,(str,Path)):raise ValueError('搜尋範圍無效。')
        if digest is not None and (not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('搜尋來源指紋無效。')
        # Capture paths, but resolving/opening and scope checks belong to the worker.
        frozen.append({'path':Path(path).expanduser().absolute(),'scope':Path(scope).expanduser().absolute() if scope is not None else None,
                       'expected_sha256':digest})
    return tuple(frozen)


def execute(store,sid,items,cancel,progress):
    results=[{'name':Path(item['path']).name,'status':'unprocessed'} for item in items]
    for index,item in enumerate(items):
        if cancel.is_set():break
        progress('reading',index)
        row=results[index]
        try:
            options=dict(item)
            if options['scope'] is not None:
                from .scope import select_root
                options['scope']=select_root(options['scope'])
            did=store.attach_document(sid,**options,cancel=cancel)
        except Exception as exc:
            row.update(status='cancelled' if cancel.is_set() else 'failed',error=str(exc)[:1000] or type(exc).__name__)
        else:
            # Once attach_document returns, its transaction has committed.
            row.update(status='imported',document_id=did)
        progress('result',(index,dict(row)))
    return {'results':results,'stop_requested':cancel.is_set()}
