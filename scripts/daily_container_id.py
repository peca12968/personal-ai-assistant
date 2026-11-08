"""Resolve an exact current container identifier from this conversation's user statements."""
import json
import re

from assistant import evidence_logic, local_answer_receipts


MODE = 'local_calculation'
FIELD = 'current_container_id'
SCHEMA = 'current_container_id_v1'
MAX_USER_TURNS = 8
MAX_SOURCE_BYTES = 4096
ITEM = r'[\u4e00-\u9fffA-Za-z0-9]{2,24}?'
CONTAINER = r'箱子|櫃子|收納盒|容器'
ALNUM = r'[A-Za-z0-9Ａ-Ｚａ-ｚ０-９]'
CODE = rf'{ALNUM}(?:[A-Za-z0-9Ａ-Ｚａ-ｚ０-９_＿\-－]{{0,30}}{ALNUM})?'
UNSAFE_ITEM = re.compile(r'或|以及|與|和|另外|這個|那個|這些|那些|前者|後者|上述|可能|疑似|暫定|例如|範例|請|如果|假設')
REQUEST = re.compile(
    rf'(?:請)?(?:依|根據)(?:最新(?:資訊|紀錄|更正)|最後一次更正)[，,][ \t]*'
    rf'(?:請)?只(?:回|回答|回覆|輸出)(?P<item>{ITEM})(?:目前)?所在的?'
    rf'(?P<container>{CONTAINER})的編號[。！]?'
)
PLACEMENT = re.compile(
    rf'(?P<item>{ITEM})(?:(?:已)?(?:換|移|改放)到|(?:目前|現在)?(?:放在|存放在|位於))'
    rf'編號[ \t]*(?P<code>{CODE})[ \t]*的(?P<container>{CONTAINER})'
)
UNCHANGED = re.compile(rf'(?P<item>{ITEM})(?:的)?(?:箱子|櫃子|收納盒|容器|箱|櫃)(?:照舊|維持原狀)')
UPDATE_PREFIX = re.compile(r'^(?:最新)?(?:更正|更新)[，,：:][ \t]*')


def parse(request):
    if not isinstance(request, str) or len(request) > 160:
        return None
    match = REQUEST.fullmatch(request)
    return match.groupdict() if match and not UNSAFE_ITEM.search(match['item']) else None


def inspect_message(text, focus):
    """Return whether a user statement is fully understood and its optional matching placement."""
    clauses = re.split(r'[，,。；;]', UPDATE_PREFIX.sub('', text.strip()).removesuffix('。'))
    selected = None
    target = focus['item']
    target_key = target.casefold()
    for part in clauses:
        clause = part.strip(' \t')
        placement = PLACEMENT.fullmatch(clause)
        statement = placement or UNCHANGED.fullmatch(clause)
        if statement is None or UNSAFE_ITEM.search(statement['item']):
            return False, None
        item = statement['item']
        item_key = item.casefold()
        if item_key not in target_key and target_key not in item_key:
            continue
        if (item != target or placement is None or placement['container'] != focus['container']
                or selected is not None):
            return False, None
        selected = dict(quote=clause, code=placement['code'])
    return True, selected


def lookup(db, sid, request, before):
    focus = parse(request)
    if focus is None:
        return None
    rows = db.execute('''SELECT m.id, CASE WHEN length(CAST(m.content AS BLOB))<=? THEN m.content END,
                         EXISTS(SELECT 1 FROM local_memory_messages l WHERE l.message_id=m.id)
                         FROM messages m WHERE m.session_id=? AND m.role='user'
                         AND m.id<=COALESCE(?-1,9223372036854775807)
                         ORDER BY m.id DESC LIMIT ?''',
                      (MAX_SOURCE_BYTES, sid, before, MAX_USER_TURNS))
    trail = []
    for mid, text, local in rows:
        if not isinstance(text, str) or local:
            return None
        trail.append(dict(message_id=str(mid), sha256=evidence_logic.digest(text)))
        if parse(text.strip()) is not None:
            continue
        understood, selected = inspect_message(text, focus)
        if not understood:
            return None
        if selected is not None:
            return dict(schema=SCHEMA, request=request, **focus, **selected, trail=trail)
    return None


def capture(store, sid, request, before=None):
    with store.connect() as db:
        return lookup(db, sid, request, before)


def evaluate(plan):
    return plan['code'], dict(plan=plan, model_called=False, result=plan['code'])


def read(store, answer_id):
    with store.connect() as db:
        db.execute('BEGIN')
        rows = db.execute('''SELECT m.session_id, r.user_message_id,
            CASE WHEN length(CAST(c.payload AS BLOB))<=? THEN c.payload END,
            CASE WHEN length(CAST(r.context_json AS BLOB))<=? THEN r.context_json END
            FROM answer_contexts c JOIN messages m ON m.id=c.message_id
            JOIN answer_runs r ON r.answer_message_id=m.id WHERE m.id=? LIMIT 2''',
                          (local_answer_receipts.MAX_JSON_BYTES, local_answer_receipts.MAX_JSON_BYTES,
                           answer_id)).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError('編號查找缺少唯一的回答流程。')
        sid, before, raw, original = rows[0]
        context = local_answer_receipts.load_context(raw)
        if not isinstance(context, dict) or FIELD not in context:
            return None
        if context.get('response_mode') != MODE or type(before) is not int or not 0 < before < answer_id:
            raise ValueError('編號查找的模式或原問順序不符。')
        initial = local_answer_receipts.load_context(original)
        if (not isinstance(initial, dict) or initial.get('image_inputs') or initial.get('engine_selection') is not None
                or initial.get('documents') != {'catalog': [], 'excerpts': []}):
            raise ValueError('原始編號查找的資料範圍不符。')

        def validate(receipt, delivered, question):
            plan = lookup(db, sid, question, before)
            if plan is None:
                raise ValueError('目前無法重建編號的來源。')
            answer, expected = evaluate(plan)
            if (delivered != answer or json.dumps(receipt, sort_keys=True, allow_nan=False)
                    != json.dumps(expected, sort_keys=True)):
                raise ValueError('編號查找與原始來源或交付不符。')
            return receipt

        return local_answer_receipts.checked_row(db, answer_id, context, MODE, FIELD, validate)


def describe(receipt):
    plan = receipt['plan']
    return '\n'.join([plan['request'], '', f'採用的使用者訊息 #{plan["trail"][-1]["message_id"]}',
                      plan['quote'], '', f'{plan["item"]}所在{plan["container"]}的編號為 {receipt["result"]}'])
