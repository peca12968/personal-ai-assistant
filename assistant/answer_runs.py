"""Durable answer states with a live SQLite lease; no automatic model retries."""
import json
import re
import sqlite3
from contextlib import contextmanager


ERROR_LABELS = {
    'transcription_invalid': '本機逐字轉錄未完成；來源、格式或輸出核對失敗，沒有保存完成答案。',
    'local_system_memory': 'Windows 回報記憶體／分頁檔可用額度不足（1455），本機模型未完成作答，沒有保存完成答案。',
    'calculation_invalid': '本機精確計算的算式、範圍或輸出要求不符，沒有保存完成答案；未呼叫模型。請修正算式或要求後重新提問。',
    'temp_cleanup_failed': '推理暫存資料未能完全清理，沒有保存完成答案；此狀態本身不證明雲端請求已停止。',
    'timeout': '上次回答逾時，沒有保存完成答案。',
    'cancelled': '這次回答已取消，沒有保存完成答案。',
    'process_failed': '推理引擎未正常完成；確切登入、網路或額度原因未確認。',
    'launch_failed': '本機未能啟動推理引擎。',
    'empty_response': '引擎沒有傳回可用的非空答案。',
    'invalid_response': '引擎回覆不是有效文字。',
    'input_too_large': '本次輸入超過引擎限制。',
    'output_too_large': '引擎輸出超過限制，未保存成完成答案。',
    'local_io': '本機推理資料讀寫失敗。',
    'cleanup_unconfirmed': '已要求停止，但未能確認推理子程序結束。',
    'invalid_input': '推理輸入格式無效。',
    'owner_lost': '先前本機流程已沒有工作鎖，且未保存完成狀態；可能曾中斷，雲端請求是否結束未確認。',
    'unexpected_error': '回答流程發生未分類錯誤，沒有保存完成答案。',
}


class LeaseBusy(RuntimeError):
    pass


@contextmanager
def lease(store, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('回答紀錄編號無效。')
    folder = store.path.parent / 'answer-locks'
    folder.mkdir(exist_ok=True)
    db = sqlite3.connect(folder / (run_id + '.sqlite3'), timeout=0, isolation_level=None)
    try:
        try:
            db.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError as exc:
            if (getattr(exc, 'sqlite_errorcode', 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise LeaseBusy('這次回答仍由另一個本機流程持有。') from None
            raise
        yield
    finally:
        db.close()  # OS releases the lock on process exit as well; never delete a locked file.


def runs(store, sid, limit=None):
    with store.connect() as db:
        sql = '''SELECT id,session_id,user_message_id,answer_message_id,status,error_code,started_at,finished_at
                 FROM answer_runs WHERE session_id=? ORDER BY rowid DESC'''
        return [dict(row) for row in db.execute(sql + (' LIMIT ?' if limit else ''), (sid, limit) if limit else (sid,))]


def recover(store, sid):
    """Only an acquired exclusive lease can establish that the local owner is absent."""
    with store.connect() as db:
        active = [dict(row) for row in db.execute("SELECT id FROM answer_runs WHERE session_id=? AND status='running'", (sid,))]
    for row in active:
        try:
            with lease(store, row['id']), store.connect() as db:
                db.execute("UPDATE answer_runs SET status='interrupted',error_code='owner_lost',finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'", (row['id'],))
        except LeaseBusy:
            continue


def retry_target(store, sid):
    recover(store, sid)
    rows = runs(store, sid, 1)
    if not rows or rows[0]['status'] not in {'failed', 'cancelled', 'interrupted'}:
        raise ValueError('目前沒有可重試的最後一題；進行中或已完成的回答不會重送。')
    row = rows[0]
    messages = store.messages(sid, 1)
    if not messages or messages[0]['id'] != row['user_message_id'] or messages[0]['role'] != 'user':
        raise ValueError('這題之後已有其他對話；請將需要的問題重新送出，不在新對話後補插舊答案。')
    return {**row, 'question': messages[0]['content']}


def begin(store, sid, run_id, question, context, retry=None, draft=None):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM answer_runs WHERE session_id=? AND status='running'", (sid,)).fetchone():
            raise ValueError('目前對話已有尚未完成的回答流程，請先等候或停止。')
        if 'image_inputs' in context:
            from .image_inputs import check_references
            check_references(db, sid, context['image_inputs'], historical=retry is not None)
        if retry:
            latest = db.execute('SELECT id,status,user_message_id FROM answer_runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (sid,)).fetchone()
            message = db.execute('SELECT id,role,content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1', (sid,)).fetchone()
            if (not latest or latest['id'] != retry['id'] or latest['status'] not in {'failed','cancelled','interrupted'} or
                    not message or message['id'] != latest['user_message_id'] or message['role'] != 'user' or message['content'] != question):
                raise ValueError('對話或重試狀態已變更，沒有重送。')
            uid = latest['user_message_id']
        else:
            uid = db.execute("INSERT INTO messages(session_id,role,content) VALUES(?,'user',?)", (sid, question)).lastrowid
            from .chat_drafts import consume
            consume(db, sid, question, draft, uid)
            db.execute("UPDATE sessions SET title=? WHERE id=? AND title='新對話'", (question[:50], sid))
        db.execute('INSERT INTO answer_runs(id,session_id,user_message_id,status,context_json) VALUES(?,?,?,\'running\',?)',
                   (run_id, sid, uid, json.dumps(context, ensure_ascii=False)))
    return uid


def finish(store, run_id, answer, context, checks, *, delivery=None):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT session_id FROM answer_runs WHERE id=? AND status='running'", (run_id,)).fetchone()
        if row is None:
            raise ValueError('回答狀態已改變，沒有另存答案。')
        mid = db.execute("INSERT INTO messages(session_id,role,content) VALUES(?,'assistant',?)", (row['session_id'], answer)).lastrowid
        db.execute('INSERT INTO answer_contexts(message_id,payload) VALUES(?,?)', (mid, json.dumps(context, ensure_ascii=False)))
        db.execute('INSERT INTO answer_checks(message_id,payload) VALUES(?,?)', (mid, json.dumps(checks, ensure_ascii=False)))
        if delivery is not None:
            from .response_delivery import save
            save(db, mid, answer, delivery)
        db.execute("UPDATE answer_runs SET status='succeeded',answer_message_id=?,finished_at=CURRENT_TIMESTAMP WHERE id=?", (mid, run_id))
    return mid


def fail(store, run_id, code):
    code = code if code in ERROR_LABELS and code != 'owner_lost' else 'unexpected_error'
    with store.connect() as db:
        db.execute("UPDATE answer_runs SET status=?,error_code=?,finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'",
                   ('cancelled' if code == 'cancelled' else 'failed', code, run_id))


def context_status(store, sid):
    return [{**row, 'explanation': ('本機只記錄尚未完成，不能據此確認雲端推理仍在運作。' if row['status'] == 'running' else
                                  '答案已保存。' if row['status'] == 'succeeded' else ERROR_LABELS.get(row['error_code'], ERROR_LABELS['unexpected_error']))}
            for row in runs(store, sid, 3)]


def conversation_tail(store, sid):
    with store.connect() as db:
        row = db.execute('''SELECT m.id AS message_id,m.role,
            EXISTS(SELECT 1 FROM local_memory_messages l WHERE l.message_id=m.id) AS local_memory
            FROM messages m WHERE session_id=? ORDER BY m.id DESC LIMIT 1''', (sid,)).fetchone()
    return dict(row) if row else None
