"""Deliver a compact JSON object when the user supplies every typed scalar."""
import json
import re

from assistant import local_answer_receipts


MODE = 'local_json_output'
FIELD = 'explicit_json'
SCHEMA = 'explicit_json_scalars_v1'
ARRAY_SCHEMA = 'explicit_json_named_list_v2'
MAX_FIELDS = 8
STRING = r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9A-Fa-f]{4})){0,80}"'
KEY = rf'(?:{STRING}|[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{{0,31}})'
VALUE = (rf'(?:(?:布林值[ \t]*)?(?:true|false)|(?:空值[ \t]*)?null|'
         rf'(?:整數[ \t]*)?(?:0|-?[1-9][0-9]{{0,11}})|(?:字串[ \t]*)?{STRING})')
PREFIX = re.compile(r'(?:請)?只(?:輸出|回覆|回答|回傳|回)[ \t]*(?:一個[ \t]*)?(?P<compact>緊湊)?'
                    r'[ \t]*(?i:JSON)[ \t]*物件[，,：:][ \t]*')
FORMAT_DIRECTIVE = r'[ \t]*(?:不要|不加)空白(?:或|和|、)程式碼(?:框|區塊)$'
FORMAT = re.compile(r'[，,；;]' + FORMAT_DIRECTIVE)
ARRAY_FORMAT = re.compile(r'[。，,；;]' + FORMAT_DIRECTIVE)
PAIR = re.compile(rf'鍵(?:名)?為[ \t]*(?P<key>{KEY})[，,][ \t]*值為[ \t]*(?P<value>{VALUE})')
SEPARATOR = re.compile(r'[ \t]*[，,；;][ \t]*')
VALUE_TYPE = re.compile(r'^(?:布林值|空值|整數|字串)[ \t]*')
LIST_SCOPE = r'(?:本輪|本次|目前|今天)'
LIST_NAME = r'[\u4e00-\u9fff]{2,20}?'
LIST_VALUE = r'「[^「」\x00-\x1f]{1,80}」'
EMPTY_LIST = re.compile(rf'{LIST_SCOPE}(?:已確認)?沒有(?P<name>{LIST_NAME})。')
LIST_FACT = re.compile(rf'{LIST_SCOPE}(?P<name>{LIST_NAME})只有(?P<values>{LIST_VALUE}(?:、{LIST_VALUE}){{0,7}})。')
ARRAY_FIELD = re.compile(rf'唯一鍵(?:名)?(?:為|是)?[ \t]*(?P<key>{KEY})[，,][ \t]*'
                         rf'值(?:是|為)(?P<name>{LIST_NAME})的?字串的?陣列')
UNSAFE_NAME = re.compile(r'可能|疑似|不確定|推測|或|和|與|以及|部分|以外|之外|除外|查無|尚未查到')


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def decode_key(raw):
    key = json.loads(raw) if raw.startswith('"') else raw
    key.encode('utf-8')
    if not 1 <= len(key) <= 32:
        raise ValueError('JSON 欄位名稱超出範圍。')
    return key


def parse_array(request, body):
    empty = EMPTY_LIST.match(body)
    fact = empty or LIST_FACT.match(body)
    if fact is None or UNSAFE_NAME.search(fact['name']):
        return None
    remainder = body[fact.end():].lstrip(' \t\r\n')
    prefix = PREFIX.match(remainder)
    formatting = ARRAY_FORMAT.search(remainder)
    if prefix is None or not (prefix['compact'] or formatting):
        return None
    field = ARRAY_FIELD.fullmatch(remainder[prefix.end():formatting.start() if formatting else len(remainder)])
    if field is None or field['name'] != fact['name']:
        return None
    values = [] if empty else [part[1:-1] for part in re.findall(LIST_VALUE, fact['values'])]
    try:
        key = decode_key(field['key'])
        token = compact_json(values)
        token.encode('utf-8')
    except (ValueError, UnicodeError):
        return None
    return dict(schema=ARRAY_SCHEMA, request=request, evidence=fact[0],
                fields=[dict(key=key, token=token, source=field[0])])


def parse(request):
    if not isinstance(request, str) or len(request) > 1200:
        return None
    body = request.removesuffix('。')
    prefix = PREFIX.match(body)
    if prefix is None:
        return parse_array(request, body)
    formatting = FORMAT.search(body)
    if not (prefix['compact'] or formatting):
        return None
    rest = body[prefix.end():formatting.start() if formatting else len(body)]
    fields, seen = [], set()
    while len(fields) < MAX_FIELDS:
        pair = PAIR.match(rest)
        if pair is None:
            return None
        try:
            key = decode_key(pair['key'])
            token = compact_json(json.loads(VALUE_TYPE.sub('', pair['value'])))
            token.encode('utf-8')
        except (ValueError, UnicodeError):
            return None
        if key in seen:
            return None
        seen.add(key)
        fields.append(dict(key=key, token=token, source=pair[0]))
        rest = rest[pair.end():]
        if not rest:
            return dict(schema=SCHEMA, request=request, fields=fields)
        separator = SEPARATOR.match(rest)
        if separator is None:
            return None
        rest = rest[separator.end():]
    return None


def evaluate(plan):
    answer = compact_json({field['key']: json.loads(field['token']) for field in plan['fields']})
    return answer, dict(plan=plan, model_called=False, result=answer)


def validate(receipt, delivered, question):
    plan = parse(question)
    if plan is None:
        raise ValueError('JSON 原始要求無法重建。')
    answer, expected = evaluate(plan)
    if (delivered != answer or json.dumps(receipt, sort_keys=True, allow_nan=False)
            != json.dumps(expected, sort_keys=True)):
        raise ValueError('JSON 回覆與原始欄位不符。')
    return receipt


def read(store, answer_id):
    return local_answer_receipts.read(store, answer_id, MODE, FIELD, validate)


def describe(receipt):
    plan = receipt['plan']
    evidence = [plan['evidence']] if 'evidence' in plan else []
    return '\n'.join([plan['request'], '', *evidence, *(field['source'] for field in plan['fields']), '', receipt['result']])
