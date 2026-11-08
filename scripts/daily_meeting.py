"""Read-only earliest common slot in explicitly listed availability."""
from datetime import date, timedelta, timezone
import hashlib
import json
import re

from assistant import document_dates
from assistant.local_answer_receipts import MAX_JSON_BYTES, load_context
from scripts import daily_meeting_zones as meeting_zones

CLOCK = r'(?:(?:[01]?[0-9]|2[0-3]):[0-5][0-9]|24:00)'
SPAN = re.compile(rf'({CLOCK})\s*[-–—至到]\s*({CLOCK})')
# Explicit aliases keep dates, qualifications and partial instructions out of the ledger.
NAME = r'(?:「[\u4e00-\u9fffA-Za-z0-9]{1,16}」|小[\u4e00-\u9fff]|[甲乙丙丁戊己庚辛]|[A-Za-z]{1,20})'
AVAILABILITY = re.compile(rf'({NAME})(原本)?(可開會的時段為|可開會[：:]|可用時段[為：:]|有空[：:]|為)\s*(.+)')
NATURAL_NAME = rf'(?:{NAME}|[\u4e00-\u9fff]{{2,3}}?)'
NATURAL_AVAILABILITY = re.compile(
    rf'({NATURAL_NAME})(?:今天)?(?:原本)?(?:可碰面|可談)(?:的(?:時段|時間)原?(?:是|為))?\s*([0-9].*)')
UNBOUND_NAME = re.compile(r'^(?:大家|我們|你們|他們|她們|對方|其他|某人|所有|其中|兩人|三人)$'
                          r'|^(?:若|如|假設|假如|如果|聽說|據說|或者)'
                          r'|今天|昨天|明天|後天|本週|下週|週末|上午|下午|早上|晚上|可能|大概|[和與或跟及]'
                          r'|[不未也都只暫尚]$')
MORNING = r'(?:整個上午|(?:上午|早上)的?(?:時段|安排))'
CANCEL = re.compile(rf'({NATURAL_NAME})(?:(?:剛|已)?取消\s*(.+)|的({MORNING})(?:已經|已|剛)取消)')
PEOPLE = r'(兩|二|三|四|五|六|七|八|[2-8])人'
LENGTH = r'(半小時|一小時|一刻鐘|[1-9][0-9]{0,3}\s*分鐘)'
DURATION = re.compile(rf'(?:{PEOPLE})?(?:需要(?:連續)?|想開)\s*{LENGTH}(?:的會議?)?')
NATURAL_DURATION = re.compile(rf'(?:{PEOPLE}(?:想|要)(?:談|碰面)|(?:碰面|談話)需要(?:連續)?)\s*{LENGTH}')
LENGTHS = {'半小時': 30, '一小時': 60, '一刻鐘': 15}
FREE = re.compile(rf'({NAME})\s*([0-9].*)')
OUTPUT = re.compile(r'(?:請)?只(?:輸出|回答|回覆)最早可行的?(?:開始及結束時間|時段|時間區間)[，,]\s*格式\s*HH:MM-HH:MM')
ASK = re.compile(r'[，,。；;\n]\s*(?:請問)?最早(?:什麼時候|幾點)(?:能|可以)開(?:會)?[？?。]?$')
COUNT = dict(zip('兩二三四五六七八', (2, 2, 3, 4, 5, 6, 7, 8)))
DATED_PLAN = 'daily_availability_dates_v2'
RELATIVE_PLAN = 'daily_availability_relative_v3'
ZONED_PLAN = 'daily_availability_zoned_v4'
NAMED_ZONED_PLAN = 'daily_availability_named_zones_v5'
ZONED_PLANS = (ZONED_PLAN, NAMED_ZONED_PLAN)
DATE_PLANS = (DATED_PLAN, RELATIVE_PLAN)
DATE = r'[0-9]{4}-[0-9]{2}-[0-9]{2}'
WEEK_KIND = r'[本這下]'
WEEK_LABEL = r'(?:週|周|星期)'
WEEK_DAY = r'[一二三四五六日天]'
WEEKDAY = re.compile(rf'({WEEK_KIND}){WEEK_LABEL}({WEEK_DAY})')
RELATIVE_DAY = rf'(?:今天|明天|後天|{WEEK_KIND}{WEEK_LABEL}{WEEK_DAY})'
DAY_LINE = re.compile(rf'({DATE}|{RELATIVE_DAY})[：:]\s*(.+)')
ANCHOR_LINE = re.compile(rf'以\s*({DATE})\s*為今天')
WEEKDAYS = dict(zip('一二三四五六日天', (0, 1, 2, 3, 4, 5, 6, 6)))
DATED_OUTPUT = re.compile(r'(?:請)?只(?:輸出|回答|回覆)最早可行的?(?:日期[及和與])?時段[，,]\s*'
                          r'格式\s*YYYY-MM-DD HH:MM-HH:MM')
DATED_ASK = re.compile(r'(?:請問)?最早哪一天(?:的哪個時段)?(?:能|可以)(?:開會|碰面)[？?]?')
NO_AVAILABILITY = r'(?:無空檔|沒有空檔)'
UNAVAILABLE = re.compile(rf'({NATURAL_NAME})(?:當天)?{NO_AVAILABILITY}')
UTC_OFFSET = r'UTC[+-](?:0[0-9]|1[0-4]):[0-5][0-9]'
ZONE = rf'(?:{UTC_OFFSET}|{meeting_zones.IANA_ZONE})'
ZONED_HEADER = re.compile(r'\A跨時區會議[：:]')
ZONED_PERSON = re.compile(rf'({NATURAL_NAME})\s+({ZONE})[：:]\s*(.+)')
ZONED_DAY = re.compile(rf'({DATE})\s+(.+)')
ZONED_OUTPUT = re.compile(r'(?:請)?只(?:輸出|回答|回覆)最早可行的?(?:日期[及和與])?時段[，,]\s*'
                          r'格式\s*YYYY-MM-DD HH:MM-HH:MM UTC±HH:MM')
NAMED_ZONED_OUTPUT = re.compile(r'(?:請)?只(?:輸出|回答|回覆)最早可行的?(?:日期[及和與])?時段[，,]\s*'
                                r'格式\s*YYYY-MM-DD HH:MM UTC±HH:MM 至 HH:MM UTC±HH:MM')
ZONED_REVISION = re.compile(rf'({NATURAL_NAME})\s+({DATE})\s+(取消|的?空檔改為)\s*(.+)')


def minutes(clock):
    hour, minute = clock.split(':')
    return int(hour) * 60 + int(minute)


def span(text):
    match = SPAN.fullmatch(text)
    if match and minutes(match[1]) < minutes(match[2]):
        return [minutes(match[1]), minutes(match[2])]
    return None


def slot_list(text, *, allow_alternatives=False):
    alternatives = allow_alternatives and '或' in text
    slots = [span(s.strip()) for s in re.split('或' if alternatives else r'[和與、]', text)]
    if len(slots) > 12 or not all(slots):
        return None
    # An adjacent or overlapping pair of alternatives does not establish a
    # longer continuous window. Leave the interpretation to ordinary chat.
    if alternatives:
        ordered = sorted(slots)
        if any(left[1] >= right[0] for left, right in zip(ordered, ordered[1:])):
            return None
    return slots


def cancelled_interval(removal):
    text = removal[2] or removal[3]
    return [0, 720] if re.fullmatch(MORNING, text) else span(text)


def parse(question, *, observation=None, use_local_clock=False):
    if ZONED_HEADER.match(question):
        return parse_zoned(question)
    return parse_same_day(question) or parse_dated(question, observation=observation, use_local_clock=use_local_clock)


def parse_same_day(question):
    if len(question) > 2048:
        return None
    conversational = ASK.search(question)
    if conversational:
        lines = re.split(r'[。\n]+', question[:conversational.start()])
    else:
        lines = re.split(r'[。\n]+', question.removesuffix('。'))
        if not lines or not OUTPUT.fullmatch(lines.pop().strip()):
            return None
    clauses = [part.strip() for line in lines for part in re.split(r'[，,；;]', line)]
    if not clauses:
        return None
    duration = parse_duration(clauses.pop())
    availability = parse_availability(clauses)
    if not duration or not availability:
        return None
    length, participants = duration
    if participants is not None and participants != len(availability['available']):
        return None
    plan = dict(schema='daily_availability_v1', **availability, duration=length)
    if conversational:
        plan['output'] = 'conversational'
    return plan


def parse_duration(text):
    duration = DURATION.fullmatch(text) or NATURAL_DURATION.fullmatch(text)
    if duration:
        length_text = duration[2]
        length = LENGTHS[length_text] if length_text in LENGTHS else int(length_text[:-2].strip())
        if length <= 1440:
            count = int(COUNT.get(duration[1], duration[1])) if duration[1] else None
            return length, count
    return None


def parse_availability(clauses, *, dated=False):
    available, cancelled = {}, []
    for clause in clauses:
        if dated and '今天' in clause:
            return None
        if not dated and not available:
            clause = clause.removeprefix('今天')
        removal = CANCEL.fullmatch(clause)
        if removal:
            cut = cancelled_interval(removal)
            name = removal[1].strip('「」')
            if name not in available or not cut or len(cancelled) >= 24:
                return None
            cancelled.append(dict(name=name, interval=cut, source=clause))
            continue
        if dated and (unavailable := UNAVAILABLE.fullmatch(clause)):
            alias = unavailable[1]
            name = alias.strip('「」')
            if ((not alias.startswith('「') and UNBOUND_NAME.search(alias))
                    or len(available) >= 8 or name in available):
                return None
            available[name] = []
            continue
        entry = AVAILABILITY.fullmatch(clause)
        natural = NATURAL_AVAILABILITY.fullmatch(clause)
        if entry and (entry[3] != '為' or available):
            alias, intervals = entry[1], entry[4]
        elif clause.endswith('有空') and (match := FREE.fullmatch(clause[:-2].rstrip())):
            alias, intervals = match.groups()
        elif natural:
            alias, intervals = natural.groups()
            if not alias.startswith('「') and UNBOUND_NAME.search(alias):
                return None
        else:
            return None
        name = alias.strip('「」')
        if len(available) >= 8 or name in available:
            return None
        slots = slot_list(intervals, allow_alternatives=natural is not None)
        if not slots:
            return None
        available[name] = slots
    return dict(available=available, cancelled=cancelled) if len(available) >= 2 else None


def date_term(term, anchor):
    if term in ('今天', '明天', '後天'):
        return anchor + timedelta(days=document_dates.DAY_OFFSETS[term])
    if week := WEEKDAY.fullmatch(term):
        shift = WEEKDAYS[week[2]] - anchor.weekday() + (7 if week[1] == '下' else 0)
        return anchor + timedelta(days=shift)
    return date.fromisoformat(term)


def recorded_observation(plan):
    anchor = plan.get('date_anchor') if isinstance(plan, dict) else None
    return anchor.get('observation') if isinstance(anchor, dict) else None


def parse_dated(question, *, observation=None, use_local_clock=False):
    if len(question) > 8192:
        return None
    text = re.sub(r'\A會議排程[：:]\s*', '', question.strip())
    lines = [part.strip() for part in re.split(r'[。\r\n]+', text) if part.strip()]
    explicit = ANCHOR_LINE.fullmatch(lines[0]) if lines else None
    if explicit:
        lines.pop(0)
    if not 3 <= len(lines) <= 9:
        return None
    output = lines.pop()
    conversational = bool(DATED_ASK.fullmatch(output))
    duration = parse_duration(lines.pop())
    if not duration or not (conversational or DATED_OUTPUT.fullmatch(output)):
        return None
    length, participants = duration
    entered = {}
    names = None
    for line in lines:
        entry = DAY_LINE.fullmatch(line)
        if not entry or entry[1] in entered:
            return None
        clauses = [part.strip() for part in re.split(r'[，,；;]', entry[2])]
        availability = parse_availability(clauses, dated=True)
        if not availability:
            return None
        present = set(availability['available'])
        if (names is not None and present != names) or (participants is not None and len(present) != participants):
            return None
        names = present
        entered[entry[1]] = availability
    anchor = None
    reference = None
    if explicit:
        try:
            anchor = date.fromisoformat(explicit[1])
        except ValueError:
            return None
        reference = dict(source='user', date=anchor.isoformat())
    elif any(not re.fullmatch(DATE, term) for term in entered):
        if observation is None:
            if not use_local_clock:
                return None
            observation = document_dates.observe_local_clock()
        anchor = document_dates._observed_date(observation)
        reference = dict(source='computer_local', date=anchor.isoformat(), observation=observation)
    days, terms = {}, {}
    for term, availability in entered.items():
        try:
            day = date_term(term, anchor).isoformat()
        except (ValueError, OverflowError):
            return None
        if day in days:
            return None
        days[day], terms[day] = availability, term
    plan = dict(schema=DATED_PLAN, days=days, duration=length, output='conversational' if conversational else 'exact')
    if reference is not None:
        plan.update(schema=RELATIVE_PLAN, date_anchor=reference, date_terms=terms)
    return plan


def offset_minutes(text):
    value = minutes(text[4:])
    if value > 840 or text == 'UTC-00:00':
        raise ValueError('偏移量須介於 UTC-14:00 與 UTC+14:00，零偏移請使用 UTC+00:00。')
    return value if text[3] == '+' else -value


def offset_text(value):
    hours, remainder = divmod(abs(value), 60)
    return f'UTC{"-" if value < 0 else "+"}{hours:02d}:{remainder:02d}'


def zoned_slots(text):
    if re.fullmatch(NO_AVAILABILITY, text):
        return []
    return slot_list(text, allow_alternatives=True)


def parse_zone(text, named):
    if re.fullmatch(UTC_OFFSET, text):
        offset = offset_minutes(text)
        return offset_text(offset) if named else offset
    if named and len(text) <= 96 and re.fullmatch(meeting_zones.IANA_ZONE, text):
        return text
    raise ValueError('請使用固定 UTC 偏移或完整的 IANA 時區名稱。')


def parse_zoned(question):
    if len(question) > 8192:
        return None
    body = ZONED_HEADER.sub('', question, count=1)
    lines = [part.strip() for part in re.split(r'[。\r\n]+', body) if part.strip()]
    if not 6 <= len(lines) <= 12:
        return None
    named = bool(NAMED_ZONED_OUTPUT.fullmatch(lines[-1]))
    if not named and not ZONED_OUTPUT.fullmatch(lines[-1]):
        return None
    reference = re.fullmatch(rf'基準時區\s*({ZONE})', lines[0])
    candidate_line = re.fullmatch(r'候選日期[：:]\s*(.+)', lines[1])
    duration = parse_duration(lines[-2])
    if not reference or not candidate_line or not duration:
        return None
    length, participants = duration
    dates = [item.strip() for item in re.split(r'[、，,]', candidate_line[1])]
    if not 1 <= len(dates) <= 7 or len(set(dates)) != len(dates):
        return None
    if participants is not None and participants != len(lines[2:-2]):
        return None
    people = {}
    try:
        reference_zone = parse_zone(reference[1], named)
        for day in dates:
            if not re.fullmatch(DATE, day):
                return None
            date.fromisoformat(day)
        for line in lines[2:-2]:
            entry = ZONED_PERSON.fullmatch(line)
            if not entry:
                return None
            alias = entry[1]
            name = alias.strip('「」')
            if name in people or (not alias.startswith('「') and UNBOUND_NAME.search(alias)):
                return None
            local_days = {}
            slots = re.split(r'[，,；;]', entry[3])
            if not 1 <= len(slots) <= 14:
                return None
            for slot in slots:
                local = ZONED_DAY.fullmatch(slot.strip())
                if not local or local[1] in local_days:
                    return None
                date.fromisoformat(local[1])
                available = zoned_slots(local[2])
                if available is None:
                    return None
                local_days[local[1]] = dict(available=available, cancelled=[])
            zone = parse_zone(entry[2], named)
            people[name] = {('zone' if named else 'offset'): zone, 'days': local_days}
    except ValueError:
        return None
    plan = dict(schema=NAMED_ZONED_PLAN if named else ZONED_PLAN, dates=dates, people=people, duration=length)
    plan['reference_zone' if named else 'reference_offset'] = reference_zone
    return plan


def zone_keys(plan):
    return {plan['reference_zone'], *(person['zone'] for person in plan['people'].values())}


def named_keys(plan):
    return {key for key in zone_keys(plan) if re.fullmatch(meeting_zones.IANA_ZONE, key)}


def bind_named_zones(plan, previous, *, fresh=False):
    if plan['schema'] != NAMED_ZONED_PLAN:
        return plan
    keys = named_keys(plan)
    if fresh:
        saved = meeting_zones.snapshot(keys)
    elif isinstance(previous, dict) and previous.get('schema') == NAMED_ZONED_PLAN and 'zone_rules' in previous:
        saved = previous['zone_rules']
    else:
        raise ValueError('會議缺少原先保存的時區規則。')
    meeting_zones.load_rules(saved, keys)
    return {**plan, 'zone_rules': saved}


FOLLOWUP = re.compile(r'上一場會議[，,]\s*(.{1,512})[，,]\s*(?:請)?(?:重算|重新計算)(?:最早時段)?[。！]?')


def revise_zoned(plan, clause):
    edit = ZONED_REVISION.fullmatch(clause)
    if not edit:
        raise ValueError('請先列出參與者與完整的當地日期，再取消或更改當日空檔。')
    person = plan['people'].get(edit[1].strip('「」'))
    day = person['days'].get(edit[2]) if person else None
    if day is None:
        raise ValueError('請使用原先列出的參與者及該人的當地日期。')
    if edit[3] != '取消':
        slots = zoned_slots(edit[4])
        if slots is None:
            raise ValueError('請列出當日完整空檔，或寫「無空檔」。')
        day.update(available=slots, cancelled=[])
        return
    cut = [0, 720] if re.fullmatch(MORNING, edit[4]) else span(edit[4])
    if not cut or len(day['cancelled']) >= 24:
        raise ValueError('請提供有效的當地取消時段，每日最多 24 筆。')
    day['cancelled'].append(dict(interval=cut, source=clause))


def revise(plan, clause):
    """Change the detached prior snapshot, retaining unaffected attendees and constraints."""
    duration = re.fullmatch(r'(?:會議)?時長改為\s*([1-9][0-9]{0,3})\s*分鐘', clause)
    if duration:
        length = int(duration[1])
        if length > 1440:
            raise ValueError('請指定同一天內的會議時長（1 至 1440 分鐘）。')
        plan['duration'] = length
        return
    if plan.get('schema') in ZONED_PLANS:
        revise_zoned(plan, clause)
        return
    dated = plan.get('schema') in DATE_PLANS
    availability = plan
    if dated:
        day = re.fullmatch(rf'({DATE})(?:\s+|[：:]\s*)(.+)', clause)
        if not day or day[1] not in plan['days']:
            raise ValueError('請先指定原先列出的完整日期，再更正當天的空檔。')
        availability, clause = plan['days'][day[1]], day[2]
    replacement = re.fullmatch(rf'({NATURAL_NAME})的(?:空檔|可用時段)改為\s*(.+)', clause)
    removal = CANCEL.fullmatch(clause)
    if not (replacement or removal):
        raise ValueError('請指明要更正哪位參與者（' + '、'.join(availability['available'])
                         + '）及其同日空檔；若要換日期，請重新列出當天每人的空檔。')
    name = (replacement or removal)[1].strip('「」')
    if name not in availability['available']:
        raise ValueError('上一場會議的參與者是' + '、'.join(availability['available']) + '。請指定其中一人，或重新提供完整明細。')
    if replacement:
        slots = ([] if dated and re.fullmatch(NO_AVAILABILITY, replacement[2])
                 else slot_list(replacement[2], allow_alternatives=dated))
        if slots is None:
            raise ValueError('請列出這位參與者當天完整的新空檔，例如 13:00-14:30。')
        availability['available'][name] = slots
        availability['cancelled'] = [c for c in availability['cancelled'] if c['name'] != name]
    else:
        cut = cancelled_interval(removal)
        if not cut or len(availability['cancelled']) >= 24:
            raise ValueError('請提供有效的同日取消時段；累計超過 24 筆時，請重新列出空檔。')
        availability['cancelled'].append(dict(name=name, interval=cut, source=clause))


def ranges(mask):
    """Convert minute cells to half-open runs, including a run ending at midnight."""
    start = None
    for i, free in enumerate([*mask, False]):
        if free and start is None:
            start = i
        elif not free and start is not None:
            yield start, i
            start = None


def display(interval):
    return '-'.join(f'{t // 60:02d}:{t % 60:02d}' for t in interval)


def resolve(plan):
    if plan.get('schema') == NAMED_ZONED_PLAN:
        return resolve_named_zoned(plan)
    if plan.get('schema') == ZONED_PLAN:
        return resolve_zoned(plan)
    if plan.get('schema') in DATE_PLANS:
        return resolve_dated(plan)
    return resolve_same_day(plan)


def shared_windows(plan, width):
    common = bytearray([1]) * width
    effective = {}
    for name, intervals in plan['available'].items():
        mask = bytearray(width)
        for a, b in intervals:
            mask[a:b] = bytes([1]) * (b-a)
        for cut in plan['cancelled']:
            if cut['name'] == name:
                a, b = cut['interval']
                mask[a:b] = bytes(b-a)
        effective[name] = list(ranges(mask))
        common = bytearray(x & y for x, y in zip(common, mask))
    return effective, list(ranges(common))


def resolve_same_day(plan):
    # Each cell represents one minute in a fixed-offset day.
    effective, windows = shared_windows(plan, 1440)
    length = plan['duration']
    first = next(((a, a + length) for a, b in windows if b - a >= length), None)
    answer = display(first) if first else '沒有可行時段'
    receipt = dict(schema='daily_meeting_calculation_v1', status='complete', model_called=False,
                   effective={name: list(map(display, slots)) for name, slots in effective.items()},
                   common=list(map(display, windows)), duration=length,
                   expression=f'共同空檔中最早連續 {length} 分鐘', result=dict(exact=answer, calculations=[]))
    return visible_answer(plan, receipt), receipt


def resolve_dated(plan):
    length = plan['duration']
    days = []
    first = None
    for day, availability in sorted(plan['days'].items()):
        _, calculation = resolve_same_day(dict(**availability, duration=length))
        earliest = calculation['result']['exact']
        slot = None if earliest == '沒有可行時段' else earliest
        days.append(dict(date=day, effective=calculation['effective'], common=calculation['common'], first=slot))
        if first is None and slot is not None:
            first = f'{day} {slot}'
    exact = first or '沒有可行時段'
    answer = exact
    if plan['output'] == 'conversational':
        answer = (f'最早可以排在 {first}，共 {length} 分鐘。' if first
                  else f'列出的日期沒有可行時段。每一天都沒有連續 {length} 分鐘的共同空檔。')
    schema = ('daily_meeting_relative_calculation_v3' if plan['schema'] == RELATIVE_PLAN
              else 'daily_meeting_dates_calculation_v2')
    receipt = dict(schema=schema, status='complete', model_called=False,
                   days=days, duration=length, expression=f'依日期查找最早共同連續 {length} 分鐘',
                   result=dict(exact=exact, calculations=[]))
    return answer, receipt


def shifted_intersection(interval, shift, width=1440):
    start, end = interval
    start, end = max(0, start + shift), min(width, end + shift)
    return [start, end] if start < end else None


def project_zoned_day(plan, reference_day):
    base = date.fromisoformat(reference_day).toordinal()
    available, cancelled = {}, []
    for name, person in plan['people'].items():
        available[name] = []
        for local_day, windows in person['days'].items():
            difference = date.fromisoformat(local_day).toordinal() - base
            shift = difference * 1440 + plan['reference_offset'] - person['offset']
            for interval in windows['available']:
                if overlap := shifted_intersection(interval, shift):
                    available[name].append(overlap)
            for cut in windows['cancelled']:
                if overlap := shifted_intersection(cut['interval'], shift):
                    cancelled.append(dict(name=name, interval=overlap))
    return dict(available=available, cancelled=cancelled)


def resolve_zoned(plan):
    days = {day: project_zoned_day(plan, day) for day in plan['dates']}
    answer, receipt = resolve_dated(dict(schema=DATED_PLAN, days=days, output='exact', duration=plan['duration']))
    if answer != '沒有可行時段':
        answer = f'{answer} {offset_text(plan["reference_offset"])}'
    receipt.update(schema='daily_meeting_zoned_calculation_v4', reference_offset=plan['reference_offset'])
    receipt['result']['exact'] = answer
    return answer, receipt


def utc_availability(plan, zones):
    available, cancelled = {}, []
    for name, person in plan['people'].items():
        available[name] = []
        zone = zones[person['zone']]
        label = f'{name}（{person["zone"]}）'
        for day, windows in person['days'].items():
            available[name].extend(meeting_zones.utc_intervals(day, windows['available'], zone, label))
            cuts = [cut['interval'] for cut in windows['cancelled']]
            cancelled.extend(dict(name=name, interval=interval)
                             for interval in meeting_zones.utc_intervals(day, cuts, zone, label))
    return available, cancelled


def named_day(day, zone, label, available, cancelled, length):
    start, end = next(meeting_zones.utc_intervals(day, [(0, 1440)], zone, label))
    width = end - start
    if not 1 <= width <= 2880:
        raise ValueError('基準日期超出可比較的日界。')
    projected = {name: [overlap for interval in slots if (overlap := shifted_intersection(interval, -start, width))]
                 for name, slots in available.items()}
    removed = [dict(name=cut['name'], interval=overlap) for cut in cancelled
               if (overlap := shifted_intersection(cut['interval'], -start, width))]
    effective, common = shared_windows(dict(available=projected, cancelled=removed), width)
    first = next(((left, left + length) for left, right in common if right - left >= length), None)
    civil_day = date.fromisoformat(day)

    def show(interval):
        return ' 至 '.join(meeting_zones.local_clock(start + minute, zone, civil_day) for minute in interval)

    return dict(date=day, effective={name: list(map(show, slots)) for name, slots in effective.items()},
                common=list(map(show, common)), first=show(first) if first else None)


def resolve_named_zoned(plan):
    zones = meeting_zones.load_rules(plan['zone_rules'], named_keys(plan))
    for key in zone_keys(plan) - zones.keys():
        zones[key] = timezone(timedelta(minutes=offset_minutes(key)))
    reference = plan['reference_zone']
    length = plan['duration']
    available, cancelled = utc_availability(plan, zones)
    days = [named_day(day, zones[reference], reference, available, cancelled, length) for day in sorted(plan['dates'])]
    answer = next((f'{day["date"]} {day["first"]}' for day in days if day['first']), '沒有可行時段')
    receipt = dict(schema='daily_meeting_named_zone_calculation_v5', status='complete', model_called=False,
                   days=days, duration=length, reference_zone=reference,
                   expression=f'依基準時區查找最早共同連續 {length} 分鐘',
                   result=dict(exact=answer, calculations=[]))
    return answer, receipt


def visible_answer(plan, receipt):
    exact = receipt['result']['exact']
    if plan.get('output') != 'conversational':
        return exact
    length = receipt['duration']
    if exact != '沒有可行時段':
        return f'最早可以排在 {exact}，共 {length} 分鐘。'
    if not receipt['common']:
        return '沒有可行時段，當天沒有共同空檔。'
    longest = max(b-a for a, b in map(span, receipt['common']))
    return f'沒有可行時段。最長的共同空檔只有 {longest} 分鐘，會議需要 {length} 分鐘。'


def describe(context):
    plan, receipt = context['meeting_plan'], context['calculation']
    lines = [context['current_request']]
    zoned = plan['schema'] in ZONED_PLANS
    dated = zoned or plan['schema'] in DATE_PLANS
    if source := context.get('meeting_source'):
        scope = '日期與時段' if dated else '同日時段與條件'
        lines.append(f'沿用本對話會議回答 #{source["answer_id"]} 的{scope}')
    if anchor := plan.get('date_anchor'):
        if anchor['source'] == 'user':
            lines.append(f'依原問指定的日期 {anchor["date"]} 為今天。')
        else:
            observed = anchor['observation']['local_datetime']
            lines.append(f'相對日期以原本的電腦本地日期 {anchor["date"]} 為基準。')
            lines.append(f'原本的本機時鐘觀察 {observed}')
    if zoned:
        named = plan['schema'] == NAMED_ZONED_PLAN
        reference = plan['reference_zone'] if named else offset_text(plan['reference_offset'])
        lines += ['', f'基準時區 {reference}']
        if named:
            lines.append(f'沿用保存的 tzdata {plan["zone_rules"]["version"]} 規則。')
        lines.append('原始當地空檔與更正')
        for name, person in plan['people'].items():
            zone = person['zone'] if named else offset_text(person['offset'])
            lines.append(f'{name} {zone}')
            for local_day, windows in sorted(person['days'].items()):
                slots = '、'.join(map(display, windows['available'])) or '無'
                lines.append(f'  {local_day} {slots}')
                lines.extend(f'  {cut["source"]}' for cut in windows['cancelled'])
    days = receipt['days'] if dated else [receipt]
    for day in days:
        availability = plan
        lines.append('')
        if dated:
            term = plan.get('date_terms', {}).get(day['date'], day['date'])
            lines.append(day['date'] if term == day['date'] else f'{day["date"]}（原文「{term}」）')
            if not zoned:
                availability = plan['days'][day['date']]
        if zoned:
            lines.append('換算後每人剩餘時段')
        else:
            lines.append('採用的取消條件')
            lines += [c['source'] for c in availability['cancelled']] or ['無']
            lines += ['', '每人剩餘時段']
        for name, slots in day['effective'].items():
            lines.append(name + '：' + ('、'.join(slots) or '無'))
        lines.append('共同空檔：' + ('、'.join(day['common']) or '無'))
        if dated:
            lines.append('當日最早：' + (day['first'] or '沒有可行時段'))
    lines += ['', f"所需時間：{receipt['duration']} 分鐘", '結果：' + receipt['result']['exact']]
    return '\n'.join(lines)


def previous_id(db, sid, before):
    row = db.execute('SELECT id FROM messages WHERE session_id=? AND (? IS NULL OR id<?) '
                     'ORDER BY id DESC LIMIT 1', (sid, before, before)).fetchone()
    return row['id'] if row else None


def same_json(left, right):
    return (json.dumps(left, sort_keys=True, allow_nan=False)
            == json.dumps(right, sort_keys=True, allow_nan=False))


def saved_meeting(db, mid):
    rows = db.execute('''SELECT a.session_id,a.role,a.content,u.id AS uid,u.role AS user_role,
        u.session_id AS user_session,u.content AS question,r.session_id AS run_session,r.status,
        CASE WHEN length(CAST(c.payload AS BLOB))<=? THEN c.payload END AS payload,
        CASE WHEN length(CAST(r.context_json AS BLOB))<=? THEN r.context_json END AS initial,
        CASE WHEN length(CAST(k.payload AS BLOB))<=? THEN k.payload END AS checks
        FROM messages a JOIN answer_runs r ON r.answer_message_id=a.id
        JOIN messages u ON u.id=r.user_message_id JOIN answer_contexts c ON c.message_id=a.id
        LEFT JOIN answer_checks k ON k.message_id=a.id WHERE a.id=? LIMIT 2''',
        (MAX_JSON_BYTES, MAX_JSON_BYTES, MAX_JSON_BYTES, mid)).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError('會議缺少唯一的回答流程。')
    row = rows[0]
    saved, initial = load_context(row['payload']), load_context(row['initial'])
    if not isinstance(saved, dict) or not isinstance(initial, dict):
        raise ValueError('會議的回答紀錄格式有誤。')
    if 'meeting_plan' not in saved and 'meeting_plan' not in initial:
        return None
    if (row['role'] != 'assistant' or row['user_role'] != 'user' or row['status'] != 'succeeded'
            or not row['session_id'] == row['user_session'] == row['run_session']
            or not 0 < row['uid'] < mid
            or not row['question'] == saved.get('current_request') == initial.get('current_request')
            or saved.get('response_mode') != 'local_calculation'
            or initial.get('response_mode') != 'local_calculation'
            or not same_json(saved.get('meeting_source'), initial.get('meeting_source'))
            or not same_json(saved.get('meeting_plan'), initial.get('meeting_plan'))
            or saved.get('image_inputs') or saved.get('engine_selection') is not None
            or saved.get('documents') != {'catalog': [], 'excerpts': []}
            or load_context(row['checks']) != []):
        raise ValueError('會議來源與已完成的回答流程不符。')
    ref = dict(answer_id=mid, sha256=hashlib.sha256(row['payload'].encode('utf-8')).hexdigest())
    return row, saved, ref


def verified_meeting(db, mid):
    tip = saved_meeting(db, mid)
    if tip is None:
        return None
    history = [tip]
    while 'meeting_source' in history[-1][1]:
        row, saved, _ = history[-1]
        parent = saved_meeting(db, previous_id(db, row['session_id'], row['uid']))
        if parent is None or saved['meeting_source'] != parent[2]:
            raise ValueError('會議的上一筆來源已變更。')
        history.append(parent)
    original = history.pop()
    plan = parse(original[0]['question'], observation=recorded_observation(original[1].get('meeting_plan')))
    if plan is None:
        raise ValueError('會議紀錄缺少可核對的原始時段。')
    plan = bind_named_zones(plan, original[1].get('meeting_plan'))

    def check(record, effective):
        row, saved, _ = record
        answer, calculation = resolve(effective)
        if (not same_json(saved.get('meeting_plan'), effective)
                or not same_json(saved.get('calculation'), calculation)):
            raise ValueError('會議紀錄與重算結果不符。')
        if answer != row['content']:
            raise ValueError('會議回答與重算結果不符。')

    check(original, plan)
    for record in reversed(history):
        revision = FOLLOWUP.fullmatch(record[0]['question'])
        if not revision:
            raise ValueError('會議更正與原問不符。')
        revise(plan, revision[1])
        check(record, plan)
    return tip


def read(store, mid):
    with store.connect() as db:
        db.execute('BEGIN')
        verified = verified_meeting(db, mid)
        return verified[1] if verified else None


def previous(store, sid, before=None, *, required=True):
    message = '上一則回答沒有可接續的同日會議紀錄，請提供每人的空檔與會議長度。'
    try:
        with store.connect() as db:
            db.execute('BEGIN')
            verified = verified_meeting(db, previous_id(db, sid, before))
            if verified:
                return verified[1]['meeting_plan'], verified[2]
    except ValueError as exc:
        raise ValueError(message) from exc
    if required:
        raise ValueError(message)
