"""Bind explicit replies to the goal and question the user actually reviewed."""
import re

from .answer_runs import lease
from .autonomy import MAX_STEPS, text
from . import decision_table, goal_question_withdrawals, goal_reply_sources
from .goal_guidance import (_append_step, _snapshot, _steps, append_followup_question, append_waiting_recheck,
                            apply_waiting_review, snapshot)


def _save_partial_followup(db, current, notes, kind, review):
    goal, count = current['goal'], current['step_count']
    gid = goal['id']
    rechecked = review is not None and review['field_reference']['status'] == 'needs_field'
    if not current['sources_match'] or count + (3 if kind == 'field' else 4) + int(rechecked) > MAX_STEPS:
        raise ValueError('來源已改變或剩餘步數不足，請帶入目前進度建立下一階段。')
    if rechecked:
        append_waiting_recheck(db, current, notes)
    steps = _steps(db, gid)
    replies = goal_reply_sources.read(db, gid, notes)
    args = {'field_reference': replies[-1]['citation'], 'partial_reply': True}
    result = decision_table.observe(args, steps, replies, goal.get('guidance_sources', ()))
    action = {'tool': 'check_decision', 'args': args, 'note': '保存原分項提問已回答的項目'}
    _append_step(db, goal, notes, len(steps) + 1, action, result)
    remaining = result['field_reference']['partial_reply']['next_question']
    append_followup_question(db, {**goal, 'reply_sources': replies}, notes, len(steps) + 2,
                             remaining, '保留原分項提問尚未回答的項目')
    db.execute("UPDATE autonomous_goals SET notes=?,status='needs_input',question=?,notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (notes, remaining, '已保存這一項。請回答剩下的問題。', gid))


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
        review = goal.get('reference_review')
        if current.get('reference_partial_reply') and (kind := decision_table.partial_reply_kind(_steps(db, gid), answer)):
            _save_partial_followup(db, current, notes, kind, review)
            return _snapshot(db, sid, gid)
        if review and review['field_reference']['status'] == 'needs_field' and current['step_count'] + 3 <= MAX_STEPS:
            append_waiting_recheck(db, current, notes)
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
        goal = current['goal']
        source = next(item for item in goal['guidance_sources'] if item['number'] == correction_number)
        review = goal.get('reference_review')
        if (review and review['field_reference']['status'] != 'needs_field' and current['step_count'] + 2 <= MAX_STEPS
                and source['citation'] == review['field_reference']['recheck']['evidence']['citation']):
            return apply_waiting_review(db, current)
        goal_question_withdrawals.record(db, gid, options, source)
        db.execute("UPDATE autonomous_goals SET question='',status='paused',notice=?,updated_at=CURRENT_TIMESTAMP "
                   "WHERE id=? AND session_id=?", ('已依所選更正撤下問題，可開始／接續。', gid, sid))
        return _snapshot(db, sid, gid)
