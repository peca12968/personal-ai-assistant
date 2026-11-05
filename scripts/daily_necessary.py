"""Exact local checks for a closed natural-language prerequisite request."""
import json
import re

from assistant import chat_evidence, evidence_logic, local_answer_receipts
from scripts import daily_prerequisite_order

MODE = 'local_calculation'
FIELD = 'necessary_conditions'
V1 = 'daily_necessary_plan_v1'
V2 = 'daily_necessary_plan_v2'
REVISION_PLAN = 'daily_necessary_revision_v1'
DISPUTE_PLAN = 'daily_necessary_dispute_v1'
UPDATE_PLANS = (REVISION_PLAN, DISPUTE_PLAN)
INQUIRY_PLAN = 'daily_necessary_inquiry_v1'
COST_PLAN = 'daily_necessary_check_order_v1'
READ_ONLY_PLANS = (INQUIRY_PLAN, COST_PLAN)
MAX_REVISIONS = 8
REVISION_PREFIX = re.compile(r'(?:請)?沿用上一輪(?:必要)?條件[：:，,]\s*')
REVISION = re.compile(REVISION_PREFIX.pattern + r'([^。！？?\r\n，,；;：:]{2,100}?)'
                      r'(?:[。；;，,]\s*(?:請)?(?:重算|重新核對|只(?:回|回覆|回答|輸出)結論))?[。！]?')
INQUIRY = re.compile(rf'(?:{REVISION_PREFIX.pattern})?'
                     r'(?:還要(?:核對|查)什麼|哪些條件(?:還要|仍要)核對)[？?。]?')
REPORT = (r'([\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9_.-]{0,23})'
          r'回報「([^「」。！？?\r\n，,；;：:]{2,55})」')
DISPUTE = re.compile(rf'{REVISION_PREFIX.pattern}{REPORT}(?:[，,]\s*{REPORT})?'
                     r'[。；;]\s*(?:請)?保留分歧[。！]?')
NAME = r'[\u4e00-\u9fff]{1,24}?'
COST_PREFIX = re.compile(rf'{REVISION_PREFIX.pattern}檢查時間[：:]\s*')
COST_REQUEST = re.compile(rf'{COST_PREFIX.pattern}([^。；;？?\r\n]{{1,300}})'
                          r'[。；;]\s*(?:請)?先查哪(?:一)?項最省時間[？?。]?')
COST_ITEM = re.compile(rf'({NAME})\s*([1-9][0-9]{{0,2}})\s*分鐘')
RULE = re.compile(rf'(?P<verb>使用|啟用|啟動|開啟|進入)(?P<target>{NAME})'
                  r'(?P<rule>要有|必須有|必須要有|需要同時有|必須同時有|須同時具備|的必要且充分條件是同時具備)'
                  r'\s*(?P<terms>.+)')
TYPES = {
    '有效': (r'已(?:確認|確定)?有效', r'已(?:確認|確定)?(?:失效|作廢|撤銷)'),
    '已校驗': (r'已(?:確認|確定)?(?:校驗|通過校驗)', r'已(?:確認|確定)(?:未校驗|未通過校驗)'),
    '已核准': (r'已(?:確認|確定)?核准', r'已(?:確認|確定)?(?:駁回|撤銷核准)'),
    '已開啟': (r'已(?:確認|確定)?開啟', r'已(?:確認|確定)?關閉'),
    '可用': (r'已(?:確認|確定)可用', r'已(?:確認|確定)(?:不可用|不能使用)'),
}
TERM = re.compile(rf'({"|".join(TYPES)})的({NAME})')
PENDING = r'(?:尚未|還沒)(?:檢查|核實|測試|確認|校驗)'
UNBOUND = re.compile(r'^(?:它|這個|那個|上述|前者|後者|全部|同一個)$|或|和|與|並且|以及|'
                     r'之一|任一|任何|至少|除非|可能|大概|若|如果|假設|照舊|前文|上述|附件|但是|從未|並非|不|未|無|'
                     r'必要|充分|必須|需要|通常|原則上|基本上|理論上|大抵|或許')
RULE_MODIFIER = re.compile(r'(?:單單|[只僅但須也還仍想再就才])$')
OUTPUT = re.compile(r'(?:請)?只(?:回|回覆|回答|輸出|給)(?:最終|最後)?(?:結論|標籤)')
LABEL = re.compile(r'([^「」『』]{1,24}?)回(?:「([\u4e00-\u9fffA-Za-z0-9]{1,16})」|'
                   r'『([\u4e00-\u9fffA-Za-z0-9]{1,16})』)')
MEANINGS = {
    'supported': r'確定(?:可用|能(?:用|使用|進入|開啟|啟用|啟動))',
    'refuted': r'確定(?:不可用|不能(?:用|使用|進入|開啟|啟用|啟動))',
    'undetermined': r'尚(?:不能|無法)判定|資料不足|證據不足',
}

CONJUNCTION = r'和|與|、|並且|以及'
OUTSIDE_CONJUNCTION = re.compile(rf'(?:{CONJUNCTION})(?![^()（）]*[)）])')
ALTERNATIVE = re.compile(r'或者|或')
GROUP = re.compile(r'\(([^()（）]+)\)|（([^()（）]+)）')


def alternative_groups(text):
    # Mixed AND/OR requires a whole parenthesized OR group.
    outside = GROUP.sub('', text)
    if re.search(r'[()（）]', outside):
        return None
    if text == outside and not re.search(CONJUNCTION, text):
        members = ALTERNATIVE.split(text)
        return [members] if 2 <= len(members) <= 4 else None
    groups = []
    for part in OUTSIDE_CONJUNCTION.split(text):
        part = part.strip()
        group = GROUP.fullmatch(part)
        if group:
            members = ALTERNATIVE.split(group[1] or group[2])
            if not 2 <= len(members) <= 4:
                return None
            groups.append(members)
        else:
            groups.append([part])
    return groups if 1 <= sum(len(g) > 1 for g in groups) <= 2 else None


def labels(text):
    result = {}
    for clause in re.split(r'[，,；;]', text):
        match = LABEL.fullmatch(clause.strip())
        if not match:
            return None
        kind = next((key for key, pattern in MEANINGS.items() if re.fullmatch(pattern, match[1])), None)
        value = match[2] or match[3]
        if not kind or kind in result or value in result.values():
            return None
        result[kind] = value
    return result if set(result) == set(MEANINGS) else None


def observation(clause, condition):
    name = re.escape(condition['name'])
    prop = condition['property']
    yes, no = TYPES[prop]
    if re.fullmatch(rf'已(?:確認|確定)沒有{prop}的{name}|唯一的{name}(?:{no})', clause):
        return 'false'
    if re.fullmatch(rf'(?:唯一的)?{name}(?:{yes})', clause):
        return 'true'
    if re.fullmatch(rf'(?:唯一的)?{name}(?:{PENDING})', clause):
        return 'unknown'
    return None


def parse(question):
    return parse_version(question, V1) or parse_version(question, V2)


def parse_version(question, version):
    if version not in (V1, V2):
        return None
    if not isinstance(question, str) or not 1 <= len(question) <= 1600:
        return None
    body = question.strip().rstrip('。！')
    if re.search(r'[！？?]', body):
        return None
    sentences = [s.strip() for s in re.split(r'[。\r\n]+', body)]
    if not 4 <= len(sentences) <= (8 if version == V1 else 11) or not OUTPUT.fullmatch(sentences[-1]):
        return None
    rule = RULE.fullmatch(sentences[0])
    options = labels(sentences[-2])
    if not rule or not options or UNBOUND.search(rule['target']) or RULE_MODIFIER.search(rule['target']):
        return None
    if version == V1:
        groups = [[part] for part in re.split(CONJUNCTION, rule['terms'])]
    else:
        groups = alternative_groups(rule['terms'])
        if groups is None:
            return None
        if '(' not in rule['terms'] and '（' not in rule['terms'] and '同時' in rule['rule']:
            return None
    conditions, names = {}, []
    for group in groups:
        names.append([])
        for part in group:
            term = TERM.fullmatch(part.strip())
            if not term or term[2] in conditions or UNBOUND.search(term[2]):
                return None
            conditions[term[2]] = dict(name=term[2], property=term[1], requirement=part.strip())
            names[-1].append(term[2])
    if not 2 <= len(conditions) <= (4 if version == V1 else 8):
        return None
    for sentence in sentences[1:-2]:
        for part in re.split(r'[，,；;]', sentence):
            clause = part.strip()
            matches = [(condition, status) for condition in conditions.values()
                       if (status := observation(clause, condition))]
            if len(matches) != 1 or 'observation' in matches[0][0]:
                return None
            condition, status = matches[0]
            condition.update(observation=clause, state=status)
    if any('observation' not in condition for condition in conditions.values()):
        return None
    plan = dict(schema=version, request=question, request_sha256=evidence_logic.digest(question),
                action=rule['verb'] + rule['target'], rule=sentences[0],
                sufficient=rule['rule'].startswith('的必要且充分'),
                conditions=list(conditions.values()), labels=options)
    if version == V2:
        plan['groups'] = names
    return plan


def matches_revision(question):
    return isinstance(question, str) and bool(REVISION_PREFIX.match(question))


def matches_inquiry(question):
    return isinstance(question, str) and len(question) <= 150 and bool(INQUIRY.fullmatch(question))


def valid_source(source):
    return (isinstance(source, dict) and set(source) == {'answer_id', 'sha256'}
            and type(source['answer_id']) is int and source['answer_id'] > 0
            and isinstance(source['sha256'], str) and bool(re.fullmatch(r'[0-9a-f]{64}', source['sha256'])))


def match_observation(conditions, clause):
    matches = [(condition, status) for condition in conditions
               if (status := observation(clause, condition))]
    if len(matches) != 1:
        raise ValueError('請用原本的完整條件名稱，明確寫出一項已知或待查狀態。')
    return matches[0]


def preserve_dispute(conditions, match):
    source, clause, other_source, other_clause = match.groups()
    condition, status = match_observation(conditions, clause)
    if condition.get('conflict'):
        raise ValueError('這項條件已有待核對的分歧。請在重新核對後更正它。')
    if other_source is None:
        other_status = condition['state']
        other_clause = condition['observation']
    else:
        other_condition, other_status = match_observation(conditions, other_clause)
        if condition is not other_condition or source.casefold() == other_source.casefold():
            raise ValueError('請提供不同來源針對同一項條件的相反回報。')
    if {status, other_status} != {'true', 'false'}:
        raise ValueError('保留分歧需要同一條件的一正一反。可提供兩個來源，或一筆與既有確認相反的回報。')
    reports = [dict(source=source, observation=clause, state=status),
               dict(source=other_source, observation=other_clause, state=other_status)]
    condition.update(state='unknown', conflict=reports)


def effective(plan):
    if not isinstance(plan, dict):
        raise ValueError('必要條件規約與原問不符。')
    if plan.get('schema') in READ_ONLY_PLANS:
        if (set(plan) != {'schema', 'request', 'request_sha256', 'basis', 'source'}
                or not isinstance(plan['request'], str)
                or evidence_logic.digest(plan['request']) != plan['request_sha256']
                or not valid_source(plan['source'])
                or not isinstance(plan['basis'], dict)
                or plan['basis'].get('schema') not in (V1, V2, *UPDATE_PLANS)):
            raise ValueError('待核對項目的原問或來源不符。')
        state = effective(plan['basis'])
        if plan['schema'] == COST_PLAN:
            inspection_minutes(plan['request'], state)
        elif not matches_inquiry(plan['request']):
            raise ValueError('待核對項目的原問或來源不符。')
        return state
    if plan.get('schema') not in UPDATE_PLANS:
        if parse_version(plan.get('request'), plan.get('schema')) != plan or type(plan['sufficient']) is not bool:
            raise ValueError('必要條件規約與原問不符。')
        return plan

    if set(plan) != {'schema', 'request', 'request_sha256', 'original', 'revisions', 'source'}:
        raise ValueError('沿用條件的規約格式不符。')
    original, revisions, source = plan['original'], plan['revisions'], plan['source']
    if (not isinstance(original, dict) or set(original) != {'request', 'schema'}
            or not isinstance(revisions, list) or not 1 <= len(revisions) <= MAX_REVISIONS
            or any(not isinstance(text, str) or len(text) > 150 for text in revisions)
            or revisions[-1] != plan['request'] or evidence_logic.digest(revisions[-1]) != plan['request_sha256']
            or not valid_source(source)):
        raise ValueError('沿用條件的來源或更正範圍不符。')
    state = parse_version(original['request'], original['schema'])
    if state is None:
        raise ValueError('原始條件規則無法核對。')
    has_dispute = False
    for request in revisions:
        if plan['schema'] == DISPUTE_PLAN and (dispute := DISPUTE.fullmatch(request)):
            preserve_dispute(state['conditions'], dispute)
            has_dispute = True
            continue
        match = REVISION.fullmatch(request)
        if not match:
            raise ValueError('每次只能更正一項已列出的必要條件。')
        clause = match[1].strip()
        condition, status = match_observation(state['conditions'], clause)
        if 'conflict' in condition and status == 'unknown':
            raise ValueError('這項條件仍有分歧，請在確認結果後更正。')
        condition.update(observation=clause, state=status)
        condition.pop('conflict', None)
    if plan['schema'] == DISPUTE_PLAN and not has_dispute:
        raise ValueError('必要條件紀錄缺少可核對的分歧。')
    return state


def revise(question, parent, source):
    if parent['schema'] in READ_ONLY_PLANS:
        parent = parent['basis']
    if parent['schema'] in UPDATE_PLANS:
        original = parent['original']
        earlier = parent['revisions']
    else:
        original = {key: parent[key] for key in ('request', 'schema')}
        earlier = []
    if len(earlier) >= MAX_REVISIONS:
        raise ValueError('已達沿用條件的八次更正上限，請重新列出完整條件。')
    schema = DISPUTE_PLAN if parent['schema'] == DISPUTE_PLAN or DISPUTE.fullmatch(question) else REVISION_PLAN
    plan = dict(schema=schema, request=question, request_sha256=evidence_logic.digest(question),
                original=original, revisions=[*earlier, question], source=source)
    effective(plan)
    return plan


def carry_forward(question, parent, source):
    order_requested = COST_PREFIX.match(question)
    if not order_requested and not matches_inquiry(question):
        return revise(question, parent, source)
    if parent['schema'] == COST_PLAN:
        raise ValueError('上一則已列出檢查順序。確認一項後可沿用條件重算。')
    if not order_requested and parent['schema'] == INQUIRY_PLAN:
        raise ValueError('上一則已列出待核對項目。確認一項後可沿用條件重算。')
    basis = parent['basis'] if parent['schema'] == INQUIRY_PLAN else parent
    schema = COST_PLAN if order_requested else INQUIRY_PLAN
    plan = dict(schema=schema, request=question, request_sha256=evidence_logic.digest(question),
                basis=basis, source=source)
    effective(plan)
    return plan


def inspection_groups(state):
    observed = {c['name']: c['state'] for c in state['conditions']}
    groups = state.get('groups', [[name] for name in observed])
    blockers = []
    pending = []
    for group in groups:
        if any(observed[name] == 'true' for name in group):
            continue
        unknown = [name for name in group if observed[name] == 'unknown']
        if unknown:
            pending.append(unknown)
        else:
            blockers.append(group)
    return blockers, pending


def inspection_minutes(request, state):
    match = COST_REQUEST.fullmatch(request)
    if not match:
        raise ValueError('請用「檢查時間：條件名稱3分鐘，另一項5分鐘。先查哪一項最省時間？」列出時間。')
    names = {condition['name'] for condition in state['conditions']}
    minutes = {}
    for part in re.split(r'[，,]', match[1]):
        item = COST_ITEM.fullmatch(part.strip())
        if not item or item[1] not in names or item[1] in minutes:
            raise ValueError('請用原本的完整條件名稱，各填一次 1 至 999 的整數分鐘。')
        minutes[item[1]] = int(item[2])
    blockers, pending = inspection_groups(state)
    if not blockers:
        missing = [name for group in pending for name in group if name not in minutes]
        if missing:
            raise ValueError('請補上這些待查項目的檢查時間：' + '、'.join(missing) + '。')
    return minutes


def inquiry_result(state, status):
    blockers, pending = inspection_groups(state)
    if status != 'undetermined':
        pending = []
    return dict(conclusion=status, blockers=blockers, refute=pending,
                support=pending if state['sufficient'] else [],
                needs_sufficiency=status == 'undetermined' and not state['sufficient'])


def inquiry_text(state, result):
    labels = state['labels']
    names = {c['name']: c['requirement'] for c in state['conditions']}
    lines = [f'目前結論「{labels[result["conclusion"]]}」。']
    if result['blockers']:
        lines += ['下列各組已確認全部不符合要求，任一組就足以作出目前結論。']
        lines += ['- ' + '、'.join(names[name] for name in group) for group in result['blockers']]
    elif result['conclusion'] == 'supported':
        lines += ['現有觀察已足以作出結論。']
    if result['refute']:
        lines += [f'下列任一組的待查項目若全數確認不符合要求，就可判定「{labels["refuted"]}」。']
        lines += ['- ' + '、'.join(names[name] for name in group) for group in result['refute']]
    if result['support']:
        lines += [f'下列每組若都至少有一項確認符合要求，就可判定「{labels["supported"]}」。']
        lines += ['- ' + ' 或 '.join(names[name] for name in group) for group in result['support']]
    if result['needs_sufficiency']:
        lines += [f'即使列出的必要條件全數滿足，要確定「{state["action"]}」仍需要充分條件或直接證據。']
    return '\n'.join(lines)


def dispute_text(conflicts):
    lines = ['下列回報互相矛盾，對應條件保留為未定。']
    for item in conflicts:
        descriptions = [f'{report["source"] or "既有觀察"}「{report["observation"]}」'
                        for report in item['reports']]
        lines.append(f'- {item["name"]} · ' + '／'.join(descriptions))
    return '\n'.join(lines)


def evaluate(plan, cancel=None):
    state = effective(plan)
    action = '「' + state['action'] + '」'
    items = [f'「條件{index} {c["name"]}{c["property"]}」'
             for index, c in enumerate(state['conditions'], 1)]
    group_rules = []
    required = items
    if state['schema'] == V2:
        required = []
        atoms = {c['name']: atom for c, atom in zip(state['conditions'], items, strict=True)}
        for index, names in enumerate(state['groups'], 1):
            if len(names) == 1:
                required.append(atoms[names[0]])
                continue
            group = f'「備援組{index}至少一項滿足」'
            required.append(group)
            group_rules.extend(f'若{atoms[name]}，則{group}' for name in names)
            absent = '並且'.join('並非' + atoms[name] for name in names)
            group_rules.append(f'若{absent}，則並非{group}')
    combined = '並且'.join(required)
    clauses = [f'條件核對：只有{combined}，才會{action}']
    if state['sufficient']:
        clauses.append(f'若{combined}，則{action}')
    clauses.extend(group_rules)
    for atom, condition in zip(items, state['conditions'], strict=True):
        if condition['state'] != 'unknown':
            clauses.append('已確認' + ('並非' if condition['state'] == 'false' else '') + atom)
    canonical = '。'.join(clauses) + '。能否確定' + action + '？'
    _, checked = chat_evidence.evaluate(chat_evidence.parse_request(canonical), cancel)
    status = checked['analysis']['status']
    schema = {V1: 'daily_necessary_receipt_v1', V2: 'daily_necessary_receipt_v2',
              REVISION_PLAN: 'daily_necessary_revision_receipt_v1',
              DISPUTE_PLAN: 'daily_necessary_dispute_receipt_v1',
              INQUIRY_PLAN: 'daily_necessary_inquiry_receipt_v1',
              COST_PLAN: 'daily_necessary_check_order_receipt_v1'}[plan['schema']]
    receipt = dict(schema=schema, plan=plan, status='complete', model_called=False,
                   analysis=checked['analysis'], canonical=canonical)
    answer = state['labels'][status]
    if plan['schema'] == INQUIRY_PLAN:
        result = inquiry_result(state, status)
        answer = inquiry_text(state, result)
        receipt['inquiry'] = result
    if plan['schema'] == COST_PLAN:
        minutes = inspection_minutes(plan['request'], state)
        order = daily_prerequisite_order.plan_order(
            inspection_groups(state)[1], minutes, status, state['sufficient'])
        receipt['check_order'] = order
        answer = daily_prerequisite_order.answer_text(order, state['labels'])
    conflicts = [dict(name=c['name'], reports=c['conflict']) for c in state['conditions'] if 'conflict' in c]
    if conflicts:
        receipt['conflicts'] = conflicts
        if plan['schema'] in READ_ONLY_PLANS:
            answer += '\n' + dispute_text(conflicts)
        elif DISPUTE.fullmatch(plan['request']):
            answer = f'目前結論「{answer}」。\n' + dispute_text(conflicts)
    receipt['output'] = answer
    return answer, receipt


def validate(receipt, delivered, question):
    if not isinstance(receipt, dict) or not isinstance(receipt.get('plan'), dict):
        raise ValueError('必要條件紀錄缺少規約。')
    output, expected = evaluate(receipt['plan'])
    if (receipt['plan']['request'] != question or output != delivered
            or json.dumps(receipt, sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True)):
        raise ValueError('必要條件紀錄與原問或推論不符。')
    return receipt


def previous_id(db, sid, before):
    row = db.execute('SELECT id FROM messages WHERE session_id=? AND (? IS NULL OR id<?) '
                     'ORDER BY id DESC LIMIT 1', (sid, before, before)).fetchone()
    return row['id'] if row else None


def saved(db, mid):
    rows = db.execute('''SELECT m.session_id, r.user_message_id,
         CASE WHEN length(CAST(c.payload AS BLOB))<=? THEN c.payload END AS payload
         FROM answer_contexts c JOIN messages m ON m.id=c.message_id
         JOIN answer_runs r ON r.answer_message_id=m.id WHERE m.id=? LIMIT 2''',
         (local_answer_receipts.MAX_JSON_BYTES, mid)).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError('必要條件來源缺少唯一回答流程。')
    row = rows[0]
    context = local_answer_receipts.load_context(row['payload'])
    if not isinstance(context, dict) or context.get('response_mode') != MODE or FIELD not in context:
        return None
    if not 0 < row['user_message_id'] < mid:
        raise ValueError('必要條件來源的訊息順序不符。')
    receipt = local_answer_receipts.checked_row(db, mid, context, MODE, FIELD, validate)
    source = dict(answer_id=mid, sha256=evidence_logic.digest(row['payload']))
    return receipt, row, source


def verified_chain(db, mid):
    first = saved(db, mid)
    if first is None:
        return None
    receipt, row, _ = first
    # Each state may have an inquiry followed by one check order.
    for _ in range(3 * MAX_REVISIONS + 2):
        plan = receipt['plan']
        if plan['schema'] in (V1, V2):
            return first
        predecessor = previous_id(db, row['session_id'], row['user_message_id'])
        parent = saved(db, predecessor)
        if parent is None:
            raise ValueError('沿用條件的前一筆回答已不存在。')
        receipt, row, source = parent
        if carry_forward(plan['request'], receipt['plan'], source) != plan:
            raise ValueError('沿用條件的來源已變更。')
    if receipt['plan']['schema'] not in (V1, V2):
        raise ValueError('必要條件缺少可核對的原始規則。')
    return first


def read(store, mid):
    with store.connect() as db:
        db.execute('BEGIN')
        record = verified_chain(db, mid)
        return record[0] if record else None


def previous(store, sid, before=None, *, required=True):
    with store.connect() as db:
        db.execute('BEGIN')
        record = verified_chain(db, previous_id(db, sid, before))
        if record is None:
            if required:
                raise ValueError('上一則回答沒有可沿用的必要條件紀錄。')
            return None
        receipt, _, source = record
        return receipt['plan'], source


def describe(receipt):
    plan = receipt['plan']
    resolved = effective(plan)
    states = {'true': '已確認滿足', 'false': '已確認不滿足', 'unknown': '尚未核實'}
    lines = [plan['request']]
    if plan['schema'] in (*UPDATE_PLANS, *READ_ONLY_PLANS):
        basis = plan['basis'] if plan['schema'] in READ_ONLY_PLANS else plan
        updates = '條件更新' if basis['schema'] == DISPUTE_PLAN else '明確更正'
        lines += ['', f'沿用上一則回答 #{plan["source"]["answer_id"]}',
                  f'已核對 {len(basis.get("revisions", []))} 次{updates}', '原始問題：' + resolved['request']]
    observation_title = '沿用的觀察' if plan['schema'] in READ_ONLY_PLANS else (
        '沿用及更新後的觀察' if plan['schema'] in UPDATE_PLANS else '採用的本輪觀察')
    lines += ['', '規則：' + resolved['rule'], observation_title]
    for condition in resolved['conditions']:
        if 'conflict' in condition:
            lines.append(f'{condition["name"]} → 回報有分歧（先前觀察：{condition["observation"]}）')
        else:
            lines.append(f'{condition["observation"]} → {states[condition["state"]]}')
    if resolved['schema'] == V2:
        observed = {c['name']: c['state'] for c in resolved['conditions']}
        for group in resolved['groups']:
            if len(group) > 1:
                values = {observed[name] for name in group}
                state = 'true' if 'true' in values else 'false' if values == {'false'} else 'unknown'
                lines.append('備援（' + '或'.join(group) + '） → ' + states[state])
    if receipt.get('conflicts') and plan['schema'] == DISPUTE_PLAN and not DISPUTE.fullmatch(plan['request']):
        lines += ['', dispute_text(receipt['conflicts'])]
    lines.extend(['', f'相容情況數：{receipt["analysis"]["compatible_worlds"]}', '交付：' + receipt['output']])
    if receipt.get('check_order') and 'check' in receipt['check_order']['tree']:
        lines += ['', '完整檢查順序']
        lines.extend(daily_prerequisite_order.tree_lines(receipt['check_order']['tree'], resolved['labels']))
    return '\n'.join(lines)
