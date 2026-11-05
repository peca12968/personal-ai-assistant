"""Explicit Gregorian dates used by table filters."""
import re
import unicodedata
from operator import eq, ge, gt, le, lt, ne

from .date_tools import parse_date


_SEPARATED = re.compile(r'([0-9]{4})([-/])([0-9]{1,2})\2([0-9]{1,2})')
_CHINESE = re.compile(r'([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日')
_COMPARISONS = dict(eq=eq, ne=ne, ge=ge, gt=gt, le=le, lt=lt)


def canonical(raw):
    """Normalize the spelling while leaving calendar validation to parse_date."""
    value = unicodedata.normalize('NFKC', raw).strip()
    if match := _SEPARATED.fullmatch(value):
        year, _, month, day = match.groups()
    elif match := _CHINESE.fullmatch(value):
        year, month, day = match.groups()
    else:
        return None
    return f'{year}-{month.zfill(2)}-{day.zfill(2)}'


def condition_error(condition):
    start = condition['date']
    end = condition.get('end_date', start)
    for boundary in (start, end):
        try:
            parse_date(boundary)
        except ValueError:
            return f'{boundary} 不是有效的公曆日期。'
    if end < start:
        return f'起日 {start} 晚於迄日 {end}。'
    return None


def test(raw, condition):
    normalized = canonical(raw)
    try:
        parse_date(normalized)
    except ValueError:
        return dict(value=raw, status='unknown', reason='filter_date_invalid' if raw.strip() else 'filter_blank')
    operator = condition['operator']
    # Valid canonical YYYY-MM-DD strings sort in Gregorian date order.
    if operator == 'between':
        matches = condition['date'] <= normalized <= condition['end_date']
    else:
        matches = _COMPARISONS[operator](normalized, condition['date'])
    return dict(value=raw, status='true' if matches else 'false', normalized_date=normalized)
