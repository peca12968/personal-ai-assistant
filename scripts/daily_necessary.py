"""Exact local checks for a closed natural-language prerequisite request."""
import json
import re

from assistant import chat_evidence, evidence_logic, local_answer_receipts
from scripts import daily_observation_words, daily_prerequisite_order

MODE = 'local_calculation'
FIELD = 'necessary_conditions'
V1 = 'daily_necessary_plan_v1'
V2 = 'daily_necessary_plan_v2'
V3 = 'daily_necessary_roster_plan_v3'
V4 = 'daily_necessary_grouped_roster_plan_v4'
V5 = 'daily_necessary_physical_plan_v5'
V6 = 'daily_necessary_physical_codes_plan_v6'
V7 = 'daily_necessary_declared_plan_v7'
V8 = 'daily_necessary_scoped_declared_plan_v8'
V9 = 'daily_necessary_object_property_plan_v9'
V10 = 'daily_necessary_natural_property_plan_v10'
BOUND_PLANS = (V9, V10)
DECLARED_PLANS = (V7, V8, *BOUND_PLANS)
INITIAL_PLANS = (V1, V2, V3, V4, V5, V6, *DECLARED_PLANS)
REVISION_PLAN = 'daily_necessary_revision_v1'
DISPUTE_PLAN = 'daily_necessary_dispute_v1'
ROUND_PLAN = 'daily_necessary_new_round_v1'
UPDATE_PLANS = (REVISION_PLAN, DISPUTE_PLAN, ROUND_PLAN)
INQUIRY_PLAN = 'daily_necessary_inquiry_v1'
COST_PLAN = 'daily_necessary_check_order_v1'
READ_ONLY_PLANS = (INQUIRY_PLAN, COST_PLAN)
MAX_REVISIONS = 8
MAX_ROUND_CHARS = 560
ROUND_PREFIX = re.compile(r'(?:請)?沿用上一輪(?:的)?規則[：:，,]\s*開始新一輪核對(?=$|[。！\r\n])')
ROUND_START = re.compile(ROUND_PREFIX.pattern + r'(?:[。\r\n]\s*(?P<observations>[^！!?？\r\n]+?))?[。！]?')
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
ROSTER_ORDINALS = '一二三四五六七八'
ROSTER_COUNTS = dict(zip('兩二三四五六七八', (2, 2, 3, 4, 5, 6, 7, 8), strict=True))
ROSTER_COUNTS.update({str(n): n for n in range(2, 9)})
ROSTER_RULE = re.compile(
    rf'(?P<scope>今天|本次|目前)?\s*(?P<count>[兩二三四五六七八2-8])\s*位'
    r'(?:講者|講員|參與者|成員|訪客|工作人員|操作者|申請人|隊員)(?:都|各自)'
    rf'(?:須有|必須有|要有|需要有)(?:一份)?有效的?(?P<credential>[\u4e00-\u9fff]{{1,18}}?)'
    rf'才(?:能|可以|可)(?P<action>{NAME})')
ROSTER_OUTPUT = re.compile(r'(?:請)?只(?:回|回覆|回答|輸出)(?:一個)?(?:結論|標籤)')
GROUPED_SCOPE = r'今天|本次|目前'
GROUPED_COUNT = r'[兩二三四五六七八2-8]'
GROUPED_ORDINAL = r'第([一二三四五六七八1-8])'
GROUPED_PERSON = r'第[一二三四五六七八1-8]'
GROUPED_PEOPLE = rf'(?:{GROUPED_PERSON}[名位]?(?:、|及|與|和))*{GROUPED_PERSON}[名位]'
GROUPED_INTRO = re.compile(rf'(?:請)?只(?:按|依)(?P<scope>{GROUPED_SCOPE})的(?:模擬)?規則判斷')
GROUPED_RULE = re.compile(
    rf'(?P<count>{GROUPED_COUNT})[名位](?:製圖員|維護員|測試員|檢測員|操作員|講者|參與者|成員|訪客|工作人員|操作者|申請人|隊員)'
    rf'(?:必須各自|都必須)持有(?P<scope>{GROUPED_SCOPE})(?:一張|一份|一組)?有效的'
    r'(?P<credential>[\u4e00-\u9fff]{0,14}?(?P<kind>使用碼|通行碼|門禁碼|入場證|通行證|操作證|工作證|識別證))'
    rf'(?:[，,]\s*(?P<complete>{GROUPED_COUNT})[名位]都有效就是全部要求)?'
)
GROUPED_MEANINGS = dict(supported=r'(?:確定)?(?:能|可|可以)使用',
                        refuted=r'(?:確定)?(?:不能|不可)使用',
                        undetermined=MEANINGS['undetermined'] + r'|無法判定')
PHYSICAL_RULE = re.compile(rf'(?P<action>(?:啟用|使用|開啟|啟動|試行|試轉){NAME})須同時確認'
                           r'(?P<terms>[^，,]+)[，,]\s*兩項(?P<complete>就是全部要求|都是必要條件)')
PHYSICAL_TERMS = {
    '接妥': ('接頭|插頭|電源線', '接妥', r'已接妥|接妥', r'已(?:經)?卸下|確定(?:沒有|尚未|未)接妥'),
    '開啟': ('閥|閘門', '開啟|開', r'已(?:開啟|全開|開)', r'已關閉|確定(?:仍)?關閉'),
    '扣緊': ('護罩|卡扣|扣具', '扣緊', r'已(?:扣緊|鎖緊)', r'確定(?:沒有|沒)扣上|確定(?:仍)?未扣緊'),
    '固定': ('光罩|外罩|護罩|支架', '固定', r'已固定(?:好|妥)?', r'確定(?:仍)?未固定'),
    '取出': ('插銷|固定銷|旁通塞', '取出|拔出', r'已(?:完全|整支)?(?:取出|拔出)',
             r'確定仍(?:留在座內|插在底座)'),
}
PHYSICAL_CODES = re.compile(r'(?:請)?只(?:回|輸出)\s*(.+)')
PHYSICAL_LABEL = re.compile(r'(?P<code>[A-Za-z0-9]{1,12})\s*(?:代表|表示)\s*(?P<meaning>[\u4e00-\u9fff]{1,12})')
SEPARATE_PHYSICAL_CODES = re.compile(r'(?:請)?只輸出(?:指定)?代碼')
SEPARATE_PHYSICAL_LABEL = re.compile(r'(?P<meaning>[\u4e00-\u9fff]{1,12})\s*回\s*(?P<code>[A-Za-z0-9]{1,12})')
PHYSICAL_MEANINGS = {
    'supported': '能|確定能|可行|確定可行',
    'refuted': '不能|確定不能|不可行|確定不可行',
    'undetermined': '未定|待查|待確認|無法判定',
}
SEPARATE_PHYSICAL_MEANINGS = {**PHYSICAL_MEANINGS,
                              'undetermined': PHYSICAL_MEANINGS['undetermined'] + r'|尚無法判定|資料不足'}
DECLARED_NAME = r'[\u3400-\u9fffA-Za-z0-9][\u3400-\u9fffA-Za-z0-9_ -]{0,39}'
DECLARED_ATOM = rf'「{DECLARED_NAME}」'
DECLARED_GROUP = rf'(?:{DECLARED_ATOM}|（{DECLARED_ATOM}(?:或{DECLARED_ATOM}){{1,2}}）)'
DECLARED_TERMS = re.compile(rf'{DECLARED_GROUP}(?:\s*(?:、|及|和|與|並且)\s*{DECLARED_GROUP}){{0,3}}')
DECLARED_RULE = re.compile(rf'判斷「(?P<action>{DECLARED_NAME})」須同時符合(?P<terms>[^，,]+)[，,]\s*'
                           r'這些(?P<complete>就是全部要求|只是必要條件)')
DECLARED_COST_ITEM = re.compile(rf'「({DECLARED_NAME})」\s*([1-9][0-9]{{0,2}})\s*分鐘')
CURRENT_OBSERVATION = re.compile(r'(?:本輪|當次|這次)\s*(.+)')
EARLIER_OBSERVATION = re.compile(r'(?:上一次|上一輪|上輪|上次|昨天|前一日)\s*(.+)')
BOUND_NAME = daily_observation_words.NAME
BOUND_ATOM = rf'「{BOUND_NAME}」的「{BOUND_NAME}」'
BOUND_GROUP = rf'(?:{BOUND_ATOM}|（{BOUND_ATOM}(?:或{BOUND_ATOM}){{1,2}}）)'
BOUND_TERMS = re.compile(rf'{BOUND_GROUP}(?:\s*(?:、|及|和|與|並且)\s*{BOUND_GROUP}){{0,3}}')
BOUND_PAIR = re.compile(rf'「({BOUND_NAME})」的「({BOUND_NAME})」')
BOUND_REPORT = re.compile(rf'核對「({BOUND_NAME})」的(.+)')
BOUND_ITEM = re.compile(rf'「({BOUND_NAME})」(成立|不成立|尚未核對)')
BOUND_STATES = {'成立': 'true', '不成立': 'false', '尚未核對': 'unknown'}
BOUND_COST_ITEM = re.compile(BOUND_PAIR.pattern + r'\s*([1-9][0-9]{0,2})\s*分鐘')
BOUND_COST_REQUEST = re.compile(rf'{COST_PREFIX.pattern}([^。；;？?\r\n]{{1,420}})'
                                r'[。；;]\s*(?:請)?先查哪(?:一)?項最省時間[？?。]?')


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


def labels(text, meanings=MEANINGS):
    result = {}
    for clause in re.split(r'[，,；;]', text):
        match = LABEL.fullmatch(clause.strip())
        if not match:
            return None
        kind = next((key for key, pattern in meanings.items() if re.fullmatch(pattern, match[1])), None)
        value = match[2] or match[3]
        if not kind or kind in result or value in result.values():
            return None
        result[kind] = value
    return result if set(result) == set(meanings) else None


def roster_observation(clause, condition, carried):
    person = condition['participant']
    credential = re.escape(condition['credential'])
    scope = condition['scope']
    subject = person + (rf'(?:{scope})?' if scope and not carried else '')
    if re.fullmatch(rf'{subject}(?:的)?{credential}已(?:確認|確定)有效', clause):
        return 'true'
    if re.fullmatch(rf'{subject}(?:的唯一的?|唯一的){credential}已(?:確認|確定)?(?:撤回|撤銷|作廢|失效)'
                    rf'|{subject}已(?:確認|確定)沒有有效的?{credential}'
                    rf'|{subject}已撤回唯一的{credential}', clause):
        return 'false'
    item = credential if carried else rf'(?:{credential}|文件)'
    if re.fullmatch(rf'{subject}(?:的)?{item}(?:尚未|還沒)(?:核對|核實|檢查|確認)', clause):
        return 'unknown'
    return None


def observation(clause, condition, *, carried=False):
    if 'participant' in condition:
        return roster_observation(clause, condition, carried)
    if condition.get('physical'):
        return physical_observation(clause, condition, carried)
    if 'subject' in condition:
        report = (natural_bound_observations(clause, (condition,)) if 'observation_family' in condition
                  else bound_observations(clause))
        if report and not report[0] and len(report[1]) == 1:
            subject, predicate, state = report[1][0]
            if (subject, predicate) == (condition['subject'], condition['predicate']):
                return state
        return None
    if condition.get('declared'):
        if condition.get('scoped'):
            current = CURRENT_OBSERVATION.fullmatch(clause)
            if current is None:
                return None
            clause = current[1]
        return declared_observation(clause, condition['name'])
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
    return next((plan for version in INITIAL_PLANS if (plan := parse_version(question, version))), None)


def roster_conditions(count, credential, scope):
    return [dict(name=person + credential, property='有效', requirement=f'{person}有有效的{credential}',
                 participant=person, credential=credential, scope=scope)
            for person in (f'第{ordinal}位' for ordinal in ROSTER_ORDINALS[:count])]


def parse_roster(question, sentences):
    if not 4 <= len(sentences) <= 11 or not ROSTER_OUTPUT.fullmatch(sentences[-1]):
        return None
    rule = ROSTER_RULE.fullmatch(sentences[0])
    if not rule or UNBOUND.search(rule['credential']) or UNBOUND.search(rule['action']):
        return None
    action = rule['action']
    scope = rule['scope'] or ''
    credential = rule['credential']
    label_text = sentences[-2]
    if scope:
        label_text = re.sub(rf'^依{scope}的已知條件[，,]\s*', '', label_text)
    action_pattern = '(?:錄音|錄)' if action == '錄音' else re.escape(action)
    meanings = dict(supported=rf'(?:可|確定能|確定可){action_pattern}',
                    refuted=rf'(?:不可|確定不能|確定不可){action_pattern}',
                    undetermined=MEANINGS['undetermined'] + r'|仍有可能但不能確定')
    options = labels(label_text, meanings)
    if not options:
        return None
    conditions = roster_conditions(ROSTER_COUNTS[rule['count']], credential, scope)
    if not bind_observations(conditions, sentences[1:-2]):
        return None
    return dict(schema=V3, request=question, request_sha256=evidence_logic.digest(question),
                action=action, rule=sentences[0], sufficient=False, conditions=conditions, labels=options)


def bind_grouped_roster(conditions, sentences, scope, item):
    people = {condition['participant']: condition for condition in conditions}
    verified = re.compile(rf'(?:{scope})?已(?:核對|確認)({GROUPED_PEOPLE})(?:的)?{item}'
                          r'[，,]\s*(?:全都|都|均)有效')
    subject = re.compile(rf'(?:{scope})?({GROUPED_PEOPLE})(.+)')
    positive = re.compile(rf'(?:(?:的)?{item})?(?:全都|都|均)?已(?:核對|確認|確定)(?:全都|均)?有效')
    negative = re.compile(rf'(?:的)?唯一(?:的)?{item}已(?:確認|確定)?(?:作廢|失效|撤銷|撤回)'
                          rf'|已(?:確認|確定)沒有有效的?{item}')
    pending = re.compile(rf'(?:(?:的)?{item})?(?:尚未|還沒)(?:核對|核實|檢查|確認)')
    for sentence in sentences:
        batch = verified.fullmatch(sentence)
        clauses = [sentence] if batch else re.split(r'[，,；;]', sentence)
        for part in clauses:
            clause = part.strip()
            if batch:
                group, status = batch[1], 'true'
            else:
                match = subject.fullmatch(clause)
                if match is None:
                    return False
                group, detail = match.groups()
                if positive.fullmatch(detail):
                    status = 'true'
                elif negative.fullmatch(detail):
                    status = 'false'
                elif pending.fullmatch(detail):
                    status = 'unknown'
                else:
                    return False
            ordinals = re.findall(GROUPED_ORDINAL, group)
            if status == 'false' and len(ordinals) != 1:
                return False
            for ordinal in ordinals:
                if ordinal.isascii():
                    ordinal = ROSTER_ORDINALS[int(ordinal) - 1]
                condition = people.get(f'第{ordinal}位')
                if condition is None or 'observation' in condition:
                    return False
                condition.update(observation=clause, state=status)
    return all('observation' in condition for condition in conditions)


def parse_grouped_roster(question, sentences):
    intro = GROUPED_INTRO.fullmatch(sentences[0])
    if intro:
        sentences = sentences[1:]
    if not 4 <= len(sentences) <= 11:
        return None
    rule = GROUPED_RULE.fullmatch(sentences[0])
    if not rule or UNBOUND.search(rule['credential']):
        return None
    scope = rule['scope']
    observations = sentences[1:-2]
    if (intro and intro['scope'] != scope) or (not intro and not observations[0].startswith(scope)):
        return None
    count = ROSTER_COUNTS[rule['count']]
    if rule['complete'] and ROSTER_COUNTS[rule['complete']] != count:
        return None
    output, label_text = sentences[-2:]
    if ROSTER_OUTPUT.fullmatch(label_text):
        output, label_text = label_text, output
    options = labels(label_text, GROUPED_MEANINGS)
    if not ROSTER_OUTPUT.fullmatch(output) or not options:
        return None
    credential = rule['credential']
    conditions = roster_conditions(count, credential, scope)
    item = re.escape(credential)
    if rule['kind'] != credential:
        item = rf'(?:{item}|{re.escape(rule["kind"])})'
    if not bind_grouped_roster(conditions, observations, scope, item):
        return None
    return dict(schema=V4, request=question, request_sha256=evidence_logic.digest(question),
                action='使用', rule=sentences[0], sufficient=bool(rule['complete']), conditions=conditions, labels=options)


def physical_observation(clause, condition, carried):
    name = re.escape(condition['name'])
    alias = condition.get('alias')
    if alias and not carried:
        name = rf'(?:{name}|(?<=已確認){alias})'
    _, _, positive, negative = PHYSICAL_TERMS[condition['property']]
    if condition['property'] == '接妥' and condition['name'].endswith('接頭'):
        positive += r'|(?:已)?鎖好'
    subject = rf'(?:已確認|已核對|唯一)?{name}(?:也)?'
    for state, pattern in (
        ('true', positive), ('false', negative),
        ('unknown', r'(?:位置)?(?:尚未|未|還沒)(?:檢查|核對|確認|查到)'),
    ):
        if re.fullmatch(rf'{subject}(?:{pattern})', clause):
            return state
    return None


def physical_condition(text):
    for property_name, (parts, requirement, _, _) in PHYSICAL_TERMS.items():
        found = re.fullmatch(rf'(?P<name>[\u4e00-\u9fff]{{0,19}}?(?:{parts}))已(?:{requirement})', text)
        if found and '沒有' not in found['name'] and not UNBOUND.search(found['name']):
            return dict(name=found['name'], property=property_name, requirement=text, physical=True)
    return None


def physical_labels(text, pattern, meanings):
    options = {}
    for clause in re.split(r'[、，,；;]', text):
        match = pattern.fullmatch(clause.strip())
        if match is None or match['code'] in options.values():
            return None
        state = next((name for name, meaning in meanings.items() if re.fullmatch(meaning, match['meaning'])), None)
        if state is None or state in options:
            return None
        options[state] = match['code']
    return options if options.keys() == meanings.keys() else None


def split_code_request(sentences):
    if len(sentences) < 4:
        return None
    if SEPARATE_PHYSICAL_CODES.fullmatch(sentences[0]):
        output, mapping, *facts = sentences
    else:
        *facts, output, mapping = sentences
        if not SEPARATE_PHYSICAL_CODES.fullmatch(output):
            return None
    options = physical_labels(mapping, SEPARATE_PHYSICAL_LABEL, SEPARATE_PHYSICAL_MEANINGS)
    if not options:
        return None
    return facts, output + '。' + mapping, options


def parse_physical(question, sentences, version):
    if version == V5:
        output = PHYSICAL_CODES.fullmatch(sentences[-1])
        if not output:
            return None
        options = physical_labels(output[1], PHYSICAL_LABEL, PHYSICAL_MEANINGS)
        facts = sentences[:-1]
    else:
        parts = split_code_request(sentences)
        if parts is None:
            return None
        facts, output, options = parts
    if not 2 <= len(facts) <= 3:
        return None
    rule = PHYSICAL_RULE.fullmatch(facts[0])
    if rule is None or not options or UNBOUND.search(rule['action']):
        return None
    terms = re.split(r'、|與|和|並且', rule['terms'])
    if len(terms) != 2:
        return None
    conditions = [physical_condition(term.strip()) for term in terms]
    if any(c is None for c in conditions) or conditions[0]['name'] == conditions[1]['name']:
        return None
    connectors = [c for c in conditions if c['name'].endswith('接頭')]
    if len(connectors) == 1 and connectors[0]['name'] != '接頭':
        connectors[0]['alias'] = '接頭'
    if not bind_observations(conditions, facts[1:]):
        return None
    plan = dict(schema=version, request=question, request_sha256=evidence_logic.digest(question),
                action=rule['action'], rule=facts[0], sufficient=rule['complete'] == '就是全部要求',
                conditions=conditions, labels=options)
    if version == V6:
        plan['output'] = output
    return plan


def declared_observation(clause, name):
    name = re.escape(name)
    checked = re.fullmatch(rf'已確認「{name}」(成立|不成立)', clause)
    if checked:
        return 'true' if checked[1] == '成立' else 'false'
    return 'unknown' if re.fullmatch(rf'「{name}」(?:尚未|還沒)(?:核對|確認)', clause) else None


def bind_scoped_observations(conditions, sentences):
    for clause in observation_clauses(sentences):
        earlier = EARLIER_OBSERVATION.fullmatch(clause)
        current = CURRENT_OBSERVATION.fullmatch(clause)
        scoped = earlier or current
        if scoped is None:
            return False
        matched = [(condition, state) for condition in conditions
                   if (state := declared_observation(scoped[1], condition['name']))]
        if len(matched) != 1:
            return False
        condition, state = matched[0]
        field = 'earlier_observation' if earlier else 'observation'
        if field in condition:
            return False
        if earlier:
            condition[field] = dict(source=clause, state=state)
        else:
            condition.update(observation=clause, state=state)
    return (all('observation' in condition for condition in conditions)
            and any('earlier_observation' in condition for condition in conditions))


def parse_declared(question, sentences, version):
    parts = split_code_request(sentences)
    if parts is None:
        return None
    facts, output, options = parts
    if not 2 <= len(facts) <= (7 if version == V7 else 13):
        return None
    rule = DECLARED_RULE.fullmatch(facts[0])
    if rule is None or not DECLARED_TERMS.fullmatch(rule['terms']):
        return None
    groups = [re.findall(rf'「({DECLARED_NAME})」', group)
              for group in re.findall(DECLARED_GROUP, rule['terms'])]
    names = [name for group in groups for name in group]
    action = rule['action']
    if (not 2 <= len(names) <= 6 or len(set(names)) != len(names) or action in names
            or sum(len(group) > 1 for group in groups) > 2 or any(name != name.strip() for name in [action, *names])):
        return None
    conditions = [dict(name=name, property='', requirement=name, declared=True) for name in names]
    if version == V8:
        for condition in conditions:
            condition['scoped'] = True
    bind = bind_scoped_observations if version == V8 else bind_observations
    if not bind(conditions, facts[1:]):
        return None
    return dict(schema=version, request=question, request_sha256=evidence_logic.digest(question),
                action=action, rule=facts[0], sufficient=rule['complete'] == '就是全部要求',
                conditions=conditions, groups=groups, labels=options, output=output)


def valid_bound_name(name):
    return name == name.strip() and ' 的 ' not in name


def bound_observations(clause):
    earlier = EARLIER_OBSERVATION.fullmatch(clause)
    scope = earlier or CURRENT_OBSERVATION.fullmatch(clause)
    report = BOUND_REPORT.fullmatch(scope[1]) if scope else None
    if report is None or not valid_bound_name(report[1]):
        return None
    items = [BOUND_ITEM.fullmatch(part.strip()) for part in report[2].split('、')]
    if not 1 <= len(items) <= 6 or any(item is None or not valid_bound_name(item[1]) for item in items):
        return None
    return bool(earlier), [(report[1], item[1], BOUND_STATES[item[2]]) for item in items]


def natural_bound_observations(clause, conditions):
    earlier = EARLIER_OBSERVATION.fullmatch(clause)
    scope = earlier or CURRENT_OBSERVATION.fullmatch(clause)
    report = daily_observation_words.observation(scope[1]) if scope else None
    if report is None or not valid_bound_name(report[0]):
        return None
    subject, items = report
    observations = []
    for field, family, value in items:
        matches = [c for c in conditions if c['subject'] == subject and c['observation_field'] == field
                   and (family is None or c['observation_family'] == family)]
        if len(matches) > 1:
            return None
        if matches:
            condition, = matches
            predicate, expected = condition['predicate'], condition['expected_value']
        else:
            predicate = field + daily_observation_words.CANONICAL.get(family, '')
            expected = True
        state = 'unknown' if value is None else ('true' if value == expected else 'false')
        observations.append((subject, predicate, state))
    return bool(earlier), observations


def bind_bound_observations(conditions, sentences, *, current_only=False, natural=False):
    bound = {(c['subject'], c['predicate']): c for c in conditions}
    seen, other = set(), []
    clauses = (sentence.strip() for sentence in sentences) if natural else observation_clauses(sentences)
    for clause in clauses:
        report = natural_bound_observations(clause, conditions) if natural else bound_observations(clause)
        if report is None or (current_only and report[0]):
            return None
        earlier, observations = report
        for subject, predicate, state in observations:
            key = earlier, subject, predicate
            if key in seen or len(seen) == 12:
                return None
            seen.add(key)
            condition = bound.get((subject, predicate))
            if condition is None:
                if current_only:
                    return None
                other.append(dict(subject=subject, predicate=predicate, state=state, source=clause, earlier=earlier))
            elif earlier:
                condition['earlier_observation'] = dict(source=clause, state=state)
            else:
                condition.update(observation=clause, state=state)
    return other


def bound_name(subject, predicate):
    return f'{subject} 的 {predicate}'


def parse_bound(question, sentences, version):
    parts = split_code_request(sentences)
    if parts is None:
        return None
    facts, output, options = parts
    if not 2 <= len(facts) <= 13:
        return None
    rule = DECLARED_RULE.fullmatch(facts[0])
    if rule is None or not BOUND_TERMS.fullmatch(rule['terms']):
        return None
    pairs = [BOUND_PAIR.findall(group) for group in re.findall(BOUND_GROUP, rule['terms'])]
    identities = [item for group in pairs for item in group]
    if (not 2 <= len(identities) <= 6 or len(set(identities)) != len(identities)
            or sum(len(group) > 1 for group in pairs) > 2 or rule['action'] != rule['action'].strip()
            or any(not valid_bound_name(name) for identity in identities for name in identity)):
        return None
    groups = [[bound_name(*item) for item in group] for group in pairs]
    conditions = [dict(name=bound_name(subject, predicate), subject=subject, predicate=predicate, property='',
                       requirement=f'「{subject}」的「{predicate}」', scoped=True, state='unknown', observation=None)
                  for subject, predicate in identities]
    if version == V10:
        keys = set()
        for condition in conditions:
            observed = daily_observation_words.property_status(condition['predicate'])
            if observed is None:
                return None
            field, family, value = observed
            key = condition['subject'], field, family
            if key in keys:
                return None
            keys.add(key)
            condition.update(observation_field=field, observation_family=family, expected_value=value)
    other = bind_bound_observations(conditions, facts[1:], natural=version == V10)
    if other is None:
        return None
    return dict(schema=version, request=question, request_sha256=evidence_logic.digest(question), rule=facts[0],
                action=rule['action'], sufficient=rule['complete'] == '就是全部要求', conditions=conditions,
                groups=groups, labels=options, output=output, other_observations=other)


def observation_clauses(sentences):
    for sentence in sentences:
        for part in re.split(r'[，,；;]', sentence):
            yield part.strip()


def bind_observations(conditions, sentences):
    for clause in observation_clauses(sentences):
        matches = [(condition, status) for condition in conditions if (status := observation(clause, condition))]
        if len(matches) != 1 or 'observation' in matches[0][0]:
            return False
        condition, status = matches[0]
        condition.update(observation=clause, state=status)
    return all('observation' in condition for condition in conditions)


def parse_version(question, version):
    if version not in INITIAL_PLANS:
        return None
    if not isinstance(question, str) or not 1 <= len(question) <= 1600:
        return None
    body = question.strip().rstrip('。！')
    if re.search(r'[！？?]', body):
        return None
    sentences = [s.strip() for s in re.split(r'[。\r\n]+', body)]
    if version == V3:
        return parse_roster(question, sentences)
    if version == V4:
        return parse_grouped_roster(question, sentences)
    if version in (V5, V6):
        return parse_physical(question, sentences, version)
    if version in BOUND_PLANS:
        return parse_bound(question, sentences, version)
    if version in DECLARED_PLANS:
        return parse_declared(question, sentences, version)
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
    if not bind_observations(conditions.values(), sentences[1:-2]):
        return None
    plan = dict(schema=version, request=question, request_sha256=evidence_logic.digest(question),
                action=rule['verb'] + rule['target'], rule=sentences[0],
                sufficient=rule['rule'].startswith('的必要且充分'),
                conditions=list(conditions.values()), labels=options)
    if version == V2:
        plan['groups'] = names
    return plan


def matches_revision(question):
    return isinstance(question, str) and bool(REVISION_PREFIX.match(question) or ROUND_PREFIX.match(question))


def matches_inquiry(question):
    return isinstance(question, str) and len(question) <= 150 and bool(INQUIRY.fullmatch(question))


def valid_source(source):
    return (isinstance(source, dict) and set(source) == {'answer_id', 'sha256'}
            and type(source['answer_id']) is int and source['answer_id'] > 0
            and isinstance(source['sha256'], str) and bool(re.fullmatch(r'[0-9a-f]{64}', source['sha256'])))


def match_observation(conditions, clause, *, new_round=False):
    unscoped = clause
    if new_round:
        current = CURRENT_OBSERVATION.fullmatch(clause)
        if current is None:
            raise ValueError('請用「本輪」及原本的完整條件名稱標明新觀察。')
        unscoped = current[1]
    matches = [(condition, status) for condition in conditions
               if (status := observation(clause if condition.get('scoped') else unscoped, condition, carried=True))]
    if len(matches) != 1:
        raise ValueError('請用原本的完整條件名稱，明確寫出一項已知或待查狀態。')
    return matches[0]


def start_round(state, match):
    if state['schema'] in (V3, V4):
        raise ValueError('新一輪逐人核對需要重新列出人員、憑證和範圍。')
    conditions = state['conditions']
    for condition in conditions:
        previous = {key: condition[key] for key in ('state', 'observation', 'conflict') if key in condition}
        condition.update(previous_round=previous, state='unknown', observation=None)
        condition.pop('conflict', None)
    facts = match['observations']
    if facts is None:
        return
    sentences = facts.split('。')
    if state['schema'] in BOUND_PLANS:
        if bind_bound_observations(conditions, sentences, current_only=True, natural=state['schema'] == V10) is None:
            raise ValueError('新一輪須用原本的完整物件和屬性，各提供至多一筆本輪觀察。')
        return
    for clause in observation_clauses(sentences):
        condition, status = match_observation(conditions, clause, new_round=True)
        if condition['observation'] is not None:
            raise ValueError('每項條件在開始新一輪時只接受一筆本輪觀察。')
        condition.update(observation=clause, state=status)


def preserve_dispute(conditions, match, *, new_round=False):
    source, clause, other_source, other_clause = match.groups()
    condition, status = match_observation(conditions, clause, new_round=new_round)
    if condition.get('conflict'):
        raise ValueError('這項條件已有待核對的分歧。請在重新核對後更正它。')
    if other_source is None:
        other_status = condition['state']
        other_clause = condition['observation']
    else:
        other_condition, other_status = match_observation(conditions, other_clause, new_round=new_round)
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
                or plan['basis'].get('schema') not in (*INITIAL_PLANS, *UPDATE_PLANS)):
            raise ValueError('待核對項目的原問或來源不符。')
        state = effective(plan['basis'])
        if plan['schema'] == COST_PLAN:
            inspection_minutes(plan['request'], state)
        elif not matches_inquiry(plan['request']):
            raise ValueError('待核對項目的原問或來源不符。')
        return state
    if plan.get('schema') not in UPDATE_PLANS:
        schema = plan.get('schema')
        reconstructed = parse_version(plan.get('request'), schema)
        if (reconstructed != plan or type(plan['sufficient']) is not bool
                or (schema == V10 and json.dumps(reconstructed, sort_keys=True) != json.dumps(plan, sort_keys=True))):
            raise ValueError('必要條件規約與原問不符。')
        return plan

    if set(plan) != {'schema', 'request', 'request_sha256', 'original', 'revisions', 'source'}:
        raise ValueError('沿用條件的規約格式不符。')
    original, revisions, source = plan['original'], plan['revisions'], plan['source']
    if (not isinstance(original, dict) or set(original) != {'request', 'schema'}
            or not isinstance(revisions, list) or not 1 <= len(revisions) <= MAX_REVISIONS
            or any(not isinstance(text, str) or len(text) > (
                MAX_ROUND_CHARS if plan['schema'] == ROUND_PLAN and ROUND_PREFIX.match(text) else 150)
                   for text in revisions)
            or revisions[-1] != plan['request'] or evidence_logic.digest(revisions[-1]) != plan['request_sha256']
            or not valid_source(source)):
        raise ValueError('沿用條件的來源或更正範圍不符。')
    state = parse_version(original['request'], original['schema'])
    if state is None:
        raise ValueError('原始條件規則無法核對。')
    has_dispute = new_round = False
    for request in revisions:
        if plan['schema'] == ROUND_PLAN and (start := ROUND_START.fullmatch(request)):
            start_round(state, start)
            new_round = True
            continue
        if plan['schema'] in (DISPUTE_PLAN, ROUND_PLAN) and (dispute := DISPUTE.fullmatch(request)):
            preserve_dispute(state['conditions'], dispute, new_round=new_round)
            has_dispute = True
            continue
        if ROUND_PREFIX.match(request):
            raise ValueError('開始新一輪後，只能列出使用完整條件名稱的本輪觀察。')
        match = REVISION.fullmatch(request)
        if not match:
            raise ValueError('每次只能更正一項已列出的必要條件。')
        clause = match[1].strip()
        condition, status = match_observation(state['conditions'], clause, new_round=new_round)
        if 'conflict' in condition and status == 'unknown':
            raise ValueError('這項條件仍有分歧，請在確認結果後更正。')
        condition.update(observation=clause, state=status)
        condition.pop('conflict', None)
    if plan['schema'] == DISPUTE_PLAN and not has_dispute:
        raise ValueError('必要條件紀錄缺少可核對的分歧。')
    if plan['schema'] == ROUND_PLAN and not new_round:
        raise ValueError('必要條件紀錄缺少可核對的新一輪起點。')
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
    if parent['schema'] == ROUND_PLAN or ROUND_PREFIX.match(question):
        schema = ROUND_PLAN
    elif parent['schema'] == DISPUTE_PLAN or DISPUTE.fullmatch(question):
        schema = DISPUTE_PLAN
    else:
        schema = REVISION_PLAN
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
    pattern = BOUND_COST_REQUEST if state['schema'] in BOUND_PLANS else COST_REQUEST
    match = pattern.fullmatch(request)
    if not match:
        raise ValueError('請用「檢查時間：條件名稱3分鐘，另一項5分鐘。先查哪一項最省時間？」列出時間。')
    names = {condition['name'] for condition in state['conditions']}
    minutes = {}
    for part in re.split(r'[，,]', match[1]):
        if state['schema'] in BOUND_PLANS:
            item = BOUND_COST_ITEM.fullmatch(part.strip())
            name = bound_name(item[1], item[2]) if item else None
        else:
            item = COST_ITEM.fullmatch(part.strip())
            if item is None and state['schema'] in DECLARED_PLANS:
                item = DECLARED_COST_ITEM.fullmatch(part.strip())
            name = item[1] if item else None
        if name not in names or name in minutes:
            raise ValueError('請用原本的完整條件名稱，各填一次 1 至 999 的整數分鐘。')
        minutes[name] = int(item.groups()[-1])
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
    target = '待判 ' + state['action'] if state['schema'] in DECLARED_PLANS else state['action']
    action = '「' + target + '」'
    items = [f'「條件{index} {c["name"]}{c["property"]}」'
             for index, c in enumerate(state['conditions'], 1)]
    group_rules = []
    required = items
    if state['schema'] in (V2, *DECLARED_PLANS):
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
              V3: 'daily_necessary_roster_receipt_v3',
              V4: 'daily_necessary_grouped_roster_receipt_v4',
              V5: 'daily_necessary_physical_receipt_v5',
              V6: 'daily_necessary_physical_codes_receipt_v6',
              V7: 'daily_necessary_declared_receipt_v7',
              V8: 'daily_necessary_scoped_declared_receipt_v8',
              V9: 'daily_necessary_object_property_receipt_v9',
              V10: 'daily_necessary_natural_property_receipt_v10',
              REVISION_PLAN: 'daily_necessary_revision_receipt_v1',
              DISPUTE_PLAN: 'daily_necessary_dispute_receipt_v1',
              ROUND_PLAN: 'daily_necessary_new_round_receipt_v1',
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
        if plan['schema'] in INITIAL_PLANS:
            return first
        predecessor = previous_id(db, row['session_id'], row['user_message_id'])
        parent = saved(db, predecessor)
        if parent is None:
            raise ValueError('沿用條件的前一筆回答已不存在。')
        receipt, row, source = parent
        if carry_forward(plan['request'], receipt['plan'], source) != plan:
            raise ValueError('沿用條件的來源已變更。')
    if receipt['plan']['schema'] not in INITIAL_PLANS:
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
        updates = '條件更新' if basis['schema'] in (DISPUTE_PLAN, ROUND_PLAN) else '明確更正'
        lines += ['', f'沿用上一則回答 #{plan["source"]["answer_id"]}',
                  f'已核對 {len(basis.get("revisions", []))} 次{updates}', '原始問題：' + resolved['request']]
    observation_title = '沿用的觀察' if plan['schema'] in READ_ONLY_PLANS else (
        '沿用及更新後的觀察' if plan['schema'] in UPDATE_PLANS else '採用的本輪觀察')
    new_round = 'previous_round' in resolved['conditions'][0]
    if new_round:
        observation_title = '新一輪的觀察'
    lines += ['', '規則：' + resolved['rule'], observation_title]
    for condition in resolved['conditions']:
        source = condition['observation']
        if 'conflict' in condition:
            line = f'{condition["name"]} → 回報有分歧'
            lines.append(line if source is None else line + f'（先前觀察：{source}）')
        elif source is None:
            lines.append(f'{condition["name"]} → 尚未核實（本輪未提供觀察）')
        else:
            if resolved['schema'] == V4:
                source = f'{condition["participant"]}｜{source}'
            elif resolved['schema'] in BOUND_PLANS:
                source = f'{condition["requirement"]}｜{source}'
            lines.append(f'{source} → {states[condition["state"]]}')
    if resolved['schema'] in (V2, *DECLARED_PLANS):
        observed = {c['name']: c['state'] for c in resolved['conditions']}
        for group in resolved['groups']:
            if len(group) > 1:
                values = {observed[name] for name in group}
                state = 'true' if 'true' in values else 'false' if values == {'false'} else 'unknown'
                lines.append('備援（' + '或'.join(group) + '） → ' + states[state])
    earlier = [c['earlier_observation']['source'] for c in resolved['conditions'] if 'earlier_observation' in c]
    if earlier:
        if resolved['schema'] in BOUND_PLANS:
            earlier = list(dict.fromkeys(earlier))
        lines += ['', '原問中的舊輪觀察' if new_round else '舊輪觀察', *earlier]
    if resolved.get('other_observations'):
        lines += ['', '原問中其他物件或屬性的觀察']
        for item in resolved['other_observations']:
            lines.append(f'「{item["subject"]}」的「{item["predicate"]}」｜{item["source"]}')
    if new_round:
        lines += ['', '上一輪的觀察（供查閱）']
        for condition in resolved['conditions']:
            previous = condition['previous_round']
            if 'conflict' in previous:
                lines += dispute_text([dict(name=condition['name'], reports=previous['conflict'])]).splitlines()
            elif previous['observation'] is None:
                lines.append(f'{condition["name"]} → 上一輪未提供觀察')
            else:
                lines.append(f'{previous["observation"]} → {states[previous["state"]]}')
    if (receipt.get('conflicts') and plan['schema'] in (DISPUTE_PLAN, ROUND_PLAN)
            and not DISPUTE.fullmatch(plan['request'])):
        lines += ['', dispute_text(receipt['conflicts'])]
    lines.extend(['', f'相容情況數：{receipt["analysis"]["compatible_worlds"]}', '交付：' + receipt['output']])
    if receipt.get('check_order') and 'check' in receipt['check_order']['tree']:
        lines += ['', '完整檢查順序']
        lines.extend(daily_prerequisite_order.tree_lines(receipt['check_order']['tree'], resolved['labels']))
    return '\n'.join(lines)
