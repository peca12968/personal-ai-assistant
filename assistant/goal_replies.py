"""Bind explicit replies to the goal and question the user actually reviewed."""
import re

from .answer_runs import lease
from .autonomy import text
from . import goal_reply_sources
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
        notes = goal['notes'] + goal_reply_sources.note(goal['question'], answer)
        if len(notes) > 12000:
            raise ValueError('補充內容已達上限，請建立下一階段目標。')
        goal_reply_sources.record(db, gid, goal['question'], answer, len(goal['notes']))
        db.execute("UPDATE autonomous_goals SET notes=?,question='',status='paused',notice='',updated_at=CURRENT_TIMESTAMP WHERE id=? AND session_id=?",
                   (notes, gid, sid))
        return _snapshot(db, sid, gid)


def withdraw(store, sid, gid, correction_number, *, expected_fingerprint):
    if type(correction_number) is not int or correction_number < 1:
        raise ValueError('請選擇已保存的更正編號。')
    if not isinstance(expected_fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', expected_fingerprint):
        raise ValueError('請先核對目前問題、更正及目標指紋。')
    with lease(store, gid), store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = _snapshot(db, sid, gid)
        if current['fingerprint'] != expected_fingerprint:
            raise ValueError('問題、目標進度或來源已變更；請重新載入後核對。')
        options = current.get('question_withdrawal')
        if not options or correction_number not in options['correction_numbers']:
            raise ValueError('需選擇目前提問後保存的更正，且目標仍可接續。')
        source = next(item for item in current['goal']['guidance_sources'] if item['number'] == correction_number)
        db.execute('INSERT INTO autonomous_question_withdrawals(goal_id,ask_sequence,guidance_number,question_sha256,guidance_sha256) '
                   'VALUES(?,?,?,?,?)', (gid, options['ask_sequence'], correction_number,
                                        options['question_sha256'], source['sha256']))
        db.execute("UPDATE autonomous_goals SET question='',status='paused',notice=?,updated_at=CURRENT_TIMESTAMP "
                   "WHERE id=? AND session_id=?", ('已依所選更正撤下問題，可開始／接續。', gid, sid))
        return _snapshot(db, sid, gid)
