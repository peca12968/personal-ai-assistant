"""Recompute explicitly scoped counts from the current user message."""
import json
import re

from assistant import local_answer_receipts


MODE = 'local_calculation'
FIELD = 'counted_items'
SCHEMA = 'counted_items_v1'
TOTAL_SCHEMA = 'counted_items_exhaustive_v2'
UNITS = '支塊顆枚件個張份條瓶本卷片枝冊'
DIGITS = '零一二三四五六七八九'
POSITIVE = '一二兩三四五六七八九'
COUNT = rf'(?:[0-9]{{1,6}}|[零{POSITIVE}]|[{POSITIVE}]?十[一二三四五六七八九]?)'
STOCK = re.compile(
    rf'(?:原?有[ \t]*)?(?P<groups>{COUNT})[ \t]*(?P<group_unit>[箱盒袋包組籃盤捆])[ \t]*'
    rf'(?:原先|原本)?(?:各|每(?P=group_unit))(?:裝有?|有|放了?)[ \t]*(?P<each>{COUNT})[ \t]*'
    rf'(?P<unit>[{UNITS}])[ \t]*(?P<item>[\u4e00-\u9fff]{{1,12}})'
)
UNSAFE_ITEM = re.compile(
    r'[零一二兩三四五六七八九十百千萬和與及或但另各每只約共多不少未無除等剩]'
    r'|例如|以上|以下|是否|好像|可能|損壞|破損|故障|壞掉|壞了|報廢|失效'
    r'|取出|拿走|移走|用掉|吃掉|丟掉|遺失|失蹤'
)
DEFECT = re.compile(
    rf'(?:(?:開箱|拆封|檢查|清點)後)?(?:發現|確認|查出)(?:其中|當中)[ \t]*(?P<loss>{COUNT})[ \t]*'
    rf'(?P<unit>[{UNITS}])(?:都)?(?:損壞|破損|不能用|無法使用)(?P<remainder>[，,][ \t]*其餘(?:皆|都)?可用)?'
)
REMOVAL = re.compile(rf'已?從這批(?:取出|移出|拿走)[ \t]*(?P<loss>{COUNT})[ \t]*(?P<unit>[{UNITS}])')
OUTPUT = re.compile(
    rf'(?:請)?只(?:輸出|回答|回覆|回傳|回)(?P<remaining>剩下的?|剩餘)?(?P<usable>可用)?'
    rf'(?:(?P<unit>[{UNITS}])數|數量)[，,][ \t]*(?:用|使用)阿拉伯數字'
)
NUMBER_ONLY = r'(?:請)?只(?:輸出|回答|回覆|回傳|回)(?:阿拉伯)?數字'
QUESTION = re.compile(
    rf'(?:請問)?(?P<remaining>剩下的?|剩餘)?(?P<usable>可用)?'
    rf'(?:(?P<unit>[{UNITS}])數|數量)(?:還有|是|有)?多少[？?][ \t]*{NUMBER_ONLY}'
)
TOTAL_STOCK = re.compile(
    rf'(?:已有|原有)[ \t]*(?P<groups>{COUNT})[ \t]*(?P<group_unit>[箱盒袋包組籃盤捆])[ \t]*'
    rf'(?:各|每(?P=group_unit))[ \t]*(?P<each>{COUNT})[ \t]*'
    rf'(?P<unit>[{UNITS}])[ \t]*(?P<item>[\u4e00-\u9fff]{{1,12}})'
)
TOTAL_DEFECT = re.compile(
    rf'(?:檢查|清點|盤點)(?:後|完)(?:這批|本批)?共(?:有|計)[ \t]*(?P<loss>{COUNT})[ \t]*'
    rf'(?P<unit>[{UNITS}])(?:都)?(?:損壞|破損|不能用|無法使用)'
)
TOTAL_OUTPUT = re.compile(r'(?:請)?只(?:輸出|回答|回覆|回傳|回)可用(?P<item>[\u4e00-\u9fff]{1,12})的阿拉伯數字總數')


def number(raw):
    if raw.isascii():
        return int(raw)
    raw = raw.replace('兩', '二')
    tens, ten, ones = raw.partition('十')
    if ten:
        return (DIGITS.index(tens) if tens else 1) * 10 + (DIGITS.index(ones) if ones else 0)
    return DIGITS.index(tens)


def parse(request):
    if not isinstance(request, str) or len(request) > 400:
        return None
    clauses = [part.strip(' \t') for part in request.removesuffix('。').split('。')]
    if len(clauses) != 3:
        return None
    stock = STOCK.fullmatch(clauses[0])
    if stock is None:
        return parse_total_loss(request, clauses)
    if UNSAFE_ITEM.search(stock['item']):
        return None
    defect = DEFECT.fullmatch(clauses[1])
    loss = defect or REMOVAL.fullmatch(clauses[1])
    output = OUTPUT.fullmatch(clauses[2]) or QUESTION.fullmatch(clauses[2])
    if loss is None or output is None:
        return None
    if (defect and not output['usable']) or (not defect and (not output['remaining'] or output['usable'])):
        return None
    unit = stock['unit']
    if loss['unit'] != unit or output['unit'] not in (None, unit):
        return None
    groups, each, deducted = number(stock['groups']), number(stock['each']), number(loss['loss'])
    total = groups * each
    if not total or deducted > total or (defect and deducted < total and not defect['remainder']):
        return None
    return counted_plan(SCHEMA, request, clauses, stock, groups, each, deducted,
                        'unusable' if defect else 'removed')


def parse_total_loss(request, clauses):
    stock = TOTAL_STOCK.fullmatch(clauses[0])
    loss = TOTAL_DEFECT.fullmatch(clauses[1])
    output = TOTAL_OUTPUT.fullmatch(clauses[2])
    if (not stock or not loss or not output or UNSAFE_ITEM.search(stock['item'])
            or stock['unit'] != loss['unit'] or stock['item'] != output['item']):
        return None
    groups, each, deducted = number(stock['groups']), number(stock['each']), number(loss['loss'])
    if not groups or not each or deducted > groups * each:
        return None
    return counted_plan(TOTAL_SCHEMA, request, clauses, stock, groups, each, deducted, 'unusable')


def counted_plan(schema, request, clauses, stock, groups, each, deducted, deduction):
    return {'schema': schema, 'request': request, 'clauses': clauses, 'unit': stock['unit'],
            'item': stock['item'], 'group_unit': stock['group_unit'],
            'groups': str(groups), 'each': str(each), 'deducted': str(deducted),
            'deduction': deduction}


def evaluate(plan):
    answer = str(int(plan['groups']) * int(plan['each']) - int(plan['deducted']))
    return answer, {'plan': plan, 'result': answer, 'model_called': False}


def validate(receipt, delivered, question):
    plan = parse(question)
    if plan is None:
        raise ValueError('原始問題無法重建本機數量計算。')
    answer, expected = evaluate(plan)
    if (answer != delivered or json.dumps(receipt, sort_keys=True, allow_nan=False)
            != json.dumps(expected, sort_keys=True)):
        raise ValueError('本機數量計算與原始問題或交付結果不符。')
    return receipt


def read(store, answer_id):
    with store.connect() as db:
        db.execute('BEGIN')
        row = db.execute('''SELECT CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END
                            FROM answer_contexts WHERE message_id=?''',
                         (local_answer_receipts.MAX_JSON_BYTES, answer_id)).fetchone()
        if row is None:
            return None
        context = local_answer_receipts.load_context(row[0])
        if not isinstance(context, dict) or FIELD not in context:
            return None
        if context.get('response_mode') != MODE:
            raise ValueError('本機數量計算的回答模式不符。')
        return local_answer_receipts.checked_row(db, answer_id, context, MODE, FIELD, validate)


def describe(receipt):
    plan = receipt['plan']
    calculation = f"{plan['groups']} × {plan['each']} − {plan['deducted']} = {receipt['result']} {plan['unit']}"
    return '\n'.join([*plan['clauses'], '', calculation, '本次答案由本機計算。'])
