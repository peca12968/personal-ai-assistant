"""Bind exact arithmetic leaves to literals in saved local evidence."""
import hashlib
import re

from fractions import Fraction

from . import document_units, exact_math

_DOCUMENT = re.compile(r'\[D([1-9][0-9]{0,11}):L([1-9][0-9]{0,11})\]')
_USER = re.compile(r'\[(?:補充|更正)#[1-9][0-9]{0,3}\]')
_OBJECTIVE = '[目標]'
_NUMERIC_CHARS = str.maketrans('０１２３４５６７８９．／－＋％，−', '0123456789./-+%,-')
_NUMBER = r'(?<![\dA-Za-z_./+\-])(?<![0-9],)(?P<number>-?[0-9]+(?:\.[0-9]+|/[0-9]+)?)'
_LITERAL = re.compile(_NUMBER + r'(?![\dA-Za-z_./+\-%])(?!,[0-9])')
_FIELD = re.compile(r'欄位「(?P<name>[^「」\r\n\x00]{1,80})」[ \t]*(?:為|[:：])[ \t]*\Z')
_REQUIRED_KEYS = {'path', 'citation', 'quote'}
_OPTIONAL_KEYS = {'conversion', 'field'}


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
        boundary = r'(?=$|[\s,，。；;、！!？?：:）)\]」』】]|\.(?!\S))'
    pattern = re.compile(_NUMBER + rf'[ \t]*(?P<unit>{re.escape(source)})' + boundary)
    return pattern, factor


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


def validate(rows, expression, ranges, *, omitted_paths=()):
    leaves = {path: value for path, value in _leaves(expression) if path not in omitted_paths}
    if not isinstance(rows, list) or len(rows) != len(leaves):
        raise ValueError('運算元證據須涵蓋算式中每個數字葉節點。')
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or not _REQUIRED_KEYS <= row.keys()
                or row.keys() - _REQUIRED_KEYS - _OPTIONAL_KEYS
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
        pattern, factor = _conversion_spec(row['conversion']) if 'conversion' in row else (_LITERAL, 1)
        token, _ = _numeric_match(quote, leaves[path], pattern, factor)
        if 'field' in row:
            _field_position(quote, token.start('number'), row['field'])


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


def bind(rows, expression, excerpts, user_sources):
    leaves = dict(_leaves(expression))
    bound = []
    for row in rows:
        path, citation, quote = row['path'], row['citation'], row['quote']
        label = path or '根'
        doc_line = _DOCUMENT.fullmatch(citation)
        if doc_line:
            text, sha = _document_text(doc_line, excerpts)
        elif citation in user_sources:
            text, sha = user_sources[citation]
        else:
            raise ValueError(f'運算元 {label} 未引用本目標當時已保存的補充。')
        start = text.find(quote)
        if start < 0 or text.find(quote, start + 1) >= 0:
            raise ValueError(f'運算元 {label} 的短句在該來源無法唯一定位。')
        pattern, factor = _conversion_spec(row['conversion']) if 'conversion' in row else (_LITERAL, 1)
        token, number = _numeric_match(quote, leaves[path], pattern, factor)
        low, high = token.span('number')
        # Match within the complete source too, so a clipped quote cannot turn 17 into 7.
        if not any(candidate.span() == (start + token.start(), start + token.end())
                   for candidate in pattern.finditer(text.translate(_NUMERIC_CHARS))):
            raise ValueError(f'運算元 {label} 的短句截斷了來源中的數值。')
        receipt = {**row, 'source_sha256': sha, 'quote_start': start, 'quote_end': start + len(quote),
                   'number_start': start + low, 'number_end': start + high, 'number': quote[low:high]}
        if 'field' in row:
            field_start, field_end = _field_position(quote, low, row['field'])
            receipt.update(field_start=start + field_start, field_end=start + field_end)
        if 'conversion' in row:
            unit_start, unit_end = token.span('unit')
            receipt['conversion'] = {**row['conversion'], 'source_exact': str(number), 'factor_exact': str(factor),
                                     'converted_exact': str(number * factor), 'unit': quote[unit_start:unit_end],
                                     'unit_start': start + unit_start, 'unit_end': start + unit_end}
        bound.append(receipt)
    return bound
