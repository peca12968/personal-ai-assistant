"""Local cross-session follow-up, never a model prompt or an execution queue."""
import hashlib
import json

from .autonomy import MAX_STEPS, signature
from .artifacts import inventory
from . import autonomy_csv
from . import conversation_history


def next_action(status, sources_match, step_count):
    if status == 'report_ready':
        return 'review', '查看已保存成果；追加工作需另建下一階段。'
    if not sources_match:
        return 'new_goal', '參考文件已變更，請按目前文件另建目標，不能直接接續。'
    if step_count >= MAX_STEPS:
        return 'new_goal', '已達此目標32步上限；查看紀錄後，可帶入所選進度建立下一階段。'
    if status == 'needs_input':
        return 'reply', '先保存必要補充，再開始／接續。'
    if status == 'running':
        return 'inspect', '紀錄尚未結束；先查看流程。接續會檢查工作鎖，不能據此判斷是否仍在執行。'
    if status in ('pending', 'paused', 'failed'):
        return 'resume', '查看紀錄後可開始／接續一輪自主工作。'
    return 'inspect', '請查看目標紀錄。'


def _rows(db, gid=None):
    # One SQLite read transaction supplies the goals, current sources and receipts.
    query = '''SELECT g.*,s.title AS session_title,
        (SELECT COUNT(*) FROM autonomous_steps t WHERE t.goal_id=g.id) AS step_count,
        EXISTS(SELECT 1 FROM autonomous_delivery d WHERE d.goal_id=g.id) AS file_delivery,
        r.fingerprint AS hidden_fingerprint
        FROM autonomous_goals g JOIN sessions s ON s.id=g.session_id
        LEFT JOIN goal_reviews r ON r.goal_id=g.id'''
    if gid is not None:
        query += ' WHERE g.id=?'
    query += ''' ORDER BY CASE g.status WHEN 'needs_input' THEN 0 WHEN 'report_ready' THEN 1
        WHEN 'failed' THEN 2 WHEN 'paused' THEN 3 WHEN 'pending' THEN 4 ELSE 5 END,
        g.updated_at DESC,g.rowid DESC'''
    current_sources = {}
    for row in db.execute(query, (gid,) if gid is not None else ()):
        row = dict(row)
        sid = row['session_id']
        if sid not in current_sources:
            current_sources[sid] = signature(db.execute(
                'SELECT id,name,sha256 FROM documents WHERE session_id=? ORDER BY id', (sid,)).fetchall())
        files = inventory(db, row['id'])
        hidden_fingerprint = row.pop('hidden_fingerprint')
        # Full local state drives redisplay, even when the visible excerpt is clipped.
        evidence = {**row, 'current_sources': current_sources[sid], 'artifacts': files,
                    'csv_plans': autonomy_csv.inventory(db, row['id'])}
        history_sha = conversation_history.saved_fingerprint(db, row['id'])
        if history_sha is not None:
            evidence['conversation_history_sha256'] = history_sha
        latest = db.execute('SELECT * FROM autonomous_continuous_runs WHERE goal_id=? ORDER BY rowid DESC LIMIT 1', (row['id'],)).fetchone()
        if latest is not None:  # Preserve pre-upgrade fingerprints until there is new work.
            evidence['continuous_run'] = dict(latest)
        fingerprint = hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')).hexdigest()
        sources_match = json.loads(row['sources_json']) == current_sources[sid]
        action, explanation = next_action(row['status'], sources_match, row['step_count'])
        yield {'goal_id': row['id'], 'session_id': sid,
               'session_title': row['session_title'][:100], 'objective': row['objective'][:600],
               'objective_truncated': len(row['objective']) > 600,
               'recorded_status': row['status'], 'question': row['question'][:800],
               'step_count': row['step_count'], 'sources_match': sources_match,
               'file_delivery': bool(row['file_delivery']), 'saved_artifact_count': len(files),
               'next_action': action, 'explanation': explanation, 'updated_at': row['updated_at'],
               'fingerprint': fingerprint, 'hidden': hidden_fingerprint == fingerprint}


def snapshot(store, *, include_hidden=False, offset=0, limit=100):
    if type(include_hidden) is not bool or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('回顧清單參數不符。')
    with store.connect() as db:
        db.execute('BEGIN')
        rows = list(_rows(db))
    visible = [row for row in rows if not row['hidden']]
    selected = rows if include_hidden else visible
    return {'items': selected[offset:offset + limit], 'visible_count': len(visible),
            'hidden_count': len(rows) - len(visible), 'total_count': len(rows),
            'offset': offset, 'limit': limit, 'listed_count': len(selected),
            'has_more': offset + limit < len(selected),
            'note': '本機跨對話回顧；未呼叫模型、未啟動工作、未驗收成果。狀態是讀取當時的紀錄。'}


def set_hidden(store, sid, gid, *, fingerprint, hidden):
    if type(hidden) is not bool:
        raise ValueError('收起選項需為布林值。')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = next(_rows(db, gid), None)
        if row is None or row['session_id'] != sid:
            raise ValueError('找不到此對話的目標。')
        if row['fingerprint'] != fingerprint:
            raise ValueError('目標或參考資料已有新進度，請重新整理後再選擇。')
        if hidden:
            db.execute('''INSERT INTO goal_reviews(goal_id,fingerprint) VALUES(?,?)
                ON CONFLICT(goal_id) DO UPDATE SET fingerprint=excluded.fingerprint,hidden_at=CURRENT_TIMESTAMP''',
                (gid, fingerprint))
        else:
            db.execute('DELETE FROM goal_reviews WHERE goal_id=?', (gid,))
    return {'goal_id': gid, 'hidden': hidden,
            'note': '僅變更回顧顯示；未停止、接續、刪除或驗收目標。新進度會再次顯示。'}
