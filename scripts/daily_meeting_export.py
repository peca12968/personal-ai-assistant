"""Export one verified meeting to a new local iCalendar file."""
import argparse
from datetime import date, datetime, time, timedelta, timezone
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unicodedata
from uuid import uuid4

from assistant.date_tools import parse_date
from assistant.engine import ROOT
from assistant.store import Store
from scripts import daily_meeting as meeting
from scripts import daily_meeting_zones as zones

SAME_DAY_PLAN = 'daily_availability_v1'
STORED_OFFSET = r'UTC[+-](?:[01][0-9]|2[0-3]):[0-5][0-9]'
NAMED_RESULT = re.compile(rf'({meeting.DATE}) ({meeting.CLOCK}) ({STORED_OFFSET}) 至 '
                          rf'({meeting.CLOCK}) ({STORED_OFFSET})')
FIXED_RESULT = re.compile(rf'({meeting.DATE}) ({meeting.CLOCK})-({meeting.CLOCK}) ({meeting.UTC_OFFSET})')
DATED_RESULT = re.compile(rf'({meeting.DATE}) ({meeting.CLOCK})-({meeting.CLOCK})')


def utc_at(day, clock, offset):
    delta = timedelta(minutes=meeting.minutes(clock) - offset)
    return datetime.combine(parse_date(day), time(), tzinfo=timezone.utc) + delta


def stored_offset(text):
    return meeting.minutes(text[4:]) * (-1 if text[3] == '-' else 1)


def utc_from_minute(instant):
    ordinal, minute = divmod(instant, 1440)
    return datetime.combine(date.fromordinal(ordinal), time(), tzinfo=timezone.utc) + timedelta(minutes=minute)


def explicit_zone_instants(day, start, end, zone_name):
    name = meeting.parse_zone(zone_name, named=True)
    if re.fullmatch(meeting.UTC_OFFSET, name):
        offset = meeting.offset_minutes(name)
        return utc_at(day, start, offset), utc_at(day, end, offset)
    zone = zones.load_rules(zones.snapshot({name}), {name})[name]
    first, last = next(zones.utc_intervals(day, [(meeting.minutes(start), meeting.minutes(end))], zone, name))
    return utc_from_minute(first), utc_from_minute(last)


def event_instants(record, day=None, zone=None):
    schema = record['meeting_plan']['schema']
    calculation = record['calculation']
    result = calculation['result']['exact']
    if result == '沒有可行時段':
        raise ValueError('這筆紀錄沒有可匯出的會議時段。')
    if schema != SAME_DAY_PLAN and day is not None:
        raise ValueError('請使用會議紀錄中已確認的日期。')
    if schema in meeting.ZONED_PLANS and zone is not None:
        raise ValueError('請使用會議紀錄中已確認的時區。')
    try:
        if schema == meeting.OVERNIGHT_ZONED_PLAN:
            start = utc_from_minute(calculation['result']['utc_start_minute'])
            end = utc_from_minute(calculation['result']['utc_end_minute'])
        elif schema in meeting.NAMED_ZONED_PLANS and (match := NAMED_RESULT.fullmatch(result)):
            start = utc_at(match[1], match[2], stored_offset(match[3]))
            end = utc_at(match[1], match[4], stored_offset(match[5]))
        elif schema == meeting.ZONED_PLAN and (match := FIXED_RESULT.fullmatch(result)):
            offset = meeting.offset_minutes(match[4])
            start, end = (utc_at(match[1], clock, offset) for clock in (match[2], match[3]))
        elif schema in meeting.DATE_PLANS and (match := DATED_RESULT.fullmatch(result)):
            if not zone:
                raise ValueError('請提供這場會議的時區。')
            start, end = explicit_zone_instants(match[1], match[2], match[3], zone)
        elif schema == SAME_DAY_PLAN and (match := re.fullmatch(rf'({meeting.CLOCK})-({meeting.CLOCK})', result)):
            if not day or not zone:
                raise ValueError('請提供這場會議的完整日期與時區。')
            start, end = explicit_zone_instants(day, match[1], match[2], zone)
        else:
            raise ValueError('無法從已驗證的會議紀錄取得起訖。')
    except OverflowError:
        raise ValueError('會議換算後超出可匯出的公曆範圍。') from None
    if end - start != timedelta(minutes=calculation['duration']) or end <= start:
        raise ValueError('所選時區換算後的時長與原排程不同，請先以跨時區會議重新計算。')
    return start, end


def summary_text(title):
    cleaned = title.strip()
    if not 1 <= len(cleaned) <= 160 or any(unicodedata.category(c) == 'Cc' or c in '\u2028\u2029' for c in title):
        raise ValueError('行事曆標題須為 1 至 160 字的單行文字。')
    return cleaned.replace('\\', '\\\\').replace(',', '\\,').replace(';', '\\;')


def fold_line(line):
    parts = []
    physical = bytearray()
    for char in line:
        token = char.encode('utf-8')
        if len(physical) + len(token) > 75:
            parts.append(bytes(physical))
            physical = bytearray(b' ')
        physical.extend(token)
    parts.append(bytes(physical))
    return b'\r\n'.join(parts)


def calendar_payload(title, start, end):
    def stamp(instant):
        return instant.isoformat(timespec='seconds').replace('-', '').replace(':', '').removesuffix('+0000') + 'Z'

    lines = [
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Personal AI Assistant//Local Meeting Export//EN',
        'BEGIN:VEVENT', f'UID:{uuid4().hex}@personal-ai-assistant.local', f'DTSTAMP:{stamp(datetime.now(timezone.utc))}',
        f'DTSTART:{stamp(start)}', f'DTEND:{stamp(end)}', 'SUMMARY:' + summary_text(title),
        'END:VEVENT', 'END:VCALENDAR',
    ]
    return b'\r\n'.join(map(fold_line, lines)) + b'\r\n'


def write_new_calendar(destination, payload):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.meeting-', suffix='.ics', delete=False) as file:
            temporary = Path(file.name)
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        # Windows rename and POSIX link both refuse an existing destination.
        if os.name == 'nt':
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
    except FileExistsError:
        raise ValueError('指定的行事曆檔案已存在，請另存新檔。') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    with destination.open('rb') as file:
        if file.read(len(payload) + 1) != payload:
            raise ValueError('行事曆檔案內容無法核對。')


def export(store, answer_id, title, destination, *, day=None, zone=None):
    destination = Path(destination)
    if destination.suffix.lower() != '.ics':
        raise ValueError('行事曆檔案請使用 .ics 副檔名。')
    if type(answer_id) is not int or answer_id < 1:
        raise ValueError('請指定有效的會議回答編號。')
    record = meeting.read(store, answer_id)
    if record is None:
        raise ValueError('找不到已驗證的會議紀錄。')
    start, end = event_instants(record, day, zone)
    write_new_calendar(destination, calendar_payload(title, start, end))
    return destination.resolve()


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='匯出一筆已驗證的本機會議紀錄')
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--answer-id', type=int, required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--date')
    parser.add_argument('--zone')
    args = parser.parse_args()
    database = args.data_dir / 'assistant.sqlite3'
    if not database.is_file():
        parser.exit(1, '找不到指定的本機助理資料庫。\n')
    try:
        print(export(Store(database), args.answer_id, args.title, args.output, day=args.date, zone=args.zone))
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, '匯出未完成：' + str(exc) + '\n')


if __name__ == '__main__':
    main()
