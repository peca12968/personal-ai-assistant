"""Match explicit fields in saved waiting reports without crossing cells."""
from collections import defaultdict
from itertools import groupby
import re

from assistant import document_table


_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
_BULLET = re.compile(r'^(?:[-+*]|[0-9]+[.)])\s+')
_STRONG = re.compile(r'\*\*([^*]+)\*\*|__([^_]+)__')
_LABEL_VALUE = re.compile(r'(?P<label>[^：:]+)[：:]\s*(?P<value>.*)')
_SOURCE_TAIL = re.compile(r'(?:\s*(?:\[D\d+:L\d+(?:-L?\d+)?\]|\[(?:補充|更正)#\d+\]))+\s*$')
_LABEL_HEADERS = {'欄位', '項目'}
_VALUE_HEADERS = {'值', '內容', '狀態', '目前值', '目前狀態', '結果'}


def validate(fields):
    if not isinstance(fields, list) or not fields:
        raise ValueError('暫表欄位規約須列出欄名及可接受的值。')
    seen = set()
    for field in fields:
        if not isinstance(field, dict) or field.keys() != {'labels', 'value_forms'}:
            raise ValueError('暫表欄位須指定 labels 及 value_forms。')
        labels, forms = field['labels'], field['value_forms']
        if not isinstance(labels, list) or not labels or any(
                not isinstance(label, str) or not label or label.strip() != label
                or any(char in label for char in ':：|\r\n') for label in labels):
            raise ValueError('暫表欄名須是完整、非空的單行文字。')
        if len(set(labels)) != len(labels) or seen.intersection(labels):
            raise ValueError('同一暫表欄名只能對應一份規約。')
        seen.update(labels)
        if not isinstance(forms, list) or not forms or any(
                not isinstance(form, str) or not form.strip() or re.fullmatch(form, '') is not None for form in forms):
            raise ValueError('暫表欄值規約須為完整、非空的比對條件。')
        for form in forms:
            re.compile(form)


def _plain(text):
    return _STRONG.sub(lambda match: match[1] or match[2], text).strip()


def _value(text):
    return _SOURCE_TAIL.sub('', _plain(text).removesuffix('。')).strip().removesuffix('。').rstrip()


def _visible_lines(text):
    fence = ''
    for raw in text.splitlines():
        if marker := _FENCE.fullmatch(raw):
            span, rest = marker.groups()
            if not fence:
                fence = span
            elif span.startswith(fence) and not rest.strip():
                fence = ''
            yield ''
        elif not fence and not raw.startswith(('    ', '\t')) and not raw.lstrip().startswith('>'):
            yield raw.strip()
        else:
            yield ''


def _fields_in_table(lines, wanted):
    wrapped = [f"|{line.removeprefix('|').removesuffix('|')}|" for line in lines]
    table = document_table._table_rows({'name': 'partial.md', 'rows': wrapped})
    if table is None:
        header = {_plain(label) for label in wrapped[0][1:-1].split('|')}
        if header & wanted or (header & _LABEL_HEADERS and header & _VALUE_HEADERS):
            raise ValueError('暫表缺少可核對的表頭對照。')
        return []
    header, rows, _ = table
    header = [_plain(label) for label in header]
    columns = [(i, label) for i, label in enumerate(header) if label in wanted]
    label_columns = [i for i, label in enumerate(header) if label in _LABEL_HEADERS]
    value_columns = [i for i, label in enumerate(header) if label in _VALUE_HEADERS]
    column_layout = bool(columns)
    row_layout = bool(label_columns and value_columns)
    if not (column_layout or row_layout):
        return []
    if not rows or not all(header) or len(set(header)) != len(header) or any(len(row) != len(header) for row in rows):
        raise ValueError('暫表欄位無法對應到完整且唯一的表格。')
    result = [(label, row[i]) for row in rows for i, label in columns]
    if row_layout:
        if len(label_columns) != 1 or len(value_columns) != 1:
            raise ValueError('暫表的欄名與欄值欄位須各有一欄。')
        label_index, value_index = label_columns[0], value_columns[0]
        result.extend((_plain(row[label_index]), row[value_index]) for row in rows)
    return result


def matches(text, fields):
    wanted = {label for rule in fields for label in rule['labels']}
    values = defaultdict(list)
    try:
        for pipe, group in groupby(_visible_lines(text), lambda line: '|' in line):
            if pipe:
                for label, value in _fields_in_table(list(group), wanted):
                    values[label].append(_value(value))
            else:
                for line in group:
                    if match := _LABEL_VALUE.fullmatch(_plain(_BULLET.sub('', line))):
                        label = match['label'].strip()
                        if label in wanted:
                            values[label].append(_value(match['value']))
    except ValueError:
        return False
    for rule in fields:
        actual = [value for label in rule['labels'] for value in values[label]]
        if not actual or not all(any(re.fullmatch(form, value) for form in rule['value_forms']) for value in actual):
            return False
    return True
