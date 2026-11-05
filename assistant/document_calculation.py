"""Two explicitly bound document operands and reproducible exact arithmetic."""
import json
import re
import unicodedata
from decimal import Decimal

from . import document_table, document_units, exact_math, local_answer_receipts
from .document_ranges import STATUS_TEXT, requested_ranges, select_ranges
from .expression_math import check_cancel
from .response_delivery import digest


MODE = 'local_document_calculation'
FIELD = 'document_calculation'
CONVERSION_PLAN = 'document_calculation_plan_v2'
_LABEL = r'[\u4e00-\u9fffA-Za-z0-9_]{1,24}'
_UNIT = r'(?:[\u4e00-\u9fff]{1,8}|[A-Za-z]{1,8})'
_INDEX = r'[1-9][0-9]{0,11}'
_REF = rf'(?:\[D{_INDEX}:L{_INDEX}\]|D{_INDEX} *第 *{_INDEX} *行)'
_FORMULA = re.compile(
    rf'文件問答: *(?:請)?計算 *({_REF}) *的({_LABEL}) *(加上|減去|[+-]) *'
    rf'({_REF}) *的({_LABEL}), *單位為 *({_UNIT})[。?]?')
_CONVERSION = re.compile(rf'(文件問答:[^\r\n]+), *(?:統一|結果|再)?換算成 *({_UNIT})[。?]?')
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


def _parse_conversion(question):
    if not isinstance(question, str) or len(question) > 320:
        return None
    text = unicodedata.normalize('NFKC', question).strip()
    match = _CONVERSION.fullmatch(text)
    if match and match[2] in document_units.SCALES and (parsed := parse_request(f'{match[1]}, 單位為 {match[2]}')):
        return {**parsed, 'schema': CONVERSION_PLAN, 'request': question}
    return None


def parse_live_request(question):
    return (parse_request(question) or _parse_conversion(question)
            or document_table.parse_request(question, _UNIT, document_units.SCALES))


def snapshot(store, sid, plan):
    operands = plan['operands']
    ids = {operand['document_id'] for operand in operands}
    sources = store.documents(sid, include_content=True, document_ids=ids)
    query = document_table.query(operands) if plan['schema'] == document_table.PLAN else plan['request']
    excerpts, ranges, limited = select_ranges(query, sources, max_excerpts=2, max_chars=14000)
    catalog = [dict(document_id=row['id'], name=row['name'], sha256=row['sha256'],
                    total_lines=len(row['content'].splitlines())) for row in sources]
    covered = {(row['document_id'], line) for row in excerpts for line in range(row['start_line'], row['end_line'] + 1)}
    return dict(catalog=catalog, excerpts=excerpts, requested_ranges=ranges, requested_ranges_limited=limited,
                coverage_limited=sum(row['total_lines'] for row in catalog) > len(covered))


def _number(value):
    if value.isascii():
        return value
    if '十' not in value:
        return str(_DIGITS.index(value.replace('兩', '二')))
    tens, ones = value.split('十')
    return str(10 * (_DIGITS.index(tens) if tens else 1) + _DIGITS.index(ones or '零'))


def _field_pattern(label):
    field = re.escape(label)
    if label != '重量' and label.endswith('重量'):
        subject = re.escape(label.removesuffix('重量'))
        field = rf'{subject}(?:樣本)?(?:的)?(?:實測)?重量'
    return field


def _value(text, label, unit):
    match = re.fullmatch(rf'{_field_pattern(label)} *(?:[:=]|為|是) *({_NUMBER}) *{re.escape(unit)}[。.]?',
                         unicodedata.normalize('NFKC', text).strip())
    return _number(match[1]) if match else None


def _observe_line(operand, requested, catalog, excerpts):
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
    return dict(status='provided', sha256=rows[0]['sha256'], original=text[len(prefix):])


def _read_sources(plan, request):
    documents = plan['documents']
    if (request is None or plan != {**request, 'documents': documents}
            or documents['requested_ranges_limited'] is not False
            or len(documents['requested_ranges']) != 2 or len(documents['catalog']) > 2
            or len(documents['excerpts']) != sum(row['status'] == 'provided' for row in documents['requested_ranges'])):
        raise ValueError('文件計算計畫與原問不符。')
    operands = request['operands']
    catalog = _catalog(documents, operands)
    return [_observe_line(operand, row, catalog, documents['excerpts'])
            for operand, row in zip(operands, documents['requested_ranges'], strict=True)]


def _catalog(documents, operands):
    catalog = {row['document_id']: row for row in documents['catalog']}
    if (len(catalog) != len(documents['catalog']) or catalog.keys() - {row['document_id'] for row in operands}
            or any(type(row['total_lines']) is not int or row['total_lines'] < 0
                   or not re.fullmatch(r'[0-9a-f]{64}', row['sha256']) for row in catalog.values())):
        raise ValueError('保存的文件來源清單無法核對。')
    return catalog


def _observe_v1(operand, row, unit):
    if row['status'] != 'provided':
        return row
    value = _value(row['original'], operand['label'], unit)
    if value is None:
        return dict(status='value_unconfirmed')
    return dict(status='confirmed', value=value, sha256=row['sha256'], original=row['original'])


def evaluate(plan, cancel=None):
    check_cancel(cancel)
    if plan.get('schema') == document_table.PLAN:
        return _evaluate_table(plan, cancel)
    if plan.get('schema') == CONVERSION_PLAN:
        return _evaluate_conversion(plan, cancel)
    request = parse_request(plan['request'])
    sources = _read_sources(plan, request)
    unit, operands = request['unit'], request['operands']
    observed = [_observe_v1(operand, row, unit) for operand, row in zip(operands, sources, strict=True)]
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


def _convert(operand, row, target):
    if row['status'] != 'provided':
        return row
    field = _field_pattern(operand['label'])
    match = re.fullmatch(rf'{field} *(?:[:=]|為|是) *({_NUMBER}) *({_UNIT})[。.]?',
                         unicodedata.normalize('NFKC', row['original']).strip())
    if not match:
        return dict(status='value_unconfirmed')
    unit = match[2]
    scale = document_units.SCALES.get(unit)
    if scale is None:
        return {**row, 'status': 'unit_unsupported', 'source_unit': unit}
    observed = {**row, 'value': _number(match[1]), 'source_unit': unit}
    return _scaled_quantity(observed, scale, target)


def _scaled_quantity(observed, scale, target):
    if scale[0] != target[0]:
        return {**observed, 'status': 'unit_incompatible'}
    factor = str(scale[1] / target[1])
    converted = exact_math.calculate({'operation': 'product', 'values': [observed['value'], factor]})
    return {**observed, 'status': 'confirmed', 'factor_exact': factor, 'converted': converted}


def _display_exact(result):
    return result['exact'] if result['rounded'] else format(Decimal(result['decimal']), 'f')


def _conversion_line(operand, row, unit):
    label, status, citation = operand['label'], row['status'], operand['citation']
    if 'value' in row:
        original = f'{label}為 {row["value"]} {row["source_unit"]}。 {citation}'
        if status == 'confirmed':
            return f'{original} 換算為 {_display_exact(row["converted"])} {unit}。'
        return f'{original} 這個單位與 {unit} 屬於不同類別，無法換算。'
    if status == 'unit_unsupported':
        return f'{label}的指定行含未支援的單位文字「{row["source_unit"]}」。 {citation}'
    source = f'D{operand["document_id"]} 第{operand["line_number"]}行'
    reason = STATUS_TEXT.get(status, '該行沒有可核對的完整欄位、確定數值和單位。')
    return f'缺少可用的{label}（{source}）。{reason}'


def _evaluate_conversion(plan, cancel):
    request = _parse_conversion(plan['request'])
    sources = _read_sources(plan, request)
    unit, operands = request['unit'], request['operands']
    target = document_units.SCALES[unit]
    observed = [_convert(operand, row, target) for operand, row in zip(operands, sources, strict=True)]
    result = None
    if all(row['status'] == 'confirmed' for row in observed):
        converted = [row['converted'] for row in observed]
        operation = request['operation']
        result = exact_math.calculate({'operation': operation, 'values': [row['exact'] for row in converted]})
        left, right = map(_display_exact, converted)
        sign = '+' if operation == 'sum' else '−'
        summary = f'結果為 {_display_exact(result)} {unit}（({left}) {sign} ({right})）。'
    else:
        summary = '目前無法計算。請核對以下來源與單位。'
    check_cancel(cancel)
    answer = '\n'.join([summary, *(_conversion_line(operand, row, unit)
                                  for operand, row in zip(operands, observed, strict=True))])
    return answer, dict(schema='document_calculation_receipt_v2', plan=plan, model_called=False,
                        observations=observed, result=result, output=answer, output_sha256=digest(answer))


def _same_unit(first, second):
    scales = document_units.SCALES
    return first == second or (first in scales and scales[first] == scales.get(second))


def _table_quantity(operand, source, target, convert):
    row = document_table.lookup(operand, source, _UNIT)
    if row['status'] != 'selected':
        return row
    value = unicodedata.normalize('NFKC', row['cell']).strip()
    match = re.fullmatch(rf'({_NUMBER}) *(?:({_UNIT}))?', value)
    if match is None:
        return {**row, 'status': 'value_unconfirmed'}
    header_unit, cell_unit = row['header_unit'], match[2]
    if header_unit and cell_unit and not _same_unit(header_unit, cell_unit):
        return {**row, 'status': 'unit_conflict', 'cell_unit': cell_unit}
    unit = cell_unit or header_unit
    if unit is None:
        return {**row, 'status': 'unit_missing'}
    observed = {**row, 'value': _number(match[1]), 'source_unit': unit}
    if convert:
        scale = document_units.SCALES.get(unit)
        return (_scaled_quantity(observed, scale, document_units.SCALES[target]) if scale is not None
                else {**observed, 'status': 'unit_unsupported'})
    return {**observed, 'status': 'confirmed' if _same_unit(unit, target) else 'unit_incompatible'}


def _table_line(operand, row, unit):
    label = f'「{operand["key_column"]}={operand["key_value"]}」的「{operand["value_column"]}」'
    if 'row_citation' in row:
        location = f'{row["header_citation"]} {row["row_citation"]}'
    else:
        location = f'D{operand["document_id"]} 第{operand["start_line"]}到{operand["end_line"]}行'
    if row['status'] != 'confirmed':
        reason = document_table.reason(row['status'])
        if matches := row.get('matching_lines'):
            numbers = '、'.join(map(str, matches[:8]))
            reason += f'符合的行號為 {numbers}。' if len(matches) <= 8 else f'前八個符合的行號為 {numbers}。'
        return f'{label}（{location}）無法取值。{reason}'
    line = f'{label}為 {row["value"]} {row["source_unit"]}。 {location}'
    if 'converted' in row and row['source_unit'] != unit:
        line += f' 換算為 {_display_exact(row["converted"])} {unit}。'
    return line


def _evaluate_table(plan, cancel):
    request = document_table.parse_request(plan['request'], _UNIT, document_units.SCALES)
    documents = plan['documents']
    if request is None or plan != {**request, 'documents': documents} or len(documents['catalog']) > 2:
        raise ValueError('表格計算計畫與原問不符。')
    operands, unit, convert = request['operands'], request['unit'], request['convert']
    sources = document_table.lines_for_requests(documents, _catalog(documents, operands), operands)
    observed = [_table_quantity(operand, source, unit, convert)
                for operand, source in zip(operands, sources, strict=True)]
    result = None
    summary = '目前無法計算。請核對以下表格欄位、資料列或單位。'
    if all(row['status'] == 'confirmed' for row in observed):
        values = [row['converted']['exact'] if convert else row['value'] for row in observed]
        result = exact_math.calculate({'operation': request['operation'], 'values': values})
        sign = '+' if request['operation'] == 'sum' else '−'
        left, right = [_display_exact(row['converted']) if convert else row['value'] for row in observed]
        summary = f'結果為 {_display_exact(result)} {unit}（({left}) {sign} ({right})）。'
    check_cancel(cancel)
    answer = '\n'.join([summary, *(_table_line(operand, row, unit)
                                  for operand, row in zip(operands, observed, strict=True))])
    return answer, dict(schema=document_table.RECEIPT, plan=plan, model_called=False,
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


def describe(receipt):
    plan = receipt['plan']
    factors = [f'{row.get("row_citation", operand["citation"])} 1 {row["source_unit"]} = {row["factor_exact"]} {plan["unit"]}'
               for operand, row in zip(plan['operands'], receipt['observations'], strict=True)
               if 'factor_exact' in row and row['source_unit'] != plan['unit']]
    return receipt['output'] + ('\n\n換算係數\n' + '\n'.join(factors) if factors else '')
