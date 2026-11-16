"""Explicit append-only user steering of an existing, stopped goal."""
from datetime import datetime, timezone
import hashlib
import json
import re

from .answer_runs import lease
from .autonomy import MAX_STEPS, signature, text
from .documents import check_citations
from . import (artifact_names, artifacts, conversation_history, decision_table, delivery_preference, document_ranges, document_rule_update, goal_guidance_sources,
               goal_partials, goal_partial_recheck, goal_question_withdrawals, goal_reply_sources,
               goal_required_supplement, goal_review, goal_sources, work_plan,
               work_plan_scope)


class StaleGuidance(ValueError):
    """The reviewed goal or its sources changed before saving guidance."""


def _steps(db, gid):
    return [{'sequence': number, 'action': json.loads(action), 'observation': json.loads(observation)}
            for number, action, observation in db.execute(
                'SELECT sequence,action_json,observation_json FROM autonomous_steps WHERE goal_id=? ORDER BY sequence', (gid,))]


def _document(db, session_id, document_id):
    row = db.execute('SELECT id,name,sha256,content FROM documents WHERE session_id=? AND id=?',
                     (session_id, document_id)).fetchone()
    return dict(row) if row is not None else None


def _ordinary_missing_target(db, goal, steps, sources):
    if steps[-1]['observation'].get('question') != goal['question']:
        return []
    scope = json.loads(goal['sources_json'])
    current = {**goal, 'sources': scope, 'steps': steps, 'reply_sources': sources}
    target = goal_required_supplement.target(current, steps[-1])
    if not target:
        return []
    did = target[0][0]
    row = _document(db, goal['session_id'], did)
    frozen = next((source for source in scope if source['document_id'] == did), None)
    count = goal_required_supplement.line_count({did: row}, frozen) if frozen else None
    return target if count is not None and all(start > count for _, start, _ in target) else []


def _snapshot(db, sid, gid, *, include_reply_positions=False, include_work_plan=False):
    goal = db.execute('SELECT * FROM autonomous_goals WHERE id=? AND session_id=?',(gid,sid)).fetchone()
    if goal is None:
        raise ValueError('找不到此對話的自主目標。')
    review = next(goal_review._rows(db,gid))
    goal = dict(goal)
    fingerprint = review['fingerprint']
    if delivery_change := delivery_preference.latest(db, gid):
        goal['delivery_mode_change'] = delivery_change
    sources = goal_reply_sources.read(db, gid, goal['notes'], include_positions=True)
    if sources:
        goal['reply_sources'] = sources if include_reply_positions else goal_reply_sources.without_positions(sources)
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
    colloquial_question = None
    if goal['status'] == 'needs_input':
        steps = _steps(db, gid)
        filename_status = artifact_names.describe({**goal, 'steps': steps, 'reply_sources': sources,
                                                   'file_delivery': review['file_delivery']})
        if filename_status:
            goal['artifact_filename_status'] = filename_status
        if steps:
            colloquial_question = steps[-1]['observation'].get('report_colloquial_question')
            if colloquial_question:
                receipt = json.dumps(steps[-1], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                fingerprint = hashlib.sha256((fingerprint + '\0' + receipt).encode('utf-8')).hexdigest()
        details = goal_partial_recheck.waiting(db, goal, steps, sources, guidance,
                                               sources_match=review['sources_match'])
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
    if include_work_plan and review['step_count']:
        plan_goal = _condition_goal(db, goal, goal['notes'])
        plan_goal['steps'] = steps if waiting_step is not None else _steps(db, gid)
        current_sources = plan_goal['sources'] if review['sources_match'] else None
        if plan := work_plan.for_goal(plan_goal, current_sources):
            result['work_plan'] = plan
    if options:
        result['question_withdrawal'] = options
    if colloquial_question:
        result['report_colloquial_question'] = colloquial_question
    if goal['status'] == 'needs_input' and decision_table.compound_reference_ask(steps):
        result['reference_partial_reply'] = True
    if (goal['status'] == 'needs_input' and steps and not colloquial_question
            and waiting_step + 3 + review['file_delivery'] <= MAX_STEPS):
        plan_supplement = work_plan.can_supplement_question(steps)
        ordinary_target = [] if plan_supplement else _ordinary_missing_target(db, goal, steps, sources)
        original = json.loads(goal['sources_json'])
        correction_target = []
        if not (plan_supplement or ordinary_target) and steps[-1]['observation'].get('question') == goal['question']:
            scope_goal = {**goal, 'sources': original, 'steps': steps, 'reply_sources': sources}
            correction_target = goal_required_supplement.correction_targets(scope_goal, steps[-1])
        if plan_supplement or ordinary_target or correction_target:
            files = signature(db.execute('SELECT id,name,sha256 FROM documents WHERE session_id=? ORDER BY id', (sid,)))
            if goal_sources.matching_scope(original, files, scoped=True) and (
                    correction_target or any(row not in original for row in files)):
                if plan_supplement:
                    result['plan_document_supplement'] = True
                elif correction_target:
                    result['required_supplement_correction'] = True
                else:
                    result['required_document_supplement'] = True
                result['supplement_documents'] = files
    if goal['status'] == 'report_ready' and not review['file_delivery']:
        steps = _steps(db, gid)
        reference = decision_table.reference_report(steps, goal['result'])
        version = decision_table.document_version_report(steps, goal['result'])
        supplement = decision_table.document_supplement_report(steps, goal['result'])
        conflict = decision_table.document_conflict_report(steps, goal['result'])
        if reference or version or supplement or conflict:
            decision_table._recheck_saved(steps, sources, guidance)
            if reference:
                result['reference_report'] = reference['observation']['field_reference']
            if version:
                result['document_version_report'] = version['observation']['field_reference']
            if supplement:
                _, selection = supplement
                result['document_supplement'] = {key: selection[key] for key in ('scope', 'version')}
                result['supplement_documents'] = signature(db.execute(
                    'SELECT id,name,sha256 FROM documents WHERE session_id=? ORDER BY id', (sid,)).fetchall())
                result['supplement_sources_match'] = goal_sources.matching_scope(
                    json.loads(goal['sources_json']), result['supplement_documents'], scoped=True)
            if conflict:
                result['document_conflict_report'] = conflict[1]
            checked = reference or version or (supplement or conflict)[0]
            receipt = json.dumps(checked, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            result['fingerprint'] = hashlib.sha256((fingerprint + '\0' + receipt).encode('utf-8')).hexdigest()
    if review['sources_match'] and review['step_count'] and (include_work_plan or goal['status'] == 'report_ready'):
        plan_scope = work_plan.conditions(_condition_goal(db, goal, goal['notes']))
        if goal['status'] != 'needs_input' and not (goal['status'] == 'report_ready' and not review['file_delivery']):
            steps = _steps(db, gid)
        confirmations = work_plan_scope.confirmation_targets(steps, plan_scope)
        if confirmed := [question for question, target in confirmations.items() if target['confirmed']]:
            result['confirmed_plan_questions'] = confirmed
            result['plan_correction_file_delivery'] = review['file_delivery']
    return result


def snapshot(store, sid, gid, *, include_work_plan=False):
    with store.connect() as db:
        db.execute('BEGIN')
        return _snapshot(db, sid, gid, include_work_plan=include_work_plan)


def eligibility(value):
    status = value['goal']['status']
    if value.get('plan_document_supplement'):
        return True, ('可用「補件 [D2:L2] 改由 [D3:L1]」。有多個分問時，下一行寫「請撤下分問『原句』」，'
                      '請以「」括住完整原句。只有一個問題時可寫「請撤下目前問題。」')
    if value.get('required_document_supplement'):
        return True, '可用「補件 [D2:L2] 改由 [D3:L1]」，下一行寫「請撤下目前問題。」'
    if value.get('required_supplement_correction'):
        return True, '可用「補件 [原缺行] 改由 [新補件行號]」，下一行寫「請撤下目前問題。」'
    if question := value.get('report_colloquial_question'):
        if question.get('ordered', {}).get('answers'):
            if not value['sources_match']:
                return False, '參考文件已改變，請以目前文件另建目標。'
            if value['step_count'] + 2 > MAX_STEPS:
                return False, '剩餘步數不足以核對正式更正及更新成果，請帶入進度建立下一階段。'
            return True, '可更正原表的完整欄名，例如「核對班別正式改為晚班」。保存後會重核目前的問題。'
        return False, '請在目前問題保存答覆，或回答「取消這則補充」。'
    conflict = value.get('document_conflict_report', {})
    choice_hint = ''
    if options := conflict.get('options'):
        choice_hint = f'可指定正式採用的一筆原行，例如 正式採用 {options[0]["citation"]}。'
    if amendments := conflict.get('amendments'):
        active = amendments[0]['citation']
        alternate = next((option['citation'] for option in amendments[0]['options'] if option['citation'] != active), None)
        if alternate:
            choice_hint += f'已定案的配對可用 改採 {alternate}。'
        choice_hint += f'要撤回目前選擇，可用 撤回正式採用 {active}。'
    if status == 'report_ready' and 'document_supplement' in value:
        if not value['supplement_sources_match']:
            return False, '原目標已有文件版本變更或移除，請先恢復原匯入版本。'
        if value['step_count'] + 2 > MAX_STEPS:
            return False, '剩餘步數不足以核對補件及更新成果，請帶入原成果建立下一階段。'
        scope = value['document_supplement']
        return True, (f'可補齊「{scope["scope"]}」的「{scope["version"]}」版規則。先匯入文件，再指定完整原行，'
                      f'例如 補件 [D5:L1-L2]。{choice_hint}')
    if status == 'report_ready' and value.get('confirmed_plan_questions'):
        if value['step_count'] + 3 + int(value['plan_correction_file_delivery']) > MAX_STEPS:
            return False, '剩餘步數不足以重核工作清單及更新成果，請帶入原成果建立下一階段。'
        question = value['confirmed_plan_questions'][0]
        return True, (f'可用完整欄名更正，例如「集合點移到未央館」。也可用 更正「{question}」為「新值」，'
                      f'或 撤回「{question}」的答覆。先前成果保留於步驟紀錄。')
    reference_report = status == 'report_ready' and bool(
        value.keys() & {'reference_report', 'document_version_report', 'document_conflict_report'})
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
        if 'document_version_report' in value:
            scope = value['document_version_report']['document_rules']['records'][0]['scope']
            example = f'本輪「{scope}」正式採用版本「版號」。'
            field = '也可用完整欄名更正一欄。' if 'reference_report' in value else ''
            return True, f'{choice_hint}可保存本批正式版本更正，例如 {example}{field}保存後保留先前成果並等待你接續。'
        if conflict:
            return True, choice_hint
        return True, '可用完整欄名更正一欄。保存時會按原話先後重核各分支，保留先前成果，等待你接續。'
    if value.get('confirmed_plan_questions'):
        question = value['confirmed_plan_questions'][0]
        return True, f'可用 更正「{question}」為「新值」，或 撤回「{question}」的答覆。'
    return True, '保存後不會自動執行；回到自主目標按開始／接續。'


def _condition_goal(db, goal, notes):
    history = conversation_history.saved_fingerprint(db, goal['id'])
    scope = {**goal, 'notes': notes, 'sources': json.loads(goal['sources_json'])}
    if history is not None:
        scope['conversation_history'] = {'sha256': history}
    return scope


def _append_step(db, goal, notes, sequence, action, observation):
    observation = {**observation, 'goal_conditions_sha256': work_plan.conditions(_condition_goal(db, goal, notes))}
    db.execute('INSERT INTO autonomous_steps(goal_id,sequence,action_json,observation_json) VALUES(?,?,?,?)',
               (goal['id'], sequence, json.dumps(action, ensure_ascii=False), json.dumps(observation, ensure_ascii=False)))


def _append_reference_recheck(db, goal, notes, sequence, args, observation):
    action = {'tool': 'check_decision', 'args': args, 'note': '依使用者正式更正重新核對原候選分支'}
    _append_step(db, goal, notes, sequence, action, observation)


def _save_reference_recheck(db, current, notes):
    goal, step_count = current['goal'], current['step_count']
    gid = goal['id']
    steps = _steps(db, gid)
    sources = goal_guidance_sources.read(db, gid, notes, step_count)
    latest = sources[-1]
    content = latest['content']
    choice = document_rule_update.conflict_choice(content)
    amendment_mentioned = '[D' in content and re.search(r'改採|改為正式採用|撤回(?:正式)?採用', content)
    regular = current.keys() & {'reference_report', 'document_version_report'}
    conflict = 'document_conflict_report' in current
    if conflict and not choice and decision_table.report_guidance_has_bundle(content):
        args = {'report_mixed_guidance': latest['citation']}
    elif conflict and (choice or amendment_mentioned or not regular):
        args = {'report_conflict_choice': latest['citation']}
    elif 'document_version_report' in current and (
            'reference_report' not in current or document_rule_update.is_adoption_notice(latest['content'])):
        args = {'report_version_recheck': latest['citation']}
    else:
        args = {'field_reference': latest['citation'], 'recheck': True}
    replies = goal.get('reply_sources', ())
    try:
        observation = decision_table.observe(args, steps, replies, sources)
    except ValueError as original:
        if not conflict:
            raise
        try:
            question = decision_table.prepare_report_colloquial_question(steps, replies, sources, latest['citation'])
        except decision_table.ReportColloquialUnrecognized:
            raise original
        if question:
            ask, partial, receipt = question
            if step_count + (7 if 'ordered' in receipt else 3) > MAX_STEPS:
                raise ValueError('剩餘步數不足以釐清並更新成果，請帶入原成果建立下一階段。')
            append_followup_question(db, {**goal, 'guidance_sources': sources}, notes, step_count + 1,
                                     ask, '釐清同一則口語補充中未確認的事項',
                                     partial_result=partial, colloquial_receipt=receipt)
            db.execute("UPDATE autonomous_goals SET notes=?,status='needs_input',question=?,result='',notice=?,"
                       "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (notes, ask, ('已保存補充。先確認這一項，再判斷是否還需要另一項。' if 'ordered' in receipt
                                     else '已保存補充。只需要確認這一項，再一起重算。'), gid))
            return
        args = {'report_colloquial_guidance': latest['citation']}
        observation = decision_table.observe(args, steps, replies, sources)
    needs_field = (observation.get('max_questions', 0) > 0 if args.keys() & {
        'report_version_recheck', 'report_conflict_choice', 'report_mixed_guidance', 'report_colloquial_guidance'}
                   else observation['field_reference']['status'] == 'needs_field')
    if step_count + (4 if needs_field else 2) > MAX_STEPS:
        raise ValueError('剩餘步數不足以重核及完成新成果，請帶入原成果建立下一階段。')
    _append_reference_recheck(db, goal, notes, step_count + 1, args, observation)
    notice = '已重核原分支'
    if 'report_conflict_choice' in args:
        notice = '已撤回原行的正式採用' if choice.get('operation') == 'withdraw' else '已核對正式採用的原行'
    if args.keys() & {'report_mixed_guidance', 'report_colloquial_guidance'}:
        notice = '已核對這則補充中的正式更正'
    db.execute("UPDATE autonomous_goals SET notes=?,status='paused',result='',notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (notes, notice + '；先前成果保留在步驟紀錄，請接續更新成果。', gid))


def _save_report_supplement(db, current, notes, content):
    goal, count = current['goal'], current['step_count']
    gid = goal['id']
    if 'document_supplement' not in current:
        raise ValueError('此目標目前沒有已交付且缺所選版本規則的報告。')
    ranges = goal_sources.supplement_ranges(content)
    original = json.loads(goal['sources_json'])
    wanted = {row['document_id'] for row in ranges}
    placeholders = ','.join('?' for _ in wanted)
    documents = [dict(row) for row in db.execute(
        f'SELECT id,name,sha256,content FROM documents WHERE session_id=? AND id IN ({placeholders}) ORDER BY id',
        (goal['session_id'], *wanted))]
    read = document_ranges.read_batch(documents, {'ranges': ranges})
    if any(row['status'] != 'provided' for row in read['requested_ranges']):
        raise ValueError('無法完整查讀指定補件，請核對目前對話的文件與行號。')
    existing = {row['document_id'] for row in original}
    added = [source for source in signature(documents) if source['document_id'] not in existing]
    merged = goal_sources.merge_sources(original, added)
    guidance = goal_guidance_sources.read(db, gid, notes, count)
    args = {'report_supplement': guidance[-1]['citation'], 'original_sources': original, 'added_sources': added}
    observation = decision_table.observe(args, _steps(db, gid), goal.get('reply_sources', ()), guidance,
                                         excerpts=read['excerpts'])
    if count + (4 if observation.get('max_questions', 0) else 2) > MAX_STEPS:
        raise ValueError('剩餘步數不足以核對補件及更新成果，請帶入原成果建立下一階段。')
    sources_json = json.dumps(merged, ensure_ascii=False)
    _append_reference_recheck(db, {**goal, 'sources_json': sources_json}, notes, count + 1, args, observation)
    db.execute("""UPDATE autonomous_goals SET sources_json=?,notes=?,status='paused',result='',notice=?,
                  updated_at=CURRENT_TIMESTAMP WHERE id=?""",
               (sources_json, notes, '已核對指定補件，先前成果保留在步驟紀錄，請接續更新成果。', gid))


def _save_plan_document_supplement(db, current, notes, content):
    goal, count = current['goal'], current['step_count']
    gid = goal['id']
    missing, replacement, _ = goal_sources.plan_mapping(content)
    original = json.loads(goal['sources_json'])
    did = replacement['document_id']
    documents = [dict(row) for row in db.execute(
        'SELECT id,name,sha256,content FROM documents WHERE session_id=? AND id=?', (goal['session_id'], did))]
    added = signature(documents)
    if not added:
        raise ValueError('請指定目前對話中已匯入的新補件副本。')
    merged = goal_sources.merge_sources(original, added)
    original_doc = db.execute('SELECT content FROM documents WHERE session_id=? AND id=?',
                              (goal['session_id'], missing['document_id'])).fetchone()
    if original_doc is None or missing['start_line'] <= len(original_doc[0].splitlines()):
        raise ValueError('原指定範圍含有可讀原行，請保留它們。')
    steps = _steps(db, gid)
    guidance = goal_guidance_sources.read(db, gid, notes, count)
    source = guidance[-1]
    read = document_ranges.read_batch(documents, {'ranges': [replacement]})
    prior_scope = work_plan.conditions(_condition_goal(db, goal, goal['notes']))
    observation = work_plan.supplement_receipt(steps, original, source, read, retain_replies=True, prior_scope=prior_scope)
    observation['evidence_requirements'] = work_plan.supplemented_requirements(steps, observation, missing, replacement)
    args = {'guidance': source['citation'], 'original_sources': original, 'added_sources': added,
            'missing': missing, 'replacement': replacement, 'check_question_ranges': True, 'retain_replies': True,
            'carry_confirmations': True}
    action = {'tool': goal_sources.PLAN_SUPPLEMENT, 'args': args, 'note': '依使用者指定的原文補件'}
    sources_json = json.dumps(merged, ensure_ascii=False)
    merged_goal = {**goal, 'sources_json': sources_json, 'guidance_sources': guidance}
    _append_step(db, merged_goal, notes, count + 1, action, observation)
    options = goal_question_withdrawals.options(db, gid, goal['question'], count, guidance)
    goal_question_withdrawals.record(db, gid, options, source)
    question, status = '', 'paused'
    notice = '已保存指定補件與原提問，請接續核對及交付。'
    if successor := observation.get('successor'):
        if count + 4 + int(artifacts.enabled(db, gid)) > MAX_STEPS:
            raise ValueError('剩餘步數不足以保存補件、保留分問及交付，請建立下一階段。')
        question, status = successor['question'], 'needs_input'
        append_followup_question(db, merged_goal, notes, count + 2, question, '依補件保留尚待回答的工作分問',
                                 plan_questions=successor['plan_questions'])
        notice = '已保存指定補件。請處理保留的分問；原暫表與補件可在步驟紀錄核對。'
    db.execute("""UPDATE autonomous_goals SET sources_json=?,notes=?,question=?,status=?,notice=?,
                  updated_at=CURRENT_TIMESTAMP WHERE id=?""",
               (sources_json, notes, question, status, notice, gid))


def _save_required_document_supplement(db, current, notes, content):
    goal, count = current['goal'], current['step_count']
    gid, sid = goal['id'], goal['session_id']
    missing, replacement, _ = goal_sources.plan_mapping(content)
    original = json.loads(goal['sources_json'])
    new_copy = _document(db, sid, replacement['document_id'])
    new_scope = signature([new_copy]) if new_copy is not None else []
    new_count = len(new_copy['content'].splitlines()) if new_copy is not None else None
    steps = _steps(db, gid)
    guidance = goal_guidance_sources.read(db, gid, notes, count)
    source = guidance[-1]
    scope_goal = {**goal, 'sources': original, 'steps': steps}
    if current.get('required_supplement_correction'):
        added = [row for row in new_scope if row not in original]
        action, observation, merged = goal_required_supplement.bind_correction(
            scope_goal, steps[-1], source, added, new_count)
    else:
        missing_id = missing['document_id']
        previous = next((row for row in original if row['document_id'] == missing_id), None)
        if previous is None:
            raise ValueError('缺行須屬於目前目標的原匯入副本。')
        old_copy = _document(db, sid, missing_id)
        old_count = goal_required_supplement.line_count({missing_id: old_copy}, previous)
        action, observation, merged = goal_required_supplement.bind(
            scope_goal, steps[-1], source, new_scope, old_count, new_count)
    sources_json = json.dumps(merged, ensure_ascii=False)
    _append_step(db, {**goal, 'sources_json': sources_json}, notes, count + 1, action, observation)
    options = goal_question_withdrawals.options(db, gid, goal['question'], count, guidance)
    goal_question_withdrawals.record(db, gid, options, source)
    db.execute("""UPDATE autonomous_goals SET sources_json=?,notes=?,question='',status='paused',notice=?,
                  updated_at=CURRENT_TIMESTAMP WHERE id=?""",
               (sources_json, notes, '已綁定指定補件並保留原提問，請接續查讀原文。', gid))


def append_waiting_recheck(db, current, notes):
    goal = current['goal']
    source = goal['guidance_sources'][-1]
    args = {'field_reference': source['citation'], 'waiting_recheck': True}
    _append_reference_recheck(db, goal, notes, current['step_count'] + 1, args, goal['reference_review'])


def append_waiting_field_version(db, current, notes):
    goal = current['goal']
    review = goal['reference_review']
    receipt = review['field_reference']['waiting_field_version']
    if current['step_count'] + (2 if receipt['withdraws_question'] else 3) > MAX_STEPS:
        raise ValueError('剩餘步數不足以重核版本與原欄名，請帶入目前進度建立下一階段。')
    args = {'waiting_field_version': receipt['source']}
    _append_reference_recheck(db, goal, notes, current['step_count'] + 1, args, review)


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


def append_followup_question(db, goal, notes, sequence, question, note, *, partial_result=None, colloquial_receipt=None,
                             plan_questions=None, question_part_withdrawal=None):
    steps = _steps(db, goal['id'])
    excerpts = [row for step in steps for row in step['observation'].get('excerpts', [])]
    citations = goal_partials.question_citations(question, excerpts, goal.get('reply_sources', ()),
                                                  goal.get('guidance_sources', ()))
    observation = {'question': question}
    if question_part_withdrawal is not None:
        observation[goal_question_withdrawals.PART_MARK] = question_part_withdrawal
    if citations:
        observation['question_citations'] = citations
    action = {'tool': 'ask', 'args': {'question': question}, 'note': note}
    if colloquial_receipt is not None:
        action['args']['report_colloquial_question'] = colloquial_receipt['source']
        observation['report_colloquial_question'] = colloquial_receipt
    if plan_questions is not None:
        action['args']['plan_questions'] = plan_questions
        observation['work_plan_question'] = work_plan_scope.bind_questions(action['args'], steps)
    if partial_result is not None:
        goal_partials.validate(partial_result)
        checks = check_citations(partial_result, excerpts, allow_joined_lines=True)
        if any(not item['range_valid'] for item in checks):
            raise ValueError('更正後暫表引用的文件行尚未查讀。')
        action['args']['partial_result'] = partial_result
        observation.update(partial_result=partial_result, citation_checks=checks)
        replies = goal_reply_sources.citations(partial_result, goal.get('reply_sources', ()))
        guidance = goal_guidance_sources.citations(partial_result, goal.get('guidance_sources', ()))
        if replies:
            observation['reply_citations'] = replies
        if guidance:
            observation['guidance_citations'] = guidance
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
    if adoption := proof.get('waiting_field_version'):
        if not adoption['withdraws_question']:
            return current
        source = next(item for item in goal['guidance_sources'] if item['citation'] == adoption['source'])
        append_waiting_field_version(db, current, goal['notes'])
        goal_question_withdrawals.record(db, goal['id'], current['question_withdrawal'], source)
        notice = decision_table.describe_waiting_review(review)
        db.execute("UPDATE autonomous_goals SET status='paused',question='',notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (notice, goal['id']))
        return _snapshot(db, goal['session_id'], goal['id'])
    if version := proof.get('waiting_version_recheck'):
        if not version['withdraws_question']:
            return current
        successor = version.get('next_question', '')
        if current['step_count'] + (4 if successor else 2) > MAX_STEPS:
            raise ValueError('剩餘步數不足以重核版本及更新成果，請帶入目前進度建立下一階段。')
        citation = version['changes'][-1]['evidence']['citation']
        source = next(item for item in goal['guidance_sources'] if item['citation'] == citation)
        args = {'waiting_version_recheck': citation}
        _append_reference_recheck(db, goal, goal['notes'], current['step_count'] + 1, args, review)
        goal_question_withdrawals.record(db, goal['id'], current['question_withdrawal'], source)
        status = 'needs_input' if successor else 'paused'
        notice = decision_table.describe_waiting_review(review)
        if successor:
            append_followup_question(db, goal, goal['notes'], current['step_count'] + 2, successor,
                                     '依最新去向只保留欄名追問', partial_result=notice)
        db.execute('UPDATE autonomous_goals SET status=?,question=?,notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (status, successor, notice, goal['id']))
        return _snapshot(db, goal['session_id'], goal['id'])
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


def _save_ordered_waiting_correction(db, current, notes):
    goal, count = current['goal'], current['step_count']
    gid = goal['id']
    steps = _steps(db, gid)
    sources = goal_guidance_sources.read(db, gid, notes, count)
    source = sources[-1]
    replies = goal.get('reply_sources', ())
    citation = current['report_colloquial_question']['source']
    args = {'report_colloquial_guidance': citation, 'waiting_correction': source['citation']}
    observation = decision_table.observe(args, steps, replies, sources)
    question = observation['report_waiting_correction']['next_question']
    ordered = observation['field_reference']['report_mixed_guidance']['ordered']
    needs_clarification = ordered['next'] is not None
    required = 6 if needs_clarification else 4 if question else 2
    if count + required > MAX_STEPS:
        raise ValueError('剩餘步數不足以重核更正及交付，請帶入進度建立下一階段。')
    options = goal_question_withdrawals.options(db, gid, goal['question'], count, sources)
    if not options or options['correction_numbers'] != [source['number']]:
        raise ValueError('正式更正與目前等待的問題不一致。')
    _append_reference_recheck(db, goal, notes, count + 1, args, observation)
    goal_question_withdrawals.record(db, gid, options, source)
    if question:
        partial, receipt = None, None
        if needs_clarification:
            _, partial, receipt = decision_table.prepare_report_colloquial_question(_steps(db, gid), replies, sources, citation)
        append_followup_question(db, {**goal, 'guidance_sources': sources}, notes, count + 2, question,
                                 '依正式更正保留仍影響交付的問題', partial_result=partial, colloquial_receipt=receipt)
    if question == goal['question']:
        notice = '已核對正式更正。原問題仍會影響成果，請依最新條件回答。'
    elif question:
        notice = '已核對正式更正。先前的問題已撤下，只需確認現在顯示的欄名。'
    else:
        notice = '已核對正式更正。先前的問題已無須回答，請接續更新成果。'
    db.execute('UPDATE autonomous_goals SET notes=?,status=?,question=?,notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (notes, 'needs_input' if question else 'paused', question or '', notice, gid))
    return _snapshot(db, goal['session_id'], gid)


def _save_plan_scope_change(db, current, notes, delivery_change=None, *, filename_guidance=None, confirmation=False):
    goal, count = current['goal'], current['step_count']
    gid = goal['id']
    if goal['status'] == 'report_ready' and not confirmation:
        return None
    steps = _steps(db, gid)
    if work_plan.latest(steps) is None:
        return None
    waiting = goal['status'] == 'needs_input'
    filename_ask = waiting and steps[-1]['observation'].get(artifact_names.ASK_MARK)
    if waiting and (not steps or steps[-1]['action']['tool'] != 'ask'
                    or steps[-1]['action']['args'].keys() - {'question', 'partial_result', 'plan_questions', artifact_names.ASK_MARK}
                    or decision_table.reference_question_for_steps(steps[:-1])):
        return None
    guidance = goal_guidance_sources.read(db, gid, notes, count)
    source = guidance[-1]
    named = (artifact_names.declaration(source['content'], only=True)
             if not filename_guidance and not delivery_change and delivery_preference.initially_enabled(db, gid) else None)
    if filename_ask and not (confirmation or named or delivery_change):
        return None
    scope_goal = _condition_goal(db, goal, goal['notes'])
    prior_scope = work_plan.conditions(scope_goal)
    scope = work_plan.conditions({**scope_goal, 'notes': notes})
    if confirmation:
        version = work_plan_scope.confirmation_guidance_version(source['content'])
        observation = work_plan_scope.observe_confirmation_correction(
            steps, source, scope, prior_scope, waiting=waiting, version=version)
    else:
        observation = work_plan_scope.observe(steps, source, scope, prior_scope, waiting=waiting,
            delivery=delivery_change, filename_declaration=named, filename_guidance=filename_guidance)
    if observation is None:
        if filename_guidance:
            raise ValueError('工作條件已變更。請先核對目前工作清單與待選欄位。')
        return None
    proof = observation['scope_change']
    successor = proof.get('successor')
    minimum = 3 if successor else 2
    if confirmation:
        minimum += 1 + int(delivery_preference.enabled(db, gid))
        if proof.get('withdrawn_confirmations'):
            minimum += 1
    elif delivery_change or filename_guidance:
        pending = proof.get('language', {}).get('pending_revisions', ())
        minimum = max(minimum, 2 + 2 * len(pending)) + int(delivery_preference.enabled(db, gid))
    elif named:
        minimum += int(delivery_preference.enabled(db, gid))
    if count + minimum > MAX_STEPS:
        raise ValueError('剩餘步數不足以更新工作清單及交付，請帶入目前進度建立下一階段。')
    args = {'scope_correction': source['citation']}
    if confirmation:
        args[work_plan_scope.CONFIRMATION_CORRECTION] = True
        args[work_plan_scope.CONFIRMATION_CORRECTION_VERSION] = version
    elif delivery_change and not filename_guidance:
        args['delivery_mode'] = True
    if filename_guidance:
        args[artifact_names.GUIDANCE_MARK] = True
    elif named:
        args['filename_declaration'] = True
    if waiting:
        args['ask_sequence'] = proof['ask_sequence']
        options = goal_question_withdrawals.options(db, gid, goal['question'], count, guidance)
        if not options or source['number'] not in options['correction_numbers']:
            raise ValueError('這筆更正與目前等待的問題不一致。')
        goal_question_withdrawals.record(db, gid, options, source)
    note = ('依使用者更正更新已核對的答覆' if confirmation else
            '依使用者更正更新交付方式與工作清單' if delivery_change else
            '依使用者更正更新定稿檔名與工作清單' if named or filename_guidance else '依使用者更正取消指定工作項目')
    action = {'tool': 'update_plan', 'args': args, 'note': note}
    _append_step(db, goal, notes, count + 1, action, observation)
    if successor:
        reply_sources = goal_reply_sources.read(db, gid, notes) if confirmation else goal.get('reply_sources', ())
        append_followup_question(db, {**goal, 'guidance_sources': guidance, 'reply_sources': reply_sources}, notes,
                                 count + 2, successor['question'],
                                 '依目前工作範圍保留仍需回答的問題', plan_questions=successor.get('plan_questions'))
    status = 'needs_input' if successor else 'paused'
    notice = ('已取消指定項目。只需回答目前保留的問題。' if successor else
              '已取消指定項目並更新工作清單，可接續處理剩餘工作。')
    if proof.get('withdrawn_confirmations'):
        notice = '已撤回先前答覆。請依目前分問重新確認，其他有效證據會保留。'
    elif confirmation:
        notice = ('已核對更正，請處理目前保留的問題。' if successor else
                  '已核對更正，可接續更新成果。先前的成果仍可在步驟紀錄查看。')
    elif delivery_change:
        notice = delivery_preference.describe(delivery_change) + ('仍請回答目前的問題。' if successor else '工作清單已更新。')
    if filename_guidance:
        name = filename_guidance[0]['selection']['filename']
        message = (f'目前定稿檔名為「{name}」。' if delivery_preference.enabled(db, gid)
                   else f'若恢復文件交付，定稿檔名為「{name}」。')
        notice = (delivery_preference.describe(delivery_change) if delivery_change else '') + message + '仍請回答目前的欄位問題。'
    elif named:
        notice = f'已確認定稿檔名「{named["filename"]}」，可以接續交付。'
    db.execute('UPDATE autonomous_goals SET notes=?,status=?,question=?,notice=?,result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (notes, status, successor['question'] if successor else '', notice,
                '' if confirmation else goal['result'], gid))
    return _snapshot(db, goal['session_id'], gid)


def _save_filename_guidance(db, current, notes):
    goal, count = current['goal'], current['step_count']
    gid = goal['id']
    if goal['status'] != 'needs_input' or not delivery_preference.initially_enabled(db, gid):
        return None
    steps = _steps(db, gid)
    language = work_plan_scope.language_followup(steps)
    if not language or not language.get('pending_revisions') or language.get('cancellation'):
        return None
    source = goal_guidance_sources.read(db, gid, notes, count)[-1]
    requirement = artifact_names.requirement({**goal, 'steps': steps})
    if not artifact_names.has_guidance_name(source['content']):
        return None
    delivery = delivery_preference.filename_guidance_receipt(source)
    choice = artifact_names.guidance_choice(steps, source['content'], requirement, delivery=delivery)
    if not choice:
        return None
    name = choice[0]['selection']['filename']
    if any(row['name'].casefold() == name.casefold() for row in artifacts.inventory(db, gid)):
        raise ValueError('請提供同格式且尚未使用的定稿檔名。')
    delivery_preference.record_filename_guidance(db, gid, source, delivery)
    return _save_plan_scope_change(db, current, notes, delivery, filename_guidance=choice)


def save(store, sid, gid, content, *, expected_fingerprint):
    content = text(content,4000)
    if not isinstance(expected_fingerprint,str) or not re.fullmatch('[0-9a-f]{64}',expected_fingerprint):
        raise ValueError('需要讀取目標時的指紋，請重新載入。')
    with lease(store,gid), store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = _snapshot(db, sid, gid, include_reply_positions=True)
        if current['fingerprint'] != expected_fingerprint:
            raise StaleGuidance('目標或來源已有變更，請重新載入並核對；未保存補充。')
        if current['goal']['status'] == 'needs_input' and work_plan_scope.pending_confirmation_target(_steps(db, gid)):
            raise ValueError('請先在目前問題指定要更正的欄位，或回答「都不改」。')
        plan_supplement = current.get('plan_document_supplement') and content.startswith('補件')
        mapped = goal_sources.is_plan_mapping(content)
        required_paths = current.keys() & {'required_document_supplement', 'required_supplement_correction'}
        required_supplement = bool(required_paths) and mapped
        if current['goal']['status'] == 'needs_input' and mapped and not (plan_supplement or required_supplement):
            raise ValueError('目前提問無法用這筆補件撤下。請核對提問、缺行與目前補件。')
        allowed, reason = eligibility(current)
        if not allowed:
            raise ValueError(reason)
        supplement = 'document_supplement' in current and re.match(r'^補件(?:[ \t]|$)', content)
        if ((current.get('plan_document_supplement') or required_paths)
                and not current['sources_match'] and not (plan_supplement or required_supplement)):
            raise ValueError('請先指定缺行與補件的對應範圍。')
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
        if plan_supplement:
            _save_plan_document_supplement(db, current, notes, content)
            return _snapshot(db, sid, gid)
        if required_supplement:
            _save_required_document_supplement(db, current, notes, content)
            return _snapshot(db, sid, gid)
        if changed_confirmation := _save_plan_scope_change(db, current, notes, confirmation=True):
            return changed_confirmation
        if goal['status'] == 'report_ready' and not current.keys() & {
                'reference_report', 'document_version_report', 'document_conflict_report', 'document_supplement'}:
            raise ValueError('請指明要撤回的原問句，或提供已核對答覆的原問句及新值。也可帶入成果建立下一階段。')
        if 'document_supplement' in current and not (supplement or 'document_conflict_report' in current):
            raise ValueError(reason)
        if named_scope := _save_filename_guidance(db, current, notes):
            return named_scope
        delivery_change = delivery_preference.capture(db, gid, notes, step_count)
        if 'report_colloquial_question' in current:
            return _save_ordered_waiting_correction(db, current, notes)
        if changed_scope := _save_plan_scope_change(db, current, notes, delivery_change):
            return changed_scope
        if supplement:
            _save_report_supplement(db, current, notes, content)
        elif current.keys() & {'reference_report', 'document_version_report', 'document_conflict_report'}:
            _save_reference_recheck(db, current, notes)
        elif delivery_change:
            db.execute('UPDATE autonomous_goals SET notes=?,notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (notes, delivery_preference.describe(delivery_change), gid))
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
        reviewed = apply_waiting_review(db, saved)
        pending = reviewed['goal']
        if (pending['status'] == 'needs_input' and pending['question'] == goal['question']
                and not pending.get('reference_review') and not pending.get('answered_reference_review')
                and (options := reviewed.get('question_withdrawal')) and number in options['correction_numbers']):
            steps = _steps(db, gid)
            if (not steps[-1]['action']['args'].keys() & {artifact_names.ASK_MARK, 'plan_questions',
                                                    'reference_remaining', 'document_version'}
                    and not decision_table.reference_question_for_steps(steps[:-1])):
                source = pending['guidance_sources'][-1]
                remaining = (goal_question_withdrawals.remaining_question(content, pending['question'])
                             if work_plan.latest(steps) is None else None)
                if remaining:
                    required_steps = 2 + int(delivery_preference.enabled(db, gid))
                    if step_count + required_steps > MAX_STEPS:
                        raise ValueError('剩餘步數不足以保留分問及交付，請帶入目前進度建立下一階段。')
                    goal_question_withdrawals.record(db, gid, options, source)
                    append_followup_question(db, pending, notes, step_count + 1, remaining, '保留未撤下的分問',
                                             question_part_withdrawal={'ask_sequence': step_count,
                                                                       'guidance_number': number})
                    db.execute('UPDATE autonomous_goals SET question=?,notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                               (remaining, '已撤下指定分問。請回答仍顯示的問題。', gid))
                    return _snapshot(db, sid, gid)
                if goal_question_withdrawals.requests_current_dismissal(content):
                    goal_question_withdrawals.dismiss(db, gid, options, source)
                    return _snapshot(db, sid, gid)
        return reviewed
