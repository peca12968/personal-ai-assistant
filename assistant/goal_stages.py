"""Explicit one-stage handoffs; reports remain historical, unverified evidence."""
import hashlib
import json


def capture(db, sid, parent_id):
    row = db.execute('SELECT * FROM autonomous_goals WHERE session_id=? AND id=?', (sid, parent_id)).fetchone()
    if row is None:
        raise ValueError('找不到此對話的上一階段目標。')
    if row['status'] != 'report_ready' or not row['result'].strip():
        raise ValueError('上一階段需先交付成果；未完成目標請先補充或接續。')
    if len(row['result']) > 12000 or len(row['notes']) > 12000 or len(row['objective']) > 4000:
        raise ValueError('上一階段資料超過支援上限，請整理為新目標。')
    return {'parent_goal_id': row['id'], 'objective': row['objective'],
            'report': row['result'], 'report_sha256': hashlib.sha256(row['result'].encode('utf-8')).hexdigest(),
            'user_notes': row['notes'], 'sources': json.loads(row['sources_json']),
            'recorded_status': row['status'], 'parent_updated_at': row['updated_at']}


def read(db, gid):
    row = db.execute('SELECT parent_goal_id,context_json FROM autonomous_handoffs WHERE goal_id=?', (gid,)).fetchone()
    if row is None:
        return None
    value = json.loads(row['context_json'])
    if value['parent_goal_id'] != row['parent_goal_id'] or hashlib.sha256(value['report'].encode('utf-8')).hexdigest() != value['report_sha256']:
        raise ValueError('上一階段快照不一致，請查看原目標並另建階段。')
    return value
