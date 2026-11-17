"""Bind exact arithmetic leaves to literals in saved local evidence."""
import hashlib
import re

from fractions import Fraction

from . import document_units, exact_math
from .goal_guidance_sources import standalone_clauses

_DOCUMENT = re.compile(r'\[D([1-9][0-9]{0,11}):L([1-9][0-9]{0,11})\]')
_USER = re.compile(r'\[(?:補充|更正)#[1-9][0-9]{0,3}\]')
_OBJECTIVE = '[目標]'
_NUMERIC_CHARS = str.maketrans('０１２３４５６７８９．／－＋％，−', '0123456789./-+%,-')
_NUMBER = r'(?<![\dA-Za-z_./+\-])(?<![0-9],)(?P<number>-?[0-9]+(?:\.[0-9]+|/[0-9]+)?)'
_LITERAL = re.compile(_NUMBER + r'(?![\dA-Za-z_./+\-%])(?!,[0-9])')
_FIELD = re.compile(r'欄位「(?P<name>[^「」\r\n\x00]{1,80})」[ \t]*(?:為|[:：])[ \t]*\Z')
_SCOPED_NAME = r'[^「」『』“”\[\]：:；;、，,。！？?!\r\n\x00]{1,80}'
SCOPED_FIELD = re.compile(
    rf'(?P<correction>(?:請[ \t]*)?正式更正[ \t]*)?'
    rf'「(?P<subject>{_SCOPED_NAME})」[ \t]*的[ \t]*(?:欄位[ \t]*)?'
    rf'「(?P<field>{_SCOPED_NAME})」'
)
_REQUIRED_KEYS = {'path', 'citation', 'quote'}
_OPTIONAL_KEYS = {'conversion', 'field'}
_UNIT = re.compile(r'[A-Za-zµμ%°㐀-鿿][A-Za-zµμ%°㐀-鿿0-9^²³/·]{0,15}')
_UNIT_END = r'(?=$|[\s,，。；;、！!？?：:）)\]」』】]|\.(?!\S))'
_CHOICE_QUANTITY = re.compile(_NUMBER + rf'[ \t\u3000]*(?P<unit>{_UNIT.pattern})\Z')
_OPTION_NOTE = re.compile(r'(?:（(?P<wide>[^()（）]{1,40})）|\((?P<ascii>[^()（）]{1,40})\))\Z')
_LINE_NAME = re.compile(r'第[ \t]*[0-9０-９一二三四五六七八九十百]+[ \t]*[行筆列]\Z')
_DOCUMENT_GROUP = re.compile(
    r'(?P<name>(?:本次|本批|本輪|當次|當批)[ \t]*[A-Za-z0-9０-９㐀-鿿_-]{1,32}(?:組|隊|班))'
    r'[ \t]*[：:][ \t]*')
_UNCERTAIN_GROUP = re.compile(r'待查|待定|待確認|未確認|未登記|待補|假設|暫定|試算|預估|舊版|前次|或者|或是')
_ZERO_UNITS = r'台|臺|件|個|支|張|片|套|箱|盒|袋|組|份|瓶|包|卷|冊|本|人|次'
_ZERO_STATEMENT = re.compile(r'(?:已確認|確認|確定)(?:為)?(?:沒有|無)[A-Za-z㐀-鿿]{0,24}\Z')
_ZERO_AMBIGUITY = re.compile(
    r'無法|登記|資料|紀錄|記錄|清單|表單|[台臺]帳|依據|佐證|提供|填|證據|回報|查到|發現|'
    r'未|暫|疑|待|或|但|若|估|可能|確認|不|仍|歷史|前次|前批|舊版')
_ZERO_RULE = re.compile(
    rf'(?:(?:文字|原文|狀態)\s*)?「(?P<phrase>[^「」\r\n]{{1,40}})」\s*'
    rf'(?:代表|表示|等於|視為|計為|記為)\s*(?:(?:確定|確認)\s*為?)?'
    rf'(?P<amount>[0-9０-９]+|[零一二兩三四五六七八九十]+)\s*(?P<unit>{_ZERO_UNITS})')
_RULE_COMMA = re.compile(r'(?![0-9])')


def _leaves(node, path=''):
    if isinstance(node, str):
        yield path, node
    else:
        for index, child in enumerate(node['values']):
            yield from _leaves(child, f'{path}/values/{index}')


def _conversion_spec(conversion):
    if (not isinstance(conversion, dict) or conversion.keys() != {'from', 'to'}
            or any(not isinstance(unit, str) for unit in conversion.values())):
        raise ValueError('換算須指定原單位 from 與算式單位 to。')
    source, target = conversion['from'], conversion['to']
    if (source, target) == ('%', 'ratio'):
        factor = Fraction(1, 100)
        boundary = r'(?![\d%/^])'
    else:
        scales = document_units.SCALES
        if source not in scales or target not in scales or scales[source][0] != scales[target][0]:
            raise ValueError('換算須使用相同量綱的支援單位，或將百分比換成比例。')
        factor = scales[source][1] / scales[target][1]
        boundary = _UNIT_END
    pattern = re.compile(_NUMBER + rf'[ \t]*(?P<unit>{re.escape(source)})' + boundary)
    return pattern, factor


def _spec(row):
    if 'conversion' in row:
        return _conversion_spec(row['conversion'])
    if 'unit' not in row:
        return _LITERAL, 1
    unit = row['unit']
    if not isinstance(unit, str) or not _UNIT.fullmatch(unit):
        raise ValueError('運算元須附原文中的單一單位。')
    return re.compile(_NUMBER + rf'[ 	]*(?P<unit>{re.escape(unit)})' + _UNIT_END), 1


def _numeric_match(quote, value, pattern, factor):
    target = exact_math.parse(value)
    matches = []
    for token in pattern.finditer(quote.translate(_NUMERIC_CHARS)):
        try:
            number = exact_math.parse(token['number'])
        except ValueError:
            continue
        if number * factor == target:
            matches.append((token, number))
    if len(matches) != 1:
        if pattern is not _LITERAL:
            raise ValueError('運算元短句須有一處原數量及單位，換算後與算式精確相符。')
        raise ValueError('運算元短句須有一處數值與算式精確相符。')
    return matches[0]


def _field_position(quote, number_start, field):
    if not isinstance(field, str) or not re.fullmatch(r'[^「」\r\n\0]{1,80}', field) or field.strip() != field:
        raise ValueError('運算元欄位名須與該數字前明列的完整欄位名逐字相同。')
    prefix = quote[:number_start]
    if (match := _FIELD.search(prefix)) and match['name'] == field:
        return match.span('name')
    name = re.escape(field)
    revision = re.fullmatch(rf'(?:(?:欄位\s*)?「(?P<quoted>{name})」|(?P<bare>{name}))\s*'
                            r'(?:正式\s*|(?:剛才|先前|原先|上一則)\s*(?:說|填|寫|報)錯了?\s*[，,、]?\s*)?'
                            r'(?:改成|改為|更正為|更正成|應改為)\s*(?P<literal_quote>「\s*)?', prefix)
    if revision and (not revision['literal_quote'] or quote[number_start:].rstrip('。').endswith('」')):
        return revision.span('quoted' if revision['quoted'] is not None else 'bare')
    raise ValueError('運算元欄位名須與該數字前明列的完整欄位名逐字相同。')


def scoped_field_prefix(quote, number_start, field, *, document):
    match = SCOPED_FIELD.match(quote)
    if not match or match['field'] != field or match['subject'].strip() != match['subject']:
        return None
    end = quote[match.end():number_start].strip()
    if match['correction'] and end == '為' or re.fullmatch(r'正式(?:改[為成]|更正為)', end):
        return match
    if document and not match['correction'] and end in {'為', '：', ':'}:
        return match
    return None


def _operand_field(quote, token, field, *, ordinary, document_group):
    try:
        return field_position(quote, token.start('number'), field, ordinary=ordinary), None
    except ValueError:
        if (not document_group or not (group := _DOCUMENT_GROUP.match(quote))
                or _UNCERTAIN_GROUP.search(group['name']) or 'unit' not in token.re.groupindex
                or quote[token.end():].strip() not in {'', '。', '.'}):
            raise
        offset = group.end()
        lo, hi = field_position(quote[offset:], token.start('number') - offset, field, ordinary=True)
        return (offset + lo, offset + hi), group


def _confirmed_absence(phrase):
    return (bool(_ZERO_STATEMENT.fullmatch(phrase))
            and not _ZERO_AMBIGUITY.search(re.sub(r'^(?:已確認|確認|確定)', '', phrase)))


def explicit_zero_rules(text, *, include_comma_clauses=False):
    rules = []
    source_sha256 = hashlib.sha256(text.encode('utf-8')).hexdigest()
    comma_before = _RULE_COMMA if include_comma_clauses else None
    for offset, quote, question in standalone_clauses(text, comma_before=comma_before):
        match = _ZERO_RULE.fullmatch(quote)
        if not question and match and _confirmed_absence(match['phrase']):
            rules.append({'citation': _OBJECTIVE, 'quote': quote, 'quote_start': offset,
                          'phrase': match['phrase'], 'unit': match['unit'], 'declared_amount': match['amount'],
                          'source_sha256': source_sha256})
    return rules


def matches_zero_rule(rule, unit):
    return rule['declared_amount'] in {'0', '０', '零'} and (unit is None or rule['unit'] == unit)


def _absence_lexeme(row, value):
    """Find a literal, complete named absence claim for a zero operand."""
    if (not _DOCUMENT.fullmatch(row['citation']) or 'conversion' in row or exact_math.parse(value) != 0
            or not isinstance(field := row.get('field'), str)
            or not re.fullmatch(r'[^「」：:\r\n\0]{1,80}', field) or field.strip() != field):
        return None
    quote = row['quote']
    group = _DOCUMENT_GROUP.match(quote)
    if group and _UNCERTAIN_GROUP.search(group['name']):
        return None
    offset = group.end() if group else 0
    prefix = re.match(rf'(?:(?:欄位)?「(?P<quoted>{re.escape(field)})」|(?P<bare>{re.escape(field)}))'
                      r'[ \t]*[：:]?[ \t]*', quote[offset:])
    if not prefix:
        return None
    start = offset + prefix.end()
    phrase = quote[start:].removesuffix('。').removesuffix('.').rstrip()
    if not _confirmed_absence(phrase):
        return None
    lo, hi = prefix.span('quoted' if prefix['quoted'] is not None else 'bare')
    return {'phrase': phrase, 'start': start, 'end': start + len(phrase),
            'field_start': offset + lo, 'field_end': offset + hi}, group


def validate(rows, expression, ranges, *, omitted_paths=(), ordinary=False, document_groups=False,
             explicit_zero=False, scoped_fields=False):
    leaves = {path: value for path, value in _leaves(expression) if path not in omitted_paths}
    if not isinstance(rows, list) or len(rows) != len(leaves):
        raise ValueError('運算元證據須涵蓋算式中每個數字葉節點。')
    optional = _OPTIONAL_KEYS | {'unit'} if ordinary else _OPTIONAL_KEYS
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or not _REQUIRED_KEYS <= row.keys()
                or row.keys() - _REQUIRED_KEYS - optional
                or {'unit', 'conversion'} <= row.keys()
                or not isinstance(row['path'], str) or row['path'] not in leaves or row['path'] in seen):
            raise ValueError('運算元證據須使用不同且實際存在的數字節點路徑。')
        path, citation, quote = row['path'], row['citation'], row['quote']
        seen.add(path)
        if not isinstance(citation, str):
            raise ValueError('運算元須引用原行、補充、更正或目標。')
        doc_line = _DOCUMENT.fullmatch(citation)
        if doc_line:
            did, line = map(int, doc_line.groups())
            if ranges is not None and not any(
                    span['document_id'] == did and span['start_line'] <= line <= span['end_line'] for span in ranges):
                raise ValueError(f'運算元 {path or "根"} 的原行不在該項指定範圍。')
        elif citation != _OBJECTIVE and not _USER.fullmatch(citation):
            raise ValueError('運算元須引用原行、補充、更正或目標。')
        if (not isinstance(quote, str) or not 1 <= len(quote) <= 180 or quote.strip() != quote
                or any(ch in quote for ch in '\0\r\n')):
            raise ValueError('運算元證據須提供一段最多 180 字的逐字短句。')
        pattern, factor = _spec(row)
        if explicit_zero and _absence_lexeme(row, leaves[path]):
            continue
        token, _ = _numeric_match(quote, leaves[path], pattern, factor)
        if 'field' in row and not (scoped_fields and scoped_field_prefix(
                quote, token.start('number'), row['field'], document=doc_line is not None)):
            _operand_field(quote, token, row['field'], ordinary=ordinary,
                           document_group=document_groups and doc_line is not None)


def user_texts(objective, replies, guidance, sequence):
    values = {row['citation']: (row['answer'], row['sha256']) for row in replies if row['ask_sequence'] < sequence}
    values.update((row['citation'], (row['content'], row['sha256'])) for row in guidance if row['steps_seen'] < sequence)
    values[_OBJECTIVE] = (objective, hashlib.sha256(objective.encode('utf-8')).hexdigest())
    return values


def _document_text(doc_line, excerpts):
    did, line = map(int, doc_line.groups())
    prefix = f'L{line}: '
    found = {(text[len(prefix):], row['sha256']) for row in excerpts
             if row['document_id'] == did and row['start_line'] <= line <= row['end_line']
             for text in row['text'].split('\n') if text.startswith(prefix)}
    if len(found) != 1:
        raise ValueError('運算元引用的原行缺少唯一完整的查讀文字。')
    return next(iter(found))


def source_text(citation, excerpts, user_sources):
    if doc_line := _DOCUMENT.fullmatch(citation):
        return _document_text(doc_line, excerpts)
    if citation in user_sources:
        return user_sources[citation]
    raise ValueError('引文未引用本目標當時已保存的來源。')


def field_position(quote, number_start, field, *, ordinary):
    if ordinary and isinstance(field, str) and re.fullmatch(r'[^「」\r\n\0]{1,80}', field) and field.strip() == field:
        if match := re.fullmatch(
                rf'(?P<name>{re.escape(field)})(?:[ 	]*(?:[：:]|已確認為|確認為|正式改為|正式更正為|為|共)[ 	]*|[ 	]+)',
                quote[:number_start]):
            return match.span('name')
    return _field_position(quote, number_start, field)


def choice_quantity(text, *, field=None, displayed=False):
    normalized = text.translate(_NUMERIC_CHARS)
    if field is not None or displayed:
        token = _CHOICE_QUANTITY.search(normalized)
    else:
        token = _CHOICE_QUANTITY.fullmatch(normalized)
    if not token:
        return None
    try:
        exact = str(exact_math.parse(token['number']))
        if field is not None:
            field_position(text, token.start('number'), field, ordinary=True)
    except ValueError:
        return None
    return {'number': text[token.start('number'):token.end('number')], 'exact': exact,
            'unit': text[token.start('unit'):token.end('unit')]}


def displayed_choice_quantities(text):
    values = []
    if note := _OPTION_NOTE.search(text):
        text = text[:note.start()].rstrip()
        if value := choice_quantity((note['wide'] or note['ascii']).strip()):
            values.append(value)
    if not _LINE_NAME.search(text) and (value := choice_quantity(text, displayed=True)):
        values.insert(0, value)
    return values


def bind(rows, expression, excerpts, user_sources, *, ordinary=False, document_groups=False,
         zero_rules=None, scoped_fields=False):
    leaves = dict(_leaves(expression))
    bound = []
    for row in rows:
        path, citation, quote = row['path'], row['citation'], row['quote']
        label = path or '根'
        try:
            text, sha = source_text(citation, excerpts, user_sources)
        except ValueError as exc:
            if ordinary or _DOCUMENT.fullmatch(citation):
                raise
            raise ValueError(f'運算元 {label} 未引用本目標當時已保存的補充。') from exc
        start = text.find(quote)
        if start < 0 or text.find(quote, start + 1) >= 0:
            raise ValueError(f'運算元 {label} 的短句在該來源無法唯一定位。')
        if zero_rules is not None and (absence := _absence_lexeme(row, leaves[path])):
            lexeme, group = absence
            rules = [rule for rule in zero_rules if rule['phrase'] == lexeme['phrase']]
            if (len(rules) != 1 or not matches_zero_rule(rules[0], row.get('unit'))
                    or text[:start].strip() or text[start + len(quote):].strip() not in {'', '。', '.'}):
                raise ValueError('零值須引用完整的已確認缺項原句及唯一適用的目標換算規則，單位須一致。')
            receipt = {**row, 'source_sha256': sha, 'quote_start': start, 'quote_end': start + len(quote),
                       'field_start': start + lexeme['field_start'], 'field_end': start + lexeme['field_end'],
                       'explicit_zero': {'phrase': lexeme['phrase'], 'phrase_start': start + lexeme['start'],
                                         'phrase_end': start + lexeme['end'], 'exact': '0', 'rule': rules[0]}}
            if group:
                receipt['document_group'] = {'name': group['name'], 'start': start + group.start('name'),
                                             'end': start + group.end('name')}
            bound.append(receipt)
            continue
        pattern, factor = _spec(row)
        token, number = _numeric_match(quote, leaves[path], pattern, factor)
        low, high = token.span('number')
        # Match within the complete source too, so a clipped quote cannot turn 17 into 7.
        if not any(candidate.span() == (start + token.start(), start + token.end())
                   for candidate in pattern.finditer(text.translate(_NUMERIC_CHARS))):
            raise ValueError(f'運算元 {label} 的短句截斷了來源中的數值。')
        receipt = {**row, 'source_sha256': sha, 'quote_start': start, 'quote_end': start + len(quote),
                   'number_start': start + low, 'number_end': start + high, 'number': quote[low:high]}
        if 'field' in row:
            document = _DOCUMENT.fullmatch(citation) is not None
            scope = (scoped_field_prefix(quote, token.start('number'), row['field'], document=document)
                     if scoped_fields else None)
            if scope:
                field_start, field_end = scope.span('field')
                subject_start, subject_end = scope.span('subject')
                receipt['field_scope'] = {'name': scope['subject'], 'start': start + subject_start,
                                          'end': start + subject_end}
                group = None
            else:
                (field_start, field_end), group = _operand_field(
                    quote, token, row['field'], ordinary=ordinary, document_group=document_groups and document)
            receipt.update(field_start=start + field_start, field_end=start + field_end)
            if group:
                if text[:start].strip() or text[start + len(quote):].strip() not in {'', '。', '.'}:
                    raise ValueError('分組運算元須引用整行的唯一已確認數量，不可取用附註或截斷原行。')
                receipt['document_group'] = {'name': group['name'], 'start': start + group.start('name'),
                                             'end': start + group.end('name')}
        if 'unit' in row:
            unit_start, unit_end = token.span('unit')
            receipt.update(unit_start=start + unit_start, unit_end=start + unit_end)
        if 'conversion' in row:
            unit_start, unit_end = token.span('unit')
            receipt['conversion'] = {**row['conversion'], 'source_exact': str(number), 'factor_exact': str(factor),
                                     'converted_exact': str(number * factor), 'unit': quote[unit_start:unit_end],
                                     'unit_start': start + unit_start, 'unit_end': start + unit_end}
        bound.append(receipt)
    return bound
