"""Ground model stock-count reasoning in the direction of explicit user-reported movements."""
import re

QTY = r'(?:0|[1-9][0-9]{0,5}|[一二兩三四五六七八九十])'
UNIT = r'[本盒張個支瓶包件]'
OUT = ('外借','借出','領走','捐出','發出','用掉','售出')
IN = ('收到','補進','借入','入庫','買進')
VERB = '(?:' + '|'.join((*OUT,*IN)) + ')'
HEAD = re.compile(rf'([\u4e00-\u9fff]{{1,12}})(?:開始有|原有|原本有)\s*({QTY})\s*({UNIT})([\u4e00-\u9fff]{{1,12}})')
EVENT = re.compile(rf'(?:先|再|之後|後來)?(?:又)?({VERB})(?:了)?(?:別人捐的)?\s*({QTY})\s*({UNIT})')
EDIT = re.compile(rf'更正[，,]\s*(?:實際)?({VERB})(?:的)?是\s*({QTY})\s*({UNIT})[，,]\s*'
                  rf'({VERB})(?:的)?數量(?:照舊|不變)')
COUNT_END = re.compile(r'[。；]\s*(?:現在)?([\u4e00-\u9fff]{0,12}?)(?:還剩|剩|有)幾(' + UNIT +
                       r')[？?]\s*只(?:回|給|回答|輸出)數字[。！]?$')
MAYBE = re.compile(rf'(?:接著[\u4e00-\u9fff]{{1,8}}說)?(?:可能|也許)(?:又|另有)?\s*'
                   rf'(?:(?P<v>{VERB})\s*(?P<n>{QTY})\s*(?P<u>{UNIT})|'
                   rf'(?P<n2>{QTY})\s*(?P<u2>{UNIT})被(?P<v2>{VERB}))[，,]\s*'
                   r'(?:但)?(?:還沒查到紀錄|尚未證實|尚未確認)[。；](?:僅憑現有資訊[，,])?'
                   rf'(?:現在的(?:(?P<asked>{UNIT})數|庫存數|數量)能唯一確定|能確定唯一的剩餘數量)嗎[？?]只回答「能」或「不能」[。！]?$')


def number(text):
    if text.isascii():
        return int(text)
    return '一二三四五六七八九十'.index(text.replace('兩','二')) + 1


def direction(verb):
    return -1 if verb in OUT else 1


def parts(q):
    end = COUNT_END.search(q)
    return (q[:end.start()],end[1],end[2]) if end else (None,None,None)


def start(q):
    body, asked, unit = parts(q)
    if not body:
        return None
    clauses = [c.strip() for c in re.split(r'[。；，,]',body)]
    initial = HEAD.fullmatch(clauses[0])
    if not initial or initial[3] != unit or not 2 <= len(clauses) <= 12:
        return None
    place = initial[1]
    if re.search(r'如果|假設|聽說|可能|至少|最多|各|每|不|大約', place + initial[4]):
        return None
    if asked and not place.endswith(asked.removesuffix('裡')):
        return None
    events = []
    for clause in clauses[1:]:
        m = EVENT.fullmatch(clause)
        if not m or m[3] != unit:
            return None
        events.append([m[1], number(m[2])])
    return dict(place=place, asked_place=asked, item=initial[4], unit=unit, initial=number(initial[2]), events=events)


def correct(state, q):
    body, asked, unit = parts(q)
    m = EDIT.fullmatch(body) if body else None
    if (not m or m[3] != state['unit'] or unit != state['unit']
            or (asked and not state['place'].endswith(asked.removesuffix('裡')))):
        return False
    def kind(verb):
        return '借出' if verb == '外借' else verb
    targets = [event for event in state['events'] if kind(event[0]) == kind(m[1])]
    if len(targets) != 1 or kind(m[1]) == kind(m[4]) or not any(
            kind(event[0]) == kind(m[4]) for event in state['events']):
        return False
    targets[0][1] = number(m[2])
    return True


def grounded_state(context):
    q = context['current_request']
    if len(q) > 1000 or context.get('documents', {}).get('catalog'):
        return None
    state = start(q)
    maybe = MAYBE.fullmatch(q)
    ids = []
    if state is None:
        body = parts(q)[0]
        if not maybe and not (body and EDIT.fullmatch(body)):
            return None
        history = context.get('recent_conversation', [])
        tail = context.get('conversation_tail') or {}
        if len(history)<2 or tail.get('message_id') != history[-1]['message_id']:
            return None
        changes = []
        for end in range(len(history), 1, -2):
            user, answer = history[end-2:end]
            if user['role'] != 'user' or user['truncated'] or answer['role'] != 'assistant':
                return None
            ids.insert(0, user['message_id'])
            state = start(user['content'])
            if state:
                break
            changes.insert(0,user['content'])
        if not state or not all(correct(state, previous) for previous in changes):
            return None
        if not maybe and not correct(state,q):
            return None
    total = state['initial']
    for verb, count in state['events']:
        total += direction(verb) * count
        if total < 0:
            return None
    delta = None
    if maybe:
        if (maybe['u'] or maybe['u2']) != state['unit'] or maybe['asked'] not in (None, state['unit']):
            return None
        delta = direction(maybe['v'] or maybe['v2']) * number(maybe['n'] or maybe['n2'])
        if total + delta < 0:
            return None
    return state, ids, total, delta


def context_hint(context):
    grounded = grounded_state(context)
    if not grounded:
        return None
    state, ids, _, delta = grounded
    expression = str(state['initial'])
    for verb, count in state['events']:
        expression += (' - ' if direction(verb)<0 else ' + ') + str(count)
    hint = dict(觀察位置=state['place'], 物品=state['item'], 單位=state['unit'],
                確認異動後的算式=expression, 來源訊息編號=ids,
                回答要求='按算式求出現在數量。只回十進位整數。')
    if delta is not None:
        hint.update(事件未發生=expression, 事件有發生=f'({expression}) + ({delta})',
                    回答要求='未確認的事件保留兩種可能。比較兩種算式：相同才可唯一確定。只回「能」或「不能」。')
    return hint


def concise(context, draft):
    """Remove wrapping text only if the model's stated conclusion agrees with the user events."""
    if not context.get('inventory_reasoning') or len(draft) > 8000:
        return None
    grounded = grounded_state(context)
    if not grounded:
        return None
    state, _, total, delta = grounded
    if delta is not None:
        expected = '能' if delta == 0 else '不能'
        m = re.fullmatch(r'(?:答案[是為：:]\s*)?[「『]?(不能|能)[」』]?[。！]?', draft.strip())
        if not m:
            return None
        if m[1] != expected:
            return f'本機核對：未發生時為 {total}，發生時為 {total + delta}，所以答案是「{expected}」。模型原答「{m[1]}」。'
        return expected
    if re.search(r'可能|估計|不確定|如果|假設|無法確認|約有|[?？]', draft):
        return None
    last = re.split(r'[。\n]', draft.strip().rstrip('。'))[-1].strip()
    asked = parts(context['current_request'])[1]
    places = {state['place'], state['asked_place'], asked, ''}
    place = '(?:' + '|'.join(re.escape(p) for p in sorted(places, key=len, reverse=True)) + ')'
    m = re.fullmatch(rf'(?:(?:因此|所以)[，,]\s*)?(?:現在|目前)?{place}(?:裡|內)?(?:現在|目前)?'
                     rf'(?:有|還剩|剩下|剩)\s*([0-9]+)\s*{state["unit"]}', last)
    if not m:
        m = re.fullmatch(r'([0-9]+)', draft.strip())
    if not m:
        return None
    if m[1] == str(total):
        return str(total)
    expression = context['inventory_reasoning']['確認異動後的算式']
    return f'本機核對：{expression} = {total} {state["unit"]}。模型原答為 {m[1]} {state["unit"]}。'
