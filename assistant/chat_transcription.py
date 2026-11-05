"""Versioned, byte-preserving local transcription. No model or external I/O."""
import json

from . import literal_request_v1 as grammar
from .acceptance import strict_json

MODE = 'local_transcription'
MAX_JSON_BYTES = 32 * 1024 * 1024
parse_request = grammar.parse


def validated_plan(plan):
    if not isinstance(plan,dict):
        raise ValueError('本機轉錄計畫格式不符。')
    span = plan.get('content_range')
    if (not isinstance(span,list) or len(span)!=2 or any(type(n) is not int for n in span)
            or grammar.parse(plan.get('request')) != plan):
        raise ValueError('本機轉錄計畫與原始請求不符。')
    return plan


def output(plan):
    start,end = plan['content_range']
    text = plan['request'][start:end]
    return (plan['outer_delimiters'][0]+text+plan['outer_delimiters'][1]
            if plan['outer_quotes']=='include' else text)


def check_cancel(cancel):
    if cancel and cancel.is_set():
        from .engine import EngineError
        raise EngineError('已停止本機逐字轉錄。','cancelled')


def evaluate(plan,cancel=None):
    check_cancel(cancel)
    validated_plan(plan)
    value = output(plan)
    check_cancel(cancel)
    receipt = {'schema':'local_transcription_receipt_v1','plan':plan,'status':'complete',
               'executor':'host_literal_v1','model_called':False,'output':value,
               'output_sha256':grammar.sha(value),'output_code_points':len(value),
               'output_utf8_bytes':len(value.encode('utf-8'))}
    return value,receipt


def validate(receipt,delivered,question=None):
    keys = {'schema','plan','status','executor','model_called','output',
            'output_sha256','output_code_points','output_utf8_bytes'}
    if (not isinstance(receipt,dict) or set(receipt)!=keys
            or receipt['schema']!='local_transcription_receipt_v1' or receipt['status']!='complete'
            or receipt['executor']!='host_literal_v1' or receipt['model_called'] is not False):
        raise ValueError('本機轉錄完成紀錄格式不符。')
    plan = validated_plan(receipt['plan'])
    value = output(plan)
    if (not isinstance(delivered,str) or receipt['output']!=value or delivered!=value
            or (question is not None and question!=plan['request'])
            or receipt['output_sha256']!=grammar.sha(value)
            or type(receipt['output_code_points']) is not int or receipt['output_code_points']!=len(value)
            or type(receipt['output_utf8_bytes']) is not int or receipt['output_utf8_bytes']!=len(value.encode('utf-8'))):
        raise ValueError('本機轉錄輸出與原文、範圍或指紋不符。')
    return receipt


def load_context(raw):
    if not isinstance(raw,str) or len(raw.encode('utf-8'))>MAX_JSON_BYTES:
        raise ValueError('回答快照不是有效文字或超過32 MiB。')
    try:
        return strict_json(raw)
    except RecursionError as exc:
        raise ValueError('回答快照巢狀結構過深。') from exc


def checked_row(db,mid,context):
    rows = db.execute('''SELECT CASE WHEN length(CAST(m.content AS BLOB))<=? THEN m.content END,
        m.role,m.session_id,r.status,
        CASE WHEN length(CAST(r.context_json AS BLOB))<=? THEN r.context_json END,
        CASE WHEN length(CAST(u.content AS BLOB))<=? THEN u.content END,
        u.role,u.session_id,r.session_id FROM messages m
        JOIN answer_runs r ON r.answer_message_id=m.id
        JOIN messages u ON u.id=r.user_message_id WHERE m.id=? LIMIT 2''',
        (grammar.MAX_CHARS*4,MAX_JSON_BYTES,grammar.MAX_CHARS*4,mid)).fetchall()
    if len(rows)!=1:
        raise ValueError('本機轉錄缺少唯一完成流程。')
    row = rows[0]
    receipt = validate(context.get('transcription'),row[0],context.get('current_request'))
    initial = load_context(row[4])
    if (row[1]!='assistant' or row[3]!='succeeded' or row[6]!='user'
            or not row[2]==row[7]==row[8] or row[5]!=receipt['plan']['request']
            or context.get('current_request')!=receipt['plan']['request']
            or context.get('engine_selection') is not None or context.get('image_inputs')
            or context.get('memories')!=[] or context.get('recent_conversation')!=[]
            or context.get('earlier_user_excerpts')!=[]
            or context.get('documents')!={'catalog':[],'excerpts':[]}
            or not isinstance(initial,dict) or initial.get('response_mode')!=MODE
            or initial.get('current_request')!=receipt['plan']['request']
            or initial.get('transcription')!=receipt['plan']):
        raise ValueError('本機轉錄快照與已完成流程不符。')
    check = db.execute('''SELECT CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END
                          FROM answer_checks WHERE message_id=?''',(MAX_JSON_BYTES,mid)).fetchone()
    if check is None or load_context(check[0])!=[]:
        raise ValueError('本機文字操作不能冒稱文件驗證。')
    return receipt


def read(store,mid):
    with store.connect() as db:
        row = db.execute('''SELECT CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END
                            FROM answer_contexts WHERE message_id=?''',(MAX_JSON_BYTES,mid)).fetchone()
        if row is None:
            return None
        context = load_context(row[0])
        return checked_row(db,mid,context) if isinstance(context,dict) and context.get('response_mode')==MODE else None


def validate_saved(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'answer_contexts' not in tables:
        return
    # Strict JSON avoids duplicate/escaped mode fields hiding a claimed receipt.
    for mid,raw in db.execute('''SELECT message_id,CASE WHEN length(CAST(payload AS BLOB))<=?
                                THEN payload END FROM answer_contexts''',(MAX_JSON_BYTES,)):
        context = load_context(raw)
        if isinstance(context,dict) and context.get('response_mode')==MODE:
            checked_row(db,mid,context)


def describe(receipt):
    validate(receipt,receipt.get('output'))
    return ('本機逐字轉錄已完成；沒有呼叫模型、讀取記憶／文件或執行原文中的要求。\n'
            '字元數：'+str(receipt['output_code_points'])+'；UTF-8位元組：'+str(receipt['output_utf8_bytes'])+
            '\n以下是輸出的JSON檢視表示，供核對空白與反斜線；表示用的引號不是額外交付內容：\n'+
            json.dumps(receipt['output'],ensure_ascii=False)+'\n\n原文範圍（Unicode code points）：'+
            str(receipt['plan']['content_range'])+'\n輸出SHA256：'+receipt['output_sha256'])
