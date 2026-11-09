"""Verify one explicit same-day credential deadline."""
import json
import re

from assistant import local_answer_receipts
from scripts.daily_meeting import minutes

MODE = 'local_credential_deadline'
FIELD = 'credential_deadline'
SCHEMA = 'daily_credential_deadline_v1'
CLOCK = r'(?:[01][0-9]|2[0-3]):[0-5][0-9]'
ACTIONS = {
    '進門': r'進門|進',
    '入場': r'入場',
    '進場': r'進場',
    '報到': r'報到',
    '登記': r'登記',
    '使用': r'使用|用',
}
FACTS = re.compile(
    rf'(?P<rule>這張(?P<credential>[\u4e00-\u9fff]{{0,15}}?(?P<kind>票|證|券))是唯一的(?P=kind)[，,]\s*'
    rf'(?:今天|本日)\s*(?P<deadline>{CLOCK})\s*截止(?:（(?P<boundary>含截止時刻|不含截止時刻)）)?[，,]\s*'
    rf'(?P<action>{"|".join(ACTIONS)})的唯一要求是(?:(?P=credential)|(?P=kind))仍在期限內。)\s*'
    rf'(?P<observation>現在同日\s*(?P<current>{CLOCK})。)\s*'
    r'(?P<output>(?:請)?只(?:輸出|回覆|回)\s*(?P<mapping>.+?))。?'
)
CODE = re.compile(r'([A-Za-z0-9]{1,12})\s*表示\s*([\u4e00-\u9fff]{1,14})')


def parse(request):
    if not isinstance(request, str) or len(request) > 500:
        return None
    match = FACTS.fullmatch(request)
    if match is None or re.search(r'或|和|與|及|上述|附件|其他|任一|逾期|過期|作廢|失效|若|可能|假設',
                                  match['credential']):
        return None
    boundary = match['boundary']
    if match['current'] == match['deadline'] and boundary is None:
        return None
    action = ACTIONS[match['action']]
    meanings = {
        'within': rf'(?:能|可以|可)(?:{action})?',
        'expired': rf'(?:不能|不可以|不可)(?:{action})?',
        'undetermined': r'未定|不確定|無法判定',
    }
    labels = {}
    for part in re.split(r'[、，,；;]', match['mapping']):
        code = CODE.fullmatch(part.strip())
        if code is None or code[1] in labels.values():
            return None
        meaning = next((key for key, pattern in meanings.items() if re.fullmatch(pattern, code[2])), None)
        if meaning is None or meaning in labels:
            return None
        labels[meaning] = code[1]
    if labels.keys() != meanings.keys():
        return None
    return dict(schema=SCHEMA, request=request, credential=match['credential'], action=match['action'],
                rule=match['rule'], observation=match['observation'], output=match['output'],
                current=match['current'], deadline=match['deadline'], boundary=boundary, labels=labels)


def evaluate(plan):
    current, deadline = minutes(plan['current']), minutes(plan['deadline'])
    within = current < deadline or (current == deadline and plan['boundary'] == '含截止時刻')
    state = 'within' if within else 'expired'
    answer = plan['labels'][state]
    return answer, dict(plan=plan, model_called=False, state=state, result=answer)


def validate(receipt, delivered, question):
    plan = parse(question)
    if plan is None:
        raise ValueError('無法重建原始期限及同日時間。')
    answer, expected = evaluate(plan)
    if (delivered != answer or json.dumps(receipt, sort_keys=True, allow_nan=False)
            != json.dumps(expected, sort_keys=True)):
        raise ValueError('期限回覆與原始時刻不符。')
    return receipt


def read(store, answer_id):
    return local_answer_receipts.read(store, answer_id, MODE, FIELD, validate)


def describe(receipt):
    plan = receipt['plan']
    conclusion = '仍在期限內' if receipt['state'] == 'within' else '已不在期限內'
    return '\n'.join([plan['rule'], plan['observation'], plan['output'], '',
                      f"{plan['current']} 對照同日截止 {plan['deadline']}，{conclusion}。", receipt['result']])
