"""Exact local checks for a closed natural-language prerequisite request."""
import json
import re

from assistant import chat_evidence, evidence_logic, local_answer_receipts

MODE = 'local_calculation'
FIELD = 'necessary_conditions'
NAME = r'[\u4e00-\u9fff]{1,24}?'
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
    if not isinstance(question, str) or not 1 <= len(question) <= 1600:
        return None
    body = question.strip().rstrip('。！')
    if re.search(r'[！？?]', body):
        return None
    sentences = [s.strip() for s in re.split(r'[。\r\n]+', body)]
    if not 4 <= len(sentences) <= 8 or not OUTPUT.fullmatch(sentences[-1]):
        return None
    rule = RULE.fullmatch(sentences[0])
    options = labels(sentences[-2])
    if not rule or not options or UNBOUND.search(rule['target']) or RULE_MODIFIER.search(rule['target']):
        return None
    conditions = {}
    for part in re.split(r'和|與|、|並且|以及', rule['terms']):
        term = TERM.fullmatch(part.strip())
        if not term or term[2] in conditions or UNBOUND.search(term[2]):
            return None
        conditions[term[2]] = dict(name=term[2], property=term[1], requirement=part.strip())
    if not 2 <= len(conditions) <= 4:
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
    return dict(schema='daily_necessary_plan_v1', request=question, request_sha256=evidence_logic.digest(question),
                action=rule['verb'] + rule['target'], rule=sentences[0],
                sufficient=rule['rule'].startswith('的必要且充分'),
                conditions=list(conditions.values()), labels=options)


def evaluate(plan, cancel=None):
    if not isinstance(plan, dict) or parse(plan.get('request')) != plan or type(plan['sufficient']) is not bool:
        raise ValueError('必要條件規約與原問不符。')
    action = '「' + plan['action'] + '」'
    items = [f'「條件{index} {c["name"]}{c["property"]}」'
             for index, c in enumerate(plan['conditions'], 1)]
    combined = '並且'.join(items)
    clauses = [f'條件核對：只有{combined}，才會{action}']
    if plan['sufficient']:
        clauses.append(f'若{combined}，則{action}')
    for atom, condition in zip(items, plan['conditions'], strict=True):
        if condition['state'] != 'unknown':
            clauses.append('已確認' + ('並非' if condition['state'] == 'false' else '') + atom)
    canonical = '。'.join(clauses) + '。能否確定' + action + '？'
    _, checked = chat_evidence.evaluate(chat_evidence.parse_request(canonical), cancel)
    status = checked['analysis']['status']
    answer = plan['labels'][status]
    return answer, dict(schema='daily_necessary_receipt_v1', plan=plan, status='complete',
                        model_called=False, analysis=checked['analysis'], canonical=canonical, output=answer)


def validate(receipt, delivered, question):
    if not isinstance(receipt, dict) or not isinstance(receipt.get('plan'), dict):
        raise ValueError('必要條件紀錄缺少規約。')
    output, expected = evaluate(receipt['plan'])
    if (receipt['plan']['request'] != question or output != delivered
            or json.dumps(receipt, sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True)):
        raise ValueError('必要條件紀錄與原問或推論不符。')
    return receipt


def read(store, mid):
    return local_answer_receipts.read(store, mid, MODE, FIELD, validate)


def describe(receipt):
    plan = receipt['plan']
    states = {'true': '已確認滿足', 'false': '已確認不滿足', 'unknown': '尚未核實'}
    lines = [plan['request'], '', '規則：' + plan['rule'], '採用的本輪觀察']
    lines += [f'{c["observation"]} → {states[c["state"]]}' for c in plan['conditions']]
    lines.extend(['', f'相容情況數：{receipt["analysis"]["compatible_worlds"]}', '交付：' + receipt['output']])
    return '\n'.join(lines)
