"""Bind exact arithmetic leaves to literals in saved local evidence."""
import hashlib
import re

from . import exact_math

_DOCUMENT = re.compile(r'\[D([1-9][0-9]{0,11}):L([1-9][0-9]{0,11})\]')
_USER = re.compile(r'\[(?:補充|更正)#[1-9][0-9]{0,3}\]')
_OBJECTIVE = '[目標]'
_NUMERIC_CHARS = str.maketrans('０１２３４５６７８９．／－＋％，−', '0123456789./-+%,-')
_LITERAL = re.compile(r'(?<![\dA-Za-z_./+\-])(?<![0-9],)-?[0-9]+(?:\.[0-9]+|/[0-9]+)?'
                      r'(?![\dA-Za-z_./+\-%])(?!,[0-9])')


def _leaves(node, path=''):
    if isinstance(node, str):
        yield path, node
    else:
        for index, child in enumerate(node['values']):
            yield from _leaves(child, f'{path}/values/{index}')


def _numeric_match(quote, value):
    target = exact_math.parse(value)
    matches = []
    for token in _LITERAL.finditer(quote.translate(_NUMERIC_CHARS)):
        try:
            number = exact_math.parse(token.group())
        except ValueError:
            continue
        if number == target:
            matches.append(token.span())
    if len(matches) != 1:
        raise ValueError('運算元短句須有一處數值與算式精確相符。')
    return matches[0]


def validate(rows, expression, ranges):
    leaves = dict(_leaves(expression))
    if not isinstance(rows, list) or len(rows) != len(leaves):
        raise ValueError('運算元證據須涵蓋算式中每個數字葉節點。')
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or row.keys() != {'path', 'citation', 'quote'}
                or not isinstance(row['path'], str) or row['path'] not in leaves or row['path'] in seen):
            raise ValueError('運算元證據須使用不同且實際存在的數字節點路徑。')
        path, citation, quote = row['path'], row['citation'], row['quote']
        seen.add(path)
        if not isinstance(citation, str):
            raise ValueError('運算元須引用原行、補充、更正或目標。')
        doc_line = _DOCUMENT.fullmatch(citation)
        if doc_line:
            did, line = map(int, doc_line.groups())
            if not any(span['document_id'] == did and span['start_line'] <= line <= span['end_line'] for span in ranges):
                raise ValueError(f'運算元 {path or "根"} 的原行不在該項指定範圍。')
        elif citation != _OBJECTIVE and not _USER.fullmatch(citation):
            raise ValueError('運算元須引用原行、補充、更正或目標。')
        if (not isinstance(quote, str) or not 1 <= len(quote) <= 180 or quote.strip() != quote
                or any(ch in quote for ch in '\0\r\n')):
            raise ValueError('運算元證據須提供一段最多 180 字的逐字短句。')
        _numeric_match(quote, leaves[path])


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
        low, high = _numeric_match(quote, leaves[path])
        # Match within the complete source too, so a clipped quote cannot turn 17 into 7.
        if not any(token.span() == (start + low, start + high)
                   for token in _LITERAL.finditer(text.translate(_NUMERIC_CHARS))):
            raise ValueError(f'運算元 {label} 的短句截斷了來源中的數值。')
        bound.append({**row, 'source_sha256': sha, 'quote_start': start, 'quote_end': start + len(quote),
                      'number_start': start + low, 'number_end': start + high, 'number': quote[low:high]})
    return bound
