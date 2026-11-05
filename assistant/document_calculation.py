"""Two explicitly bound document operands and reproducible exact arithmetic."""
import json
import re
import unicodedata
from decimal import Decimal

from . import exact_math, local_answer_receipts
from .document_ranges import STATUS_TEXT, requested_ranges, select_ranges
from .expression_math import check_cancel
from .response_delivery import digest


MODE = 'local_document_calculation'
FIELD = 'document_calculation'
_LABEL = r'[\u4e00-\u9fffA-Za-z0-9_]{1,24}'
_UNIT = r'(?:[\u4e00-\u9fff]{1,8}|[A-Za-z]{1,8})'
_INDEX = r'[1-9][0-9]{0,11}'
_REF = rf'(?:\[D{_INDEX}:L{_INDEX}\]|D{_INDEX} *第 *{_INDEX} *行)'
_FORMULA = re.compile(
    rf'文件問答: *(?:請)?計算 *({_REF}) *的({_LABEL}) *(加上|減去|[+-]) *'
    rf'({_REF}) *的({_LABEL}), *單位為 *({_UNIT})[。?]?')
_WEIGHT = re.compile(
    rf'文件問答: *(?:請)?用 *({_REF}) *的(?P<left>{_LABEL}?)(?:的)?重量 *(?:和|與) *'
    rf'({_REF}) *的(?P<right>{_LABEL}?)(?:的)?重量 *,? *計算 *(?P=left)比(?P=right)重多少 *(公斤|公克|克|kg|g)[。?]?'
    r'(?: *若無法計算, *請說明缺少哪個範圍(?:, *並可列出目前有依據的重量)?。)?(?: *不要估算。?)?')
_DIGITS = '零一二三四五六七八九'
_NUMBER = rf'(?:-?[0-9]{{1,18}}(?:\.[0-9]{{1,8}})?|[零一二兩三四五六七八九]|[一二三四五六七八九]?十[一二三四五六七八九]?)'


def parse_request(question):
    if not isinstance(question, str) or len(question) > 320:
        return None
    text = unicodedata.normalize('NFKC', question).strip()
    if match := _WEIGHT.fullmatch(text):
        a, left, b, right, unit = match.groups()
        left, right, operation = left + '重量', right + '重量', 'difference'
    elif match := _FORMULA.fullmatch(text):
        a, left, op, b, right, unit = match.groups()
        operation = 'sum' if op in ('加上', '+') else 'difference'
    else:
        return None
    operands = []
    for reference, label in ((a, left), (b, right)):
        ranges, _ = requested_ranges(reference)
        document, line = ranges[0]['document_id'], ranges[0]['start_line']
        operands.append(dict(label=label, document_id=document, line_number=line, citation=f'[D{document}:L{line}]'))
    if operands[0]['citation'] == operands[1]['citation']:
        return None
    return dict(schema='document_calculation_plan_v1', request=question, operation=operation, unit=unit,
                operands=operands)


def snapshot(store, sid, plan):
    ids = {operand['document_id'] for operand in plan['operands']}
    sources = store.documents(sid, include_content=True, document_ids=ids)
    excerpts, ranges, limited = select_ranges(plan['request'], sources, max_excerpts=2, max_chars=14000)
    catalog = [dict(document_id=row['id'], name=row['name'], sha256=row['sha256'],
                    total_lines=len(row['content'].splitlines())) for row in sources]
    return dict(catalog=catalog, excerpts=excerpts, requested_ranges=ranges, requested_ranges_limited=limited,
                coverage_limited=sum(row['total_lines'] for row in catalog) > len(excerpts))


def _number(value):
    if value.isascii():
        return value
    if '十' not in value:
        return str(_DIGITS.index(value.replace('兩', '二')))
    tens, ones = value.split('十')
    return str(10 * (_DIGITS.index(tens) if tens else 1) + _DIGITS.index(ones or '零'))


def _value(text, label, unit):
    field = re.escape(label)
    if label != '重量' and label.endswith('重量'):
        subject = re.escape(label.removesuffix('重量'))
        field = rf'{subject}(?:樣本)?(?:的)?(?:實測)?重量'
    match = re.fullmatch(rf'{field} *(?:[:=]|為|是) *({_NUMBER}) *{re.escape(unit)}[。.]?',
                         unicodedata.normalize('NFKC', text).strip())
    return _number(match[1]) if match else None


def _observe(operand, requested, catalog, excerpts, unit):
    document, line = operand['document_id'], operand['line_number']
    if (requested.get('document_id'), requested.get('start_line'), requested.get('end_line')) != (document, line, line):
        raise ValueError('保存的文件範圍與原問不符。')
    status = requested.get('status')
    if status not in STATUS_TEXT or requested.get('explanation') != STATUS_TEXT[status]:
        raise ValueError('保存的文件範圍狀態無法核對。')
    source = catalog.get(document)
    if source is None:
        allowed = ('document_not_attached',)
    elif line > source['total_lines']:
        allowed = ('line_out_of_bounds',)
    else:
        allowed = ('provided', 'character_limit')
    if status not in allowed:
        raise ValueError('保存的文件範圍與來源清單不符。')
    rows = [row for row in excerpts if row['document_id'] == document and row['start_line'] == line]
    if status != 'provided':
        if rows:
            raise ValueError('保存的文件範圍狀態無法核對。')
        return dict(status=status)
    prefix = f'L{line}: '
    if (len(rows) != 1 or rows[0]['end_line'] != line or rows[0]['partial_line'] is not False
            or rows[0].get('requested_range') is not True
            or rows[0]['sha256'] != source['sha256'] or rows[0]['name'] != source['name']):
        raise ValueError('保存的文件行不完整。')
    text = rows[0]['text']
    if not text.startswith(prefix) or len(text) > 14000 or len(text.splitlines()) != 1:
        raise ValueError('保存的文件行與行號不符。')
    value = _value(text[len(prefix):], operand['label'], unit)
    if value is None:
        return dict(status='value_unconfirmed')
    return dict(status='confirmed', value=value, sha256=rows[0]['sha256'], original=text[len(prefix):])


def evaluate(plan, cancel=None):
    check_cancel(cancel)
    documents = plan['documents']
    request = parse_request(plan['request'])
    if (request is None or plan != {**request, 'documents': documents}
            or documents['requested_ranges_limited'] is not False
            or len(documents['requested_ranges']) != 2 or len(documents['catalog']) > 2
            or len(documents['excerpts']) != sum(row['status'] == 'provided' for row in documents['requested_ranges'])):
        raise ValueError('文件計算計畫與原問不符。')
    unit = request['unit']
    operands = request['operands']
    catalog = {row['document_id']: row for row in documents['catalog']}
    if (len(catalog) != len(documents['catalog']) or catalog.keys() - {row['document_id'] for row in operands}
            or any(type(row['total_lines']) is not int or row['total_lines'] < 0
                   or not re.fullmatch(r'[0-9a-f]{64}', row['sha256']) for row in catalog.values())):
        raise ValueError('保存的文件來源清單無法核對。')
    observed = [_observe(operand, row, catalog, documents['excerpts'], unit)
                for operand, row in zip(operands, documents['requested_ranges'], strict=True)]
    lines = []
    for operand, observation in zip(operands, observed, strict=True):
        label, status = operand['label'], observation['status']
        if status == 'confirmed':
            lines.append(f'{label}為 {observation["value"]} {unit}。 {operand["citation"]}')
        else:
            source = f'D{operand["document_id"]} 第{operand["line_number"]}行'
            reason = STATUS_TEXT[status] if status != 'value_unconfirmed' else f'無法從該行核對「{label}」的確定數值與「{unit}」單位。'
            lines.append(f'缺少可用的{label}（{source}）。{reason}')
    result = None
    if all(row['status'] == 'confirmed' for row in observed):
        values = [row['value'] for row in observed]
        operation = request['operation']
        result = exact_math.calculate(dict(operation=operation, values=values))
        value = result['exact'] if result['rounded'] else format(Decimal(result['decimal']), 'f')
        sign = '+' if operation == 'sum' else '−'
        left, right = (f'({number})' if number.startswith('-') else number for number in values)
        lines.insert(0, f'結果為 {value} {unit}（{left} {sign} {right}）。')
    else:
        lines.insert(0, '目前無法計算。請補上以下缺少或無法核對的指定資料。')
    check_cancel(cancel)
    answer = '\n'.join(lines)
    return answer, dict(schema='document_calculation_receipt_v1', plan=plan, model_called=False,
                        observations=observed, result=result, output=answer, output_sha256=digest(answer))


def validate(receipt, delivered, question=None):
    try:
        answer, expected = evaluate(receipt['plan'])
        if (answer == delivered and question == expected['plan']['request']
                and json.dumps(receipt, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True)):
            return receipt
    except (KeyError, TypeError, AttributeError, ValueError, RecursionError):
        pass
    raise ValueError('文件計算紀錄與原問、來源或結果不符。')


def read(store, mid):
    return local_answer_receipts.read(store, mid, MODE, FIELD, validate, document_based=True)
