"""Bind explicitly requested identifiers to their named object in daily model context."""
import re

FIELD = r'(?:序號|編號|型號|代碼)'
REQUEST = re.compile(rf'(?:^|[。！？；\n])\s*(?:我要的是|我想查|請查|請問)'
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
    if len(matches) != 1:
        return None
    target, field = matches[0].groups()
    if (re.search(r'或|以及|和|跟|還是|兩者|這個|那個|上述|前者|後者', target)
            or re.search(r'改查|改問|其實|更正|改成|改要|換成|換個|算了|先別|不要查|不必追問|不要追問', plain[matches[0].end():])):
        return None
    return {'target': target, 'field': field,
            'guidance': '僅使用明確屬於指定物件的欄位值。若已有該值，按本輪要求直接回答。'
                        '若缺少該值，先點明是哪個物件的哪個欄位無法確認，然後只問一個能取得該欄位的問題，'
                        '例如能否提供該物件原包裝或登記紀錄上記載的值。不要泛問其他特徵，也不要複述其他物件的值。'
                        '若使用者指定缺資料時的回覆，遵照該格式。'}


def missing_literal(question, draft):
    missing = re.search(r'(?:若|如果)(?:沒有資料|無法確認|無法得知)[，,]\s*(?:請)?只(?:回答|回覆|輸出)'
                        r'\s*(?:「(不詳|資料不足|資訊不足|無法確定)」|(不詳|資料不足|資訊不足|無法確定))[。！]?$', question)
    if not missing or len(draft) > 4096 or re.search(r'[A-Za-z0-9]', draft):
        return None
    focus = request_focus(question)
    if not focus or focus['target'] + '的' + focus['field'] not in draft:
        return None
    literal = missing[1] or missing[2]
    # Format the model's explicit conclusion; a conditional/quoted instruction
    # alone does not establish that the value is actually unavailable.
    if (re.search(r'沒有(?:相關)?資料|無法確認|無法得知|未提供', draft)
            and re.search(rf'(?:^|[。\n])(?:因此[，,])?我只能回答「{literal}」[。！]?(?:$|如果您能提供[^。！？]+。?$)', draft)):
        return literal
    return None


CODE = r'[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*'
ONLY_FIELD = re.compile(rf'(?:^|[。！？?，,；;])\s*(?:請)?只(?:給|回答|回覆|輸出)({FIELD})[。！]?$')


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
    focus = request_focus(question)
    if focus and focus['field'] == field:
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
