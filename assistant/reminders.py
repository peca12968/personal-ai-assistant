"""Opt-in, local daily follow-up. No model calls or work execution."""
from datetime import date, datetime
import re
import uuid

from . import goal_review


def _now(value):
    value = value if value is not None else datetime.now().astimezone()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('提醒時間需包含時區。')
    if not 0 <= value.timestamp() <= 253402299899:
        raise ValueError('提醒時間超出範圍。')
    return value


def validate_settings(enabled, daily_time):
    if type(enabled) is not bool or not isinstance(daily_time, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', daily_time):
        raise ValueError('請設定每日時間HH:MM（00:00至23:59）。')


def read(db):
    rows = db.execute('SELECT id,revision,enabled,daily_time,last_day,snooze_until,notice_token FROM review_reminders').fetchall()
    if not rows:
        return dict(id=1, revision=0, enabled=False, daily_time='19:00', last_day=None, snooze_until=None, notice_token=None)
    if len(rows) != 1:
        raise ValueError('提醒設定格式不符。')
    row = dict(zip(('id','revision','enabled','daily_time','last_day','snooze_until','notice_token'), rows[0]))
    if row['id'] != 1 or type(row['revision']) is not int or row['revision'] < 1 or type(row['enabled']) is not int or row['enabled'] not in (0,1):
        raise ValueError('提醒設定版本不符。')
    row['enabled'] = bool(row['enabled'])
    validate_settings(row['enabled'], row['daily_time'])
    day, snooze, token = row['last_day'], row['snooze_until'], row['notice_token']
    if day is not None:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            raise ValueError('提醒日期格式不符。')
    if snooze is not None and (type(snooze) is not int or not 0 <= snooze <= 253402300799):
        raise ValueError('稍後提醒時間格式不符。')
    if token is not None and (not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token) or day is None or not row['enabled']):
        raise ValueError('提醒紀錄格式不符。')
    if snooze is not None and token is None:
        raise ValueError('稍後提醒缺少來源。')
    return row


def settings(store):
    with store.connect() as db:
        return read(db)


def save_settings(store, *, enabled, daily_time, expected_revision):
    validate_settings(enabled, daily_time)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('提醒設定版本不符。')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = read(db)
        if old['revision'] != expected_revision:
            raise ValueError('提醒設定已在其他視窗變更，請重新載入。')
        # Configuration changes clear the visible/snoozed notice, but never
        # reset today's receipt and silently schedule another daily notice.
        db.execute('''INSERT INTO review_reminders(id,revision,enabled,daily_time,last_day)
            VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,
            enabled=excluded.enabled,daily_time=excluded.daily_time,snooze_until=NULL,notice_token=NULL''',
            (old['revision']+1, int(enabled), daily_time, old['last_day']))
        return read(db)


def _items(db):
    # Same read/write transaction as the due-time decision. Recorded running
    # goals are omitted, without inferring that a process is alive or dead.
    rows = [r for r in goal_review._rows(db) if not r['hidden'] and r['recorded_status'] != 'running']
    return {'count':len(rows), 'items':rows[:3]}


def check(store, *, now=None, busy=False):
    if type(busy) is not bool:
        raise ValueError('忙碌狀態不符。')
    now = _now(now)
    if busy:
        return None
    today, seconds = now.date().isoformat(), int(now.timestamp())
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        state = read(db)
        if not state['enabled']:
            return None
        snooze = state['snooze_until']
        if snooze is not None and seconds < snooze:
            return None
        new_day = state['last_day'] is None or today > state['last_day']
        due = (new_day and now.strftime('%H:%M') >= state['daily_time']) or snooze is not None
        if not due and state['notice_token'] is None:
            return None
        current = _items(db)
        if not current['count']:
            db.execute('UPDATE review_reminders SET snooze_until=NULL,notice_token=NULL WHERE id=1')
            return None  # An empty day is not consumed.
        token = state['notice_token']
        if due:
            token = uuid.uuid4().hex
            # A backward clock/timezone change never moves the daily receipt back.
            last_day = max(today, state['last_day'] or today)
            db.execute('UPDATE review_reminders SET last_day=?,snooze_until=NULL,notice_token=? WHERE id=1', (last_day, token))
        return {**current, 'token':token, 'checked_at':now.isoformat()}


def respond(store, token, *, snooze=False, now=None):
    now = _now(now)
    if type(snooze) is not bool or not isinstance(token, str):
        raise ValueError('提醒操作格式不符。')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        state = read(db)
        if not state['enabled'] or state['notice_token'] != token:
            raise ValueError('這則提醒已在其他視窗處理或設定已變更，請重新查看。')
        db.execute('UPDATE review_reminders SET snooze_until=?,notice_token=?,last_day=? WHERE id=1',
            (int(now.timestamp())+900 if snooze else None, uuid.uuid4().hex if snooze else None,
             max(now.date().isoformat(),state['last_day'])))


def describe(notice):
    lines = [f"有 {notice['count']} 項工作可回顧，我幫你列出目前的下一步："]
    for row in notice['items']:
        title = ' '.join(row['objective'].split())[:65]
        lines.append(f"• {title} — {row['explanation']}")
    if notice['count'] > len(notice['items']):
        lines.append('其餘項目請按「查看全部」。')
    lines.append('本機提醒；查看時會重新整理，不會自動開始工作。')
    return '\n'.join(lines)
