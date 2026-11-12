"""Bind clarification to public plan items and apply explicit scope reductions."""
import re
import unicodedata

from . import delivery_preference, goal_guidance_sources, goal_reply_sources, goal_stages, work_plan


GUIDANCE = '''有已保存工作清單時，若缺漏會影響特定未完成項目，提問使用 plan_questions，例如
{"question":"收貨窗口是誰？\n入場門號是多少？","plan_questions":[{"question":"收貨窗口是誰？","items":[2]},{"question":"入場門號是多少？","items":[3]}]}。
question 逐行等於各分問。items 填這個問題實際影響的所有未完成項目；一個獨立分問對應一個資料缺漏，無關的問題不綁進清單。
scope_cancelled 列出使用者已取消的項目。當前成果只交保留項目的實際結果。取消項目和原因已保存於工作清單，除非使用者要求一併交代，定稿省略已取消項目的名稱、經過及結果。
scope_reply 的 confirmed 分問有實際使用者原話及值，引用其 citation。revisions 保存使用者更正後的值、舊依據和須重核的項目。成果使用現行值並引用新回覆。declined_proposals 已明示未採用。pending_revisions 保存待選原話，選定前沿用最後確認值。completed_items 只包含已完成的確認工作。其他項目仍依清單完成。若交付核對已就緒，直接交付仍需的成果。'''
_CANCEL = re.compile(r'(?:本輪|這次|本次)?\s*(?:先取消|取消|先不做|不做|先暫停|暫停|不用再做)\s*(.+)')
_TITLES = re.compile(r'「[^「」\r\n]+」(?:\s*(?:、|，|,|及|和|與)\s*「[^「」\r\n]+」)*')
_UNCHANGED = {'其他項目照舊', '其餘項目照舊', '其他項目維持原樣', '其餘照舊', '其他照舊'}
_OUTPUT_ONLY = re.compile(r'只(?:交付|交|提供|要)(?:已核對的|已確認的)?[\u4e00-\u9fff、]{1,45}')
_SCOPE_COMMA = re.compile(r'\s*(?:先取消|取消|先不做|不做|先暫停|暫停|不用再做|其餘|其他|只交|只提供|只要)')
_WORK_VERB = re.compile(r'^(?:安排|確認|核對|決定|記錄|填寫)')
_COLLECT_FIELD = re.compile(r'^(?:確認|記錄|登記|填寫)')
_ASK_FIELD = re.compile(r'(?:是誰|為誰|是哪位|是什麼|是多少|有多少|要用哪個|是哪一個|是哪個)[？?]$')
_NUMERIC_QUESTION = re.compile(r'(?:是|有)多少[？?]$')
_FIELD_NAME = re.compile(r'[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9 _-]{1,30}')
_REPLY_CONNECTOR = r'(?:(?:另外|然後)\s*)?'
_ASSIGN = re.compile(r'(?:已確認為|已確認是|確定為|確定是|是|為|：|:)\s*(.+)')
_DEFERRED = re.compile(r'(?:目前\s*)?(?:還沒確認|還未確認|尚未確認|未確認|待確認|還要確認|尚待確認|未知|還不知道|先不決定|稍後確認)')
_NUMBER = re.compile(r'(?:[0-9０-９]+(?:\.[0-9０-９]+)?|[零〇一二兩三四五六七八九十百千]+)\s*(?:[號号份件箱袋組張位人個座樓層班]|分鐘|小時)?')
_CODE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,23}')
_SHORT_WORD = re.compile(r'[\u4e00-\u9fff]{1,12}')
_UNCERTAIN = re.compile(r'待|未|還|不|暫|可能|大概|或|應該|也許|若|如果|請|要|確認|取消|改成|改為|另外|同時|再說|回覆|晚點|等下|稍後')
_POST_CANCEL = re.compile(r'(.{2,90}?)\s*(?:先不用了|不用了|先不做了|不做了|先取消|取消掉)')
_PLAIN_COMMA = re.compile(r'\s*(?!\s*「)|\s*「[^」\r\n]+」\s*(?:先不用了|不用了|先不做了|不做了|先取消|取消掉)')
_KEEP_ALL = {'都保留', '都先保留', '兩項都保留', '都不取消', '不取消'}
_REVISION_VERB = r'(?:改成|改為|更正為|更正成|應改為)'
_REVISION = re.compile(r'(?:(?:欄位\s*)?「([^「」\r\n]{2,31})」|([\u4e00-\u9fffA-Za-z0-9 _-]{2,31}?))\s*'
                       r'(?:(?:剛才|先前|原先|上一則)\s*(?:說|填|寫|報)錯了?\s*[，,、]?\s*)?'
                       rf'{_REVISION_VERB}\s*(.+)')
_REVISION_COMMA = re.compile(rf'(?!\s*{_REVISION_VERB})(?:{_PLAIN_COMMA.pattern})')
_DECLINE = r'(?:(?:目前|本輪|這次)\s*)?(?:未採用|尚未採用|沒有採用|不採用|先不採用|維持原值)'
_DECLINED_PROPOSAL = re.compile(r'(?:有人|同事|對方|主管)?\s*(?:曾)?(?:提議|建議)\s*'
                                r'(?:「[^」\r\n]{1,200}」|『[^』\r\n]{1,200}』)\s*[，,、]?\s*' + _DECLINE)
_PENDING_COMMA = re.compile(rf'(?!\s*{_DECLINE})(?:{_REVISION_COMMA.pattern})')
_KEEP_VALUE = {'沿用原值', '維持原值', '用原值', '都不改', '不改', '保持原值'}


def validate_questions(args):
    if args.keys() - {'question', 'partial_result', 'plan_questions'}:
        raise ValueError('工作清單提問須使用獨立的工作分問。')
    parts = args['plan_questions']
    if not isinstance(parts, list) or not 1 <= len(parts) <= 4:
        raise ValueError('依工作清單提問需列一至四個分問。')
    questions = []
    for part in parts:
        if not isinstance(part, dict) or part.keys() != {'question', 'items'}:
            raise ValueError('每個工作分問須列問句和相關項目。')
        question, ids = part['question'], part['items']
        if (not isinstance(question, str) or not 1 <= len(question) <= 400 or question != question.strip()
                or any(ch in question for ch in '\r\n\0') or question in questions
                or not isinstance(ids, list) or not 1 <= len(ids) <= 8
                or any(type(number) is not int or not 1 <= number <= 8 for number in ids) or len(set(ids)) != len(ids)):
            raise ValueError('工作分問及相關項目格式不符。')
        questions.append(question)
    if args['question'] != '\n'.join(questions):
        raise ValueError('顯示的問題須逐行對應工作分問。')


def bind_questions(args, steps):
    validate_questions(args)
    plan = work_plan.latest(steps)
    if plan is None:
        raise ValueError('須先保存工作清單，才能依工作項目提問。')
    active = {row['id'] for row in plan['items'] if row['state'] not in {'done', 'superseded'}}
    receipt = _scope_receipt(plan)
    language = receipt.get('language', {})
    choice = language.get('cancellation') or next(iter(language.get('pending_revisions', ())), None)
    if choice and args == receipt['successor']:
        active.update(choice['items'])
    parts = args['plan_questions']
    if any(not set(part['items']) <= active for part in parts):
        raise ValueError('問題只能連到仍需處理的工作項目。')
    anchor = steps[plan['sequence'] - 1]
    return {'plan_step': plan['sequence'], 'plan_sha256': goal_stages.digest(anchor), 'parts': parts}


def _target(task):
    return _WORK_VERB.sub('', task, count=1)


def _legacy_parts(question, plan):
    if '\n' in question or question.count('？') + question.count('?') != 1:
        return []
    matching = []
    for item in plan['items']:
        target = _target(item['task'])
        if len(target) < 2:
            continue
        suffix = rf'{re.escape(target)}(?:是誰|為誰|是哪位|是什麼|是多少|有多少|要用哪個|是哪一個)[？?]'
        if re.search(rf'(?:^|的\s*|：\s*|請問\s*)(?:正式)?{suffix}$', question):
            matching.append(item)
    if len(matching) != 1 or matching[0]['state'] in {'done', 'superseded'}:
        return []
    return [{'question': question, 'items': [matching[0]['id']]}]


def cancellations(source, plan):
    clauses = goal_guidance_sources.standalone_clauses(source['content'], comma_before=_SCOPE_COMMA)
    return _cancellations(clauses, plan)


def _cancellations(clauses, plan):
    ids_by_task = {}
    for item in plan['items']:
        ids_by_task.setdefault(item['task'], []).append(item['id'])
    active = {item['id'] for item in plan['items'] if item['state'] != 'superseded'}
    changes, used, outputs = [], set(), []
    for start, clause, question in clauses:
        if question:
            return []
        if clause in _UNCHANGED:
            continue
        if _OUTPUT_ONLY.fullmatch(clause) and not re.search(r'重算|重新|改成|改為|新增|另做', clause):
            outputs.append(clause)
            continue
        match = _CANCEL.fullmatch(clause)
        if not match:
            return []
        titles = match[1].strip()
        names = re.findall(r'「([^「」]+)」', titles) if _TITLES.fullmatch(titles) else [titles]
        ids = []
        for name in names:
            matches = ids_by_task.get(name, [])
            if len(matches) != 1 or matches[0] in used:
                return []
            number = matches[0]
            used.add(number)
            if number in active:
                ids.append(number)
        if ids:
            changes.append({'items': ids, 'start': start, 'end': start + len(clause), 'quote': clause})
    for item in plan['items']:
        if item['id'] in used and any(_target(item['task']) in clause for clause in outputs):
            return []
    return changes


def _waiting_parts(steps, plan):
    ask = steps[-1]
    action, observation = ask['action'], ask['observation']
    args = action['args']
    if action['tool'] != 'ask' or args.keys() - {'question', 'partial_result', 'plan_questions'}:
        raise ValueError('這個待答問題不屬於一般工作清單。')
    question = goal_reply_sources.checked_question(action, observation)
    if 'plan_questions' in args:
        binding = bind_questions(args, steps[:-1])
        if observation.get('work_plan_question') != binding or binding['plan_step'] != plan['sequence']:
            raise ValueError('待答問題與目前工作清單不一致。')
        parts = binding['parts']
    else:
        parts = _legacy_parts(question, plan)
    return question, parts


def _next_question(parts):
    if parts:
        return {'question': '\n'.join(part['question'] for part in parts), 'plan_questions': parts}
    return None


def _scope_receipt(plan):
    return plan.get('scope_reply') or plan.get('scope_change') or {}


def language_followup(steps):
    plan = work_plan.latest(steps)
    if plan and plan['sequence'] == len(steps) - 1:
        receipt = _scope_receipt(plan)
        if receipt.get('successor') == steps[-1]['action']['args']:
            return receipt.get('language')
    return None


def _successor(steps, plan, cancelled):
    question, parts = _waiting_parts(steps, plan)
    if not parts:
        return {'question': question}
    remaining = []
    for part in parts:
        ids = [number for number in part['items'] if number not in cancelled]
        if ids:
            remaining.append({'question': part['question'], 'items': ids})
    return _next_question(remaining)


def _without_delivery(text, spans):
    for row in spans:
        start, end = row['start'], row['end']
        text = text[:start] + ' ' * (end - start) + text[end:]
    return text


def observe(steps, source, scope, prior_scope, *, waiting, delivery=None):
    plan = work_plan.latest(steps)
    if plan is None or source['steps_seen'] != len(steps):
        return None
    if delivery:
        if not waiting or prior_scope != plan['goal_conditions_sha256'] or not _waiting_parts(steps, plan)[1]:
            return None
        text = _without_delivery(source['content'], [delivery])
        clauses = goal_guidance_sources.standalone_clauses(text, comma_before=_SCOPE_COMMA)
        changes = _cancellations(clauses, plan)
        if clauses and not changes:
            return None
    else:
        changes = cancellations(source, plan)
        if not changes:
            return None
    cancelled = {number for change in changes for number in change['items']}
    result, proof = _scope_update(steps, plan, source, changes, scope, prior_scope)
    if delivery:
        proof['delivery_mode'] = delivery
    if waiting:
        successor = _successor(steps, plan, cancelled)
        if previous := language_followup(steps):
            language = _retain_language(previous, cancelled)
            tasks = {item['id']: item['task'] for item in plan['items']}
            successor = _next_question(_show_waiting(language, tasks))
            proof['language'] = language
        proof.update(ask_sequence=steps[-1]['sequence'], ask_sha256=goal_stages.digest(steps[-1]), successor=successor)
    result['scope_change'] = proof
    return result


def _scope_update(steps, plan, source, changes, scope, prior_scope):
    cancelled = {number for change in changes for number in change['items']}
    current = prior_scope == plan['goal_conditions_sha256']
    items = []
    retained = set()
    for item in plan['items']:
        if item['id'] in cancelled:
            item = {**item, 'state': 'superseded', 'note': f'依 {source["citation"]} 取消此項。'}
        elif item['state'] == 'done':
            if current:
                retained.add(item['id'])
            else:
                item = {**item, 'state': 'pending', 'evidence_steps': [], 'note': '先前補充已變更條件，需重新核對。'}
        items.append(item)
    proof = {'citation': source['citation'], 'source_sha256': source['sha256'], 'changes': changes,
             'plan_step': plan['sequence'], 'plan_sha256': goal_stages.digest(steps[plan['sequence'] - 1]),
             'prior_scope': prior_scope, 'retained_items': [item['id'] for item in items if item['id'] in retained]}
    result = {'status': 'plan_recorded', 'items': items, 'goal_conditions_sha256': scope,
              'scope_cancelled': sorted(cancelled | set(plan.get('scope_cancelled', ()))),
              'review_note': '依實際更正更新工作清單，先前步驟及答覆仍可核對。'}
    if plan.get('verify_delivery'):
        result['verify_delivery'] = True
    if plan.get('evidence_requirements'):
        result['evidence_requirements'] = [
            {**item, 'defined_scope': scope} if item['id'] in retained else item
            for item in plan['evidence_requirements']]
        result['evidence_matches'] = [item for item in plan['evidence_matches'] if item['id'] in retained]
    return result, proof


def _asked_field(part, tasks):
    match = _ASK_FIELD.search(part['question'])
    if match is None:
        return ''
    name = part['question'][:match.start()].removeprefix('請問').strip()
    candidates = {_target(tasks[number]).removeprefix('登記') for number in part['items']}
    candidates = {item for item in candidates if name == item or name.endswith(('的' + item, '的正式' + item))}
    if len(candidates) == 1:
        return next(iter(candidates))
    # A shared question can ask a field which is not itself the title of any task.
    name = name.rsplit('的', 1)[-1].removeprefix('正式')
    return name if _FIELD_NAME.fullmatch(name) else ''


def _value(literal):
    if re.fullmatch(r'「[^「」\r\n]{1,40}」', literal):
        literal = literal[1:-1].strip()
    if literal.casefold() in {'tbd', 'unknown', 'none', 'null', 'na', '有', '無', '沒有', '某人', '有人', '等等'}:
        return None
    if (_NUMBER.fullmatch(literal) or _CODE.fullmatch(literal)
            or (_SHORT_WORD.fullmatch(literal) and not _UNCERTAIN.search(literal))):
        return literal
    return None


def _reply_value(text):
    if _DEFERRED.fullmatch(text):
        return 'deferred', None
    if match := _ASSIGN.fullmatch(text):
        literal = match[1]
        if _DEFERRED.fullmatch(literal):
            return 'deferred', None
        choices = re.split(r'\s*(?:或者|或是|或)\s*', literal)
        if len(choices) > 1 and all(_value(item) is not None for item in choices):
            return 'ambiguous', None
        if value := _value(literal):
            return 'confirmed', value
    return None


def _reply_parts(clauses, parts, fields, cancelled, *, single_followup):
    parsed = {}
    active_parts = [index for index, part in enumerate(parts) if set(part['items']) - cancelled]
    for start, raw, _ in clauses:
        clause = re.sub('^' + _REPLY_CONNECTOR, '', raw, count=1)
        matches = [(index, result) for index in active_parts if (name := fields[index]) and clause.startswith(name)
                   and (result := _reply_value(clause[len(name):].strip()))]
        if not matches and single_followup and len(active_parts) == 1 and len(clauses) == 1:
            index = active_parts[0]
            if (_NUMBER.fullmatch(clause) and _NUMERIC_QUESTION.search(parts[index]['question'])) or clause.startswith('「'):
                if value := _value(clause):
                    matches = [(index, ('confirmed', value))]
            elif _DEFERRED.fullmatch(clause):
                matches = [(index, ('deferred', None))]
        if len(matches) != 1 or matches[0][0] in parsed:
            return None
        index, (state, value) = matches[0]
        if value and _NUMERIC_QUESTION.search(parts[index]['question']) and not _NUMBER.fullmatch(value):
            return None
        evidence = {'start': start, 'end': start + len(raw), 'quote': raw}
        parsed[index] = {'state': state, 'evidence': evidence}
        if value is not None:
            parsed[index]['value'] = value
    return parsed


def observe_reply(steps, source, scope, prior_scope, *, version=1, delivery=None):
    plan = work_plan.latest(steps)
    if plan is None or source['ask_sequence'] != len(steps):
        return None
    question, parts = _waiting_parts(steps, plan)
    if not parts or source['question'] != question:
        return None
    tasks = {item['id']: item['task'] for item in plan['items']}
    if version in (2, 3, 4, 5):
        return _observe_natural_reply(steps, plan, source, scope, prior_scope, parts, tasks, version=version, delivery=delivery)
    fields = [_asked_field(part, tasks) for part in parts]
    prefixes = '|'.join(re.escape(name) for name in fields if name)
    reply_prefix = rf'|\s*{_REPLY_CONNECTOR}(?:{prefixes})' if prefixes else ''
    comma_before = re.compile(rf'{_SCOPE_COMMA.pattern}|\s*(?:本輪|這次|本次){_SCOPE_COMMA.pattern}{reply_prefix}')
    clauses = goal_guidance_sources.standalone_clauses(source['answer'], comma_before=comma_before)
    if not clauses or any(question_mark for _, _, question_mark in clauses):
        return None
    scope_clauses = [clause for clause in clauses if _CANCEL.fullmatch(clause[1]) or clause[1] in _UNCHANGED]
    changes = _cancellations(scope_clauses, plan)
    single_followup = (plan['sequence'] == len(steps) - 1
                       and (plan.get('scope_reply') or {}).get('successor') == steps[-1]['action']['args'])
    if not changes and (scope_clauses or not single_followup):
        return None
    cancelled = {number for change in changes for number in change['items']}
    parsed = _reply_parts([clause for clause in clauses if clause not in scope_clauses], parts, fields, cancelled,
                          single_followup=single_followup)
    if parsed is None:
        return None

    return _reply_result(steps, plan, source, scope, prior_scope, parts, fields, tasks, changes, parsed)


def _reply_result(steps, plan, source, scope, prior_scope, parts, fields, tasks, changes, parsed, *, language=None, revisions=()):
    cancelled = {number for change in changes for number in change['items']}

    result, proof = _scope_update(steps, plan, source, changes, scope, prior_scope)
    remaining, dispositions, complete = [], [], {}
    requirements = {item['id'] for item in plan.get('evidence_requirements', ())}
    for index, part in enumerate(parts):
        ids = [number for number in part['items'] if number not in cancelled]
        outcome = parsed.get(index, {'state': 'unanswered' if ids else 'cancelled'})
        dispositions.append({'question': part['question'], 'items': part['items'], 'active_items': ids, **outcome})
        if outcome['state'] == 'confirmed':
            for number in ids:
                task = tasks[number]
                if (number not in requirements and _COLLECT_FIELD.match(task)
                        and _COLLECT_FIELD.sub('', task, count=1) == fields[index]
                        and sum(number in other['items'] for other in parts) == 1):
                    complete[number] = outcome['value']
        elif ids:
            remaining.append({'question': part['question'], 'items': ids})
    if revisions:
        _apply_revisions(result, proof, complete, plan, revisions, source)
    for index, item in enumerate(result['items']):
        if item['id'] in complete:
            note = f'依 {source["citation"]} 確認：{complete[item["id"]]}。'
            result['items'][index] = {**item, 'state': 'done', 'evidence_steps': [len(steps) + 1], 'note': note}
    proof.update(ask_sequence=len(steps), ask_sha256=goal_stages.digest(steps[-1]), parts=dispositions,
                 completed_items=sorted(complete), successor=_next_question(remaining))
    if language is not None:
        waiting = _language_successor(language, remaining, dispositions, fields, tasks)
        successor = proof['successor'] = _next_question(waiting)
        if successor and len(successor['question']) > 800:
            return None
        if successor:
            try:
                validate_questions(successor)
            except ValueError:
                return None
        proof['language'] = language
    result['scope_reply'] = proof
    result['review_note'] = '分開核對原回覆內的範圍變更、確認值與待答分問。'
    return result


def _literal_span(start, clause):
    return {'start': start, 'end': start + len(clause), 'quote': clause}


def _cancellation_names(clause):
    match = _CANCEL.fullmatch(clause) or _POST_CANCEL.fullmatch(clause)
    if match is None:
        return []
    names = match[1].strip()
    return re.findall(r'「([^「」]+)」', names) if _TITLES.fullmatch(names) else [names]


def _candidate_items(name, tasks):
    exact = [number for number, task in tasks.items() if task == name]
    if exact or not _FIELD_NAME.fullmatch(name):
        return exact
    return [number for number, task in tasks.items() if _target(task).endswith(name)]


def _natural_scope(clauses, plan, tasks, pending, source):
    active = {item['id'] for item in plan['items'] if item['state'] != 'superseded'}
    fields, changes, used = [], [], set()
    resolution = None
    awaiting_choice = pending
    for start, clause, _ in clauses:
        evidence = _literal_span(start, clause)
        names = _cancellation_names(clause)
        if awaiting_choice and resolution is None:
            selected = clause.removeprefix('「').removesuffix('」')
            keep_all = clause in _KEEP_ALL
            choices = [] if keep_all else [number for number in awaiting_choice['items'] if tasks[number] == selected
                                           or names == [tasks[number]]]
            if len(choices) == 1 or keep_all:
                resolution = {'items': choices, 'evidence': evidence, 'from': awaiting_choice['citation']}
                if choices:
                    changes.append({'items': choices, **evidence})
                    used.update(choices)
                continue
        if clause in _UNCHANGED:
            continue
        if not names:
            fields.append((start, clause))
            continue
        ids = []
        for name in names:
            matches = _candidate_items(name, tasks)
            if len(matches) > 1:
                if pending or len(names) != 1 or not set(matches) <= active or len({tasks[n] for n in matches}) != len(matches):
                    return None
                pending = {'items': matches, 'evidence': evidence, 'citation': source['citation']}
            elif len(matches) != 1 or matches[0] in used or matches[0] not in active:
                return None
            else:
                ids.append(matches[0])
                used.add(matches[0])
        if ids:
            changes.append({'items': ids, **evidence})
    if resolution:
        if not resolution['items'] and set(awaiting_choice['items']) & used:
            return None
        pending = None
    if pending and set(pending['items']) & used:
        return None
    return changes, fields, pending, resolution


def _bare_value(clause, *, numeric):
    literal = clause
    if numeric:
        if match := re.fullmatch(r'(?:就用|用|以)\s*(.+?)(?:為準)?', literal):
            literal = match[1].strip()
        return _value(literal) if _NUMBER.fullmatch(literal) else None
    return _value(literal) if clause.startswith('「') else None


def _natural_fields(clauses, parts, fields, cancelled, *, allow_bare=True):
    statements, unassigned = {}, []
    active = [index for index, part in enumerate(parts) if set(part['items']) - cancelled]
    for start, raw in clauses:
        clause = re.sub('^' + _REPLY_CONNECTOR, '', raw, count=1)
        evidence = _literal_span(start, raw)
        matches = []
        for index in active:
            name = fields[index]
            if not name or not clause.startswith(name):
                continue
            rest = clause[len(name):].strip()
            outcome = _reply_value(rest)
            numeric = bool(_NUMERIC_QUESTION.search(parts[index]['question']))
            if outcome is None and numeric and _NUMBER.fullmatch(rest):
                outcome = ('confirmed', rest)
            if outcome and (not numeric or outcome[1] is None or _NUMBER.fullmatch(outcome[1])):
                matches.append((index, outcome))
        if not matches and allow_bare:
            for index in active:
                numeric = bool(_NUMERIC_QUESTION.search(parts[index]['question']))
                if value := _bare_value(clause, numeric=numeric):
                    matches.append((index, ('confirmed', value)))
            if len(matches) > 1:
                unassigned.append({'evidence': evidence, 'questions': [parts[index]['question'] for index, _ in matches]})
                continue
            if not matches and len(active) == 1 and _DEFERRED.fullmatch(clause):
                matches = [(active[0], ('deferred', None))]
        if len(matches) != 1:
            return None
        index, (state, value) = matches[0]
        statement = {'state': state, 'evidence': evidence}
        if value is not None:
            statement['value'] = value
        statements.setdefault(index, []).append(statement)
    parsed = {}
    for index, rows in statements.items():
        identities = {(row['state'], _value_identity(row.get('value'))) for row in rows}
        if len(identities) == 1:
            parsed[index] = {**rows[0], **({'statements': rows} if len(rows) > 1 else {})}
        else:
            parsed[index] = {'state': 'conflicting', 'statements': rows}
    return parsed, unassigned


def _value_identity(value):
    if value is not None and _NUMBER.fullmatch(value):
        return ''.join(unicodedata.normalize('NFKC', value).split())
    return value


def _language_successor(language, remaining, dispositions, fields, tasks):
    by_question = {part['question']: (part, fields[index]) for index, part in enumerate(dispositions)}
    previous = {entry['part']['question']: entry['display'] for entry in language.pop('previous_waiting')}
    waiting = []
    for part in remaining:
        outcome, field = by_question[part['question']]
        display = previous.get(part['question'], part['question'])
        if outcome['state'] == 'conflicting':
            values = list(dict.fromkeys(row['value'] for row in outcome['statements'] if 'value' in row))
            if field and len(values) == 2:
                display = f'{field}要以「{values[0]}」還是「{values[1]}」為準？'
        waiting.append({'part': part, 'display': display})
    language['waiting'] = waiting
    return _show_waiting(language, tasks)


def _retain_language(previous, cancelled):
    waiting = []
    for entry in previous['waiting']:
        part = entry['part']
        ids = [number for number in part['items'] if number not in cancelled]
        if ids:
            waiting.append({**entry, 'part': {**part, 'items': ids}})
    pending = previous['cancellation']
    if pending and set(pending['items']) & cancelled:
        pending = None
    retained = {'waiting': waiting, 'cancellation': pending}
    if 'pending_revisions' in previous:
        retained['pending_revisions'] = _trim_revision_choices(previous['pending_revisions'], cancelled)
    return retained


def _show_waiting(language, tasks):
    if cancellation := language['cancellation']:
        ids = cancellation['items']
        choices = '或'.join(f'「{tasks[number]}」' for number in ids)
        return [{'question': f'要取消哪一項？請選{choices}，也可回答「都保留」。', 'items': ids}]
    if pending := language.get('pending_revisions'):
        choice = pending[0]
        old = choice['previous']['value']
        values = '或'.join(f'「{row["value"]}」' for row in choice['options'])
        return [{'question': f'{choice["field"]}要改為{values}，還是沿用「{old}」？', 'items': choice['items']}]
    return [{**entry['part'], 'question': entry['display']} for entry in language['waiting']]


def _observe_natural_reply(steps, plan, source, scope, prior_scope, parts, tasks, *, version, delivery):
    receipt = _scope_receipt(plan)
    follows = plan['sequence'] == len(steps) - 1 and receipt.get('successor') == steps[-1]['action']['args']
    previous = receipt.get('language', {}) if follows else {}
    waiting = previous.get('waiting', [])
    if previous:
        parts = [entry['part'] for entry in waiting]
    text = source['answer']
    if version == 5:
        if (not delivery or prior_scope != plan['goal_conditions_sha256']
                or not (follows or 'plan_questions' in steps[-1]['action']['args'])):
            return None
        text = _without_delivery(text, delivery['receipt']['plan_parts'])
    comma = {3: _REVISION_COMMA, 4: _PENDING_COMMA, 5: _PENDING_COMMA}.get(version, _PLAIN_COMMA)
    clauses = goal_guidance_sources.standalone_clauses(text, comma_before=comma)
    if (not clauses and version != 5) or any(question_mark for _, _, question_mark in clauses):
        return None
    declined = []
    if version in (4, 5):
        declined = [_literal_span(start, clause) for start, clause, _ in clauses if _DECLINED_PROPOSAL.fullmatch(clause)]
        clauses = [entry for entry in clauses if not _DECLINED_PROPOSAL.fullmatch(entry[1])]
    scope_parts = _natural_scope(clauses, plan, tasks, previous.get('cancellation'), source)
    if scope_parts is None:
        return None
    changes, field_clauses, pending, resolution = scope_parts
    if not (changes or pending or resolution or follows or version == 5):
        return None
    cancelled = {number for change in changes for number in change['items']}
    language = {'cancellation': pending, 'resolution': resolution, 'unassigned': [], 'previous_waiting': waiting}
    revisions = ()
    if version in (3, 4, 5):
        if (version != 5 and not follows) or prior_scope != plan['goal_conditions_sha256']:
            return None
        if version in (4, 5):
            revised = _parse_revision_choices(field_clauses, steps, plan, tasks, cancelled, previous, source, declined,
                                             delivery_only_ok=version == 5)
        else:
            revised = _parse_revisions(field_clauses, steps, plan, tasks, cancelled)
        if revised is None:
            return None
        field_clauses, revisions = revised[:2]
        if version in (4, 5):
            language.update(revised[2])
    fields = [_asked_field(part, tasks) for part in parts]
    explicit = version in (4, 5) and bool(previous.get('pending_revisions') or previous.get('cancellation'))
    replies = _natural_fields(field_clauses, parts, fields, cancelled, allow_bare=not explicit)
    if replies is None:
        return None
    parsed, unassigned = replies
    language['unassigned'] = unassigned
    result = _reply_result(steps, plan, source, scope, prior_scope, parts, fields, tasks, changes, parsed,
                           language=language, revisions=revisions)
    if result and version == 5:
        result['scope_reply']['delivery_mode'] = delivery
    return result


def _confirmed_fields(steps, plan, tasks):
    chain = []
    sequence = plan['sequence']
    while proof := _scope_receipt(steps[sequence - 1]['observation']):
        chain.append((steps[sequence - 1], proof))
        prior = proof['plan_step']
        if not 1 <= prior < sequence or proof['prior_scope'] != steps[prior - 1]['observation'].get('goal_conditions_sha256'):
            break
        sequence = prior
    confirmed = {}
    for step, proof in reversed(chain):
        origin = {'step': step['sequence'], 'step_sha256': goal_stages.digest(step), 'citation': proof['citation'],
                  'source_sha256': proof['source_sha256']}
        for part in proof.get('parts', ()):
            field = _asked_field(part, tasks)
            if field and part['state'] == 'confirmed':
                ids = part['active_items']
                collected = sorted(set(ids) & set(proof['completed_items']))
                confirmed[part['question']] = {'field': field, 'items': ids, 'collected_items': collected,
                                               'value': part['value'], 'evidence': part['evidence'], **origin}
        for revision in proof.get('revisions', ()):
            confirmed[revision['question']] = {**{key: revision[key] for key in ('field', 'items', 'collected_items',
                                                                                'value', 'evidence', 'operand_paths')}, **origin}
    return confirmed


def _dependent_operands(plan, previous, field):
    paths = {row['id']: set(row['paths']) for row in previous.get('operand_paths', ())}
    for row in plan.get('evidence_requirements', ()):
        for operand in row.get('operands', ()):
            if operand.get('field') == field:
                paths.setdefault(row['id'], set()).add(operand['path'])
    evidence = previous['evidence']
    for row in plan.get('evidence_matches', ()):
        for operand in row.get('operands', ()):
            if (operand.get('source_sha256') == previous['source_sha256'] and operand.get('citation') == previous['citation']
                    and evidence['start'] <= operand['number_start'] < operand['number_end'] <= evidence['end']):
                paths.setdefault(row['id'], set()).add(operand['path'])
    return paths


def _parse_revisions(clauses, steps, plan, tasks, cancelled):
    if not any(_REVISION.fullmatch(clause) for _, clause in clauses):
        return None
    confirmed = _confirmed_fields(steps, plan, tasks)
    active = {row['id']: row for row in plan['items'] if row['state'] != 'superseded' and row['id'] not in cancelled}
    remaining, revisions, seen = [], [], set()
    for start, clause in clauses:
        match = _REVISION.fullmatch(clause)
        if match is None:
            remaining.append((start, clause))
            continue
        field = (match[1] or match[2]).strip()
        options = [(question, row) for question, row in confirmed.items() if row['field'] == field
                   and set(row['items']) & active.keys()]
        value = _value(match[3].strip())
        if len(options) != 1 or field in seen or value is None:
            return None
        question, previous = options[0]
        if _NUMERIC_QUESTION.search(question) and not _NUMBER.fullmatch(value):
            return None
        seen.add(field)
        revisions.append(_revision_entry(plan, active, field, question, previous, value, _literal_span(start, clause)))
    return remaining, revisions


def _revision_entry(plan, active, field, question, previous, value, evidence):
    required = {row['id'] for row in plan.get('evidence_requirements', ())}
    ids = set(previous['items']) & active.keys()
    collected = sorted(number for number in (ids & set(previous['collected_items'])) - required
                       if active[number]['state'] == 'done' and active[number]['evidence_steps'] == [previous['step']])
    dependent = _dependent_operands(plan, previous, field)
    changed = _value_identity(value) != _value_identity(previous['value'])
    recheck = sorted(((ids | dependent.keys()) & active.keys()) - set(collected)) if changed else []
    return {'field': field, 'question': question, 'items': sorted(ids), 'value': value, 'evidence': evidence,
            'previous': previous, 'collected_items': collected, 'recheck_items': recheck,
            'operand_paths': [{'id': number, 'paths': sorted(dependent[number])}
                              for number in sorted(dependent.keys() & active.keys())]}


def _trim_revision_choices(choices, cancelled):
    return [{**row, 'items': items} for row in choices
            if (items := [number for number in row['items'] if number not in cancelled])]


def _revision_selection(clause, match, choice):
    if clause in _KEEP_VALUE:
        return choice['previous']['value']
    if match:
        if (match[1] or match[2]).strip() != choice['field']:
            return None
        literal = match[3].strip()
    else:
        literal = re.sub(r'^(?:就用|改用|選用|採用|沿用|選|用)\s*', '', clause, count=1)
    value = _value(literal)
    if value is not None and any(_value_identity(value) == _value_identity(row)
                                 for row in [choice['previous']['value'], *(row['value'] for row in choice['options'])]):
        return value
    return None


def _parse_revision_choices(clauses, steps, plan, tasks, cancelled, previous, source, declined, *, delivery_only_ok=False):
    confirmed = _confirmed_fields(steps, plan, tasks)
    active = {row['id']: row for row in plan['items'] if row['state'] != 'superseded' and row['id'] not in cancelled}
    prior = previous.get('pending_revisions', [])
    pending = _trim_revision_choices(prior, cancelled)
    shown = prior[0] if prior and not previous.get('cancellation') and pending and pending[0]['field'] == prior[0]['field'] else None
    remaining, revisions, grouped = [], [], {}
    resolution = None
    for start, clause in clauses:
        evidence = _literal_span(start, clause)
        match = _REVISION.fullmatch(clause)
        if shown and (selected := _revision_selection(clause, match, shown)) is not None:
            if resolution:
                return None
            old = shown['previous']
            keep = _value_identity(selected) == _value_identity(old['value'])
            resolution = {'field': shown['field'], 'value': selected, 'kept': keep, 'evidence': evidence,
                          'from': shown['citation'], 'source_sha256': shown['source_sha256']}
            if not keep:
                revisions.append(_revision_entry(plan, active, shown['field'], shown['question'], old, selected, evidence))
            pending = pending[1:]
            continue
        if match is None:
            remaining.append((start, clause))
            continue
        field = (match[1] or match[2]).strip()
        if (resolution and field == resolution['field']) or any(row['field'] == field for row in pending):
            return None
        literals = re.split(r'\s*(?:或者|或是|或)\s*', match[3].strip())
        options = [{'value': _value(literal), 'evidence': evidence} for literal in literals]
        if any(row['value'] is None for row in options):
            return None
        grouped.setdefault(field, []).extend(options)

    out_of_scope = []
    for field, options in grouped.items():
        candidates = [(question, row) for question, row in confirmed.items() if row['field'] == field]
        if len(candidates) != 1:
            return None
        question, old = candidates[0]
        if _NUMERIC_QUESTION.search(question) and any(not _NUMBER.fullmatch(row['value']) for row in options):
            return None
        items = sorted((set(old['items']) | _dependent_operands(plan, old, field).keys()) & active.keys())
        if not items:
            if not set(old['items']) & cancelled:
                return None
            out_of_scope.append({'field': field, 'options': options})
            continue
        identities = {}
        for row in options:
            identities.setdefault(_value_identity(row['value']), row)
        if len(identities) == 1:
            selected = next(iter(identities.values()))
            revisions.append(_revision_entry(plan, active, field, question, old, selected['value'], selected['evidence']))
            continue
        alternatives = [row for identity, row in identities.items() if identity != _value_identity(old['value'])]
        if len(alternatives) > 4:
            return None
        pending.append({'field': field, 'question': question, 'previous': old, 'items': items, 'options': alternatives,
                        'statements': options, 'citation': source['citation'], 'source_sha256': source['sha256']})
    if not (declined or pending or resolution or prior or out_of_scope or delivery_only_ok):
        return None
    return remaining, revisions, {'declined_proposals': declined, 'pending_revisions': pending,
                                  'revision_resolution': resolution, 'out_of_scope_revisions': out_of_scope}


def _apply_revisions(result, proof, complete, plan, revisions, source):
    recheck = {number for row in revisions for number in row['recheck_items']}
    revised = {number: row['value'] for row in revisions for number in row['collected_items']}
    affected = recheck | revised.keys()
    for index, item in enumerate(result['items']):
        if item['id'] in recheck:
            result['items'][index] = {**item, 'state': 'pending', 'evidence_steps': [],
                                      'note': f'依 {source["citation"]} 的更正重新核對此項。'}
            complete.pop(item['id'], None)
    complete.update(revised)
    proof['retained_items'] = [number for number in proof['retained_items'] if number not in affected]
    if 'evidence_requirements' in result:
        result['evidence_requirements'] = [row if row['id'] in recheck else current for row, current in zip(
            plan['evidence_requirements'], result['evidence_requirements'], strict=True)]
        result['evidence_matches'] = [row for row in result['evidence_matches'] if row['id'] not in recheck]
    proof['revisions'] = revisions


def replay(db, args, prior, after, goal, guidance, *, withdrawals_present, delivery_present=False):
    keys = args.keys() - {'delivery_mode'}
    if (keys not in ({'scope_correction'}, {'scope_correction', 'ask_sequence'})
            or ('delivery_mode' in args and (args['delivery_mode'] is not True or not delivery_present))):
        raise ValueError('已保存的工作範圍更正格式不符。')
    source = next((item for item in guidance if item['citation'] == args['scope_correction']), None)
    if source is None:
        raise ValueError('工作範圍更正缺少使用者原話。')
    start = source['notes_start']
    end = start + len(goal_guidance_sources.note(source['content'], source['steps_seen'], source['recorded_at']))
    previous = work_plan.conditions({**goal, 'notes': goal['notes'][:start]})
    scope = work_plan.conditions({**goal, 'notes': goal['notes'][:end]})
    waiting = 'ask_sequence' in args
    delivery = delivery_preference.verify_plan_guidance(db, goal['id'], source) if 'delivery_mode' in args else None
    expected = observe(prior, source, scope, previous, waiting=waiting, delivery=delivery)
    if expected is None:
        raise ValueError('工作範圍更正與原清單或保存步驟不一致。')
    if waiting:
        proof = expected['scope_change']
        ask_sequence = args['ask_sequence']
        if type(ask_sequence) is not int or ask_sequence != proof['ask_sequence']:
            raise ValueError('工作範圍更正缺少對應的原問題。')
        record = db.execute('SELECT guidance_number,guidance_sha256 FROM autonomous_question_withdrawals '
                            'WHERE goal_id=? AND ask_sequence=?', (goal['id'], ask_sequence)).fetchone() if withdrawals_present else None
        if record != (source['number'], source['sha256']):
            raise ValueError('工作範圍更正缺少相符的問題撤下紀錄。')
        successor = proof['successor']
        if successor and (not after or after['action']['tool'] != 'ask' or after['action']['args'] != successor
                          or after['observation'].get('question') != successor['question']
                          or (delivery and after['observation'].get('goal_conditions_sha256') != scope)):
            raise ValueError('工作範圍更正缺少相符的保留問題。')
    return expected


def replay_reply(db, args, prior, after, goal, replies, *, delivery_present=False):
    version = args.get('scope_reply_version', 1)
    keys = {'scope_reply', 'scope_reply_version'} if version in (2, 3, 4, 5) else {'scope_reply'}
    if args.keys() != keys or type(version) is not int or version not in (1, 2, 3, 4, 5):
        raise ValueError('已保存的工作清單答覆格式不符。')
    source = next((item for item in replies if item['citation'] == args['scope_reply']), None)
    if source is None:
        raise ValueError('工作清單答覆缺少使用者原話。')
    start = source['notes_start']
    end = start + len(goal_reply_sources.note(source['question'], source['answer']))
    previous = work_plan.conditions({**goal, 'notes': goal['notes'][:start]})
    scope = work_plan.conditions({**goal, 'notes': goal['notes'][:end]})
    delivery = None
    if version == 5:
        if not delivery_present:
            raise ValueError('工作清單答覆缺少交付方式紀錄。')
        delivery = delivery_preference.verify_plan_reply(db, goal['id'], source)
    expected = observe_reply(prior, source, scope, previous, version=version, delivery=delivery)
    if expected is None:
        raise ValueError('工作清單答覆與原問題或保存來源不一致。')
    successor = expected['scope_reply']['successor']
    if successor and (not after or after['action']['tool'] != 'ask' or after['action']['args'] != successor
                      or after['observation'].get('question') != successor['question']
                      or after['observation'].get('goal_conditions_sha256') != scope):
        raise ValueError('工作清單答覆缺少相符的保留問題。')
    return expected
