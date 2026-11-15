"""Bind explicit replies to the goal and question the user actually reviewed."""
import re

from .answer_runs import lease
from .autonomy import MAX_STEPS, text
from . import artifact_names, artifacts, decision_table, delivery_preference, goal_question_withdrawals, goal_reply_sources, work_plan, work_plan_scope
from .goal_guidance import (_append_step, _condition_goal, _snapshot, _steps, append_answered_revision, append_followup_question,
                            append_waiting_field_version, append_waiting_recheck, apply_waiting_review, snapshot)


class StaleReply(ValueError):
    """The reviewed question or its sources changed before saving."""


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
    plan = work_plan.latest(steps)
    if (not steps or steps[-1]['action']['tool'] != 'ask'
            or steps[-1]['action']['args'].keys() - {'question', 'partial_result', 'plan_questions'}
            or decision_table.reference_question_for_steps(steps[:-1]) or plan is None):
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
    if pending_revision and scopes[1] == plan['goal_conditions_sha256']:
        versions = (4,)
    elif any(word in answer for word in ('改', '更正', '提議', '建議')):
        versions = (4, *versions)
    delivery = delivery_preference.plan_reply(db, gid, replies[-1]) if language or 'plan_questions' in steps[-1]['action']['args'] else None
    strict_choice = versions == (4,)
    if delivery:
        versions = (5,)
    requirement = None
    if pending_revision and delivery_preference.initially_enabled(db, gid):
        requirement = artifact_names.requirement({**goal, 'steps': steps, 'reply_sources': replies[:-1]})
        if artifact_names.awaiting_confirmation(steps, requirement):
            versions = (artifact_names.FIELD_CHOICE_REPLY_VERSION, artifact_names.FILENAME_FIRST_REPLY_VERSION, *versions)
        elif requirement:
            versions = (artifact_names.REPLACE_REPLY_VERSION, *versions)
    if not delivery and work_plan_scope.is_supplement_followup(steps):
        versions = (*versions, work_plan_scope.SUPPLEMENT_REPLY_VERSION)
    elif not delivery and 'plan_questions' in steps[-1]['action']['args']:
        versions = (*versions, work_plan_scope.PARTIAL_DOCUMENT_REPLY_VERSION)
    pending_target = work_plan_scope.pending_confirmation_target(steps)
    if pending_target:
        if delivery:
            raise ValueError('請先回答目前待選的更正欄位，交付方式可另從目標補充更正。')
        versions = work_plan_scope.CORRECTION_REPLY_VERSIONS[:-1]
    elif work_plan.waiting_for_reconfirmation(steps):
        if delivery:
            raise ValueError('請先回答待重新確認的欄位，交付方式可另從目標補充更正。')
        versions = (*work_plan_scope.CORRECTION_REPLY_VERSIONS, work_plan_scope.COLLOQUIAL_REPLY_VERSION,
                    work_plan_scope.TEXT_REPLY_VERSION,
                    work_plan_scope.PERSON_REPLY_VERSION, work_plan_scope.COUNT_REPLY_VERSION,
                    work_plan_scope.RECONFIRMATION_REPLY_VERSION)
    elif not delivery and 'plan_questions' in steps[-1]['action']['args']:
        versions = (work_plan_scope.COLLOQUIAL_REPLY_VERSION, work_plan_scope.TEXT_REPLY_VERSION, work_plan_scope.PERSON_REPLY_VERSION,
                    work_plan_scope.COUNT_REPLY_VERSION, work_plan_scope.CARRY_CONFIRMATIONS_REPLY_VERSION,
                    work_plan_scope.COMBINED_EVIDENCE_REPLY_VERSION, *versions)
        if '改' in answer or '更正' in answer:
            versions = (*work_plan_scope.CORRECTION_REPLY_VERSIONS, *versions)
    for version in versions:
        result = work_plan_scope.observe_reply(steps, replies[-1], *scopes, version=version, delivery=delivery,
                                               filename_requirement=requirement)
        if result is not None:
            break
    else:
        if pending_target:
            raise ValueError('請指定目前列出的一個完整欄名及明確新值，或回答「都不改」。')
        if strict_choice:
            raise ValueError('待選更正仍未確認。請明確選擇目前列出的值或沿用原值。')
        return False
    proof = result['scope_reply']
    successor = proof['successor']
    choice = proof.get('filename_choice', {}).get('selection', {}).get('filename')
    pending = proof.get('language', {}).get('pending_revisions', ())
    minimum = max(3 if successor else 2, 2 + 2 * len(pending))
    file_delivery = delivery['receipt']['file_delivery'] if delivery else delivery_preference.enabled(db, gid)
    if version in work_plan_scope.CORRECTION_REPLY_VERSIONS:
        minimum += 1 + int(file_delivery) + int('confirmation_target_choice' in proof)
    if (choice or delivery) and file_delivery:
        minimum += 1
    if choice and any(revision['recheck_items'] for revision in proof.get('revisions', ())):
        minimum += 2
    if choice and any(row['name'].casefold() == choice.casefold() for row in artifacts.inventory(db, gid)):
        raise ValueError('請提供同格式且尚未使用的定稿檔名。')
    if not current['sources_match'] or count + minimum > MAX_STEPS:
        raise ValueError('來源已改變或剩餘步數不足，請帶入目前進度建立下一階段。')
    args = {'scope_reply': proof['citation']}
    if version != 1:
        args['scope_reply_version'] = version
    action = {'tool': 'update_plan', 'args': args, 'note': '依使用者實際回覆更新工作清單'}
    if delivery:
        delivery_preference.record_plan_reply(db, gid, replies[-1], delivery)
    _append_step(db, goal, notes, count + 1, action, result)
    if successor:
        append_followup_question(db, {**goal, 'reply_sources': replies}, notes, count + 2, successor['question'],
                                 '保留目前仍需回答的工作分問', plan_questions=successor['plan_questions'])
    notice = '已更新工作清單。只需回答目前保留的問題。' if successor else '已保存答覆並更新工作清單，可接續工作。'
    if proof.get('confirmation_target_choice'):
        notice = '已保存可核對的答覆。請指定要更正的欄位。'
    if choice:
        if not file_delivery:
            named = f'若之後恢復文件交付，定稿檔名為「{choice}」。'
        elif 'filename_revision' in proof:
            named = f'已將定稿檔名改為「{choice}」。'
        else:
            named = f'已確認定稿檔名「{choice}」。'
        notice = named + notice
    if delivery and delivery['changed']:
        notice = delivery_preference.describe(delivery['receipt']) + notice
    db.execute('UPDATE autonomous_goals SET notes=?,status=?,question=?,notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (notes, 'needs_input' if successor else 'paused', successor['question'] if successor else '', notice, gid))
    return True


def _save_filename_reply(db, current, steps, notes):
    goal = current['goal']
    gid = goal['id']
    count = current['step_count']
    if not current['sources_match'] or not delivery_preference.enabled(db, gid):
        raise ValueError('來源、交付方式或可用步數已變更，請重新核對目前工作。')
    replies = goal_reply_sources.read(db, gid, notes, include_positions=True)
    source = replies[-1]
    delivery = delivery_preference.plan_reply(db, gid, source)
    before = _condition_goal(db, goal, goal['notes'])
    scope = work_plan.conditions({**before, 'notes': notes})
    previous = work_plan.conditions(before)
    version = work_plan_scope.CONFIRMATION_FILENAME_REPLY_VERSION
    observation = work_plan_scope.observe_reply(steps, source, scope, previous, version=version, delivery=delivery)
    if observation is None:
        version = artifact_names.MIXED_REPLY_VERSION
        observation = work_plan_scope.observe_reply(steps, source, scope, previous, version=version, delivery=delivery)
    if observation is None and not delivery:
        version = artifact_names.DEFERRED_REPLY_VERSION
        observation = work_plan_scope.observe_reply(steps, source, scope, previous, version=version)
    if observation is None:
        raise ValueError('請提供同格式且尚未使用的定稿檔名，或明確改用文字回覆。欄位更正請寫出完整欄名。')
    proof = observation['scope_reply']
    choice = proof.get('filename_choice', {}).get('selection', {}).get('filename')
    if choice and any(row['name'].casefold() == choice.casefold() for row in artifacts.inventory(db, gid)):
        raise ValueError('請提供同格式且尚未使用的定稿檔名。')
    successor = proof['successor']
    deferred = 'filename_pending' in proof
    pending = proof.get('language', {}).get('pending_revisions', ())
    file_delivery = not delivery or delivery['receipt']['file_delivery']
    minimum = max(3 if successor else 2, 2 + 2 * len(pending)) + int(file_delivery)
    if deferred:
        minimum += 2  # Refresh the filename question and save the eventual answer.
        if any(item['state'] not in {'done', 'superseded'} for item in observation['items']):
            minimum += 2  # Reserve a tool observation and a refreshed plan before asking.
    if count + minimum > MAX_STEPS:
        raise ValueError('剩餘步數不足，請帶入目前進度建立下一階段。')
    if delivery:
        delivery_preference.record_plan_reply(db, gid, source, delivery)
    args = {'scope_reply': source['citation'], 'scope_reply_version': version}
    _append_step(db, goal, notes, count + 1, {'tool': 'update_plan', 'args': args, 'note': '保存定稿提問的實際答覆'}, observation)
    notice = ''
    if choice:
        notice = (f'已確認定稿檔名「{choice}」。' if file_delivery
                  else f'若之後恢復文件交付，定稿檔名為「{choice}」。')
    if delivery and delivery['changed']:
        notice = delivery_preference.describe(delivery['receipt']) + notice
    question = ''
    if successor:
        question = successor['question']
        append_followup_question(db, {**goal, 'reply_sources': replies}, notes, count + 2, question,
                                 '保留仍需選擇的欄位更正', plan_questions=successor['plan_questions'])
        notice += '請先回答目前的欄位選擇，定稿檔名稍後確認。' if deferred else '請回答目前的欄位選擇。'
    elif artifact_names.needs_refreshed_question(observation):
        context = {**goal, 'notes': notes, 'steps': _steps(db, gid), 'reply_sources': replies,
                   'artifacts': artifacts.inventory(db, gid), 'file_delivery': True}
        refreshed = artifact_names.question(context, scope, artifact_names.requirement(context))
        if not refreshed or artifact_names.question_text(refreshed) != source['question']:
            raise ValueError('舊草稿或檔名已變更，請重新核對定稿問題。')
        action, receipt = artifact_names.question_step(refreshed, scope)
        _append_step(db, goal, notes, count + 2, action, receipt)
        question = receipt['question']
        notice = '已保存欄位更正。請在決定後確認定稿檔名。'
    elif deferred:
        notice = '已保存欄位更正。接續重核受影響的工作後，再確認定稿檔名。'
    else:
        notice += '可以接續工作。'
    db.execute('UPDATE autonomous_goals SET notes=?,status=?,question=?,notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (notes, 'needs_input' if question else 'paused', question, notice, gid))


def save(store, sid, gid, answer, *, expected_fingerprint):
    answer = text(answer, 4000)
    if not isinstance(expected_fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', expected_fingerprint):
        raise ValueError('請先讀取目前問題與目標指紋，再保存回答。')
    with lease(store, gid), store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = _snapshot(db, sid, gid, include_reply_positions=True)
        if current['fingerprint'] != expected_fingerprint:
            raise StaleReply('問題、目標進度或來源已變更；回答尚未保存。請重新載入並核對，草稿仍保留。')
        goal = current['goal']
        if goal['status'] != 'needs_input' or not goal['question']:
            raise ValueError('此目標目前沒有等待補充的問題。')
        steps = _steps(db, gid)
        pending_target = work_plan_scope.pending_confirmation_target(steps)
        colloquial = current.get('report_colloquial_question')
        combined = decision_table.combined_reply_requested(steps, answer, goal.get('reply_sources', ()),
                                                           goal.get('guidance_sources', ()))
        if (combined or colloquial) and (not current['sources_match'] or current['step_count'] + 2 > MAX_STEPS):
            raise ValueError('來源已改變或剩餘步數不足，請帶入目前進度建立下一階段。')
        notes = goal['notes'] + goal_reply_sources.note(goal['question'], answer)
        if len(notes) > 12000:
            raise ValueError('補充內容已達上限，請建立下一階段目標。')
        goal_reply_sources.record(db, gid, goal['question'], answer, len(goal['notes']))
        if steps and steps[-1]['observation'].get(artifact_names.ASK_MARK):
            _save_filename_reply(db, current, steps, notes)
            return _snapshot(db, sid, gid)
        if not (colloquial or combined) and _save_scope_reply(db, current, steps, notes):
            return _snapshot(db, sid, gid)
        if pending_target:
            raise ValueError('請指定目前列出的一個完整欄名及明確新值，或回答「都不改」。')
        if steps and work_plan.waiting_for_reconfirmation(steps):
            raise ValueError('撤回的欄位仍待確認。請對目前分問提供明確的值。')
        delivery_change = delivery_preference.capture(db, gid, notes, current['step_count'], reply=True)
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
        goal_question_withdrawals.dismiss(db, gid, options, source)
        return _snapshot(db, sid, gid)
