"""Verify one explicit same-day credential deadline."""
import json
import re

from assistant import local_answer_receipts
from scripts.daily_meeting import minutes

MODE = 'local_credential_deadline'
FIELD = 'credential_deadline'
SCHEMA = 'daily_credential_deadline_v1'
EVIDENCE_SCHEMA = 'daily_credential_deadline_evidence_v2'
CLOCK = r'(?:[01][0-9]|2[0-3]):[0-5][0-9]'
ACTIONS = {
    '進門': r'進門|進',
    '入場': r'入場',
    '進場': r'進場',
    '報到': r'報到',
    '登記': r'登記',
    '使用': r'使用|用',
}
RULE = (
    rf'(?P<rule>這張(?P<credential>[\u4e00-\u9fff]{{0,15}}?(?P<kind>票|證|券))是唯一的(?P=kind)[，,]\s*'
    rf'(?:今天|本日)\s*(?P<deadline>{CLOCK})\s*截止(?:（(?P<boundary>含截止時刻|不含截止時刻)）)?[，,]\s*'
    rf'(?P<action>{"|".join(ACTIONS)})的唯一要求是(?:(?P=credential)|(?P=kind))仍在期限內。)\s*'
)
OUTPUT = r'(?P<output>(?:請)?只(?:輸出|回覆|回)\s*(?P<mapping>.+?))。?'
SAME_DAY = rf'(?:現在同日|(?:本日|今天)已確認當下為|已核對(?:今天|本日)當下為)\s*(?P<current>{CLOCK})'
UNKNOWN_NOW = r'今天目前時間尚未核對|本日當下時刻還無紀錄|沒有今天現在的時間'
STALE = (rf'(?P<stale>(?P<stale_clock>{CLOCK})\s*(?:是(?:昨天的紀錄|開門登記時刻)|來自上一個開放日))'
         r'[，,；;]\s*')
UNKNOWN = rf'(?:{STALE})?(?:{UNKNOWN_NOW})'
FACTS = re.compile(RULE + rf'(?P<observation>現在同日\s*(?P<current>{CLOCK})。)\s*' + OUTPUT)
PENDING = re.compile(RULE + rf'(?P<observation>{UNKNOWN}。)\s*' + OUTPUT)
CODE = re.compile(r'(?P<code>[A-Za-z0-9]{1,12})\s*表示\s*(?P<meaning>[\u4e00-\u9fff]{1,14})')
VERDICT_CODE = re.compile(r'(?P<meaning>確定可行|確定不可行|尚無法判定)回\s*(?P<code>[A-Za-z0-9]{1,12})')
VERDICTS = dict(within='確定可行', expired='確定不可行', undetermined='尚無法判定')
KINDS = ('領取條', '回條', '票', '證', '券')
CREDENTIAL = rf'[\u4e00-\u9fff]{{0,15}}?(?:{"|".join(KINDS)})'
NATURAL_RULES = (
    re.compile(rf'當日(?P<action>投件|交件|報到)只要求手上的唯一(?P<credential>{CREDENTIAL})仍然有效'),
    re.compile(rf'憑手上的唯一(?P<credential>{CREDENTIAL})(?P<action>領[\u4e00-\u9fff]{{1,8}})'
               r'[，,]\s*期限仍有效便可領取'),
)
EXPIRY = re.compile(rf'(?P<item>{CREDENTIAL})(?:上記載本日|於今天|今天|本日)\s*(?P<deadline>{CLOCK})\s*'
                    r'(?P<verb>截止|失效)(?:[，,]\s*(?P<equal>截止那一刻仍有效|截止那一刻已失效))?')
UNBOUND = re.compile(r'或|和|與|及|上述|附件|其他|任一|逾期|過期|作廢|失效|若|可能|假設')
MIXED_ACTION = re.compile(r'並|再|同時|然後|另外|也|還|未|不')


def decode_labels(text, meanings, pattern):
    labels = {}
    for part in re.split(r'[、，,；;]', text):
        code = pattern.fullmatch(part.strip())
        if code is None or code['code'] in labels.values():
            return None
        meaning = next((key for key, expression in meanings.items() if re.fullmatch(expression, code['meaning'])), None)
        if meaning is None or meaning in labels:
            return None
        labels[meaning] = code['code']
    return labels if labels.keys() == meanings.keys() else None


def old_clock(match):
    return dict(clock=match['stale_clock'], source=match['stale']) if match['stale'] else None


def parse_natural(request):
    parts = [part.strip() for part in request.removesuffix('。').split('。')]
    if len(parts) != 5:
        return None
    if parts[0] == '只輸出指定代碼':
        directive, mapping, rule, expiry, observation = parts
    else:
        rule, expiry, observation, directive, mapping = parts
    if directive != '只輸出指定代碼':
        return None
    action = next((match for form in NATURAL_RULES if (match := form.fullmatch(rule))), None)
    end = EXPIRY.fullmatch(expiry)
    present, missing = re.fullmatch(SAME_DAY, observation), re.fullmatch(UNKNOWN, observation)
    labels = decode_labels(mapping, VERDICTS, VERDICT_CODE)
    if not action or not end or not (present or missing) or not labels:
        return None
    credential = action['credential']
    kind = next(kind for kind in KINDS if credential.endswith(kind))
    if (end['item'] not in (credential, kind) or UNBOUND.search(credential)
            or UNBOUND.search(action['action']) or MIXED_ACTION.search(action['action'])):
        return None
    inclusive = end['equal'] == '截止那一刻仍有效'
    if end['verb'] == '失效' and inclusive:
        return None
    boundary = '含截止時刻' if inclusive else ('不含截止時刻' if end['verb'] == '失效' or end['equal'] else None)
    current = present['current'] if present else None
    if current == end['deadline'] and boundary is None:
        return None
    return dict(schema=EVIDENCE_SCHEMA, request=request, credential=credential, action=action['action'],
                rule=f'{rule}。{expiry}。', observation=observation + '。', output=f'{directive}。{mapping}',
                current=current, deadline=end['deadline'], boundary=boundary, labels=labels,
                stale=old_clock(missing) if missing else None)


def parse(request):
    if not isinstance(request, str) or len(request) > 500:
        return None
    known = FACTS.fullmatch(request)
    match = known or PENDING.fullmatch(request)
    if match is None:
        return parse_natural(request)
    if UNBOUND.search(match['credential']):
        return None
    current = match['current'] if known else None
    boundary = match['boundary']
    if current == match['deadline'] and boundary is None:
        return None
    action = ACTIONS[match['action']]
    meanings = {'within': rf'(?:能|可以|可)(?:{action})?', 'expired': rf'(?:不能|不可以|不可)(?:{action})?',
                'undetermined': r'未定|不確定|無法判定'}
    labels = decode_labels(match['mapping'], meanings, CODE)
    if labels is None:
        return None
    plan = dict(schema=SCHEMA if known else EVIDENCE_SCHEMA, request=request, credential=match['credential'],
                action=match['action'], rule=match['rule'], observation=match['observation'], output=match['output'],
                current=current, deadline=match['deadline'], boundary=boundary, labels=labels)
    if not known:
        plan['stale'] = old_clock(match)
    return plan


def evaluate(plan):
    state = 'undetermined'
    if plan['current'] is not None:
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
    if receipt['state'] == 'undetermined':
        proof = f"當日實際時刻尚未核對，無法和 {plan['deadline']} 比較。"
    else:
        conclusion = '仍在期限內' if receipt['state'] == 'within' else '已不在期限內'
        proof = f"{plan['current']} 對照同日截止 {plan['deadline']}，{conclusion}。"
    return '\n'.join([plan['rule'], plan['observation'], plan['output'], '', proof, receipt['result']])
