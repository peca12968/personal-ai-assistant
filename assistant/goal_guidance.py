"""Explicit append-only user steering of an existing, stopped goal."""
from datetime import datetime, timezone
import hashlib
import json
import re

from .answer_runs import lease
from .autonomy import MAX_STEPS, text
from . import goal_partials, goal_reply_sources, goal_review


def _snapshot(db, sid, gid):
    goal = db.execute('SELECT * FROM autonomous_goals WHERE id=? AND session_id=?',(gid,sid)).fetchone()
    if goal is None:
        raise ValueError('找不到此對話的自主目標。')
    review = next(goal_review._rows(db,gid))
    goal = dict(goal)
    fingerprint = review['fingerprint']
    sources = goal_reply_sources.read(db, gid, goal['notes'])
    if sources:
        goal['reply_sources'] = sources
        fingerprint = hashlib.sha256((fingerprint + ''.join(item['sha256'] for item in sources)).encode('utf-8')).hexdigest()
    if goal['status'] == 'needs_input':
        details = goal_partials.read_waiting(db, gid, goal['question'])
        goal.update(details)
        if 'partial_result' in details:
            fingerprint = hashlib.sha256((fingerprint + '\0' + details['partial_result']).encode('utf-8')).hexdigest()
        if 'question_citations' in details:
            citations = json.dumps(details['question_citations'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            fingerprint = hashlib.sha256((fingerprint + '\0' + citations).encode('utf-8')).hexdigest()
    return {'goal':goal, 'fingerprint':fingerprint,
            'step_count':review['step_count'], 'sources_match':review['sources_match']}


def snapshot(store, sid, gid):
    with store.connect() as db:
        db.execute('BEGIN')
        return _snapshot(db,sid,gid)


def eligibility(value):
    status = value['goal']['status']
    if status == 'report_ready':
        return False, '此目標已有成果；請帶入所選成果建立下一階段。'
    if status not in ('pending','paused','failed','needs_input'):
        return False, '請先停止工作並等候流程結束，再重新載入；執行中紀錄本身不證明程序仍存活。'
    if not value['sources_match']:
        return False, '參考文件已改變，請以目前文件另建目標。'
    if value['step_count'] >= MAX_STEPS:
        return False, '此目標已達32步上限，請選取此目標並帶入進度建立下一階段。'
    return True, '保存後不會自動執行；回到自主目標按開始／接續。'


def save(store, sid, gid, content, *, expected_fingerprint):
    content = text(content,4000)
    if not isinstance(expected_fingerprint,str) or not re.fullmatch('[0-9a-f]{64}',expected_fingerprint):
        raise ValueError('需要讀取目標時的指紋，請重新載入。')
    with lease(store,gid), store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = _snapshot(db,sid,gid)
        if current['fingerprint'] != expected_fingerprint:
            raise ValueError('目標或來源已有變更，請重新載入並核對；未保存補充。')
        allowed, reason = eligibility(current)
        if not allowed:
            raise ValueError(reason)
        stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        addition = f"\n使用者主動更正／補充（已記錄{current['step_count']}步；{stamp}）：\n{content}\n"
        notes = current['goal']['notes'] + addition
        if len(notes) > 12000:
            raise ValueError('累計補充超過12000字，請整理目前紀錄另建目標。')
        # Keep objective, question, status, source/tool scope and past results.
        db.execute('UPDATE autonomous_goals SET notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(notes,gid))
        return _snapshot(db,sid,gid)
