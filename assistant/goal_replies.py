"""Bind explicit replies to the goal and question the user actually reviewed."""
import re

from .answer_runs import lease
from .autonomy import MAX_STEPS, text
from . import decision_table, delivery_preference, goal_question_withdrawals, goal_reply_sources, work_plan, work_plan_scope
from .goal_guidance import (_append_step, _condition_goal, _snapshot, _steps, append_answered_revision, append_followup_question,
                            append_waiting_field_version, append_waiting_recheck, apply_waiting_review, snapshot)


def _save_partial_followup(db, current, notes, kind, review):
    goal, count = current['goal'], current['step_count']
    gid = goal['id']
    rechecked = review is not None and review['field_reference']['status'] == 'needs_field'
    if not current['sources_match'] or count + (3 if kind == 'field' else 4) + int(rechecked) > MAX_STEPS:
        raise ValueError('來源已改變或剩餘步數不足，請帶入目前進度建立下一階段。')
    if rechecked:
        append_waiting_recheck(db, current, notes)
    steps = _steps(db, gid)
    replies = goal_reply_sources.read(db, gid, notes, include_positions=True)
    args = {'field_reference': replies[-1]['citation'], 'partial_reply': True}
    result = decision_table.observe(args, steps, replies, goal.get('guidance_sources', ()))
    action = {'tool': 'check_decision', 'args': args, 'note': '保存原分項提問已回答的項目'}
    _append_step(db, goal, notes, len(steps) + 1, action, result)
    remaining = result['field_reference']['partial_reply']['next_question']
    append_followup_question(db, {**goal, 'reply_sources': replies}, notes, len(steps) + 2,
                             remaining, '保留原分項提問尚未回答的項目')
    db.execute("UPDATE autonomous_goals SET notes=?,status='needs_input',question=?,notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (notes, remaining, '已保存這一項。請回答剩下的問題。', gid))


def _save_scope_reply(db, current, steps, notes):
    if (not steps or steps[-1]['action']['tool'] != 'ask'
            or steps[-1]['action']['args'].keys() - {'question', 'partial_result', 'plan_questions'}
            or decision_table.reference_question_for_steps(steps[:-1]) or work_plan.latest(steps) is None):
        return False
    goal, count = current['goal'], current['step_count']
    gid = goal['id']
    replies = goal_reply_sources.read(db, gid, notes, include_positions=True)
    scope_goal = _condition_goal(db, goal, goal['notes'])
    scopes = work_plan.conditions({**scope_goal, 'notes': notes}), work_plan.conditions(scope_goal)
    language = work_plan_scope.language_followup(steps) or {}
    pending_revision = bool(language.get('pending_revisions'))
    versions = (2,) if language else (1, 2)
    answer = replies[-1]['answer']
    if '改' in answer or '更正' in answer:
        versions = (3, *versions)
    if pending_revision and scopes[1] == work_plan.latest(steps)['goal_conditions_sha256']:
        versions = (4,)
    elif any(word in answer for word in ('改', '更正', '提議', '建議')):
        versions = (4, *versions)
    for version in versions:
        result = work_plan_scope.observe_reply(steps, replies[-1], *scopes, version=version)
        if result is not None:
            break
    else:
        if versions == (4,):
            raise ValueError('待選更正仍未確認。請明確選擇目前列出的值或沿用原值。')
        return False
    proof = result['scope_reply']
    successor = proof['successor']
    pending = proof.get('language', {}).get('pending_revisions', ())
    minimum = max(3 if successor else 2, 2 + 2 * len(pending))
    if not current['sources_match'] or count + minimum > MAX_STEPS:
        raise ValueError('來源已改變或剩餘步數不足，請帶入目前進度建立下一階段。')
    args = {'scope_reply': proof['citation']}
    if version != 1:
        args['scope_reply_version'] = version
    action = {'tool': 'update_plan', 'args': args, 'note': '依使用者實際回覆更新工作清單'}
    _append_step(db, goal, notes, count + 1, action, result)
    if successor:
        append_followup_question(db, {**goal, 'reply_sources': replies}, notes, count + 2, successor['question'],
                                 '保留目前仍需回答的工作分問', plan_questions=successor['plan_questions'])
    notice = '已更新工作清單。只需回答目前保留的問題。' if successor else '已保存答覆並更新工作清單，可接續工作。'
    db.execute('UPDATE autonomous_goals SET notes=?,status=?,question=?,notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (notes, 'needs_input' if successor else 'paused', successor['question'] if successor else '', notice, gid))
    return True


def save(store, sid, gid, answer, *, expected_fingerprint):
    answer = text(answer, 4000)
    if not isinstance(expected_fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', expected_fingerprint):
        raise ValueError('請先讀取目前問題與目標指紋，再保存回答。')
    with lease(store, gid), store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = _snapshot(db, sid, gid, include_reply_positions=True)
        if current['fingerprint'] != expected_fingerprint:
            raise ValueError('問題、目標進度或來源已變更；回答尚未保存。請重新載入並核對，草稿仍保留。')
        goal = current['goal']
        if goal['status'] != 'needs_input' or not goal['question']:
            raise ValueError('此目標目前沒有等待補充的問題。')
        steps = _steps(db, gid)
        colloquial = current.get('report_colloquial_question')
        combined = decision_table.combined_reply_requested(steps, answer, goal.get('reply_sources', ()),
                                                           goal.get('guidance_sources', ()))
        if (combined or colloquial) and (not current['sources_match'] or current['step_count'] + 2 > MAX_STEPS):
            raise ValueError('來源已改變或剩餘步數不足，請帶入目前進度建立下一階段。')
        notes = goal['notes'] + goal_reply_sources.note(goal['question'], answer)
        if len(notes) > 12000:
            raise ValueError('補充內容已達上限，請建立下一階段目標。')
        goal_reply_sources.record(db, gid, goal['question'], answer, len(goal['notes']))
        delivery_change = delivery_preference.capture(db, gid, notes, current['step_count'], reply=True)
        if not (colloquial or combined or delivery_change) and _save_scope_reply(db, current, steps, notes):
            return _snapshot(db, sid, gid)
        if colloquial:
            replies = goal_reply_sources.read(db, gid, notes, include_positions=True)
            args = {'report_colloquial_guidance': colloquial['source'], 'reply': replies[-1]['citation']}
            observation = decision_table.observe(args, steps, replies, goal.get('guidance_sources', ()))
            ordered = observation['field_reference']['report_mixed_guidance'].get('ordered')
            needs_followup = ordered is not None and ordered['next'] is not None
            required = 6 if needs_followup else 4 if observation.get('max_questions', 0) else 2
            if current['step_count'] + required > MAX_STEPS:
                raise ValueError('剩餘步數不足以重算並交付，請帶入目前進度建立下一階段。')
            action = {'tool': 'check_decision', 'args': args, 'note': '依確切答覆重算同一則口語補充'}
            _append_step(db, goal, notes, current['step_count'] + 1, action, observation)
            if needs_followup:
                sources = goal.get('guidance_sources', ())
                ask, partial, receipt = decision_table.prepare_report_colloquial_question(
                    _steps(db, gid), replies, sources, colloquial['source'])
                append_followup_question(db, {**goal, 'reply_sources': replies}, notes, current['step_count'] + 2,
                                         ask, '只保留仍影響交付的口語釐清', partial_result=partial,
                                         colloquial_receipt=receipt)
                db.execute("UPDATE autonomous_goals SET notes=?,status='needs_input',question=?,notice=?,"
                           "updated_at=CURRENT_TIMESTAMP WHERE id=? AND session_id=?",
                           (notes, ask, '已保存答覆。這一項仍會影響成果。', gid, sid))
                return _snapshot(db, sid, gid)
        elif combined:
            replies = goal_reply_sources.read(db, gid, notes, include_positions=True)
            args = {'field_reference': replies[-1]['citation'], 'combined_reply': True}
            observation = decision_table.observe(args, steps, replies, goal.get('guidance_sources', ()))
            action = {'tool': 'check_decision', 'args': args, 'note': '分別保存同則已答項更正、剩餘答覆及正式欄位更正'}
            _append_step(db, goal, notes, current['step_count'] + 1, action, observation)
        review = goal.get('reference_review')
        if current.get('reference_partial_reply') and (kind := decision_table.partial_reply_kind(_steps(db, gid), answer)):
            _save_partial_followup(db, current, notes, kind, review)
            return _snapshot(db, sid, gid)
        if not combined and 'answered_reference_review' in goal:
            append_answered_revision(db, current, notes)
        elif not combined and review and 'waiting_field_version' in review['field_reference']:
            if review['field_reference']['waiting_field_version']['withdraws_question']:
                raise ValueError('正式版本更正後欄名已無須回答，請先撤下此問題。')
            append_waiting_field_version(db, current, notes)
        elif (not combined and review and 'waiting_recheck' in review['field_reference']
              and review['field_reference']['status'] == 'needs_field' and current['step_count'] + 3 <= MAX_STEPS):
            append_waiting_recheck(db, current, notes)
        notice = delivery_preference.describe(delivery_change) if delivery_change else ''
        db.execute("UPDATE autonomous_goals SET notes=?,question='',status='paused',notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND session_id=?",
                   (notes, notice, gid, sid))
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
        adoption = review['field_reference'].get('waiting_field_version') if review else None
        if adoption and adoption['withdraws_question'] and source['citation'] == adoption['source']:
            return apply_waiting_review(db, current)
        version = review['field_reference'].get('waiting_version_recheck') if review else None
        if (version and version['withdraws_question']
                and source['citation'] == version['changes'][-1]['evidence']['citation']):
            return apply_waiting_review(db, current)
        if (review and 'waiting_recheck' in review['field_reference']
                and review['field_reference']['status'] != 'needs_field' and current['step_count'] + 2 <= MAX_STEPS
                and source['citation'] == review['field_reference']['recheck']['evidence']['citation']):
            return apply_waiting_review(db, current)
        goal_question_withdrawals.record(db, gid, options, source)
        db.execute("UPDATE autonomous_goals SET question='',status='paused',notice=?,updated_at=CURRENT_TIMESTAMP "
                   "WHERE id=? AND session_id=?", ('已依所選更正撤下問題，可開始／接續。', gid, sid))
        return _snapshot(db, sid, gid)
