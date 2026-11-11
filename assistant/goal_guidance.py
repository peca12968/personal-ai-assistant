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
                steps = _steps(db, gid)
                pending = decision_table.answered_reference_review(steps, sources, guidance)
                key = 'answered_reference_review'
                if pending is None:
                    pending = decision_table.waiting_reference_review(steps, sources, guidance)
                    key = 'reference_review'
                if pending is not None:
                    goal[key] = pending
                    receipt = json.dumps(pending, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                    fingerprint = hashlib.sha256((fingerprint + '\0' + receipt).encode('utf-8')).hexdigest()
    result = {'goal':goal, 'fingerprint':fingerprint,
              'step_count':review['step_count'], 'sources_match':review['sources_match']}
    if options:
        result['question_withdrawal'] = options
    if goal['status'] == 'needs_input' and decision_table.compound_reference_ask(_steps(db, gid)):
        result['reference_partial_reply'] = True
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


def _append_step(db, goal, notes, sequence, action, observation):
    gid = goal['id']
    history = conversation_history.saved_fingerprint(db, gid)
    scope = {**goal, 'notes': notes, 'sources': json.loads(goal['sources_json'])}
    if history is not None:
        scope['conversation_history'] = {'sha256': history}
    observation = {**observation, 'goal_conditions_sha256': work_plan.conditions(scope)}
    db.execute('INSERT INTO autonomous_steps(goal_id,sequence,action_json,observation_json) VALUES(?,?,?,?)',
               (gid, sequence, json.dumps(action, ensure_ascii=False), json.dumps(observation, ensure_ascii=False)))


def _append_reference_recheck(db, goal, notes, sequence, args, observation):
    action = {'tool': 'check_decision', 'args': args, 'note': '依使用者正式更正重新核對原候選分支'}
    _append_step(db, goal, notes, sequence, action, observation)


def _save_reference_recheck(db, goal, notes, step_count):
    gid = goal['id']
    steps = _steps(db, gid)
    sources = goal_guidance_sources.read(db, gid, notes, step_count)
    args = {'field_reference': sources[-1]['citation'], 'recheck': True}
    observation = decision_table.observe(args, steps, goal.get('reply_sources', ()), sources)
    needs_field = observation['field_reference']['status'] == 'needs_field'
    if step_count + (4 if needs_field else 2) > MAX_STEPS:
        raise ValueError('剩餘步數不足以重核及完成新成果，請帶入原成果建立下一階段。')
    _append_reference_recheck(db, goal, notes, step_count + 1, args, observation)
    db.execute("UPDATE autonomous_goals SET notes=?,status='paused',result='',notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (notes, '已重核原分支；先前成果保留在步驟紀錄，請接續更新成果。', gid))


def append_waiting_recheck(db, current, notes):
    goal = current['goal']
    source = goal['guidance_sources'][-1]
    args = {'field_reference': source['citation'], 'waiting_recheck': True}
    _append_reference_recheck(db, goal, notes, current['step_count'] + 1, args, goal['reference_review'])


def append_answered_revision(db, current, notes):
    goal = current['goal']
    observation = goal['answered_reference_review']
    revision = observation['field_reference']['answered_revision']
    required = 3 if revision['item'] == 'remaining' and not revision.get('withdraws_question') else 2
    if current['step_count'] + required > MAX_STEPS:
        raise ValueError('剩餘步數不足以保存更正及完成成果，請帶入目前進度建立下一階段。')
    source = revision.get('source', revision['changes'][-1]['evidence']['citation'])
    action = {'tool': 'check_decision', 'args': {'field_reference': source, 'answered_revision': True},
              'note': '保存等待期間已答項的正式更正'}
    _append_step(db, goal, notes, current['step_count'] + 1, action, observation)


def append_followup_question(db, goal, notes, sequence, question, note):
    excerpts = [row for step in _steps(db, goal['id']) for row in step['observation'].get('excerpts', [])]
    citations = goal_partials.question_citations(question, excerpts, goal.get('reply_sources', ()),
                                                  goal.get('guidance_sources', ()))
    observation = {'question': question}
    if citations:
        observation['question_citations'] = citations
    action = {'tool': 'ask', 'args': {'question': question}, 'note': note}
    _append_step(db, goal, notes, sequence, action, observation)


def apply_waiting_review(db, current):
    goal = current['goal']
    answered = goal.get('answered_reference_review')
    if answered and answered['field_reference']['answered_revision'].get('withdraws_question'):
        revision = answered['field_reference']['answered_revision']
        source = next(item for item in goal['guidance_sources'] if item['citation'] == revision['source'])
        append_answered_revision(db, current, goal['notes'])
        goal_question_withdrawals.record(db, goal['id'], current['question_withdrawal'], source)
        db.execute("UPDATE autonomous_goals SET status='paused',question='',notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   ('已核對已答項及正式條件，撤下已不影響結果的欄名問題。請接續交付。', goal['id']))
        return _snapshot(db, goal['session_id'], goal['id'])
    review = goal.get('reference_review')
    if review is None:
        return current
    proof = review['field_reference']
    still_waiting = proof['status'] == 'needs_field'
    remaining = proof['waiting_recheck'].get('remaining_question', '')
    if current['step_count'] + (3 if still_waiting or remaining else 2) > MAX_STEPS:
        raise ValueError('剩餘步數不足以重核欄名與交付，請帶入進度建立下一階段。')
    if still_waiting:
        return current
    append_waiting_recheck(db, current, goal['notes'])
    goal_question_withdrawals.record(db, goal['id'], current['question_withdrawal'], goal['guidance_sources'][-1])
    status, notice = 'paused', '已重核並撤下原欄名問題。請接續更新成果。'
    if remaining:
        append_followup_question(db, goal, goal['notes'], current['step_count'] + 2, remaining, '依重核結果保留原題的獨立補問')
        status, notice = 'needs_input', '已撤下原題的欄名部分，請只回答保留的補問。'
    db.execute('UPDATE autonomous_goals SET status=?,question=?,notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (status, remaining, notice, goal['id']))
    return _snapshot(db, goal['session_id'], goal['id'])


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
        revise_answer = (goal['status'] == 'needs_input'
                         and decision_table.answered_revision_requested(_steps(db, gid), content,
                             goal.get('reply_sources', ()), goal.get('guidance_sources', ())))
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
        saved = _snapshot(db, sid, gid)
        if revise_answer:
            review = saved['goal'].get('answered_reference_review')
            if review is None:
                question = decision_table.answered_revision_question(_steps(db, gid))
                raise ValueError(f'請用一個明確的新值更正已答項「{question}」，並先確認其他待核對的補充。'
                                 '若有兩個可能新值，請先確認其中一個。原待答問題及草稿仍保留。')
            revision = review['field_reference']['answered_revision']
            required = 3 if revision['item'] == 'remaining' and not revision.get('withdraws_question') else 2
            if step_count + required > MAX_STEPS:
                raise ValueError('剩餘步數不足以保存更正及完成成果，請帶入目前進度建立下一階段。')
        return apply_waiting_review(db, saved)
