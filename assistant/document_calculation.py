"""Explicitly bound document values and reproducible exact arithmetic."""
import json
import re
import unicodedata
from decimal import Decimal
from itertools import groupby
from operator import eq, ge, gt, le, lt, ne

from . import document_boolean, document_dates, document_table, document_units, exact_math, local_answer_receipts
from .document_ranges import STATUS_TEXT, requested_ranges, select_ranges
from .expression_math import check_cancel
from .response_delivery import digest


MODE = 'local_document_calculation'
FIELD = 'document_calculation'
CONVERSION_PLAN = 'document_calculation_plan_v2'
SCENARIO_PLAN = 'document_calculation_scenario_plan_v1'
SCENARIO_RECEIPT = 'document_calculation_scenario_receipt_v1'
ORIGINAL_PLAN = 'document_calculation_original_plan_v1'
ORIGINAL_RECEIPT = 'document_calculation_original_receipt_v1'
EXPLICIT_ORIGINAL_PLAN = 'document_calculation_original_plan_v2'
EXPLICIT_ORIGINAL_RECEIPT = 'document_calculation_original_receipt_v2'
REMAPPED_ORIGINAL_PLAN = 'document_calculation_original_plan_v3'
REMAPPED_ORIGINAL_RECEIPT = 'document_calculation_original_receipt_v3'
ORIGINAL_RECEIPTS = {ORIGINAL_PLAN: ORIGINAL_RECEIPT, EXPLICIT_ORIGINAL_PLAN: EXPLICIT_ORIGINAL_RECEIPT,
                     REMAPPED_ORIGINAL_PLAN: REMAPPED_ORIGINAL_RECEIPT}
PINNED_PLANS = (SCENARIO_PLAN, *ORIGINAL_RECEIPTS)
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
_SCENARIO_REQUEST = re.compile(r'([^\r\n]{1,319}[。？?])\s*((?:假設|如果)[^\r\n]{1,130})')
_SCENARIO_VALUE = re.compile(rf'(?:假設|如果) *(?:把|將)? *({_REF}) *的({_LABEL}) *改(?:用|為|成) *({_NUMBER}) *({_UNIT})'
                             r'(?:, *只(?:做)?試算)?[。?!]?')
_ORIGINAL_REQUEST = re.compile(r'(?:請)?(?:回到|改用|依照|依|按照|按|用)(?:文件原值|原文件數值)[，, ]*'
                               r'(?:重算|重新計算|重新算)[。！!？?]?')
_EXPLICIT_ORIGINAL = re.compile(r'(?:請)?(?:依照|依|按照|按|用|沿用)文件試算回答 *#([0-9]{1,30}) *的'
                                r'(?:文件原值|原值|原文件數值)[，, ]*(?:重算|重新計算|重新算)[。！!？?]?')
_REPLACE_LINE = re.compile(rf'將 *({_REF}) *換成 *({_REF})')
_REFRESH_LINE = re.compile(rf'重新讀取 *({_REF})')
_COMPARISONS = dict(eq=eq, ne=ne, ge=ge, gt=gt, le=le, lt=lt)
_GROUPED_PLANS = (document_table.MULTI_AGGREGATE_PLAN, document_table.FILTER_AGGREGATE_PLAN,
                  document_table.NUMERIC_AGGREGATE_PLAN, document_table.LOGIC_AGGREGATE_PLAN,
                  document_table.DATE_AGGREGATE_PLAN, document_table.RELATIVE_DATE_AGGREGATE_PLAN)


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


def _parse_scenario(question):
    if not isinstance(question, str) or len(question) > 455:
        return None
    parts = _SCENARIO_REQUEST.fullmatch(question.strip())
    if parts is None:
        return None
    original, hypothesis = parts.groups()
    base = parse_request(original) or _parse_conversion(original)
    match = _SCENARIO_VALUE.fullmatch(unicodedata.normalize('NFKC', hypothesis))
    if base is None or match is None:
        return None
    reference, label, number, unit = match.groups()
    ranges, _ = requested_ranges(reference)
    selected = ranges[0]
    for index, operand in enumerate(base['operands']):
        if (operand['document_id'], operand['line_number'], operand['label']) == (
                selected['document_id'], selected['start_line'], label):
            return dict(schema=SCENARIO_PLAN, request=question, base=base,
                        hypothesis=dict(operand_index=index, value=_number(number), unit=unit))
    raise ValueError('請用原算式中其中一筆的文件行號和完整欄位名稱指定試算值。')


def parse_live_request(question):
    return (parse_request(question) or _parse_conversion(question) or _parse_scenario(question)
            or document_table.parse_request(question, _UNIT, document_units.SCALES)
            or document_table.parse_aggregate(question, _UNIT, document_units.SCALES)
            or document_table.parse_multi_aggregate(question, _UNIT, document_units.SCALES)
            or document_table.parse_filter_aggregate(question, _UNIT, document_units.SCALES)
            or _parse_numeric_aggregate(question)
            or _parse_logic_aggregate(question)
            or _parse_date_aggregate(question)
            or _parse_date_aggregate(question, relative=True))


def _parse_original_followup(question):
    if not isinstance(question, str) or len(question) > 240:
        return None
    text = question.strip()
    if len(question) <= 36 and _ORIGINAL_REQUEST.fullmatch(text):
        return ORIGINAL_PLAN, None, []
    if len(question) <= 80 and (match := _EXPLICIT_ORIGINAL.fullmatch(text)):
        return EXPLICIT_ORIGINAL_PLAN, int(match[1]), []
    if text.endswith(('。', '！', '!', '？', '?')):
        text = text[:-1]
    parts = re.split(r'\s*[，,；;]\s*', text)
    head = _EXPLICIT_ORIGINAL.fullmatch(parts[0])
    if head is None or not 2 <= len(parts) <= 3:
        return None
    remaps = []
    for clause in parts[1:]:
        if match := _REPLACE_LINE.fullmatch(clause):
            old, new = map(_canonical_ref, match.groups())
        elif match := _REFRESH_LINE.fullmatch(clause):
            old = new = _canonical_ref(match[1])
        else:
            return None
        remaps.append(dict(old=old, new=new))
    return REMAPPED_ORIGINAL_PLAN, int(head[1]), remaps


def _canonical_ref(reference):
    ranges, _ = requested_ranges(reference)
    row = ranges[0]
    return f'[D{row["document_id"]}:L{row["start_line"]}]'


def matches_original_followup(question):
    return _parse_original_followup(question) is not None


def _original_followup(db, sid, question, before, target):
    schema, selected, remaps = target
    if selected is not None and not 0 < selected <= 2**63 - 1:
        return None
    row = db.execute('''SELECT m.id, CASE WHEN length(CAST(c.payload AS BLOB))<=? THEN c.payload END
        FROM messages m JOIN answer_contexts c ON c.message_id=m.id
        WHERE m.session_id=? AND m.role='assistant' AND (? IS NULL OR m.id<?)
          AND m.id=COALESCE(?, (SELECT id FROM messages WHERE session_id=?
            AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT 1))''',
        (local_answer_receipts.MAX_JSON_BYTES, sid, before, before, selected, sid, before, before)).fetchone()
    if row is None:
        return None
    mid, raw = row
    context = local_answer_receipts.load_context(raw)
    if not isinstance(context, dict) or context.get('response_mode') != MODE:
        return None
    stored = context.get(FIELD)
    if not isinstance(stored, dict) or stored.get('schema') != SCENARIO_RECEIPT:
        return None
    receipt = local_answer_receipts.checked_row(db, mid, context, MODE, FIELD, validate, True, validate_origin)
    scenario = receipt['plan']
    plan = dict(schema=schema, request=question, base=scenario['base'], documents=scenario['documents'],
                scenario_source=dict(answer_id=mid, sha256=digest(raw)))
    if remaps:
        plan['remaps'] = remaps
    return plan


def capture_original(store, sid, question, before=None):
    target = _parse_original_followup(question)
    if target is None:
        raise ValueError('文件原值重算的請求無法核對。')
    with store.connect() as db:
        db.execute('BEGIN')
        plan = _original_followup(db, sid, question, before, target)
    if plan is None:
        if target[1] is not None:
            raise ValueError(f'找不到本對話中可核對的文件試算回答 #{target[1]}。請確認回答編號。')
        raise ValueError('請緊接在文件試算回答後重算，或重新提供完整的文件算式。')
    return plan


def _parse_numeric_aggregate(question):
    return document_table.parse_numeric_aggregate(question, _UNIT, document_units.SCALES, _parse_table_quantity)


def _parse_logic_aggregate(question):
    return document_table.parse_logic_aggregate(question, _UNIT, document_units.SCALES, _parse_table_quantity)


def _parse_date_aggregate(question, *, relative=False):
    return document_table.parse_date_aggregate(
        question, _UNIT, document_units.SCALES, _parse_table_quantity, relative=relative)


def bind_live_request(plan, previous=None):
    if plan['schema'] != document_table.RELATIVE_DATE_AGGREGATE_PLAN:
        return plan
    if isinstance(previous, dict) and previous.get('schema') == plan['schema']:
        bound = document_dates.bind_relative_plan(plan, previous['date_observation'])
        if previous != {**bound, 'documents': previous['documents']}:
            raise ValueError('重試所需的相對日期計畫與原問不符。')
        return bound
    return document_dates.bind_relative_plan(plan, document_dates.observe_local_clock())


def snapshot(store, sid, plan):
    if plan['schema'] in PINNED_PLANS:
        plan = _original_formula(plan)
    operands = plan['operands']
    ids = {operand['document_id'] for operand in operands}
    sources = store.documents(sid, include_content=True, document_ids=ids)
    grouped_plan = plan['schema'] in _GROUPED_PLANS
    table_plan = grouped_plan or plan['schema'] in (document_table.PLAN, document_table.AGGREGATE_PLAN)
    query = document_table.query(operands) if table_plan else plan['request']
    excerpts, ranges, limited = select_ranges(query, sources, max_excerpts=len(operands) if grouped_plan else 2,
                                               max_chars=14000)
    catalog = [dict(document_id=row['id'], name=row['name'], sha256=row['sha256'],
                    total_lines=len(row['content'].splitlines())) for row in sources]
    covered = {(row['document_id'], line) for row in excerpts for line in range(row['start_line'], row['end_line'] + 1)}
    return dict(catalog=catalog, excerpts=excerpts, requested_ranges=ranges, requested_ranges_limited=limited,
                coverage_limited=sum(row['total_lines'] for row in catalog) > len(covered))


def _original_formula(plan):
    base = plan['base']
    if plan['schema'] != REMAPPED_ORIGINAL_PLAN:
        return base
    mapping = {row['old']: row['new'] for row in plan['remaps']}
    original = base['operands']
    if len(mapping) != len(plan['remaps']) or not mapping.keys() <= {row['citation'] for row in original}:
        raise ValueError('請逐一指定原算式中的引用行，每行只能替換一次。')
    left, right = [mapping.get(row['citation'], row['citation']) for row in original]
    if left == right:
        raise ValueError('更換後的兩個來源行須不同。')
    question = unicodedata.normalize('NFKC', base['request']).strip()
    question = re.sub(_REF, lambda match: mapping.get(_canonical_ref(match[0]), match[0]), question)
    question = re.sub(' +', ' ', question)
    updated = _parse_conversion(question) if base['schema'] == CONVERSION_PLAN else parse_request(question)
    if updated is None:
        raise ValueError('更換文件行後無法核對原算式。')
    return updated


def bind_live_documents(plan, documents):
    schema = plan['schema']
    if schema == REMAPPED_ORIGINAL_PLAN:
        base = plan['base']
        updated = _original_formula(plan)
        prior = plan['documents']
        before = _read_sources({**base, 'documents': prior}, base)
        after = _read_sources({**updated, 'documents': documents}, updated)
        old_catalog = _catalog(prior, base['operands'])
        new_catalog = _catalog(documents, updated['operands'])
        replaced = {row['old'] for row in plan['remaps']}
        for operand, old, new in zip(base['operands'], before, after, strict=True):
            did = operand['document_id']
            if operand['citation'] not in replaced and (old != new or old_catalog.get(did) != new_catalog.get(did)):
                line = operand['line_number']
                raise ValueError(f'未指定換行的引用已變更（D{did} 第{line}行），請指定重新讀取或更換。')
    elif schema in ORIGINAL_RECEIPTS and plan['documents'] != documents:
        raise ValueError('這則試算的引用行已變更，請重新提供完整的文件算式。')
    return {**plan, 'documents': documents}


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
    if plan.get('schema') in ORIGINAL_RECEIPTS:
        return _evaluate_original(plan, cancel)
    if plan.get('schema') == SCENARIO_PLAN:
        return _evaluate_scenario(plan, cancel)
    if plan.get('schema') in _GROUPED_PLANS:
        return _evaluate_grouped_aggregate(plan, cancel)
    if plan.get('schema') == document_table.AGGREGATE_PLAN:
        return _evaluate_aggregate(plan, cancel)
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


def _scenario_quantity(base, hypothesis):
    value, unit = hypothesis['value'], hypothesis['unit']
    if base['schema'] != CONVERSION_PLAN:
        return dict(status='confirmed', value=value) if unit == base['unit'] else dict(status='unit_mismatch')
    scale = document_units.SCALES.get(unit)
    if scale is None:
        return dict(status='unit_unsupported')
    return _scaled_quantity(dict(value=value, source_unit=unit), scale, document_units.SCALES[base['unit']])


def _evaluate_original(plan, cancel):
    base, source = plan['base'], plan['scenario_source']
    target = _parse_original_followup(plan['request'])
    keys = {'schema', 'request', 'base', 'documents', 'scenario_source'}
    if plan['schema'] == REMAPPED_ORIGINAL_PLAN:
        keys.add('remaps')
    if (target is None or target[0] != plan['schema'] or target[2] != plan.get('remaps', [])
            or base != (parse_request(base['request']) or _parse_conversion(base['request']))
            or set(source) != {'answer_id', 'sha256'} or type(source['answer_id']) is not int
            or not 0 < source['answer_id'] <= 2**63 - 1 or not re.fullmatch('[0-9a-f]{64}', source['sha256'])
            or (target[1] is not None and source['answer_id'] != target[1])
            or set(plan) != keys):
        raise ValueError('文件原值重算的請求或來源無法核對。')
    original, evidence = evaluate({**_original_formula(plan), 'documents': plan['documents']}, cancel)
    intro = '依重新指定的文件行重算' if plan['schema'] == REMAPPED_ORIGINAL_PLAN else '依文件原值重算'
    answer = intro + '\n' + original
    return answer, dict(schema=ORIGINAL_RECEIPTS[plan['schema']], plan=plan, model_called=False,
                        observations=evidence['observations'], result=evidence['result'],
                        output=answer, output_sha256=digest(answer))


def _evaluate_scenario(plan, cancel):
    request = _parse_scenario(plan['request'])
    documents = plan['documents']
    if request is None or plan != {**request, 'documents': documents}:
        raise ValueError('文件試算計畫與原問不符。')
    base = request['base']
    original, evidence = evaluate({**base, 'documents': documents}, cancel)
    hypothesis = request['hypothesis']
    index = hypothesis['operand_index']
    label = base['operands'][index]['label']
    unit = base['unit']
    lines = ['依文件原值', original, '', f'假設試算採用使用者提供的{label} {hypothesis["value"]} {hypothesis["unit"]}。']
    other = evidence['observations'][1 - index]
    quantity = (_scenario_quantity(base, hypothesis) if other['status'] == 'confirmed'
                else dict(status='source_unavailable'))
    result = None
    if quantity['status'] == 'confirmed':
        if base['schema'] == CONVERSION_PLAN:
            retained, replacement = other['converted']['exact'], quantity['converted']['exact']
            if hypothesis['unit'] != unit:
                lines.append(f'試算值換算為 {_display_exact(quantity["converted"])} {unit}。')
        else:
            retained, replacement = other['value'], quantity['value']
        values = [replacement, retained] if index == 0 else [retained, replacement]
        result = exact_math.calculate(dict(operation=base['operation'], values=values))
        sign = '+' if base['operation'] == 'sum' else '−'
        lines.append(f'試算結果為 {_display_exact(result)} {unit}（({values[0]}) {sign} ({values[1]})）。')
    else:
        reasons = dict(source_unavailable='請先補齊試算中仍採用的文件原值。',
                       unit_mismatch=f'請以原算式指定的單位「{unit}」提供試算值。',
                       unit_incompatible=f'試算值的單位與「{unit}」屬於不同類別。',
                       unit_unsupported='請以支援的單位提供試算值。')
        lines.append('試算需要核對資料。' + reasons[quantity['status']])
    check_cancel(cancel)
    answer = '\n'.join(lines)
    return answer, dict(schema=SCENARIO_RECEIPT, plan=plan, model_called=False,
                        observations=evidence['observations'], result=evidence['result'],
                        scenario=dict(quantity=quantity, result=result), output=answer, output_sha256=digest(answer))


def _same_unit(first, second):
    scales = document_units.SCALES
    return first == second or (first in scales and scales[first] == scales.get(second))


def _table_quantity(operand, source, target, convert):
    row = document_table.lookup(operand, source, _UNIT)
    if row['status'] != 'selected':
        return row
    return _cell_quantity(row, target, convert)


def _parse_table_quantity(raw):
    value = unicodedata.normalize('NFKC', raw).strip()
    match = re.fullmatch(rf'({_NUMBER}) *(?:({_UNIT}))?', value)
    return (_number(match[1]), match[2]) if match is not None else None


def _cell_quantity(row, target, convert):
    quantity = _parse_table_quantity(row['cell'])
    if quantity is None:
        return {**row, 'status': 'value_unconfirmed'}
    value, cell_unit = quantity
    header_unit = row['header_unit']
    if header_unit and cell_unit and not _same_unit(header_unit, cell_unit):
        return {**row, 'status': 'unit_conflict', 'cell_unit': cell_unit}
    unit = cell_unit or header_unit
    if unit is None:
        return {**row, 'status': 'unit_missing'}
    observed = {**row, 'value': value, 'source_unit': unit}
    if convert:
        scale = document_units.SCALES.get(unit)
        return (_scaled_quantity(observed, scale, document_units.SCALES[target]) if scale is not None
                else {**observed, 'status': 'unit_unsupported'})
    return {**observed, 'status': 'confirmed' if _same_unit(unit, target) else 'unit_incompatible'}


def _test_numeric_filter(raw, header_unit, condition):
    target = condition['unit']
    convert = target in document_units.SCALES
    evidence = dict(value=raw, header_unit=header_unit)
    if not raw.strip():
        return {**evidence, 'status': 'unknown', 'reason': 'filter_blank'}
    observed = _cell_quantity(dict(cell=raw, header_unit=header_unit), target, convert)
    if observed['status'] != 'confirmed':
        reason = observed['status']
        return {**evidence, 'status': 'unknown', 'reason': 'filter_unit_incompatible' if reason == 'unit_incompatible' else reason}
    value = exact_math.parse(observed['converted']['exact'] if convert else observed['value'])
    threshold = exact_math.parse(condition['value'])
    matches = _COMPARISONS[condition['operator']](value, threshold)
    return {**evidence, 'status': 'true' if matches else 'false', 'source_unit': observed['source_unit'],
            'factor_exact': observed.get('factor_exact', '1'), 'normalized_exact': str(value)}


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


def _row_citations(document, observations):
    citations = []
    for _, group in groupby(enumerate(observations), key=lambda item: item[1]['line_number'] - item[0]):
        numbers = [row['line_number'] for _, row in group]
        start, end = numbers[0], numbers[-1]
        citations.append(f'[D{document}:L{start}]' if start == end else f'[D{document}:L{start}-L{end}]')
    return citations


def _aggregate_result(observations, convert, operation):
    total = 0
    for row in observations:
        value = row['converted']['exact'] if convert else row['value']
        total = exact_math.bounded(total + exact_math.parse(value))
    args = (dict(operation='quotient', values=[str(total), str(len(observations))]) if operation == 'mean'
            else dict(operation='sum', values=[str(total)]))
    return exact_math.calculate(args), str(total)


def _selected_quantities(operand, source, unit, convert, cancel):
    status, total_rows, matches = document_table.lookup_rows(operand, source, _UNIT)
    return status, total_rows, _quantities_from_matches(matches, unit, convert, cancel)


def _quantities_from_matches(matches, unit, convert, cancel):
    observations = []
    for line, row in matches:
        check_cancel(cancel)
        observations.append({**_cell_quantity(row, unit, convert), 'line_number': line})
    return observations


def _evaluate_aggregate(plan, cancel):
    request = document_table.parse_aggregate(plan['request'], _UNIT, document_units.SCALES)
    documents = plan['documents']
    if request is None or plan != {**request, 'documents': documents} or len(documents['catalog']) > 1:
        raise ValueError('表格彙總計畫與原問不符。')
    operands, unit, convert = request['operands'], request['unit'], request['convert']
    operand, = operands
    source, = document_table.lines_for_requests(documents, _catalog(documents, operands), operands)
    status, total_rows, observations = _selected_quantities(operand, source, unit, convert, cancel)
    result = total_exact = None
    count = len(observations)
    if status != 'selected':
        answer = '目前無法計算。' + document_table.reason(status)
        if source['status'] == 'provided':
            answer += ' ' + operand['citation']
    else:
        header = observations[0]['header_citation']
        invalid = [row for row in observations if row['status'] != 'confirmed']
        if invalid:
            lines = [f'符合條件的 {count} 列中，有 {len(invalid)} 列無法核對，無法計算。 {header}']
            lines.extend(f'{row["row_citation"]} {document_table.reason(row["status"])}' for row in invalid[:8])
            if len(invalid) > 8:
                lines.append('其餘無法核對的列可在「查看本次參考」查閱。')
        else:
            operation = request['operation']
            result, total_exact = _aggregate_result(observations, convert, operation)
            label = '平均值' if operation == 'mean' else '總和'
            lines = [f'「{operand["value_column"]}」的{label}為 {_display_exact(result)} {unit}，共 {count} 列。',
                     '表頭 ' + header,
                     '計入 ' + ' '.join(_row_citations(operand['document_id'], observations))]
        answer = '\n'.join(lines)
    check_cancel(cancel)
    return answer, dict(schema=document_table.AGGREGATE_RECEIPT, plan=plan, model_called=False,
                        selection_status=status, total_rows=total_rows, observations=observations,
                        total_exact=total_exact, result=result, output=answer, output_sha256=digest(answer))


def _range_label(operand):
    return f'D{operand["document_id"]} 第{operand["start_line"]}到{operand["end_line"]}行'


def _unknown_filter_lines(operand, group, filters):
    unknown = [row for row in group['decisions'] if row['status'] == 'unknown']
    label = _range_label(operand)
    lines = [f'{label}，{len(unknown)} 列無法判定是否符合。']
    for row in unknown[:8]:
        indexes = row.get('unresolved', range(len(filters)))
        unresolved = [(filters[index], row['tests'][index]) for index in indexes
                      if row['tests'][index]['status'] == 'unknown']
        if any('reason' in test for _, test in unresolved):
            details = ' '.join(f'「{condition["column"]}」{document_table.reason(test.get("reason", "filter_blank"))}'
                               for condition, test in unresolved)
        else:
            names = '、'.join(f'「{condition["column"]}」' for condition, _ in unresolved)
            details = f'{names}空白。'
        lines.append(f'[D{operand["document_id"]}:L{row["line_number"]}] {details}')
    invalid = [row for row in group['observations'] if row['status'] != 'confirmed']
    lines.extend(f'{row["row_citation"]} 符合條件。{document_table.reason(row["status"])}' for row in invalid[:8])
    if len(unknown) > 8 or len(invalid) > 8:
        lines.append('其餘無法核對的列可在「查看本次參考」查閱。')
    return lines


def _multi_source_lines(operand, source, group, complete, filters=None):
    label = _range_label(operand)
    status, rows = group['selection_status'], group['observations']
    if status == 'filter_unknown':
        return _unknown_filter_lines(operand, group, filters)
    if status != 'selected':
        citation = f' {operand["citation"]}' if source['status'] == 'provided' else ''
        return [f'{label}，{document_table.reason(status)}{citation}']
    header = rows[0]['header_citation']
    invalid = [row for row in rows if row['status'] != 'confirmed']
    if invalid:
        lines = [f'{label}，符合 {len(rows)} 列，其中 {len(invalid)} 列無法核對。表頭 {header}']
        lines.extend(f'{row["row_citation"]} {document_table.reason(row["status"])}' for row in invalid[:8])
        if len(invalid) > 8:
            lines.append('其餘無法核對的列可在「查看本次參考」查閱。')
        return lines
    verb = '計入' if complete else '符合'
    citations = ' '.join(_row_citations(operand['document_id'], rows))
    return [f'{label}，表頭 {header}，{verb} {len(rows)} 列。 {citations}']


def _evaluate_grouped_aggregate(plan, cancel):
    relative = plan['schema'] == document_table.RELATIVE_DATE_AGGREGATE_PLAN
    dated = relative or plan['schema'] == document_table.DATE_AGGREGATE_PLAN
    logical = dated or plan['schema'] == document_table.LOGIC_AGGREGATE_PLAN
    numeric = logical or plan['schema'] == document_table.NUMERIC_AGGREGATE_PLAN
    filtered = numeric or plan['schema'] == document_table.FILTER_AGGREGATE_PLAN
    if relative:
        request = _parse_date_aggregate(plan['request'], relative=True)
        if request is not None:
            request = document_dates.bind_relative_plan(request, plan['date_observation'])
        schema = document_table.RELATIVE_DATE_AGGREGATE_RECEIPT
    elif dated:
        request = _parse_date_aggregate(plan['request'])
        schema = document_table.DATE_AGGREGATE_RECEIPT
    elif logical:
        request = _parse_logic_aggregate(plan['request'])
        schema = document_table.LOGIC_AGGREGATE_RECEIPT
    elif numeric:
        request = _parse_numeric_aggregate(plan['request'])
        schema = document_table.NUMERIC_AGGREGATE_RECEIPT
    else:
        parser = document_table.parse_filter_aggregate if filtered else document_table.parse_multi_aggregate
        request = parser(plan['request'], _UNIT, document_units.SCALES)
        schema = document_table.FILTER_AGGREGATE_RECEIPT if filtered else document_table.MULTI_AGGREGATE_RECEIPT
    documents = plan['documents']
    if request is None or plan != {**request, 'documents': documents}:
        raise ValueError('跨表彙總計畫與原問不符。')
    operands, unit, convert = request['operands'], request['unit'], request['convert']
    filters = request.get('filters')
    logic = request.get('logic')
    catalog = _catalog(documents, operands)
    sources = document_table.lines_for_requests(documents, catalog, operands)
    overlaps = document_table.overlaps(operands, catalog)
    result = total_exact = None
    groups = []
    date_errors = [f'「{condition["column"]}」的日期條件，{issue}' for condition in filters or ()
                   if 'date' in condition and (issue := document_dates.condition_error(condition))]
    if date_errors:
        status = 'invalid_date_condition'
        lines = ['目前無法計算。請核對下列日期條件。', *date_errors]
    elif overlaps:
        status = 'range_overlap'
        lines = ['目前無法計算。下列範圍來自相同內容且行號重疊，請選擇互不重疊的範圍。']
        lines.extend(f'{_range_label(operands[a])} 與 {_range_label(operands[b])}。' for a, b in overlaps)
    else:
        for operand, source in zip(operands, sources, strict=True):
            check_cancel(cancel)
            if filtered:
                selected, total, matches, decisions = document_table.filter_rows(
                    operand, source, _UNIT, filters, _test_numeric_filter if numeric else None, logic)
                rows = _quantities_from_matches(matches, unit, convert, cancel)
                groups.append(dict(selection_status=selected, total_rows=total, observations=rows, decisions=decisions))
            else:
                selected, total, rows = _selected_quantities(operand, source, unit, convert, cancel)
                groups.append(dict(selection_status=selected, total_rows=total, observations=rows))
        rows = [row for group in groups for row in group['observations']]
        if (any(group['selection_status'] not in ('selected', 'row_missing') for group in groups)
                or any(row['status'] != 'confirmed' for row in rows)):
            status = 'source_blocked'
            lines = ['目前無法完成篩選彙總。請核對以下來源。' if filtered else '目前無法完成跨表計算。請核對以下來源。']
        elif not rows:
            status = 'no_match'
            lines = ['指定範圍內都沒有符合條件的資料列，無法計算。']
        else:
            status = 'complete'
            operation = request['operation']
            result, total_exact = _aggregate_result(rows, convert, operation)
            label = '平均值' if operation == 'mean' else '總和'
            column = operands[0]['value_column']
            scope = '篩選後' if filtered else '跨表'
            lines = [f'「{column}」的{scope}{label}為 {_display_exact(result)} {unit}，共 {len(rows)} 列。']
        for operand, source, group in zip(operands, sources, groups, strict=True):
            lines.extend(_multi_source_lines(operand, source, group, status == 'complete', filters))
    if relative:
        lines[1:1] = document_dates.describe_relative_plan(request)
    check_cancel(cancel)
    answer = '\n'.join(lines)
    return answer, dict(schema=schema, plan=plan, model_called=False,
                        status=status, overlaps=overlaps, groups=groups, total_exact=total_exact,
                        result=result, output=answer, output_sha256=digest(answer))


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
    return local_answer_receipts.read(store, mid, MODE, FIELD, validate, document_based=True,
                                     validate_origin=validate_origin)


def scenario_ids(store, rows):
    """Label visible scenario candidates. Selection still verifies the complete receipt."""
    candidates = [row['id'] for row in rows if row['role'] == 'assistant' and row['content'].startswith('依文件原值\n')]
    if not candidates:
        return set()
    placeholders = ','.join('?' for _ in candidates)
    with store.connect() as db:
        saved = db.execute(f'''SELECT message_id FROM answer_contexts WHERE message_id IN ({placeholders})
            AND CASE WHEN length(CAST(payload AS BLOB))<=? AND json_valid(payload)
                THEN json_extract(payload,'$.response_mode')=? AND json_extract(payload,'$.document_calculation.schema')=?
                END''', (*candidates, local_answer_receipts.MAX_JSON_BYTES, MODE, SCENARIO_RECEIPT)).fetchall()
    return {row[0] for row in saved}


def validate_origin(db, sid, before, plan):
    if plan['schema'] not in ORIGINAL_RECEIPTS:
        return
    target = _parse_original_followup(plan['request'])
    origin = _original_followup(db, sid, plan['request'], before, target)
    if origin is not None and plan['schema'] == REMAPPED_ORIGINAL_PLAN:
        origin = bind_live_documents(origin, plan['documents'])
    if plan != origin:
        if plan['schema'] != ORIGINAL_PLAN:
            raise ValueError('文件原值重算與指定試算回答的算式或來源不符。')
        raise ValueError('文件原值重算與上一則試算的算式或來源不符。')


def describe(receipt):
    plan = receipt['plan']
    if receipt['schema'] in ORIGINAL_RECEIPTS.values():
        intro = '沿用' if receipt['schema'] == ORIGINAL_RECEIPT else '依你指定的'
        lines = [receipt['output'], f'{intro}本對話文件試算回答 #{plan["scenario_source"]["answer_id"]} 的原算式',
                 plan['base']['request']]
        if receipt['schema'] == REMAPPED_ORIGINAL_RECEIPT:
            lines += ['本次引用的算式', _original_formula(plan)['request']]
        return '\n\n'.join(lines)
    if receipt['schema'] == SCENARIO_RECEIPT:
        lines = [receipt['output'], '依文件原值重算的算式\n' + plan['base']['request']]
        quantity = receipt['scenario']['quantity']
        if 'factor_exact' in quantity and quantity['source_unit'] != plan['base']['unit']:
            lines.append(f'試算換算係數\n1 {quantity["source_unit"]} = {quantity["factor_exact"]} {plan["base"]["unit"]}')
        return '\n\n'.join(lines)
    if receipt['schema'] in (document_table.MULTI_AGGREGATE_RECEIPT, document_table.FILTER_AGGREGATE_RECEIPT,
                             document_table.NUMERIC_AGGREGATE_RECEIPT, document_table.LOGIC_AGGREGATE_RECEIPT,
                             document_table.DATE_AGGREGATE_RECEIPT, document_table.RELATIVE_DATE_AGGREGATE_RECEIPT):
        lines = [receipt['output']]
        if 'logic' in plan:
            lines.append('\n條件結構 ' + document_boolean.describe(plan['logic']))
        groups = receipt['groups']
        if plan['operation'] == 'mean' and receipt['total_exact'] is not None:
            count = sum(len(group['observations']) for group in groups)
            lines.append(f'\n精確總和 {receipt["total_exact"]} {plan["unit"]} ÷ {count}')
        for operand, group in zip(plan['operands'], groups):
            lines.append('\n' + _range_label(operand))
            if 'decisions' in group:
                lines.extend(_describe_filter_decisions(group, plan['filters']))
            lines.extend(_describe_quantities(group, plan['unit']))
        return '\n'.join(lines)
    if receipt['schema'] == document_table.AGGREGATE_RECEIPT:
        lines = [receipt['output']]
        if receipt['total_rows'] is not None:
            lines.append(f'\n範圍內有 {receipt["total_rows"]} 筆資料列，符合條件 {len(receipt["observations"])} 筆。')
        if plan['operation'] == 'mean' and receipt['total_exact'] is not None:
            lines.append(f'精確總和 {receipt["total_exact"]} {plan["unit"]} ÷ {len(receipt["observations"])}')
        lines.extend(_describe_quantities(receipt, plan['unit']))
        return '\n'.join(lines)
    factors = [f'{row.get("row_citation", operand["citation"])} 1 {row["source_unit"]} = {row["factor_exact"]} {plan["unit"]}'
               for operand, row in zip(plan['operands'], receipt['observations'], strict=True)
               if 'factor_exact' in row and row['source_unit'] != plan['unit']]
    return receipt['output'] + ('\n\n換算係數\n' + '\n'.join(factors) if factors else '')


def _describe_quantities(group, unit):
    for row in group['observations']:
        citation = row['row_citation']
        if row['status'] != 'confirmed':
            yield f'{citation} {document_table.reason(row["status"])}'
            continue
        line = f'{citation} {row["value"]} {row["source_unit"]}'
        if 'converted' in row and row['source_unit'] != unit:
            line += f' × {row["factor_exact"]} = {_display_exact(row["converted"])} {unit}'
        yield line


def _describe_filter_decisions(group, filters):
    labels = {'matched': '符合', 'excluded': '排除', 'unknown': '未定'}
    outcomes = {'true': '符合', 'false': '不符', 'unknown': '空白'}
    for row in group['decisions']:
        details = []
        for condition, test in zip(filters, row['tests'], strict=True):
            raw = json.dumps(test['value'], ensure_ascii=False)
            outcome = outcomes[test['status']]
            if 'reason' in test:
                outcome = document_table.reason(test['reason']).removesuffix('。')
            elif 'normalized_exact' in test:
                outcome += f'，比較值 {test["normalized_exact"]} {condition["unit"]}'
            elif 'normalized_date' in test:
                outcome += f'，比較日期 {test["normalized_date"]}'
            details.append(f'「{condition["column"]}」{raw}（{outcome}）')
        if row.get('unresolved'):
            names = '、'.join(f'「{filters[index]["column"]}」' for index in row['unresolved'])
            details.append(f'仍需核對 {names}')
        yield f'第 {row["line_number"]} 行 · {labels[row["status"]]} · ' + '、'.join(details)
