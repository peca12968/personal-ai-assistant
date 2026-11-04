"""Bind explicitly requested identifiers to their named object in daily model context."""
import re

FIELD = r'(?:序號|編號|型號|代碼)'
REVISION = r'(?:更正[，,：:]\s*(?:我要的是|我想查|請查|請問|改查|改問)|改查|改問)'
REQUEST = re.compile(rf'(?:^|[。！？；\n])\s*(?P<verb>我要的是|我想查|請查|請問|{REVISION})'
                     rf'([\u4e00-\u9fffA-Za-z0-9]{{2,24}})的({FIELD})(?:是多少|是什麼)?(?=[。！？?\n]|$)')
# Keep examples and quoted instructions out of extraction; preserve punctuation outside the quotes.
QUOTED = re.compile(r'「[^」]*」|『[^』]*』|“[^”]*”|"[^"]*"|\x60[^\x60]*\x60', re.S)


def request_focus(question):
    if len(question) > 2048 or '\x60\x60\x60' in question:
        return None
    plain = QUOTED.sub('■', question)
    if re.search(r'[「『“"\x60]|翻譯|轉述|舉例|改寫|示範', plain):
        return None
    matches = list(REQUEST.finditer(plain))
    if not matches or len(matches) > 2:
        return None
    active = matches[-1]
    revised = re.fullmatch(REVISION, active['verb']) is not None
    if len(matches) == 2 and not revised:
        return None
    _, target, field = active.groups()
    if (re.search(r'或|以及|和|跟|還是|兩者|這個|那個|上述|前者|後者', target)
            or re.search(r'改查|改問|其實|更正|改成|改要|換成|換個|算了|先別|不要查|不必追問|不要追問', plain[active.end():])):
        return None
    return {'target': target, 'field': field, **({'retargeted': True} if revised else {}),
            'guidance': '僅使用明確屬於指定物件的欄位值。若已有該值，按本輪要求直接回答。'
                        '若缺少該值，先點明是哪個物件的哪個欄位無法確認，然後只問一個能取得該欄位的問題，'
                        '例如能否提供該物件原包裝或登記紀錄上記載的值。不要泛問其他特徵，也不要複述其他物件的值。'
                        '若使用者指定缺資料時的回覆，遵照該格式。'}


def missing_literal(question, draft):
    missing = re.search(r'(?:若|如果)(?:沒有資料|無法確認|無法得知)[，,]\s*(?:請)?只(?:回答|回覆|輸出)'
                        r'\s*(?:「(不詳|資料不足|資訊不足|無法確定)」|(不詳|資料不足|資訊不足|無法確定))[。！]?$', question)
    if not missing or len(draft) > 4096:
        return None
    focus = request_focus(question)
    if not focus or focus['target'] + '的' + focus['field'] not in draft:
        return None
    literal = missing[1] or missing[2]
    subject = re.escape(focus['target'] + '的' + focus['field'])
    if re.fullmatch(subject + r'(?:目前)?(?:不詳|未提供|無法確認|無法得知)[。！]?', draft.strip()):
        return literal
    if re.search(r'[A-Za-z0-9]', draft):
        return None
    # Format the model's explicit conclusion; a conditional/quoted instruction
    # alone does not establish that the value is actually unavailable.
    if (re.search(r'沒有(?:相關)?資料|無法確認|無法得知|未提供', draft)
            and re.search(rf'(?:^|[。\n])(?:因此[，,])?我只能回答「{literal}」[。！]?(?:$|如果您能提供[^。！？]+。?$)', draft)):
        return literal
    return None


CODE = r'[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*'
ONLY_FIELD = re.compile(rf'(?:^|[。！？?，,；;])\s*(?:請)?只(?:給|回答|回覆|輸出)({FIELD})[。！]?$')


FACT = re.compile(rf'([\u4e00-\u9fffA-Za-z0-9]{{2,24}})的({FIELD})(?:是|為|[：:])\s*({CODE})')
ABSENT = re.compile(rf'([\u4e00-\u9fffA-Za-z0-9]{{2,24}})的({FIELD})(?:未提供|未登記|不詳)')
LABEL_LOST = re.compile(r'([\u4e00-\u9fffA-Za-z0-9]{2,24})的標籤(?:已經|已)?(?:遺失|脫落|掉了)')


def explicit_evidence(question, target, field):
    """Accept a closed list of direct user statements, not mentions in arbitrary prose."""
    statements = re.split(r'[。！？?；\n]', question)
    if re.search(r'假設|如果|假如|例如|範例|聽說|傳聞|可能|暫定|作廢', question):
        return None
    facts = []
    for clause in map(str.strip, statements):
        if not clause or REQUEST.fullmatch(clause) or ONLY_FIELD.fullmatch(clause):
            continue
        fact = FACT.fullmatch(clause)
        absent = ABSENT.fullmatch(clause) or LABEL_LOST.fullmatch(clause)
        if fact:
            if (fact[1], fact[2]) == (target, field):
                facts.append({'quote': clause, 'value': fact[3]})
        elif absent:
            if absent[1] == target and (absent.re is LABEL_LOST or absent[2] == field):
                facts.append({'quote': clause})
        else:
            return None
    return facts[0] if len(facts) == 1 else None


def context_focus(context):
    q = context['current_request']
    focus = request_focus(q)
    if not focus or not focus.get('retargeted') or context.get('documents', {}).get('catalog'):
        return focus
    evidence = explicit_evidence(q, focus['target'], focus['field'])
    if evidence:
        focus['user_evidence'] = evidence
        return focus
    # Do not import a historical value over new information in this request.
    match = REQUEST.match(q)
    if not match or not re.fullmatch(r'[。！？?]?\s*(?:只(?:給|回答|回覆|輸出)' + FIELD +
                                     r'|(?:若|如果)沒有資料[，,]\s*只回答「(?:不詳|資料不足|資訊不足)」)?[。！]?', q[match.end():]):
        return focus
    history = context.get('recent_conversation', [])
    tail = context.get('conversation_tail') or {}
    if len(history) < 2:
        return focus
    user, answer = history[-2:]
    if (user['role'] != 'user' or answer['role'] != 'assistant' or user['truncated'] or answer['truncated']
            or tail.get('message_id') != answer['message_id'] or not request_focus(user['content'])):
        return focus
    evidence = explicit_evidence(user['content'], focus['target'], focus['field'])
    if evidence:
        focus['user_evidence'] = {**evidence, 'message_id': user['message_id']}
    return focus


def omit_unrelated_identifier(context, draft):
    """Drop only a standalone restatement of a different object's user-supplied identifier."""
    focus = context.get('identifier_request')
    if not focus or len(draft) > 4096:
        return draft
    q = context['current_request']
    if re.search(r'比較|解釋|差異|為什麼|列出|兩者|一起|都要|也要|引述|重述|保留|省略|刪除|分別|全部', q):
        return draft
    first, separator, rest = draft.partition('。')
    if not separator:
        return draft
    head = re.fullmatch(rf'(?:根據(?:您|你)的(?:說明|描述)[，,]\s*)?([\u4e00-\u9fff]{{2,24}}?)'
                        rf'的(?:標示)?({FIELD})(?:是|為)\s*({CODE})', first.strip())
    if not head or head[1] == focus['target'] or head[2] != focus['field']:
        return draft
    fact = re.escape(head[1]) + r'(?:的|標示|寫著)' + head[2] + r'(?:是|為|[：:]|\s)\s*' + re.escape(head[3])
    if not re.search(r'(?:^|[。！？；，,])\s*' + fact + r'(?=[。！？；，,]|$)', QUOTED.sub('■', q)):
        return draft
    rest = rest.lstrip()
    if (focus['target'] not in rest or focus['field'] not in rest or not re.search(r'無法確認|無法得知|未提供', rest)
            or not re.search(r'請(?:提供|核對)|能否提供', rest) or re.search(r'前者|另一個|上述|相比|前述', rest)
            or head[3] in rest):
        return draft
    return rest.removeprefix('然而，').lstrip()


def supplied_code(context, draft):
    """Shorten a model conclusion only when the user supplies the same object/value."""
    question = context['current_request']
    plain = QUOTED.sub('■', question)
    only = ONLY_FIELD.search(question)
    if (not ONLY_FIELD.search(plain) or re.search(r'假設|暫定|可能|作廢|記錯|如果|若是|例如|舉例|不確定|改寫|翻譯', plain)
            or re.search(r'[「『“"\x60]', plain)):
        return None
    if not only or len(draft) > 4096 or re.search(r'可能|不確定|無法|假設|猜測|不是|並非|不符|待確認|或許|錯誤', draft):
        return None
    field = only[1]
    focus = context_focus(context)
    if focus and focus.get('retargeted'):
        value = focus.get('user_evidence', {}).get('value')
        if focus['field'] != field or not value:
            return None
        target = focus['target']
    elif focus and focus['field'] == field:
        target = focus['target']
        evidence = re.findall(rf'(?:^|[。！？；，,])\s*{re.escape(target)}的{field}(?:是|為|[：:])\s*({CODE})(?=[。！？；，,]|$)', plain)
        if len(evidence) != 1:
            return None
        value = evidence[0]
    else:
        # A package marking without its own field name is meaningful only as
        # a direct reply to a same-session request for that exact object/field.
        history = context.get('recent_conversation', [])
        tail = context.get('conversation_tail') or {}
        if len(history) < 2 or tail.get('message_id') != history[-1]['message_id']:
            return None
        user, reply = history[-2:]
        if (user['role'] != 'user' or reply['role'] != 'assistant' or user['truncated'] or reply['truncated']
                or not (focus := request_focus(user['content'])) or focus['field'] != field):
            return None
        target = focus['target']
        if target not in reply['content'] or field not in reply['content'] or not re.search(r'提供|核對|能否', reply['content']):
            return None
        provided = re.fullmatch(rf'(?:我)?在{re.escape(target)}的(?:原包裝|包裝|標籤|登記紀錄)(?:上)?'
                                rf'(?:找到|看到|讀到|查到)(?:了)?\s*({CODE})', question[:only.start()].strip())
        if not provided:
            return None
        value = provided[1]
    if not re.search(r'[0-9]', value) or set(re.findall(CODE, draft)) != {value}:
        return None
    statement = rf'(?:^|[。！；，,\n])\s*{re.escape(target)}的{field}(?:是|為|[：:])\s*(?:\*\*)?{re.escape(value)}(?:\*\*)?\s*(?=[。\n]|$)'
    return value if re.search(statement, draft) else None
