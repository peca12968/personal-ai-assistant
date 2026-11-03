"""Bind explicit replies to the goal and question the user actually reviewed."""
import re

from .answer_runs import lease
from .autonomy import text
from .goal_guidance import _snapshot, snapshot


def save(store, sid, gid, answer, *, expected_fingerprint):
    answer = text(answer, 4000)
    if not isinstance(expected_fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', expected_fingerprint):
        raise ValueError('請先讀取目前問題與目標指紋，再保存回答。')
    with lease(store, gid), store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = _snapshot(db, sid, gid)
        if current['fingerprint'] != expected_fingerprint:
            raise ValueError('問題、目標進度或來源已變更；回答尚未保存。請重新載入並核對，草稿仍保留。')
        goal = current['goal']
        if goal['status'] != 'needs_input' or not goal['question']:
            raise ValueError('此目標目前沒有等待補充的問題。')
        notes = goal['notes'] + '\n問題：' + goal['question'] + '\n使用者補充：' + answer
        if len(notes) > 12000:
            raise ValueError('補充內容已達上限，請建立下一階段目標。')
        db.execute("UPDATE autonomous_goals SET notes=?,question='',status='paused',notice='',updated_at=CURRENT_TIMESTAMP WHERE id=? AND session_id=?",
                   (notes, gid, sid))
        return _snapshot(db, sid, gid)
