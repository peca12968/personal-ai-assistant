"""Explicit append-only user steering of an existing, stopped goal."""
from datetime import datetime, timezone
import hashlib
import json
import re

from .answer_runs import lease
from .autonomy import MAX_STEPS, text
from . import (conversation_history, decision_table, goal_guidance_sources, goal_partials, goal_question_withdrawals,
               goal_reply_sources, goal_review, work_plan)


def _steps(db, gid):
    return [{'sequence': number, 'action': json.loads(action), 'observation': json.loads(observation)}
            for number, action, observation in db.execute(
                'SELECT sequence,action_json,observation_json FROM autonomous_steps WHERE goal_id=? ORDER BY sequence', (gid,))]


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
    guidance = goal_guidance_sources.read(db, gid, goal['notes'], review['step_count'])
    if guidance:
        goal['guidance_sources'] = guidance
        fingerprint = hashlib.sha256((fingerprint + ''.join(item['sha256'] for item in guidance)).encode('utf-8')).hexdigest()
    waiting_step = review['step_count'] if goal['status'] == 'needs_input' else None
    withdrawals = goal_question_withdrawals.read(db, gid, guidance, sources, waiting_step=waiting_step)
    if withdrawals:
        goal['withdrawn_questions'] = withdrawals
        fingerprint = hashlib.sha256((fingerprint + ''.join(item['question_sha256'] + item['guidance_sha256']
                                                             for item in withdrawals)).encode('utf-8')).hexdigest()
    options = None
    if goal['status'] == 'needs_input':
        details = goal_partials.read_waiting(db, gid, goal['question'])
        goal.update(details)
        if 'partial_result' in details:
            fingerprint = hashlib.sha256((fingerprint + '\0' + details['partial_result']).encode('utf-8')).hexdigest()
        if 'question_citations' in details:
            citations = json.dumps(details['question_citations'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            fingerprint = hashlib.sha256((fingerprint + '\0' + citations).encode('utf-8')).hexdigest()
        if review['sources_match'] and waiting_step < MAX_STEPS:
            options = goal_question_withdrawals.options(db, gid, goal['question'], waiting_step, guidance)
            if options:
                fingerprint = hashlib.sha256((fingerprint + options['question_sha256']).encode('utf-8')).hexdigest()
    result = {'goal':goal, 'fingerprint':fingerprint,
              'step_count':review['step_count'], 'sources_match':review['sources_match']}
    if options:
        result['question_withdrawal'] = options
    if goal['status'] == 'report_ready' and not review['file_delivery']:
        steps = _steps(db, gid)
        if reference := decision_table.reference_report(steps, goal['result']):
            decision_table._recheck_saved(steps, sources, guidance)
            result['reference_report'] = reference['observation']['field_reference']
            receipt = json.dumps(reference, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            result['fingerprint'] = hashlib.sha256((fingerprint + '\0' + receipt).encode('utf-8')).hexdigest()
    return result


def snapshot(store, sid, gid):
    with store.connect() as db:
        db.execute('BEGIN')
        return _snapshot(db,sid,gid)


def eligibility(value):
    status = value['goal']['status']
    reference_report = status == 'report_ready' and 'reference_report' in value
    if status == 'report_ready' and not reference_report:
        return False, '此目標已有成果；請帶入所選成果建立下一階段。'
    if status not in ('pending','paused','failed','needs_input','report_ready'):
        return False, '請先停止工作並等候流程結束，再重新載入；執行中紀錄本身不證明程序仍存活。'
    if not value['sources_match']:
        return False, '參考文件已改變，請以目前文件另建目標。'
    if value['step_count'] >= MAX_STEPS:
        return False, '此目標已達32步上限，請選取此目標並帶入進度建立下一階段。'
    if reference_report:
        if value['step_count'] + 2 > MAX_STEPS:
            return False, '剩餘步數不足以重核及更新成果，請帶入原成果建立下一階段。'
        return True, '可用完整欄名更正一欄。保存時會按原話先後重核各分支，保留先前成果，等待你接續。'
    return True, '保存後不會自動執行；回到自主目標按開始／接續。'


def _save_reference_recheck(db, goal, notes, step_count):
    gid = goal['id']
    steps = _steps(db, gid)
    sources = goal_guidance_sources.read(db, gid, notes, step_count)
    args = {'field_reference': sources[-1]['citation'], 'recheck': True}
    observation = decision_table.observe(args, steps, goal.get('reply_sources', ()), sources)
    needs_field = observation['field_reference']['status'] == 'needs_field'
    if step_count + (4 if needs_field else 2) > MAX_STEPS:
        raise ValueError('剩餘步數不足以重核及完成新成果，請帶入原成果建立下一階段。')
    history = conversation_history.saved_fingerprint(db, gid)
    scope = {**goal, 'notes': notes, 'sources': json.loads(goal['sources_json'])}
    if history is not None:
        scope['conversation_history'] = {'sha256': history}
    observation['goal_conditions_sha256'] = work_plan.conditions(scope)
    action = {'tool': 'check_decision', 'args': args, 'note': '依使用者正式更正重新核對原候選分支'}
    db.execute('INSERT INTO autonomous_steps(goal_id,sequence,action_json,observation_json) VALUES(?,?,?,?)',
               (gid, step_count + 1, json.dumps(action, ensure_ascii=False), json.dumps(observation, ensure_ascii=False)))
    db.execute("UPDATE autonomous_goals SET notes=?,status='paused',result='',notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (notes, '已重核原分支；先前成果保留在步驟紀錄，請接續更新成果。', gid))


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
        goal = current['goal']
        step_count = current['step_count']
        notes = goal['notes'] + goal_guidance_sources.note(content, step_count, stamp)
        if len(notes) > 12000:
            raise ValueError('累計補充超過12000字，請整理目前紀錄另建目標。')
        number = len(goal.get('guidance_sources', ())) + 1
        db.execute('INSERT INTO autonomous_guidance_sources(goal_id,number,notes_start,steps_seen,stamp,content) '
                   'VALUES(?,?,?,?,?,?)', (gid, number, len(goal['notes']), step_count, stamp, content))
        if 'reference_report' in current:
            _save_reference_recheck(db, goal, notes, step_count)
        else:
            db.execute('UPDATE autonomous_goals SET notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(notes,gid))
        return _snapshot(db,sid,gid)
