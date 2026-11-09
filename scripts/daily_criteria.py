"""Compare and review a small option table with source-verified follow-ups."""
from copy import deepcopy
import hashlib
import json
import re

from assistant.local_answer_receipts import MAX_JSON_BYTES, load_context
from scripts import daily_dialogue

UNIT = r'(?:元|天|席|小時|公斤)'
NUMBER = r'(?:0|[1-9][0-9]{0,6})'
LABELS = {'預算':'元', '費用':'元', '價格':'元', '人數':'席', '席數':'席', '時長':'小時',
          '重量':'公斤', '送達':'天'}
LABEL = '(?:'+'|'.join(LABELS)+')'
NAME = r'[\u4e00-\u9fffA-Za-z]{1,12}'
UP = ('增加', '提高', '調高', '上調', '延長', '加')
DOWN = ('減少', '降低', '調低', '下調', '縮短', '減', '降')
DIRECTION = '(?:' + '|'.join((*UP, *DOWN)) + ')'
ACTION = rf'(改為|改成|{DIRECTION}(?:到|至)?)'
REVISION = re.compile(r'(?:(?:再)?更正[，,：:]\s*)?(?:只)?(?:把|將)'
                      rf'({LABEL}?(?:上限|下限|期限)?)'
                      rf'(?:再)?{ACTION}\s*([^。！？；，,\n]{{1,32}})[，,；;]\s*'
                      r'(?:其他條件不變|其餘條件不變|其餘照舊|其他照舊)'
                      r'(?:[。！]\s*(?:只(?:回答|輸出|給)(?:方案名稱|名稱|班名|方案名)|沒有符合的只回答「(?P<empty>無符合方案|沒有符合的方案|無)」))?[。！]?')


# Shorthand must be a complete request about a limit, not an option's own price.
SHORT_REVISION = re.compile(rf'(?:請)?(?:把|將)?({LABEL}(?:上限|下限|期限)|預算)\s*(?:再)?{ACTION}\s*'
                            rf'({NUMBER}\s*(?:{UNIT}|[%％])?|當天|一點|一些|[一二三四五六七八九]成)[。！]?$')
LIST_REQUEST = re.compile(r'(?:請)?(?:列出|查看)(?:剛才的)?方案(?:清單)?[。！!？?]?')
ORDINAL = daily_dialogue.ordinal_pattern(r'方案清單(?:中的?|的)')
STATE_FIELDS = ('option_plan', 'option_question', 'option_review')
FIELDS = (*STATE_FIELDS, 'option_source')


def review_request(question):
    if LIST_REQUEST.fullmatch(question):
        return dict(kind='list')
    if match := ORDINAL.fullmatch(question):
        return dict(kind='choice', position=daily_dialogue.ordinal_position(match), scoped=bool(match['scope']))
    return None


def revision(question):
    if len(question) > 160:
        return None
    m = REVISION.fullmatch(question) or SHORT_REVISION.fullmatch(question)
    if not m or not m[1]:
        return None
    return dict(field=m[1], action=m[2], value=m[3].strip(), empty=m.groupdict().get('empty'))


def candidate(text):
    m = re.fullmatch(rf'({NAME})[：:]\s*(.+)', text)
    if not m:
        m = re.fullmatch(rf'({NAME})(每人\s*[0-9].+)', text)
    if not m:
        return None
    if re.search(r'不含|不算|排除|除非|若|如果|不要|假設|忽略', m[1]):
        return None
    values, basis = {}, {}
    for token in m[2].split('、'):
        part = token.strip()
        metric = re.fullmatch(rf'(?:每人|上課)?\s*({NUMBER})\s*({UNIT})', part)
        trait = re.fullmatch(r'(不能|不可|可|沒有|有)(冷藏)|(?:(沒有|有)(插座))', part)
        if metric:
            field, value = metric[2], int(metric[1])
            if (part.startswith('每人') and field != '元') or (part.startswith('上課') and field != '小時'):
                return None
            basis[field] = '每人' if part.startswith('每人') else '每案'
        elif part in ('當天', '當天送達'):
            field, value = '天', 0
            basis[field] = '每案'
        elif trait:
            field, value = trait[2] or trait[4], (trait[1] or trait[3]) in ('可','有')
        else:
            return None
        if field in values:
            return None
        values[field] = value
    return dict(name=m[1], values=values, basis=basis, source=text)


def limit_rule(part):
    m = re.fullmatch(rf'({LABEL})?(最多|不超過|上限|至少|下限)\s*({NUMBER})\s*({UNIT})', part)
    if m and (not m[1] or LABELS[m[1]] == m[4]):
        return m[4], ('min' if m[2] in ('至少','下限') else 'max'), int(m[3])
    m = re.fullmatch(rf'({NUMBER})\s*天內送達', part)
    if m:
        return '天','max',int(m[1])
    m = re.fullmatch(r'(?:必須|一定要)(冷藏|有插座)', part)
    if m:
        return m[1].removeprefix('有'), 'required', True
    return None


def parse(question):
    if len(question) > 2048:
        return None
    clauses = question.rstrip('。').split('。')
    if len(clauses)<4 or not re.fullmatch(r'(?:我要選|要挑|幫我比較)(?:這些|兩個|三個|四個)?(?:配送方案|運送方案|自習室|課程|教室|場地|背包|工具包|選項|方案)', clauses[0]):
        return None
    if not re.fullmatch(r'只(?:回答|輸出)(?:方案名稱|名稱|班名|方案名)', clauses[-1]):
        return None
    options = [candidate(s.strip()) for s in re.split(r'[；;]',clauses[1])]
    if not 2 <= len(options) <= 16 or not all(options) or len({o['name'] for o in options})!=len(options):
        return None
    fields = set(options[0]['values'])
    if any(set(o['values']) != fields or o['basis'] != options[0]['basis'] for o in options):
        return None
    limits, ranking = {}, []
    for clause in clauses[2:-1]:
        for part in re.split(r'[，,]|且',clause):
            part = part.strip()
            rule = limit_rule(part)
            if rule:
                field, op, value = rule
                if field not in fields or field in limits:
                    return None
                limits[field] = dict(op=op, value=value, source=part)
                continue
            rank = re.fullmatch(r'(?:合格者挑|符合時選)(最快|最便宜|最輕|最短)(?:的)?', part)
            if rank and not ranking:
                if rank[1] in ('最快','最短'):
                    time_fields = fields & {'小時','天'}
                    if len(time_fields) != 1:
                        return None
                    key = next(iter(time_fields))
                else:
                    key = {'最便宜':'元','最輕':'公斤'}[rank[1]]
            elif part == '同速挑便宜' and ranking in (['天'], ['小時']):
                key = '元'
            else:
                return None
            if key not in fields:
                return None
            ranking.append(key)
    if not limits or not ranking:
        return None
    return dict(schema='daily_option_table_v1', options=options, limits=limits, ranking=ranking, empty='無符合方案')


def revise(plan, change):
    field = change['field']
    numeric = {k:v for k,v in plan['limits'].items() if v['op'] != 'required'}
    labels = '、'.join(v['source'] for v in numeric.values())
    key = next((unit for label,unit in LABELS.items() if field.startswith(label)), None)
    rule = numeric.get(key)
    m = re.fullmatch(rf'({NUMBER})\s*({UNIT})', change['value'])
    days = change['value']=='當天' and key=='天'
    if not rule or (field.endswith('期限') and key != '天'):
        return f'請指定要更改哪個限制，以及新的數值和單位。目前的限制有：{labels}。' if numeric else '目前沒有數值限制，請重新提供完整條件。'
    if (field.endswith(('上限', '期限')) and rule['op'] != 'max') or (field.endswith('下限') and rule['op'] != 'min'):
        return f'原條件是「{rule["source"]}」。若要改變限制方向，請重新列出完整條件。'
    action = change['action']
    absolute = action in ('改成', '改為') or action.endswith(('到', '至'))
    if not (days and absolute or m and m[2] == key):
        return f'原條件是「{rule["source"]}」，請提供更正後的確切數值（{key}）。'
    operand = 0 if days else int(m[1])
    old = rule['value']
    direction = action.rstrip('到至')
    if absolute:
        new = operand
        if (direction in DOWN and new > old) or (direction in UP and new < old):
            return f'目前限制是 {old} {key}，指定的新值與調整方向相反。請確認要改為多少{key}。'
    else:
        new = old + operand if direction in UP else old - operand
    if not 0 <= new <= 9_999_999:
        return f'從 {old} {key} 調整後會變成 {new} {key}，超出可用範圍。請確認新的限制。'
    bound = '上限' if rule['op'] == 'max' else '下限'
    rule.update(value=new, source=f'{field}（{bound} {new} {key}）')
    plan['last_adjustment'] = dict(field=field, unit=key, action=action, operand=operand,
                                   mode='absolute' if absolute else 'relative', before=old, after=new)
    if change['empty']:
        plan['empty'] = change['empty']
    return None


def resolve(plan, clarification=None):
    eligible = []
    checked = []
    for item in plan['options']:
        rejected = []
        for field, rule in plan['limits'].items():
            value = item['values'][field]
            if ((rule['op']=='max' and value>rule['value']) or (rule['op']=='min' and value<rule['value'])
                    or (rule['op']=='required' and not value)):
                rejected.append(field)
        checked.append(dict(name=item['name'], rejected=rejected))
        if not rejected:
            eligible.append((tuple(item['values'][field] for field in plan['ranking']),item['name']))
    winners = []
    if eligible:
        best = min(key for key,_ in eligible)
        winners = [name for key,name in eligible if key==best]
    answer = clarification or ('、'.join(winners) if winners else plan['empty'])
    return answer, dict(schema='daily_option_selection_v1', status='complete', model_called=False, checks=checked,
                        winners=winners, clarification=clarification, expression='逐項篩選後依指定順序比較',
                        result=dict(exact=answer, calculations=[]))


def followup(parent, change, review):
    if change is None and review is None:
        return None
    if review and review['kind'] == 'choice':
        if not parent or parent.get('option_review', {}).get('kind') != 'list':
            if review['scoped']:
                raise ValueError('請緊接在方案清單後選擇，或重新列出目前的方案。')
            return None
        count = len(parent['option_plan']['options'])
        if not 1 <= review['position'] <= count:
            raise ValueError(f'上一則列出 {count} 個方案，請選清單中的順序。')
    if parent is None:
        return None
    plan = deepcopy(parent['option_plan'])
    state = dict(option_plan=plan)
    clarification = revise(plan, change) if change else parent.get('option_question')
    if clarification:
        state['option_question'] = clarification
    if review:
        state['option_review'] = {key: value for key, value in review.items() if key != 'scoped'}
    return state


def render(state):
    plan = state['option_plan']
    clarification = state.get('option_question')
    review = state.get('option_review')
    answer, receipt = resolve(plan, clarification if review is None else None)
    if review is None:
        return answer, receipt

    def display(position, option, check):
        rejected = [plan['limits'][key]['source'] for key in check['rejected']]
        if rejected:
            status = '未符合' + '、'.join(f'「{rule}」' for rule in rejected)
        elif option['name'] in receipt['winners']:
            status = '符合限制，並列首選' if len(receipt['winners']) > 1 else '符合限制，依規則首選'
        else:
            status = '符合限制'
        return f'第 {position} 個｜{option["source"]}｜{status}'

    entries = [display(i, option, check) for i, (option, check) in enumerate(
        zip(plan['options'], receipt['checks'], strict=True), 1)]
    if review['kind'] == 'list':
        lines = [f'依原輸入順序列出 {len(entries)} 個方案。', *entries,
                 '要查看其中一個，可依清單順序回「第二個」。']
    else:
        lines = [entries[review['position'] - 1]]
    if clarification:
        lines.insert(0, '上次更正待確認，以下按目前有效的限制核對。')
    answer = '\n'.join(lines)
    receipt.update(schema='daily_option_review_v1', review=review, clarification=clarification,
                   result=dict(exact=answer, calculations=[]))
    return answer, receipt


def previous_id(db, sid, before):
    row = db.execute('SELECT id FROM messages WHERE session_id=? AND (? IS NULL OR id<?) '
                     'ORDER BY id DESC LIMIT 1', (sid, before, before)).fetchone()
    return row['id'] if row else None


def fields_equal(left, right):
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def saved_option(db, mid):
    rows = db.execute('''SELECT a.session_id,a.role,a.content,u.id AS uid,u.role AS user_role,
        u.session_id AS user_session,u.content AS question,r.session_id AS run_session,r.status,
        CASE WHEN length(CAST(c.payload AS BLOB))<=? THEN c.payload END AS payload,
        CASE WHEN length(CAST(r.context_json AS BLOB))<=? THEN r.context_json END AS initial,
        CASE WHEN length(CAST(k.payload AS BLOB))<=? THEN k.payload END AS checks
        FROM messages a JOIN answer_runs r ON r.answer_message_id=a.id
        JOIN messages u ON u.id=r.user_message_id JOIN answer_contexts c ON c.message_id=a.id
        LEFT JOIN answer_checks k ON k.message_id=a.id WHERE a.id=? LIMIT 2''',
        (MAX_JSON_BYTES, MAX_JSON_BYTES, MAX_JSON_BYTES, mid)).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError('方案缺少唯一的完成流程。')
    row = rows[0]
    saved, initial = load_context(row['payload']), load_context(row['initial'])
    if not isinstance(saved, dict) or not isinstance(initial, dict):
        raise ValueError('方案的回答紀錄格式有誤。')
    if 'option_plan' not in saved and 'option_plan' not in initial:
        return None
    if (row['role'] != 'assistant' or row['user_role'] != 'user' or row['status'] != 'succeeded'
            or not row['session_id'] == row['user_session'] == row['run_session'] or not 0 < row['uid'] < mid
            or not row['question'] == saved.get('current_request') == initial.get('current_request')
            or saved.get('response_mode') != 'local_calculation' or initial.get('response_mode') != 'local_calculation'
            or not fields_equal([saved.get(key) for key in FIELDS], [initial.get(key) for key in FIELDS])
            or saved.get('image_inputs') or saved.get('engine_selection') is not None
            or saved.get('documents') != {'catalog': [], 'excerpts': []} or load_context(row['checks']) != []):
        raise ValueError('方案來源與已完成的回答流程不符。')
    source = dict(answer_id=mid, sha256=hashlib.sha256(row['payload'].encode('utf-8')).hexdigest())
    return row, saved, source


def verified(db, mid):
    tip = saved_option(db, mid)
    if tip is None:
        return None
    history = [tip]
    while 'option_source' in history[-1][1]:
        row, saved, _ = history[-1]
        parent = saved_option(db, previous_id(db, row['session_id'], row['uid']))
        if parent is None or not fields_equal(saved['option_source'], parent[2]):
            raise ValueError('方案的上一筆來源已變動。')
        history.append(parent)
    parent = None
    for row, saved, _ in reversed(history):
        question = row['question']
        if parent is None:
            original = parse(question)
            state = dict(option_plan=original) if original else None
        else:
            state = followup(parent, revision(question), review_request(question))
        if state is None or not fields_equal([state.get(key) for key in STATE_FIELDS],
                                             [saved.get(key) for key in STATE_FIELDS]):
            raise ValueError('方案的原始條件與保存紀錄不符。')
        answer, receipt = render(state)
        if answer != row['content'] or not fields_equal(receipt, saved.get('calculation')):
            raise ValueError('方案的交付與重算結果不符。')
        parent = state
    return tip[1:]


def read(store, mid):
    with store.connect() as db:
        db.execute('BEGIN')
        found = verified(db, mid)
        return found[0] if found else None


def describe(context):
    plan = context['option_plan']
    receipt = context['calculation']
    lines = [context['current_request']]
    if source := context.get('option_source'):
        lines.append(f'沿用本對話方案回答 #{source["answer_id"]}')
    if context.get('option_question'):
        lines.append('更正尚待確認，沿用上一份有效條件。')
    elif change := plan.get('last_adjustment'):
        mode = '相對增減' if change['mode'] == 'relative' else '指定新值'
        lines.append(f'{change["field"]}（{mode}）：{change["before"]} → {change["after"]} {change["unit"]}')
    lines += ['', '有效限制']
    lines.extend(rule['source'] for rule in plan['limits'].values())
    lines += ['', '依序比較（數值較小者優先）：' + '、'.join(plan['ranking']), '逐項核對']
    for option, check in zip(plan['options'], receipt['checks'], strict=True):
        reason = '不符合：' + '、'.join(plan['limits'][key]['source'] for key in check['rejected']) if check['rejected'] else '符合'
        lines.append(option['source'] + ' · ' + reason)
    lines += ['', receipt['result']['exact']]
    return lines


def capture(store, sid, question, before=None, *, allow_review=True):
    original = parse(question)
    change = revision(question) if original is None else None
    review = review_request(question) if allow_review and original is None and change is None else None
    if original is None and change is None and review is None:
        return None
    if store.documents(sid, include_content=False):
        return None
    if original is not None:
        return dict(option_plan=original)
    try:
        with store.connect() as db:
            db.execute('BEGIN')
            found = verified(db, previous_id(db, sid, before))
    except ValueError as exc:
        raise ValueError('上一份方案紀錄已變動，請重新提供條件。') from exc
    state = followup(found[0] if found else None, change, review)
    if state is not None:
        state['option_source'] = found[1]
    return state
