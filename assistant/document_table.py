"""Bind requested table values to immutable, numbered document rows."""
import csv
import re
import unicodedata
from itertools import combinations
from pathlib import PurePath

from . import document_boolean, document_dates
from .document_ranges import MAX_LINES, STATUS_TEXT, requested_ranges


PLAN = 'document_calculation_plan_v3'
RECEIPT = 'document_calculation_receipt_v3'
AGGREGATE_PLAN = 'document_calculation_plan_v4'
AGGREGATE_RECEIPT = 'document_calculation_receipt_v4'
MULTI_AGGREGATE_PLAN = 'document_calculation_plan_v5'
MULTI_AGGREGATE_RECEIPT = 'document_calculation_receipt_v5'
FILTER_AGGREGATE_PLAN = 'document_calculation_plan_v6'
FILTER_AGGREGATE_RECEIPT = 'document_calculation_receipt_v6'
NUMERIC_AGGREGATE_PLAN = 'document_calculation_plan_v7'
NUMERIC_AGGREGATE_RECEIPT = 'document_calculation_receipt_v7'
LOGIC_AGGREGATE_PLAN = 'document_calculation_plan_v8'
LOGIC_AGGREGATE_RECEIPT = 'document_calculation_receipt_v8'
DATE_AGGREGATE_PLAN = 'document_calculation_plan_v9'
DATE_AGGREGATE_RECEIPT = 'document_calculation_receipt_v9'
MAX_AGGREGATE_RANGES = 4
_INDEX = r'[1-9][0-9]{0,11}'
_SPAN = rf'\[D{_INDEX}:L{_INDEX}-L{_INDEX}\]'
_REF = rf'({_SPAN})'
_TOKEN_TEXT = r'[^「」\[\]\x00-\x1f\x7f]{1,48}'
_TOKEN = rf'「({_TOKEN_TEXT})」'
_OPERAND = rf'{_REF} *中 *{_TOKEN} *為 *{_TOKEN} *的 *{_TOKEN}'
_REQUEST = re.compile(rf'文件問答[:：] *(?:請)?計算 *{_OPERAND} *(加上|減去|[+＋\-－]) *{_OPERAND}'
                      r' *[,，] *(單位為|(?:統一)?換算成) *([^。?？\s]{1,8})[。?？]?')
_AGG_SOURCE = rf'{_REF} *(?:中 *{_TOKEN} *為 *{_TOKEN} *|中 *(?:所有資料列 *)?)?的 *{_TOKEN}'
_AGG_UNIT = r' *[,，] *(單位為|(?:統一)?換算成) *([^。?？\s]{1,8})[。?？]?'
_AGG_PREFIX = re.compile(rf'文件問答[:：] *(?:請)?(加總|合計|平均) *{_AGG_SOURCE}{_AGG_UNIT}')
_AGG_SUFFIX = re.compile(rf'文件問答[:：] *(?:請)?計算 *{_AGG_SOURCE} *(?:的 *)?(總和|合計|平均值?){_AGG_UNIT}')
_MULTI_SOURCE = re.compile(rf'(文件問答[:：] *(?:請)?(?:加總|合計|平均|計算) *)'
                           rf'({_SPAN}(?: *(?:、|與|和) *{_SPAN}){{1,{MAX_AGGREGATE_RANGES - 1}}})(.*)')
_CONDITION = rf'「{_TOKEN_TEXT}」 *(?:不為|為) *「{_TOKEN_TEXT}」'
_CONDITION_PARTS = re.compile(rf'{_TOKEN} *(不為|為) *{_TOKEN}')
_FILTER_SOURCE = (rf'(?P<scope>{_SPAN}(?: *(?:、|與|和) *{_SPAN}){{0,{MAX_AGGREGATE_RANGES - 1}}})'
                  rf' *中 *(?P<conditions>{_CONDITION}(?: *且 *{_CONDITION}){{1,3}}) *的 *「(?P<column>{_TOKEN_TEXT})」')
_FILTER_UNIT = r' *[,，] *(?P<mode>單位為|(?:統一)?換算成) *(?P<unit>[^。?？\s]{1,8})[。?？]?'
_FILTER_PREFIX = re.compile(rf'文件問答[:：] *(?:請)?(?P<operation>加總|合計|平均) *{_FILTER_SOURCE}{_FILTER_UNIT}')
_FILTER_SUFFIX = re.compile(rf'文件問答[:：] *(?:請)?計算 *{_FILTER_SOURCE} *(?:的 *)?'
                            rf'(?P<operation>總和|合計|平均值?){_FILTER_UNIT}')
_NUMERIC_OPERATORS = {
    '至少': 'ge', '大於或等於': 'ge', '>=': 'ge', '≥': 'ge',
    '至多': 'le', '小於或等於': 'le', '<=': 'le', '≤': 'le',
    '大於': 'gt', '超過': 'gt', '>': 'gt', '小於': 'lt', '少於': 'lt', '<': 'lt',
    '等於': 'eq', '=': 'eq', '不等於': 'ne', '!=': 'ne', '≠': 'ne',
}
_NUMERIC_PARTS = re.compile(rf'{_TOKEN} *({"|".join(map(re.escape, _NUMERIC_OPERATORS))}) *(.{{1,48}})')
_NUMERIC_SOURCE = (rf'(?P<scope>{_SPAN}(?: *(?:、|與|和) *{_SPAN}){{0,{MAX_AGGREGATE_RANGES - 1}}})'
                   rf' *中 *(?P<conditions>.{{1,500}}?) *的 *「(?P<column>{_TOKEN_TEXT})」')
_NUMERIC_PREFIX = re.compile(rf'文件問答[:：] *(?:請)?(?P<operation>加總|合計|平均) *{_NUMERIC_SOURCE}{_FILTER_UNIT}')
_NUMERIC_SUFFIX = re.compile(rf'文件問答[:：] *(?:請)?計算 *{_NUMERIC_SOURCE} *(?:的 *)?'
                             rf'(?P<operation>總和|合計|平均值?){_FILTER_UNIT}')
_DATE_OPERATORS = {'不早於': 'ge', '不晚於': 'le', '早於': 'lt', '晚於': 'gt', '不等於': 'ne', '等於': 'eq',
                   '>=': 'ge', '≤': 'le', '<=': 'le', '≥': 'ge', '>': 'gt', '<': 'lt', '=': 'eq', '!=': 'ne', '≠': 'ne'}
_DATE_COMPARISON = re.compile(rf'{_TOKEN} *({"|".join(map(re.escape, _DATE_OPERATORS))}) *(.{{1,18}})')
_DATE_RANGE = re.compile(rf'{_TOKEN} *(?:從|自) *(.{{1,18}}?) *(?:到|至) *(.{{1,18}})')
_ALIGNMENT = re.compile(r':?-{3,}:?')
_HEADER_UNIT = re.compile(r'(.*?) *(?:\(([^()（）]{1,16})\)|（([^()（）]{1,16})）)')
_SAME_UNIT_ONLY = frozenset('元 新台幣元 美元 件 個 箱 份 張 套 瓶 包 組 盒 批 顆 人 席 台 本'.split())
_REASONS = {
    'table_invalid': '範圍需從完整表頭開始，各資料列欄數須一致。',
    'header_ambiguous': '表頭有空白或重複欄名，無法唯一對應。',
    'column_missing': '表頭沒有指定的查找欄或數值欄。',
    'row_missing': '指定範圍內找不到符合鍵值的資料列。',
    'row_ambiguous': '指定範圍內有多筆符合鍵值的資料列，請縮小範圍或改用唯一鍵值。',
    'filter_unknown': '部分資料列的條件欄位空白，無法確定是否符合。',
    'filter_blank': '欄位空白。',
    'filter_date_invalid': '不是可確認的完整公曆日期。',
    'filter_unit_incompatible': '來源單位與比較門檻的單位不相容。',
    'value_unconfirmed': '儲存格沒有可核對的確定數值。',
    'unit_missing': '表頭與儲存格都沒有明示單位。',
    'unit_conflict': '表頭與儲存格宣告的單位不一致。',
    'unit_unsupported': '儲存格的單位不在支援的換算範圍。',
    'unit_incompatible': '這筆資料的單位與指定結果單位不相容。',
}


def parse_request(question, unit_pattern, supported_units):
    if not isinstance(question, str) or len(question) > 480 or len(question.splitlines()) != 1:
        return None
    match = _REQUEST.fullmatch(question.strip())
    if not match:
        return None
    first, first_key, first_value, first_col, operator, second, second_key, second_value, second_col, mode, unit = match.groups()
    convert = mode != '單位為'
    unit = _target_unit(unit, convert, unit_pattern, supported_units)
    if unit is None:
        return None
    operands = []
    for citation, key, value, column in ((first, first_key, first_value, first_col), (second, second_key, second_value, second_col)):
        key, value, column = key.strip(), value.strip(), column.strip()
        if not all((key, value, column)):
            return None
        rows, _ = requested_ranges(citation)
        operands.append({**rows[0], 'citation': citation, 'key_column': key, 'key_value': value, 'value_column': column})
    return dict(schema=PLAN, request=question, operation='sum' if operator in ('加上', '+', '＋') else 'difference',
                unit=unit, convert=convert, operands=operands)


def _target_unit(unit, convert, unit_pattern, supported_units):
    unit = unicodedata.normalize('NFKC', unit)
    if re.fullmatch(unit_pattern, unit) and (unit in supported_units or (not convert and unit in _SAME_UNIT_ONLY)):
        return unit
    return None


def parse_aggregate(question, unit_pattern, supported_units):
    if not isinstance(question, str) or len(question) > 480 or len(question.splitlines()) != 1:
        return None
    text = question.strip()
    if match := _AGG_PREFIX.fullmatch(text):
        operation, citation, key, value, column, mode, unit = match.groups()
    elif match := _AGG_SUFFIX.fullmatch(text):
        citation, key, value, column, operation, mode, unit = match.groups()
    else:
        return None
    convert = mode != '單位為'
    unit = _target_unit(unit, convert, unit_pattern, supported_units)
    if unit is None or not column.strip() or (key is not None and not (key.strip() and value.strip())):
        return None
    spans, _ = requested_ranges(citation)
    operand = {**spans[0], 'citation': citation, 'key_column': key.strip() if key is not None else None,
               'key_value': value.strip() if value is not None else None, 'value_column': column.strip()}
    return dict(schema=AGGREGATE_PLAN, request=question, operation='mean' if operation.startswith('平均') else 'sum',
                unit=unit, convert=convert, operands=[operand])


def parse_multi_aggregate(question, unit_pattern, supported_units):
    if not isinstance(question, str) or len(question) > 620 or len(question.splitlines()) != 1:
        return None
    match = _MULTI_SOURCE.fullmatch(question.strip())
    if not match:
        return None
    prefix, scope, suffix = match.groups()
    citations = re.findall(_SPAN, scope)
    base = parse_aggregate(prefix + citations[0] + suffix, unit_pattern, supported_units)
    if base is None:
        return None
    first, = base['operands']
    operands = []
    for citation in citations:
        ranges, _ = requested_ranges(citation)
        operands.append({**first, **ranges[0], 'citation': citation})
    return {**base, 'schema': MULTI_AGGREGATE_PLAN, 'request': question, 'operands': operands}


def parse_filter_aggregate(question, unit_pattern, supported_units):
    if not isinstance(question, str) or len(question) > 820 or len(question.splitlines()) != 1:
        return None
    text = question.strip()
    match = _FILTER_PREFIX.fullmatch(text) or _FILTER_SUFFIX.fullmatch(text)
    if not match:
        return None
    convert = match['mode'] != '單位為'
    unit = _target_unit(match['unit'], convert, unit_pattern, supported_units)
    column = match['column'].strip()
    if unit is None or not column:
        return None
    filters = [dict(column=name.strip(), operator='ne' if operator == '不為' else 'eq', value=value.strip())
               for name, operator, value in _CONDITION_PARTS.findall(match['conditions'])]
    if (any(not row['column'] or not row['value'] for row in filters)
            or len({row['column'] for row in filters}) != len(filters)):
        return None
    operands = []
    for citation in re.findall(_SPAN, match['scope']):
        ranges, _ = requested_ranges(citation)
        operands.append({**ranges[0], 'citation': citation, 'value_column': column})
    return dict(schema=FILTER_AGGREGATE_PLAN, request=question,
                operation='mean' if match['operation'].startswith('平均') else 'sum',
                unit=unit, convert=convert, operands=operands, filters=filters)


def _threshold_envelope(question, unit_pattern, supported_units):
    if not isinstance(question, str) or len(question) > 820 or len(question.splitlines()) != 1:
        return None
    text = question.strip()
    match = _NUMERIC_PREFIX.fullmatch(text) or _NUMERIC_SUFFIX.fullmatch(text)
    if not match:
        return None
    convert = match['mode'] != '單位為'
    unit = _target_unit(match['unit'], convert, unit_pattern, supported_units)
    column = match['column'].strip()
    if unit is None or not column:
        return None
    operands = []
    for citation in re.findall(_SPAN, match['scope']):
        ranges, _ = requested_ranges(citation)
        operands.append({**ranges[0], 'citation': citation, 'value_column': column})
    return match['conditions'], dict(request=question, operation='mean' if match['operation'].startswith('平均') else 'sum',
                                     unit=unit, convert=convert, operands=operands)


def _threshold_condition(part, unit_pattern, supported_units, parse_quantity):
    if condition := _CONDITION_PARTS.fullmatch(part.strip()):
        name, operator, value = (value.strip() for value in condition.groups())
        if name and value:
            return dict(column=name, operator='ne' if operator == '不為' else 'eq', value=value)
    elif condition := _NUMERIC_PARTS.fullmatch(part.strip()):
        name, operator, raw = condition.groups()
        quantity = parse_quantity(raw)
        if name.strip() and quantity is not None and quantity[1] is not None:
            value, threshold_unit = quantity
            if _target_unit(threshold_unit, False, unit_pattern, supported_units) is not None:
                return dict(column=name.strip(), operator=_NUMERIC_OPERATORS[operator], value=value, unit=threshold_unit)
    return None


def parse_numeric_aggregate(question, unit_pattern, supported_units, parse_quantity):
    envelope = _threshold_envelope(question, unit_pattern, supported_units)
    if envelope is None:
        return None
    expression, plan = envelope
    parts = re.split(r' *且 *(?=「)', expression)
    if not 1 <= len(parts) <= 4:
        return None
    filters = [_threshold_condition(part, unit_pattern, supported_units, parse_quantity) for part in parts]
    if any(condition is None for condition in filters) or not any('unit' in condition for condition in filters):
        return None
    return dict(schema=NUMERIC_AGGREGATE_PLAN, **plan, filters=filters)


def parse_logic_aggregate(question, unit_pattern, supported_units, parse_quantity):
    envelope = _threshold_envelope(question, unit_pattern, supported_units)
    if envelope is None:
        return None
    expression, plan = envelope
    parsed = document_boolean.parse(expression, lambda part: _threshold_condition(part, unit_pattern, supported_units, parse_quantity))
    if parsed is None:
        return None
    filters, logic = parsed
    return dict(schema=LOGIC_AGGREGATE_PLAN, **plan, filters=filters, logic=logic)


def _date_condition(part):
    if match := _DATE_RANGE.fullmatch(part):
        name, first, last = match.groups()
        start, end = document_dates.canonical(first), document_dates.canonical(last)
        if name.strip() and start and end:
            return dict(column=name.strip(), operator='between', date=start, end_date=end)
    elif match := _DATE_COMPARISON.fullmatch(part):
        name, operator, raw = match.groups()
        normalized = document_dates.canonical(raw)
        if name.strip() and normalized:
            return dict(column=name.strip(), operator=_DATE_OPERATORS[operator], date=normalized)
    return None


def parse_date_aggregate(question, unit_pattern, supported_units, parse_quantity):
    envelope = _threshold_envelope(question, unit_pattern, supported_units)
    if envelope is None:
        return None
    expression, plan = envelope

    def parse_condition(part):
        return _date_condition(part) or _threshold_condition(part, unit_pattern, supported_units, parse_quantity)

    parsed = document_boolean.parse(expression, parse_condition, allow_plain=True)
    if parsed is None:
        return None
    filters, logic = parsed
    if not any('date' in condition for condition in filters):
        return None
    return dict(schema=DATE_AGGREGATE_PLAN, **plan, filters=filters, logic=logic)


def query(operands):
    return '\n'.join(operand['citation'] for operand in operands)


def _span(row):
    span = row['document_id'], row['start_line'], row['end_line']
    if any(type(value) is not int or not 1 <= value < 10**12 for value in span):
        raise ValueError('表格來源與行號必須是有效的正整數。')
    return span


def overlaps(operands, catalog):
    """Return range indexes with shared lines in the same snapshot bytes."""
    shared = []
    for (left_index, left), (right_index, right) in combinations(enumerate(operands), 2):
        left_id, left_start, left_end = _span(left)
        right_id, right_start, right_end = _span(right)
        same_bytes = (left_id in catalog and right_id in catalog
                      and catalog[left_id]['sha256'] == catalog[right_id]['sha256'])
        if (left_id == right_id or same_bytes) and max(left_start, right_start) <= min(left_end, right_end):
            shared.append([left_index, right_index])
    return shared


def lines_for_requests(documents, catalog, operands):
    """Recheck physical line coverage, including overlapping/shared ranges."""
    wanted, _ = requested_ranges(query(operands))
    requests = documents['requested_ranges']
    excerpts = documents['excerpts']
    if (documents['requested_ranges_limited'] is not False or len(requests) != len(wanted)
            or len(excerpts) > len(wanted) or sum(len(row['text']) for row in excerpts) > 14000):
        raise ValueError('保存的表格範圍與原問不符。')
    statuses = {}
    for actual, expected in zip(requests, wanted, strict=True):
        span, status = _span(actual), actual['status']
        did, start, end = span
        source = catalog.get(did)
        if end < start:
            allowed = ('invalid_range',)
        elif source is None:
            allowed = ('document_not_attached',)
        elif end - start + 1 > MAX_LINES:
            allowed = ('range_too_large',)
        elif end > source['total_lines']:
            allowed = ('line_out_of_bounds',)
        else:
            allowed = ('provided', 'character_limit')
        if span != _span(expected) or status not in allowed or actual['explanation'] != STATUS_TEXT[status]:
            raise ValueError('保存的表格範圍狀態不符。')
        statuses[span] = status
    physical_lines, seen = {}, set()
    for excerpt in excerpts:
        span = _span(excerpt)
        did, start, end = span
        if (statuses.get(span) != 'provided' or span in seen or excerpt['partial_line'] is not False
                or excerpt.get('requested_range') is not True or excerpt['sha256'] != catalog[did]['sha256']
                or excerpt['name'] != catalog[did]['name']):
            raise ValueError('保存的表格片段與來源不符。')
        seen.add(span)
        lines = excerpt['text'].split('\n')
        if len(lines) != end - start + 1:
            raise ValueError('保存的表格片段行數不符。')
        for line_number, text in enumerate(lines, start):
            prefix = f'L{line_number}: '
            if not text.startswith(prefix) or len(text.splitlines()) != 1:
                raise ValueError('保存的表格片段行號不符。')
            line = text[len(prefix):]
            if physical_lines.setdefault((did, line_number), line) != line:
                raise ValueError('保存的重疊表格片段不一致。')
    sources = {}
    for span, status in statuses.items():
        did, start, end = span
        if status == 'provided':
            try:
                rows = [physical_lines[(did, n)] for n in range(start, end + 1)]
            except KeyError:
                raise ValueError('保存的表格片段不完整。') from None
            sources[span] = dict(status=status, rows=rows, sha256=catalog[did]['sha256'], name=catalog[did]['name'])
        else:
            sources[span] = dict(status=status)
    return [sources[_span(operand)] for operand in operands]


def _pipe_row(text):
    line = text.strip()
    if not line.startswith('|') or not line.endswith('|') or '\\' in line or '`' in line:
        return []
    return [cell.strip() for cell in line[1:-1].split('|')]


def _table_rows(source):
    lines = source['rows']
    suffix = PurePath(source['name']).suffix.lower()
    if suffix not in ('.csv', '.tsv') and lines[0].strip().startswith('|'):
        header, *rest = map(_pipe_row, lines)
        if not rest or not rest[0] or len(rest[0]) != len(header) or not all(_ALIGNMENT.fullmatch(cell) for cell in rest[0]):
            return None
        return header, rest[1:], 2
    delimiter = '\t' if suffix == '.tsv' or (suffix != '.csv' and '\t' in lines[0]) else ','
    try:
        # Parsing each physical line separately rejects multi-line CSV fields.
        header, *data = [next(csv.reader([line], delimiter=delimiter, strict=True)) for line in lines]
    except csv.Error:
        return None
    return [cell.strip() for cell in header], data, 1


def lookup(operand, source, unit_pattern):
    status, _, matches = lookup_rows(operand, source, unit_pattern)
    if status != 'selected':
        return dict(status=status)
    if len(matches) > 1:
        return dict(status='row_ambiguous', matching_lines=[line for line, _ in matches])
    return matches[0][1]


def lookup_rows(operand, source, unit_pattern):
    """Return status, total data-row count, and matching cells in physical order."""
    key_column = operand['key_column']
    names = [] if key_column is None else [key_column]
    status, total_rows, bound = _bind_table(operand, source, unit_pattern, names)
    if bound is None:
        return status, total_rows, []
    key = bound['key_indexes'][0] if names else None
    matches = [(line, _selected_cell(operand, source, bound, line, row))
               for line, row in enumerate(bound['rows'], bound['first_row'])
               if key is None or row[key].strip() == operand['key_value']]
    return ('selected' if matches else 'row_missing'), total_rows, matches


def _bind_table(operand, source, unit_pattern, key_columns, numeric_indexes=()):
    if source['status'] != 'provided':
        return source['status'], None, None
    table = _table_rows(source)
    if table is None:
        return 'table_invalid', None, None
    header, rows, offset = table
    if not 2 <= len(header) <= 80 or not rows or any(len(row) != len(header) for row in rows):
        return 'table_invalid', None, None
    columns, units = [], []
    for cell in header:
        match = _HEADER_UNIT.fullmatch(cell)
        unit = unicodedata.normalize('NFKC', match[2] or match[3]).strip() if match else ''
        explicit_unit = bool(re.fullmatch(unit_pattern, unit))
        columns.append(match[1].strip() if explicit_unit else cell)
        units.append(unit if explicit_unit else None)
    if not all(columns) or len(set(columns)) != len(columns):
        return 'header_ambiguous', None, None
    key_headers = [columns if index in numeric_indexes else header for index in range(len(key_columns))]
    if (any(name not in keys for name, keys in zip(key_columns, key_headers, strict=True))
            or operand['value_column'] not in columns):
        return 'column_missing', len(rows), None
    value = columns.index(operand['value_column'])
    key_indexes = [keys.index(name) for name, keys in zip(key_columns, key_headers, strict=True)]
    return 'selected', len(rows), dict(rows=rows, first_row=operand['start_line'] + offset,
                                      key_indexes=key_indexes, key_units=[units[index] for index in key_indexes],
                                      value_index=value, header_unit=units[value])


def _selected_cell(operand, source, bound, line, row):
    document, start = operand['document_id'], operand['start_line']
    return dict(status='selected', header_unit=bound['header_unit'], cell=row[bound['value_index']], sha256=source['sha256'],
                header_citation=f'[D{document}:L{start}]', row_citation=f'[D{document}:L{line}]')


def _test_cell(raw, condition):
    value = raw.strip()
    if not value:
        return dict(value=raw, status='unknown')
    matches = (value == condition['value']) == (condition['operator'] == 'eq')
    return dict(value=raw, status='true' if matches else 'false')


def _test_condition(raw, header_unit, condition, test_numeric):
    if 'date' in condition:
        return document_dates.test(raw, condition)
    if 'unit' in condition:
        return test_numeric(raw, header_unit, condition)
    return _test_cell(raw, condition)


def filter_rows(operand, source, unit_pattern, filters, test_numeric=None, logic=None):
    """Apply the exact condition and retain evidence for every physical data row."""
    names = [condition['column'] for condition in filters]
    numeric_indexes = {index for index, condition in enumerate(filters) if 'unit' in condition}
    status, total_rows, bound = _bind_table(operand, source, unit_pattern, names, numeric_indexes)
    if bound is None:
        return status, total_rows, [], []
    matches, decisions = [], []
    has_unknown = False
    for line, row in enumerate(bound['rows'], bound['first_row']):
        tests = [_test_condition(row[index], header_unit, condition, test_numeric)
                 for index, header_unit, condition in zip(bound['key_indexes'], bound['key_units'], filters, strict=True)]
        outcomes = [test['status'] for test in tests]
        if logic is None:
            outcome = 'false' if 'false' in outcomes else 'unknown' if 'unknown' in outcomes else 'true'
        else:
            outcome, unresolved = document_boolean.evaluate(logic, outcomes)
        if outcome == 'false':
            decision = 'excluded'
        elif outcome == 'unknown':
            decision = 'unknown'
            has_unknown = True
        else:
            decision = 'matched'
            matches.append((line, _selected_cell(operand, source, bound, line, row)))
        evidence = dict(line_number=line, status=decision, tests=tests)
        if logic is not None:
            evidence['unresolved'] = unresolved
        decisions.append(evidence)
    if has_unknown:
        status = 'filter_unknown'
    elif not matches:
        status = 'row_missing'
    return status, total_rows, matches, decisions


def reason(status):
    return _REASONS[status] if status in _REASONS else STATUS_TEXT[status]
