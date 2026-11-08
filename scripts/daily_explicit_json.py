"""Deliver a compact JSON object when the user supplies every typed scalar."""
import json
import re

from assistant import local_answer_receipts


MODE = 'local_json_output'
FIELD = 'explicit_json'
SCHEMA = 'explicit_json_scalars_v1'
MAX_FIELDS = 8
STRING = r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9A-Fa-f]{4})){0,80}"'
KEY = rf'(?:{STRING}|[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{{0,31}})'
VALUE = (rf'(?:(?:布林值[ \t]*)?(?:true|false)|(?:空值[ \t]*)?null|'
         rf'(?:整數[ \t]*)?(?:0|-?[1-9][0-9]{{0,11}})|(?:字串[ \t]*)?{STRING})')
PREFIX = re.compile(r'(?:請)?只(?:輸出|回覆|回答|回傳|回)[ \t]*(?:一個[ \t]*)?(?P<compact>緊湊)?'
                    r'[ \t]*(?i:JSON)[ \t]*物件[，,：:][ \t]*')
FORMAT = re.compile(r'[，,；;][ \t]*(?:不要|不加)空白(?:或|和|、)程式碼(?:框|區塊)$')
PAIR = re.compile(rf'鍵(?:名)?為[ \t]*(?P<key>{KEY})[，,][ \t]*值為[ \t]*(?P<value>{VALUE})')
SEPARATOR = re.compile(r'[ \t]*[，,；;][ \t]*')
VALUE_TYPE = re.compile(r'^(?:布林值|空值|整數|字串)[ \t]*')


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def parse(request):
    if not isinstance(request, str) or len(request) > 1200:
        return None
    body = request.removesuffix('。')
    prefix = PREFIX.match(body)
    formatting = FORMAT.search(body)
    if prefix is None or not (prefix['compact'] or formatting):
        return None
    rest = body[prefix.end():formatting.start() if formatting else len(body)]
    fields, seen = [], set()
    while len(fields) < MAX_FIELDS:
        pair = PAIR.match(rest)
        if pair is None:
            return None
        try:
            key = json.loads(pair['key']) if pair['key'].startswith('"') else pair['key']
            token = compact_json(json.loads(VALUE_TYPE.sub('', pair['value'])))
            key.encode('utf-8')
            token.encode('utf-8')
        except (ValueError, UnicodeError):
            return None
        if not 1 <= len(key) <= 32 or key in seen:
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
    return '\n'.join([plan['request'], '', *(field['source'] for field in plan['fields']), '', receipt['result']])
