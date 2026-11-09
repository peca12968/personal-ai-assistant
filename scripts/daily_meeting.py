"""Read-only earliest common slot in explicitly listed availability."""
from copy import deepcopy
from datetime import date, timedelta, timezone
import hashlib
from itertools import product
import json
from math import prod
import re
import unicodedata

from assistant import document_dates
from assistant.local_answer_receipts import MAX_JSON_BYTES, load_context
from scripts import daily_meeting_zones as meeting_zones

CLOCK = r'(?:(?:[01]?[0-9]|2[0-3]):[0-5][0-9]|24:00)'
SPAN = re.compile(rf'({CLOCK})\s*[-–—至到]\s*({CLOCK})')
TOPIC_TEXT = r'「([^「」\r\n]{1,64})」'
TOPIC_HEADING = re.compile(rf'\A會議主題[：:][ \t]*{TOPIC_TEXT}(?:。|\r?\n)\s*')
# Explicit aliases keep dates, qualifications and partial instructions out of the ledger.
NAME = r'(?:「[\u4e00-\u9fffA-Za-z0-9]{1,16}」|小[\u4e00-\u9fff]|[甲乙丙丁戊己庚辛]|[A-Za-z]{1,20})'
AVAILABILITY = re.compile(rf'({NAME})(原本)?(可開會的時段為|可開會[：:]|可用時段[為：:]|有空[：:]|為)\s*(.+)')
NATURAL_NAME = rf'(?:{NAME}|[\u4e00-\u9fff]{{2,3}}?)'
EDIT_NAME = rf'(?:{NATURAL_NAME}|[\u4e00-\u9fff])'
NATURAL_AVAILABILITY = re.compile(
    rf'({NATURAL_NAME})(?:今天)?(?:原本)?(?:可碰面|可談)(?:的(?:時段|時間)原?(?:是|為))?\s*([0-9].*)')
UNBOUND_NAME = re.compile(r'^(?:大家|我們|你們|他們|她們|對方|其他|某人|所有|其中|兩人|三人)$'
                          r'|^(?:若|如|假設|假如|如果|聽說|據說|或者)'
                          r'|今天|昨天|明天|後天|本週|下週|週末|上午|下午|早上|晚上|可能|大概|[和與或跟及]'
                          r'|[不未也都只暫尚]$')
MORNING = r'(?:整個上午|(?:上午|早上)的?(?:時段|安排))'
CANCEL_BODY = rf'(?:(?:剛|已)?取消\s*(.+)|的({MORNING})(?:已經|已|剛)取消)'
CANCEL = re.compile(rf'({NATURAL_NAME}){CANCEL_BODY}')
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
FOLD_ZONED_PLAN = 'daily_availability_fold_zones_v6'
OVERNIGHT_ZONED_PLAN = 'daily_availability_overnight_zones_v7'
NAMED_ZONED_PLANS = (NAMED_ZONED_PLAN, FOLD_ZONED_PLAN, OVERNIGHT_ZONED_PLAN)
ZONED_PLANS = (ZONED_PLAN, *NAMED_ZONED_PLANS)
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
OVERNIGHT_OUTPUT = re.compile(r'(?:請)?只(?:輸出|回答|回覆)最早可行的?時段[，,]\s*'
                              r'格式\s*YYYY-MM-DD HH:MM UTC±HH:MM 至 YYYY-MM-DD HH:MM UTC±HH:MM')
REPLACE_TIME = r'的?(?:空檔|可用時段)改(?:為|成)'
UNAVAILABLE_END = r'(?:沒空|沒有空|不能開會)'
ZONED_REVISION = re.compile(rf'({EDIT_NAME})\s+({DATE})\s+(取消|{REPLACE_TIME})\s*(.+)')
ZONED_UNAVAILABLE = re.compile(rf'({EDIT_NAME})\s+({DATE})\s+([0-9].*?)\s*{UNAVAILABLE_END}')
OCCURRENCE = r'(?:（(第[一二]次)）|\((第[一二]次)\))'
OCCURRENCE_SPAN = re.compile(rf'({CLOCK})\s*{OCCURRENCE}?\s*[-–—至到]\s*({CLOCK})\s*{OCCURRENCE}?')
OVERNIGHT_SPAN = re.compile(rf'(?:({DATE})\s+)?({CLOCK})\s*{OCCURRENCE}?\s*[-–—至到]\s*'
                            rf'({DATE})\s+({CLOCK})\s*{OCCURRENCE}?')
OCCURRENCES = {None: None, '第一次': 1, '第二次': 2}


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


def valid_topic(value):
    return (isinstance(value, str) and 1 <= len(value) <= 64 and value == value.strip()
            and not any(ch in '「」\u2028\u2029' or unicodedata.category(ch).startswith('C') for ch in value))


def parse(question, *, observation=None, use_local_clock=False):
    topic = None
    if heading := TOPIC_HEADING.match(question):
        topic = heading[1]
        if not valid_topic(topic):
            return None
        question = question[heading.end():]
    if ZONED_HEADER.match(question):
        plan = parse_zoned(question)
    else:
        plan = parse_same_day(question) or parse_dated(question, observation=observation, use_local_clock=use_local_clock)
    if plan is not None and topic is not None:
        plan['topic'] = topic
    return plan


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


def zoned_span(text, *, named=False):
    if named and (match := OCCURRENCE_SPAN.fullmatch(text)):
        start_choice = OCCURRENCES[match[2] or match[3]]
        end_choice = OCCURRENCES[match[5] or match[6]]
        if start_choice is not None or end_choice is not None:
            return dict(start=minutes(match[1]), end=minutes(match[4]),
                        start_choice=start_choice, end_choice=end_choice)
    return span(text)


def zoned_slots(text, *, named=False):
    if re.fullmatch(NO_AVAILABILITY, text):
        return []
    if named and re.search(OCCURRENCE, text):
        slots = [zoned_span(part.strip(), named=True) for part in re.split(r'[和與、]', text)]
        return slots if len(slots) <= 12 and all(slots) else None
    return slot_list(text, allow_alternatives=True)


def overnight_span(day, text):
    match = OVERNIGHT_SPAN.fullmatch(text)
    if not match:
        return zoned_span(text, named=True)
    start, end = minutes(match[2]), minutes(match[6])
    if start == 1440 or end == 1440:
        return None
    local_day, start_day, end_day = map(date.fromisoformat, (day, match[1] or day, match[5]))
    if (start_day - local_day).days not in (0, 1) or (end_day - start_day).days not in (0, 1):
        return None
    return dict(start=start, end=end, start_date=match[1] or day, end_date=match[5],
                start_choice=OCCURRENCES[match[3] or match[4]], end_choice=OCCURRENCES[match[7] or match[8]])


def overnight_slots(day, text):
    if re.fullmatch(NO_AVAILABILITY, text):
        return []
    slots = [overnight_span(day, part.strip()) for part in re.split(r'[和與、]|或', text)]
    return slots if len(slots) <= 12 and all(slots) else None


def zoned_span_text(slot):
    if not isinstance(slot, dict):
        return display(slot)
    parts = []
    for key in ('start', 'end'):
        value = slot[key]
        choice = slot[key + '_choice']
        clock = f'{value // 60:02d}:{value % 60:02d}'
        day = slot.get(key + '_date')
        parts.append((f'{day} ' if day else '') + clock + {None: '', 1: '（第一次）', 2: '（第二次）'}[choice])
    return '-'.join(parts)


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
    overnight = len(lines) >= 3 and lines[2] == '允許跨基準午夜'
    if overnight:
        del lines[2]
    if not 6 <= len(lines) <= 12:
        return None
    named_format = OVERNIGHT_OUTPUT if overnight else NAMED_ZONED_OUTPUT
    named = bool(named_format.fullmatch(lines[-1]))
    if overnight and not named:
        return None
    if not named and not ZONED_OUTPUT.fullmatch(lines[-1]):
        return None
    reference = re.fullmatch(rf'基準時區\s*({ZONE})', lines[0])
    candidate_label = '候選起始日期' if overnight else '候選日期'
    candidate_line = re.fullmatch(rf'{candidate_label}[：:]\s*(.+)', lines[1])
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
    explicit_occurrence = False
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
                available = (overnight_slots(local[1], local[2]) if overnight
                             else zoned_slots(local[2], named=named))
                if available is None:
                    return None
                explicit_occurrence = explicit_occurrence or any(isinstance(slot, dict) for slot in available)
                local_days[local[1]] = dict(available=available, cancelled=[])
            zone = parse_zone(entry[2], named)
            people[name] = {('zone' if named else 'offset'): zone, 'days': local_days}
    except ValueError:
        return None
    schema = NAMED_ZONED_PLAN if named else ZONED_PLAN
    if overnight:
        schema = OVERNIGHT_ZONED_PLAN
    elif explicit_occurrence:
        schema = FOLD_ZONED_PLAN
    plan = dict(schema=schema, dates=dates, people=people, duration=length)
    plan['reference_zone' if named else 'reference_offset'] = reference_zone
    return plan


def zone_keys(plan):
    return {plan['reference_zone'], *(person['zone'] for person in plan['people'].values())}


def named_keys(plan):
    return {key for key in zone_keys(plan) if re.fullmatch(meeting_zones.IANA_ZONE, key)}


def bind_named_zones(plan, previous, *, fresh=False):
    if plan['schema'] not in NAMED_ZONED_PLANS:
        return plan
    keys = named_keys(plan)
    if fresh:
        saved = meeting_zones.snapshot(keys)
    elif isinstance(previous, dict) and previous.get('schema') == plan['schema'] and 'zone_rules' in previous:
        saved = previous['zone_rules']
    else:
        raise ValueError('會議缺少原先保存的時區規則。')
    meeting_zones.load_rules(saved, keys)
    return {**plan, 'zone_rules': saved}


REVISION_END = r'[，,；;。]\s*(?:請)?(?:幫我)?(?:重算|重新計算|重新算|再算一次)(?:最早時段)?[。！!？?]?'
FOLLOWUP = re.compile(r'(?:上一場會議|上次的?會議|剛才那場會議)[，,：:]\s*(.{1,512})' + REVISION_END)
SAVED_FOLLOWUP = re.compile(r'沿用會議回答\s*#([0-9]{1,30})[，,：:]\s*(.{1,512})' + REVISION_END)
REVISION_SEPARATOR = re.compile(r'[，,；;。](?![^「]*」)')
REVISED_TOPIC = re.compile(rf'(?:會議)?主題改(?:為|成)\s*{TOPIC_TEXT}')
REVISED_DURATION = re.compile(rf'(?:(?:會議)?(?:時長|長度)|會議)改(?:為|成)?\s*{LENGTH}')
REVISED_AVAILABILITY = re.compile(rf'({EDIT_NAME}){REPLACE_TIME}\s*(.+)')
REVISED_CANCEL = re.compile(rf'({EDIT_NAME}){CANCEL_BODY}')
AFTERNOON = ('下午', '整個下午')
REVISED_UNAVAILABLE = re.compile(
    rf'({EDIT_NAME})\s*((?:整個)?(?:上午|早上|下午)|[0-9].*?)\s*{UNAVAILABLE_END}')
DATED_REVISION = re.compile(rf'({DATE})(?:\s+|[：:]\s*)(.+)')
MAX_BATCH_EDITS = 4
DATE_REPLY = re.compile(rf'(?:日期(?:是|選)\s*)?({DATE})[。！]?')
NAME_REPLY = re.compile(r'(?P<explicit>姓名(?:是|選)\s*)?(?:「(?P<quoted>[\u4e00-\u9fffA-Za-z0-9]{1,20})」'
                        r'|(?P<bare>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]{0,19}))[。！]?')
DATE_EDIT = 'meeting_date_edit'
BATCH_DATE_EDIT = 'daily_meeting_edit_dates_v2'
SCOPE_EDIT = 'daily_meeting_edit_scope_v3'
MAX_DATE_COMBINATIONS = 128


def revision_parts(question):
    if selected := SAVED_FOLLOWUP.fullmatch(question):
        return selected[2], int(selected[1])
    if adjacent := FOLLOWUP.fullmatch(question):
        return adjacent[1], None
    return None


def attendee_revision(clause):
    return (REVISED_AVAILABILITY.fullmatch(clause), REVISED_CANCEL.fullmatch(clause),
            REVISED_UNAVAILABLE.fullmatch(clause))


def revise_zoned(plan, clause, local_date=None, attendee=None):
    if local_date is not None:
        replacement, removal, unavailable = attendee_revision(clause)
        if replacement:
            name, windows = replacement.groups()
            action = 'replace'
        elif removal:
            name, windows = removal[1], removal[2] or removal[3]
            action = '取消'
        elif unavailable and unavailable[2][0] in '0123456789':
            name, windows = unavailable.groups()
            action = '取消'
        else:
            raise ValueError('跨時區的取消請寫出具體的當地時段。')
    elif edit := ZONED_REVISION.fullmatch(clause):
        name, local_date, action, windows = edit.groups()
    elif unavailable := ZONED_UNAVAILABLE.fullmatch(clause):
        name, local_date, windows = unavailable.groups()
        action = '取消'
    else:
        raise ValueError('請先列出參與者與完整的當地日期，再取消或更改當日空檔。')
    name = attendee or name.strip('「」')
    person = plan['people'].get(name)
    day = person['days'].get(local_date) if person else None
    if day is None:
        raise ValueError('請使用原先列出的參與者及該人的當地日期。')
    named = plan['schema'] in NAMED_ZONED_PLANS
    overnight = plan['schema'] == OVERNIGHT_ZONED_PLAN
    replacing = action != '取消'
    if replacing:
        slots = overnight_slots(local_date, windows) if overnight else zoned_slots(windows, named=named)
        if slots is None:
            raise ValueError('請列出當日完整空檔，或寫「無空檔」。')
        day.update(available=slots, cancelled=[])
    else:
        if re.fullmatch(MORNING, windows):
            cut = [0, 720]
        else:
            cut = overnight_span(local_date, windows) if overnight else zoned_span(windows, named=named)
        if not cut or len(day['cancelled']) >= 24:
            raise ValueError('請提供有效的當地取消時段，每日最多 24 筆。')
        day['cancelled'].append(dict(interval=cut, source=clause))
        slots = [cut]
    if not overnight and any(isinstance(slot, dict) for slot in slots):
        plan['schema'] = FOLD_ZONED_PLAN
    return (name, local_date), replacing


def revised_unavailable_interval(text, availability, name):
    if text in ('上午', '整個上午', '早上', '整個早上'):
        return [0, 720]
    if text in AFTERNOON:
        cuts = sorted(cut['interval'] for cut in availability['cancelled'] if cut['name'] == name)
        remaining = surviving_windows(availability['available'][name], cuts)
        if any(end > 1080 for _, end in remaining):
            raise ValueError('這位參與者在 18:00 後仍有空檔，請寫出下午沒空的確切起訖時間。')
        return [720, 1080]
    return span(text)


def revise_clause(plan, clause, local_date=None, attendee=None):
    if topic := REVISED_TOPIC.fullmatch(clause):
        if not valid_topic(topic[1]):
            raise ValueError('請提供 1 至 64 字的會議主題。')
        plan['topic'] = topic[1]
        return 'topic', True
    duration = REVISED_DURATION.fullmatch(clause)
    if duration:
        term = duration[1]
        length = LENGTHS[term] if term in LENGTHS else int(term[:-2].strip())
        if length > 1440:
            if plan['schema'] == OVERNIGHT_ZONED_PLAN:
                raise ValueError('請指定 1 至 1440 分鐘的會議時長。')
            raise ValueError('請指定同一天內的會議時長（1 至 1440 分鐘）。')
        plan['duration'] = length
        return None, True
    if plan.get('schema') in ZONED_PLANS:
        return revise_zoned(plan, clause, local_date, attendee)
    dated = plan.get('schema') in DATE_PLANS
    availability = plan
    if dated:
        if local_date is None and (day := DATED_REVISION.fullmatch(clause)):
            local_date, clause = day.groups()
        if local_date not in plan['days']:
            raise ValueError('請先指定原先列出的完整日期，再更正當天的空檔。')
        availability = plan['days'][local_date]
    replacement, removal, unavailable = attendee_revision(clause)
    if not (replacement or removal or unavailable):
        raise ValueError('請指明要更正哪位參與者（' + '、'.join(availability['available'])
                         + '）及其同日空檔；若要換日期，請重新列出當天每人的空檔。')
    name = attendee or (replacement or removal or unavailable)[1].strip('「」')
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
        cut = (cancelled_interval(removal) if removal
               else revised_unavailable_interval(unavailable[2], availability, name))
        if not cut or len(availability['cancelled']) >= 24:
            raise ValueError('請提供有效的同日取消時段；累計超過 24 筆時，請重新列出空檔。')
        availability['cancelled'].append(dict(name=name, interval=cut, source=clause))
    return (name, local_date), replacement is not None


def revise(plan, clause, *, date_assignments=None, attendee_assignments=None):
    """Apply the whole follow-up to the detached snapshot or leave it unchanged."""
    if date_assignments is None and attendee_assignments is None and not REVISION_SEPARATOR.search(clause):
        revise_clause(plan, clause)
        return
    edits = [part.strip() for part in REVISION_SEPARATOR.split(clause)]
    if len(edits) > MAX_BATCH_EDITS or not all(edits):
        raise ValueError('同一輪請提供 2 至 4 項完整的會議更正。各項可用逗號、分號或句號分隔。')
    candidate = deepcopy(plan)
    touched = set()
    for index, edit in enumerate(edits, 1):
        try:
            local_date = date_assignments.get(index) if date_assignments else None
            attendee = attendee_assignments.get(index) if attendee_assignments else None
            scope, replacing = revise_clause(candidate, edit, local_date, attendee)
            if replacing and scope in touched:
                if scope is None:
                    raise ValueError('同一輪只能指定一次會議時長。')
                if scope == 'topic':
                    raise ValueError('同一輪只能指定一次會議主題。')
                raise ValueError('同一人同一日請先列一次完整新空檔，再列要扣除的時段。')
            touched.add(scope)
        except ValueError as exc:
            raise ValueError(f'第 {index} 項更正無法採用。{exc}') from None
    # Check combined constraints, including local times, before a chat run can be saved.
    resolve(candidate)
    plan.update(candidate)


def missing_edit_date(plan, clause):
    if plan['schema'] not in (*DATE_PLANS, *ZONED_PLANS):
        return None
    if ZONED_REVISION.fullmatch(clause) or ZONED_UNAVAILABLE.fullmatch(clause):
        return None
    replacement, removal, unavailable = attendee_revision(clause)
    match = replacement or removal or unavailable
    if match is None:
        return None
    name = match[1].strip('「」')
    if plan['schema'] in ZONED_PLANS:
        dates = plan['people'].get(name, {}).get('days', {})
    else:
        dates = [day for day, record in plan['days'].items() if name in record['available']]
    return (name, sorted(dates)) if dates else None


def revision_attendee(plan, clause):
    if plan['schema'] in ZONED_PLANS:
        scoped = ZONED_REVISION.fullmatch(clause) or ZONED_UNAVAILABLE.fullmatch(clause)
        if scoped:
            return scoped[1].strip('「」'), scoped[2]
    day = None
    if plan['schema'] in DATE_PLANS and (scoped := DATED_REVISION.fullmatch(clause)):
        day, clause = scoped.groups()
    replacement, removal, unavailable = attendee_revision(clause)
    match = replacement or removal or unavailable
    return (match[1].strip('「」'), day) if match else None


def revision_scope_options(plan, edits):
    zoned = plan['schema'] in ZONED_PLANS
    dated = plan['schema'] in DATE_PLANS
    if zoned:
        attendees = plan['people']
    elif dated:
        attendees = next(iter(plan['days'].values()))['available']
    else:
        attendees = plan['available']
    scopes, has_short_name = [], False
    for index, clause in enumerate(edits, 1):
        identified = revision_attendee(plan, clause)
        if identified is None:
            continue
        alias, explicit_date = identified
        if alias in attendees:
            names = [alias]
        elif UNBOUND_NAME.search(alias) or alias in {'沒', '別', '無', '勿', '莫'}:
            continue
        else:
            names = sorted(name for name in attendees if name.startswith(alias))
            if not names:
                continue
            has_short_name = True
        needs_date = explicit_date is None and (dated or zoned)
        if alias in attendees and not needs_date:
            continue
        options = []
        for name in names:
            days = [None]
            if needs_date:
                days = sorted(plan['people'][name]['days'] if zoned else plan['days'])
            options.extend((name, day) for day in days)
        scopes.append(dict(index=index, clause=clause, alias=alias, options=options))
    return scopes if has_short_name else None


def name_reply_value(question):
    match = NAME_REPLY.fullmatch(question)
    return (match['quoted'] or match['bare']) if match else None


def unresolved_scope(scopes, assignments):
    for position, scope in enumerate(scopes):
        names = sorted({assignment[position][0] for assignment in assignments})
        if len(names) > 1:
            return position, 0, dict(kind='name', index=scope['index'], clause=scope['clause'], choices=names)
        dates = {assignment[position][1] for assignment in assignments}
        if len(dates) > 1:
            return position, 1, dict(kind='date', index=scope['index'], clause=scope['clause'],
                                      choices=sorted(dates), participant=names[0])
    return None


def prepare_scope_revision(plan, question, body, scopes, replies):
    if prod(len(scope['options']) for scope in scopes) > MAX_DATE_COMBINATIONS:
        raise ValueError('姓名與日期的組合太多，請先寫明部分更正的完整姓名或日期。')
    assignments = list(product(*(scope['options'] for scope in scopes)))
    confirmations = []
    for reply in replies:
        pending = unresolved_scope(scopes, assignments)
        if pending is None:
            raise ValueError('已沒有待確認的姓名或日期。')
        position, field, target = pending
        if target['kind'] == 'date':
            match = DATE_REPLY.fullmatch(reply)
            value = match[1] if match else None
        else:
            value = name_reply_value(reply)
        if value not in target['choices']:
            label = '姓名' if target['kind'] == 'name' else '日期'
            raise ValueError(f'請回覆上一則列出的完整{label}。')
        assignments = [assignment for assignment in assignments if assignment[position][field] == value]
        confirmations.append(dict(index=target['index'], kind=target['kind'], value=value, reply=reply))
    pending = unresolved_scope(scopes, assignments)
    for assignment in assignments:
        candidate = deepcopy(plan)
        names = {scope['index']: name for scope, (name, _) in zip(scopes, assignment, strict=True)}
        dates = {scope['index']: day for scope, (_, day) in zip(scopes, assignment, strict=True) if day is not None}
        try:
            revise(candidate, body, date_assignments=dates, attendee_assignments=names)
        except ValueError as exc:
            raise ValueError(f'有些姓名或日期組合無法採用。請寫明完整姓名及日期。{exc}') from None
    if pending is None:
        plan.update(candidate)
    items = []
    for position, scope in enumerate(scopes):
        names = {assignment[position][0] for assignment in assignments}
        dates = {assignment[position][1] for assignment in assignments}
        items.append(dict(index=scope['index'], clause=scope['clause'], alias=scope['alias'],
                          name=next(iter(names)) if len(names) == 1 else None,
                          date=next(iter(dates)) if len(names) == len(dates) == 1 else None))
    mode = 'pending' if pending else 'selected' if replies else 'inferred'
    return dict(schema=SCOPE_EDIT, mode=mode, request=question, items=items, confirmations=confirmations,
                target=pending[2] if pending else None, local=plan['schema'] in ZONED_PLANS)


def prepare_batch_dates(plan, question, body, edits, missing, replies):
    if len(edits) > MAX_BATCH_EDITS or not all(edits):
        raise ValueError('同一輪請提供 2 至 4 項完整的會議更正。')
    if prod(len(dates) for _, (_, dates) in missing) > MAX_DATE_COMBINATIONS:
        raise ValueError('可用日期的組合太多，請先寫明部分更正的完整日期。')
    items = []
    reply_index = 0
    for index, (name, dates) in missing:
        chosen = dates[0] if len(dates) == 1 else None
        reply = None
        if chosen is None and reply_index < len(replies):
            reply = replies[reply_index]
            match = DATE_REPLY.fullmatch(reply) if isinstance(reply, str) else None
            if match is None or match[1] not in dates:
                raise ValueError('請回覆上一則列出的其中一個完整日期。')
            chosen = match[1]
            reply_index += 1
        items.append(dict(index=index, participant=name, clause=edits[index - 1],
                          dates=dates, selected=chosen, reply=reply))
    if reply_index != len(replies):
        raise ValueError('已沒有待補充的會議日期。')
    options = [[item['selected']] if item['selected'] is not None else item['dates'] for item in items]
    indexes = [item['index'] for item in items]
    pending = any(item['selected'] is None for item in items)
    for dates in product(*options):
        candidate = deepcopy(plan)
        try:
            revise(candidate, body, date_assignments=dict(zip(indexes, dates, strict=True)))
        except ValueError as exc:
            raise ValueError(f'有些日期組合無法一起採用。請寫明各項的完整日期。{exc}') from None
    if not pending:
        plan.update(candidate)
    mode = 'pending' if pending else 'selected' if replies else 'inferred'
    return dict(schema=BATCH_DATE_EDIT, mode=mode, request=question, items=items,
                local=plan['schema'] in ZONED_PLANS)


def prepare_revision(plan, question, selected_date=None, *, replies=(), scope_replies=()):
    revision = revision_parts(question)
    if revision is None:
        raise ValueError('會議更正與原問不符。')
    body, _ = revision
    edits = [part.strip() for part in REVISION_SEPARATOR.split(body)]
    scopes = revision_scope_options(plan, edits)
    if scopes and selected_date is None and not replies:
        if len(edits) > MAX_BATCH_EDITS or not all(edits):
            raise ValueError('同一輪最多提供 4 項完整的會議更正。')
        return prepare_scope_revision(plan, question, body, scopes, scope_replies)
    if scope_replies:
        raise ValueError('姓名或日期回覆與原更正不符。')
    missing = [(index, scope) for index, edit in enumerate(edits, 1) if (scope := missing_edit_date(plan, edit))]
    if len(missing) > 1 and selected_date is None:
        return prepare_batch_dates(plan, question, body, edits, missing, replies)
    if replies:
        raise ValueError('會議日期回覆與原更正不符。')
    if not missing and selected_date is None:
        revise(plan, body)
        return None
    if len(missing) != 1:
        raise ValueError('日期回覆與原更正不符。')
    index, (name, dates) = missing[0]
    if selected_date is not None and selected_date not in dates:
        raise ValueError('請回覆上一則列出的其中一個完整日期。')
    day = selected_date or (dates[0] if len(dates) == 1 else None)
    chosen = None
    for option in dates:
        candidate = deepcopy(plan)
        try:
            revise(candidate, body, date_assignments={index: option})
        except ValueError as exc:
            raise ValueError(f'無法套用 {option} 的更正。請指定完整日期並核對時段。{exc}') from None
        if option == day:
            chosen = candidate
    if chosen is not None:
        plan.update(chosen)
    mode = 'selected' if selected_date is not None else 'inferred' if day else 'pending'
    return dict(schema='daily_meeting_edit_date_v1', mode=mode, request=question, index=index,
                participant=name, dates=dates, selected=day, local=plan['schema'] in ZONED_PLANS)


def pending_date(context):
    edit = context.get(DATE_EDIT)
    return isinstance(edit, dict) and edit.get('mode') == 'pending'


def pending_choice_kind(context):
    if not pending_date(context):
        return None
    edit = context[DATE_EDIT]
    if edit.get('schema') != SCOPE_EDIT:
        return 'date'
    target = edit.get('target')
    if not isinstance(target, dict) or target.get('kind') not in ('name', 'date'):
        raise ValueError('會議澄清缺少可核對的問題。')
    return target['kind']


def reply_kind(question):
    if DATE_REPLY.fullmatch(question):
        return 'date'
    if NAME_REPLY.fullmatch(question):
        return 'name'
    return None


def accepts_name_reply(question, parent):
    match = NAME_REPLY.fullmatch(question)
    return bool(match and (match['explicit'] or name_reply_value(question) in parent[DATE_EDIT]['target']['choices']))


def apply_followup(plan, question, parent):
    if pending_date(parent):
        edit = parent[DATE_EDIT]
        if edit['schema'] == SCOPE_EDIT:
            replies = [selection['reply'] for selection in edit['confirmations']]
            return prepare_revision(plan, edit['request'], scope_replies=[*replies, question])
        reply = DATE_REPLY.fullmatch(question)
        if reply is None:
            raise ValueError('請回覆上一則列出的其中一個完整日期。')
        if edit['schema'] == BATCH_DATE_EDIT:
            replies = [item['reply'] for item in edit['items'] if item['reply'] is not None]
            return prepare_revision(plan, edit['request'], replies=[*replies, question])
        return prepare_revision(plan, parent['current_request'], reply[1])
    return prepare_revision(plan, question)


def evaluate_followup(plan, edit):
    if edit is not None and edit['mode'] == 'pending':
        scope = '當地日期' if edit['local'] else '日期'
        if edit['schema'] == SCOPE_EDIT:
            item = edit['target']
            target = f'第 {item["index"]} 項「{item["clause"]}」'
            if item['kind'] == 'name':
                names = '、'.join(f'「{name}」' for name in item['choices'])
                answer = f'{target}指的是哪位參與者？請回覆完整姓名：{names}。'
            else:
                dates = '、'.join(item['choices'])
                answer = f'{target}對「{item["participant"]}」適用於哪個{scope}？請回覆完整日期：{dates}。'
            return answer, dict(schema='daily_meeting_scope_question_v1', status='clarification_required', model_called=False)
        if edit['schema'] == BATCH_DATE_EDIT:
            item = next(item for item in edit['items'] if item['selected'] is None)
            target = f'第 {item["index"]} 項「{item["clause"]}」'
        else:
            item = edit
            target = f'第 {item["index"]} 項對「{item["participant"]}」的更正'
        dates = '、'.join(item['dates'])
        answer = (f'{target}適用於哪個{scope}？'
                  f'請回覆完整日期：{dates}。')
        return answer, dict(schema='daily_meeting_date_question_v1', status='clarification_required', model_called=False)
    return resolve(plan)


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
    if plan.get('schema') in NAMED_ZONED_PLANS:
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


def named_day_bounds(day, zone, label):
    try:
        start, end = next(meeting_zones.utc_intervals(day, [(0, 1440)], zone, label))
    except ValueError as exc:
        raise ValueError(f'基準日期 {day} 在 {label} 的日界無法唯一換算，請改用其他基準時區或日期。') from exc
    width = end - start
    if not 1 <= width <= 2880:
        raise ValueError('基準日期超出可比較的日界。')
    return start, end


def named_day(day, zone, label, available, cancelled, length):
    start, end = named_day_bounds(day, zone, label)
    width = end - start
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


def surviving_windows(intervals, cuts):
    for start, end in intervals:
        for left, right in cuts:
            if right <= start:
                continue
            if left >= end:
                break
            if start < left:
                yield start, left
            start = right
            if start >= end:
                break
        if start < end:
            yield start, end


def possible_starts(available, cancelled, start, end, duration):
    """Return half-open UTC runs of feasible starts, preserving per-person alternatives."""
    width = end - start
    shared = bytearray([1]) * width
    individual = {}
    for name, intervals in available.items():
        cuts = sorted(cut['interval'] for cut in cancelled if cut['name'] == name)
        mask = bytearray(width)
        for left, right in surviving_windows(intervals, cuts):
            first = max(0, left - start)
            after = min(width, right - start - duration + 1)
            if first < after:
                mask[first:after] = b'\x01' * (after - first)
        individual[name] = [(start + a, start + b) for a, b in ranges(mask)]
        shared = bytearray(a & b for a, b in zip(shared, mask))
    return individual, [(start + a, start + b) for a, b in ranges(shared)]


def resolve_overnight(plan, zone, available, cancelled):
    length = plan['duration']

    def show(instant):
        return meeting_zones.local_date_clock(instant, zone)

    def starts_text(interval):
        first, after = interval
        return show(first) if after == first + 1 else f'{show(first)} 至 {show(after - 1)}'

    days = []
    first_slot = None
    for day in sorted(plan['dates']):
        start, end = named_day_bounds(day, zone, plan['reference_zone'])
        individual, shared = possible_starts(available, cancelled, start, end, length)
        first = shared[0][0] if shared else None
        first_text = f'{show(first)} 至 {show(first + length)}' if first is not None else None
        if first_slot is None and first is not None:
            first_slot = first, first + length, first_text
        days.append(dict(date=day, effective={name: list(map(starts_text, slots)) for name, slots in individual.items()},
                         common=list(map(starts_text, shared)), first=first_text))
    answer = first_slot[2] if first_slot else '沒有可行時段'
    result = dict(exact=answer, calculations=[])
    if first_slot:
        result.update(utc_start_minute=first_slot[0], utc_end_minute=first_slot[1])
    return answer, dict(schema='daily_meeting_overnight_calculation_v7', status='complete', model_called=False,
                        days=days, duration=length, reference_zone=plan['reference_zone'],
                        expression=f'依候選起始日期查找最早共同連續 {length} 分鐘', result=result)


def resolve_named_zoned(plan):
    zones = meeting_zones.load_rules(plan['zone_rules'], named_keys(plan))
    for key in zone_keys(plan) - zones.keys():
        zones[key] = timezone(timedelta(minutes=offset_minutes(key)))
    reference = plan['reference_zone']
    length = plan['duration']
    available, cancelled = utc_availability(plan, zones)
    if plan['schema'] == OVERNIGHT_ZONED_PLAN:
        return resolve_overnight(plan, zones[reference], available, cancelled)
    days = [named_day(day, zones[reference], reference, available, cancelled, length) for day in sorted(plan['dates'])]
    answer = next((f'{day["date"]} {day["first"]}' for day in days if day['first']), '沒有可行時段')
    schema = ('daily_meeting_fold_zone_calculation_v6' if plan['schema'] == FOLD_ZONED_PLAN
              else 'daily_meeting_named_zone_calculation_v5')
    receipt = dict(schema=schema, status='complete', model_called=False,
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
    if topic := plan.get('topic'):
        lines.append(f'會議主題「{topic}」')
    date_edit = context.get(DATE_EDIT)
    zoned = plan['schema'] in ZONED_PLANS
    overnight = plan['schema'] == OVERNIGHT_ZONED_PLAN
    dated = zoned or plan['schema'] in DATE_PLANS
    if source := context.get('meeting_source'):
        scope = '日期與時段' if dated else '同日時段與條件'
        revision = revision_parts(context['current_request'])
        intro = '依你指定的本對話會議回答' if revision and revision[1] is not None else '沿用本對話會議回答'
        lines.append(f'{intro} #{source["answer_id"]} 的{scope}')
        if date_edit is not None:
            scope = '當地日期' if date_edit['local'] else '日期'
            if date_edit['schema'] == SCOPE_EDIT:
                for item in date_edit['items']:
                    if item['name'] is not None:
                        lines.append(f'第 {item["index"]} 項「{item["alias"]}」對應「{item["name"]}」。')
                    if item['date'] is not None:
                        lines.append(f'第 {item["index"]} 項採用{scope} {item["date"]}。')
                lines.extend(f'第 {item["index"]} 項原回覆：{item["reply"]}' for item in date_edit['confirmations'])
            elif date_edit['schema'] == BATCH_DATE_EDIT:
                for item in date_edit['items']:
                    if item['selected'] is None:
                        continue
                    reason = '依你的回覆' if item['reply'] is not None else '依原排程的唯一選項'
                    lines.append(f'第 {item["index"]} 項{reason}採用{scope} {item["selected"]}。')
                    if item['reply'] is not None:
                        lines.append('原回覆：' + item['reply'])
            elif date_edit['selected'] is not None:
                reason = '依你的回覆' if date_edit['mode'] == 'selected' else '依原排程的唯一選項'
                lines.append(f'第 {date_edit["index"]} 項{reason}採用{scope} {date_edit["selected"]}。')
            if date_edit['request'] != context['current_request']:
                lines += ['原更正：' + date_edit['request']]
        if pending_date(context):
            label = '姓名' if pending_choice_kind(context) == 'name' else '日期'
            lines += [f'整批更正尚待確認{label}。', evaluate_followup(plan, date_edit)[0]]
            return '\n'.join(lines)
        original_request = date_edit['request'] if date_edit else context['current_request']
        revision = revision_parts(original_request)
        if revision and REVISION_SEPARATOR.search(revision[0]):
            lines.append('本次依序採用的更正')
            lines.extend(f'{index}. {part.strip()}'
                         for index, part in enumerate(REVISION_SEPARATOR.split(revision[0]), 1))
    if anchor := plan.get('date_anchor'):
        if anchor['source'] == 'user':
            lines.append(f'依原問指定的日期 {anchor["date"]} 為今天。')
        else:
            observed = anchor['observation']['local_datetime']
            lines.append(f'相對日期以原本的電腦本地日期 {anchor["date"]} 為基準。')
            lines.append(f'原本的本機時鐘觀察 {observed}')
    if zoned:
        named = plan['schema'] in NAMED_ZONED_PLANS
        reference = plan['reference_zone'] if named else offset_text(plan['reference_offset'])
        lines += ['', f'基準時區 {reference}']
        if overnight:
            lines.append('候選日期限制會議開始。各人須有一段空檔完整覆蓋會議。')
        if named:
            lines.append(f'沿用保存的 tzdata {plan["zone_rules"]["version"]} 規則。')
        lines.append('原始當地空檔與更正')
        for name, person in plan['people'].items():
            zone = person['zone'] if named else offset_text(person['offset'])
            lines.append(f'{name} {zone}')
            for local_day, windows in sorted(person['days'].items()):
                slots = '、'.join(map(zoned_span_text, windows['available'])) or '無'
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
        if overnight:
            lines.append('可開始的分鐘（區間含起訖）')
        elif zoned:
            lines.append('換算後每人剩餘時段')
        else:
            lines.append('採用的取消條件')
            lines += [c['source'] for c in availability['cancelled']] or ['無']
            lines += ['', '每人剩餘時段']
        for name, slots in day['effective'].items():
            lines.append(name + '：' + ('、'.join(slots) or '無'))
        label = '共同可開始時間：' if overnight else '共同空檔：'
        lines.append(label + ('、'.join(day['common']) or '無'))
        if dated:
            lines.append('當日最早：' + (day['first'] or '沒有可行時段'))
    lines += ['', f"所需時間：{receipt['duration']} 分鐘", '結果：' + receipt['result']['exact']]
    return '\n'.join(lines)


def previous_id(db, sid, before):
    row = db.execute('SELECT id FROM messages WHERE session_id=? AND (? IS NULL OR id<?) '
                     'ORDER BY id DESC LIMIT 1', (sid, before, before)).fetchone()
    return row['id'] if row else None


def followup_source_id(db, sid, before, answer_id):
    if answer_id is None:
        return previous_id(db, sid, before)
    if not 0 < answer_id <= 2**63 - 1 or (before is not None and answer_id >= before):
        return None
    row = db.execute("SELECT id FROM messages WHERE id=? AND session_id=? AND role='assistant'",
                     (answer_id, sid)).fetchone()
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
            or not same_json(saved.get(DATE_EDIT), initial.get(DATE_EDIT))
            or saved.get('image_inputs') or saved.get('engine_selection') is not None
            or saved.get('documents') != {'catalog': [], 'excerpts': []}
            or load_context(row['checks']) != []):
        raise ValueError('會議來源與已完成的回答流程不符。')
    ref = dict(answer_id=mid, sha256=hashlib.sha256(row['payload'].encode('utf-8')).hexdigest())
    return row, saved, ref


def verified_meeting(db, mid, *, choice_kind=None):
    tip = saved_meeting(db, mid)
    if tip is None or (choice_kind and pending_choice_kind(tip[1]) != choice_kind):
        return None
    history = [tip]
    while 'meeting_source' in history[-1][1]:
        row, saved, _ = history[-1]
        revision = revision_parts(row['question'])
        answer_id = revision[1] if revision else None
        parent_id = followup_source_id(db, row['session_id'], row['uid'], answer_id)
        parent = saved_meeting(db, parent_id)
        if parent is None or saved['meeting_source'] != parent[2]:
            raise ValueError('會議的上一筆來源已變更。')
        history.append(parent)
    original = history.pop()
    plan = parse(original[0]['question'], observation=recorded_observation(original[1].get('meeting_plan')))
    if plan is None:
        raise ValueError('會議紀錄缺少可核對的原始時段。')
    plan = bind_named_zones(plan, original[1].get('meeting_plan'))

    def check(record, effective, edit=None):
        row, saved, _ = record
        answer, calculation = evaluate_followup(effective, edit)
        if (not same_json(saved.get('meeting_plan'), effective)
                or not same_json(saved.get(DATE_EDIT), edit)
                or not same_json(saved.get('calculation'), calculation)):
            raise ValueError('會議紀錄與重算結果不符。')
        if answer != row['content']:
            raise ValueError('會議回答與重算結果不符。')

    check(original, plan)
    parent = original[1]
    for record in reversed(history):
        edit = apply_followup(plan, record[0]['question'], parent)
        check(record, plan, edit)
        parent = record[1]
    return tip


def read(store, mid):
    with store.connect() as db:
        db.execute('BEGIN')
        verified = verified_meeting(db, mid)
        return verified[1] if verified else None


def completed_candidates(db, sid, before, limit):
    rows = db.execute('''SELECT m.id, m.content, json_extract(c.payload, '$.meeting_plan.topic') AS topic
            FROM messages m
            JOIN answer_contexts c ON c.message_id=m.id
            WHERE m.session_id=? AND m.role='assistant'
              AND (? IS NULL OR m.id<?)
              AND CASE WHEN length(CAST(c.payload AS BLOB))<=? AND json_valid(c.payload)
                       THEN json_type(c.payload, '$.meeting_plan')='object'
                         AND json_type(c.payload, '$.calculation.result')='object'
                  END
            ORDER BY m.id DESC LIMIT ?''', (sid, before, before, MAX_JSON_BYTES, limit)).fetchall()
    return [dict(row) for row in rows]


def recent_completed(store, sid):
    """List saved candidates; opening or using one still rechecks the source chain."""
    with store.connect() as db:
        return completed_candidates(db, sid, None, 20)


def previous(store, sid, before=None, *, required=True, choice_kind=None, answer_id=None):
    message = '上一則回答沒有可接續的同日會議紀錄，請提供每人的空檔與會議長度。'
    if choice_kind == 'date':
        message = '上一則回答沒有待補充的會議日期。'
    if choice_kind == 'name':
        message = '上一則回答沒有待確認的參與者姓名。'
    if answer_id is not None:
        message = f'找不到本對話中可核對的會議回答 #{answer_id}。請確認回答編號。'
    try:
        with store.connect() as db:
            db.execute('BEGIN')
            mid = followup_source_id(db, sid, before, answer_id)
            verified = verified_meeting(db, mid, choice_kind=choice_kind)
    except ValueError as exc:
        raise ValueError(message) from exc
    if verified:
        context, source = verified[1:]
        if pending_choice_kind(context) == choice_kind:
            return context, source
        if choice_kind is None:
            label = '姓名' if pending_choice_kind(context) == 'name' else '日期'
            if answer_id is not None:
                raise ValueError(f'會議回答 #{answer_id} 尚待確認{label}。請指定已完成的會議回答。')
            raise ValueError(f'上一則會議更正仍待確認{label}，請先回覆列出的選項。')
    if required:
        raise ValueError(message)
