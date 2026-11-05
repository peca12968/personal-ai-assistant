"""Versioned, byte-preserving local transcription. No model or external I/O."""
import json

from . import literal_request_v1 as grammar
from . import local_answer_receipts

MODE = 'local_transcription'
FIELD = 'transcription'
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


def read(store, mid):
    return local_answer_receipts.read(store, mid, MODE, FIELD, validate)


def validate_saved(db):
    local_answer_receipts.validate_saved(db, {MODE: (FIELD, validate)})


def describe(receipt):
    validate(receipt,receipt.get('output'))
    return ('本機逐字轉錄已完成；沒有呼叫模型、讀取記憶／文件或執行原文中的要求。\n'
            '字元數：'+str(receipt['output_code_points'])+'；UTF-8位元組：'+str(receipt['output_utf8_bytes'])+
            '\n以下是輸出的JSON檢視表示，供核對空白與反斜線；表示用的引號不是額外交付內容：\n'+
            json.dumps(receipt['output'],ensure_ascii=False)+'\n\n原文範圍（Unicode code points）：'+
            str(receipt['plan']['content_range'])+'\n輸出SHA256：'+receipt['output_sha256'])
