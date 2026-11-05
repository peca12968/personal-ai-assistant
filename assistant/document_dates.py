"""Explicit Gregorian dates used by table filters."""
import re
import unicodedata
from calendar import monthrange
from datetime import datetime, timedelta
from operator import eq, ge, gt, le, lt, ne

from .date_tools import calculate, parse_date


_SEPARATED = re.compile(r'([0-9]{4})([-/])([0-9]{1,2})\2([0-9]{1,2})')
_CHINESE = re.compile(r'([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日')
_COMPARISONS = dict(eq=eq, ne=ne, ge=ge, gt=gt, le=le, lt=lt)
DAY_OFFSETS = {'前天': -2, '昨天': -1, '今天': 0, '明天': 1, '後天': 2}
PERIODS = {'本週': 'this_week', '這週': 'this_week', '上週': 'last_week',
           '本月': 'this_month', '這個月': 'this_month', '上月': 'last_month', '上個月': 'last_month'}
_LABELS = dict(eq='等於', ne='不等於', ge='不早於', gt='晚於', le='不晚於', lt='早於')


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


def observe_local_clock():
    observed = calculate({'operation': 'today'})
    return {key: observed[key] for key in ('local_datetime', 'observed_at_utc')}


def _observed_date(observation):
    fields = ('local_datetime', 'observed_at_utc')
    if not isinstance(observation, dict) or set(observation) != set(fields):
        raise ValueError('相對日期缺少原本的本機時鐘觀察。')
    times = []
    for key in fields:
        raw = observation[key]
        if not isinstance(raw, str) or len(raw) > 40:
            raise ValueError('相對日期的本機時鐘觀察無效。')
        value = datetime.fromisoformat(raw)
        if value.utcoffset() is None or value.isoformat() != raw:
            raise ValueError('相對日期的本機時鐘觀察無效。')
        times.append(value)
    local, utc = times
    if utc.utcoffset() != timedelta(0) or local != utc:
        raise ValueError('相對日期的本機與 UTC 觀察時間不一致。')
    return local.date()


def _period_bounds(anchor, period):
    if period in ('this_week', 'last_week'):
        start = anchor - timedelta(days=anchor.weekday())
        if period == 'last_week':
            start -= timedelta(days=7)
        return start, start + timedelta(days=6)
    first = anchor.replace(day=1)
    if period == 'last_month':
        last = first - timedelta(days=1)
        return last.replace(day=1), last
    return first, first.replace(day=monthrange(first.year, first.month)[1])


def _resolve_relative(condition, anchor):
    start = condition['date']
    if start in PERIODS:
        first, last = _period_bounds(anchor, PERIODS[start])
        return {**condition, 'date': first.isoformat(), 'end_date': last.isoformat()}
    resolved = dict(condition)
    for field in ('date', 'end_date'):
        if (term := condition.get(field)) in DAY_OFFSETS:
            resolved[field] = (anchor + timedelta(days=DAY_OFFSETS[term])).isoformat()
    return resolved


def bind_relative_plan(plan, observation):
    anchor = _observed_date(observation)
    try:
        filters = [_resolve_relative(condition, anchor) if 'relative_expression' in condition else condition
                   for condition in plan['filters']]
    except OverflowError:
        raise ValueError('相對日期換算超出可用的公曆日期範圍。') from None
    return {**plan, 'filters': filters, 'date_observation': observation}


def describe_relative_plan(plan):
    anchor = plan['date_observation']['local_datetime'][:10]
    yield f'相對日期以電腦本地日期 {anchor} 為基準。'
    for index, condition in enumerate(plan['filters'], 1):
        if 'relative_expression' not in condition:
            continue
        operator, day = condition['operator'], condition['date']
        if operator == 'between':
            span = f'從 {day} 到 {condition["end_date"]}（含起迄日）'
        else:
            span = f'{_LABELS[operator]} {day}'
        yield f'日期條件 {index} · 「{condition["column"]}」{span}。'


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
