"""Find completed, source-verified meeting versions in the current conversation."""
from datetime import date
import re

from assistant import local_answer_receipts
from scripts import daily_meeting as meeting

FIELD = 'meeting_lookup'
MODE = 'local_calculation'
PLAN_V1 = 'daily_meeting_lookup_plan_v1'
PLAN_V2 = 'daily_meeting_lookup_plan_v2'
PLAN_V3 = 'daily_meeting_lookup_plan_v3'
RECEIPTS = {PLAN_V1: 'daily_meeting_lookup_receipt_v1', PLAN_V2: 'daily_meeting_lookup_receipt_v2',
            PLAN_V3: 'daily_meeting_lookup_receipt_v3'}
MAX_SCAN = 100
MAX_RESULTS = 10
REQUEST = re.compile(r'(?:請)?(?:查詢?|找)會議紀錄[，,：:]\s*(.{1,240}?)[。！!？?]?')
DATE_FILTER = re.compile(rf'(?:基準)?日期(?:是|為)?\s*({meeting.DATE})')
RANGE_FILTER = re.compile(r'(?:基準)?日期(?:範圍)?(?:是|為|從)?\s*(.+)')
DATE_RANGE = re.compile(rf'({meeting.DATE})\s*[至到～~]\s*({meeting.DATE})')
ATTENDEE_FILTER = re.compile(r'(?:參與者|人員)(?:是|為)?\s*(.+)')
PERSON = re.compile(r'「([\u4e00-\u9fffA-Za-z0-9]{1,16})」|([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]{0,19})')
TOPIC_FILTER = re.compile(rf'(?:會議)?主題(?:是|為)?\s*{meeting.TOPIC_TEXT}')


def parse_ranges(text):
    parts = [DATE_RANGE.fullmatch(part.strip()) for part in text.split('或')]
    if not 1 <= len(parts) <= 3 or not all(parts):
        return None
    ranges = []
    for part in parts:
        try:
            start, end = (date.fromisoformat(value).isoformat() for value in part.groups())
        except ValueError:
            raise ValueError('請輸入有效的完整日期，例如 2028-02-29。') from None
        if start > end:
            raise ValueError('日期範圍的起日必須早於或等於迄日。')
        ranges.append([start, end])
    return ranges


def parse(question, *, schema=PLAN_V3):
    request = REQUEST.fullmatch(question) if isinstance(question, str) else None
    if not request or not isinstance(schema, str) or schema not in RECEIPTS:
        return None
    day, attendees, topic, ranges = None, [], None, []
    separator = r'[，,；;]' if schema == PLAN_V1 else r'[，,；;](?![^「]*」)'
    for clause in re.split(rf'\s*{separator}\s*', request[1]):
        if day is None and not ranges and (match := DATE_FILTER.fullmatch(clause)):
            try:
                day = date.fromisoformat(match[1]).isoformat()
            except ValueError:
                raise ValueError('請輸入有效的完整日期，例如 2028-02-29。') from None
        elif schema == PLAN_V3 and day is None and not ranges and (match := RANGE_FILTER.fullmatch(clause)):
            ranges = parse_ranges(match[1])
            if ranges is None:
                return None
        elif not attendees and (match := ATTENDEE_FILTER.fullmatch(clause)):
            names = [PERSON.fullmatch(part.strip()) for part in re.split(r'[、與及和](?![^「]*」)', match[1])]
            if not 1 <= len(names) <= 8 or not all(names):
                return None
            attendees = [name[1] or name[2] for name in names]
            if len(set(attendees)) != len(attendees):
                return None
        elif schema != PLAN_V1 and topic is None and (match := TOPIC_FILTER.fullmatch(clause)):
            if not meeting.valid_topic(match[1]):
                return None
            topic = match[1]
        else:
            return None
    criteria = dict(date=day, attendees=attendees)
    if schema != PLAN_V1:
        criteria['topic'] = topic
    if schema == PLAN_V3:
        criteria['date_ranges'] = ranges
    return criteria


def summary(context):
    plan = context['meeting_plan']
    if plan['schema'] in meeting.ZONED_PLANS:
        dates = sorted(plan['dates'])
        attendees = list(plan['people'])
    elif plan['schema'] in meeting.DATE_PLANS:
        dates = sorted(plan['days'])
        attendees = list(plan['days'][dates[0]]['available'])
    else:
        dates, attendees = [], list(plan['available'])
    return dict(dates=dates, attendees=attendees, result=context['calculation']['result']['exact'])


def lookup(db, sid, question, before, cancel=None, *, schema=PLAN_V3):
    criteria = parse(question, schema=schema)
    if criteria is None:
        return None
    ranges = criteria.get('date_ranges', [])
    candidates = meeting.completed_candidates(db, sid, before, MAX_SCAN + 1)
    checked, matches = [], []
    for row in candidates[:MAX_SCAN]:
        if cancel and cancel.is_set():
            from assistant.engine import EngineError
            raise EngineError('已停止查詢會議紀錄。', 'cancelled')
        mid = row['id']
        try:
            verified = meeting.verified_meeting(db, mid)
        except (KeyError, TypeError, ValueError):
            verified = None
        if verified is None or meeting.pending_date(verified[1]):
            checked.append(dict(answer_id=mid, sha256=None))
            continue
        _, context, source = verified
        checked.append(source)
        item = summary(context)
        if schema != PLAN_V1 and (topic := context['meeting_plan'].get('topic')):
            item['topic'] = topic
        if ranges:
            matched_dates = [day for day in item['dates'] if any(start <= day <= end for start, end in ranges)]
            if not matched_dates:
                continue
            item['matched_dates'] = matched_dates
        if ((criteria['date'] is None or criteria['date'] in item['dates'])
                and (criteria.get('topic') is None or criteria['topic'] == item.get('topic'))
                and set(criteria['attendees']).issubset(item['attendees'])):
            matches.append(dict(answer_id=mid, **item))
    return dict(schema=schema, request=question, criteria=criteria, checked=checked,
                matches=matches, limited=len(candidates) > MAX_SCAN)


def capture(store, sid, question, before=None, cancel=None, *, schema=PLAN_V3):
    with store.connect() as db:
        db.execute('BEGIN')
        return lookup(db, sid, question, before, cancel, schema=schema)


def evaluate(plan):
    matches = plan['matches']
    lines = ([f'找到 {len(matches)} 筆符合的會議回答。'] if matches
             else ['這段對話沒有符合條件的可核對會議回答。'])
    if plan['criteria'].get('date_ranges'):
        lines.append('日期欄列出符合範圍的原候選日期，含起訖日。時段保留原回答。')
    elif plan['criteria']['date'] is not None:
        lines.append('日期依原排程列出的候選日期比對。')
    for match in matches[:MAX_RESULTS]:
        dates = '、'.join(match.get('matched_dates', match['dates'])) or '未記錄日期'
        people = '、'.join(match['attendees'])
        topic = f'「{match["topic"]}」｜' if 'topic' in match else ''
        lines.append(f'#{match["answer_id"]}｜{topic}{people}｜{dates}｜{match["result"]}')
    if len(matches) > MAX_RESULTS:
        lines.append(f'上方列出最近 {MAX_RESULTS} 筆，可再加上日期或參與者縮小範圍。')
    if plan['limited']:
        lines.append(f'這次查詢限於最近 {MAX_SCAN} 筆已保存的會議回答。')
    skipped = sum(row['sha256'] is None for row in plan['checked'])
    if skipped:
        lines.append(f'有 {skipped} 筆保存的會議回答無法核對。')
    if matches:
        lines.append('要更正其中一版，可用「沿用會議回答 #編號」指定來源。')
    answer = '\n'.join(lines)
    return answer, dict(schema=RECEIPTS[plan['schema']], plan=plan, output=answer, model_called=False)


def read(store, mid):
    with store.connect() as db:
        db.execute('BEGIN')
        rows = db.execute('''SELECT r.session_id, r.user_message_id,
            CASE WHEN length(CAST(c.payload AS BLOB))<=? THEN c.payload END AS payload
            FROM answer_runs r JOIN answer_contexts c ON c.message_id=r.answer_message_id
            WHERE r.answer_message_id=? LIMIT 2''', (local_answer_receipts.MAX_JSON_BYTES, mid)).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or not 0 < rows[0]['user_message_id'] < mid:
            raise ValueError('會議查詢缺少唯一且有效的回答流程。')
        row = rows[0]
        context = local_answer_receipts.load_context(row['payload'])
        if not isinstance(context, dict) or context.get('response_mode') != MODE or FIELD not in context:
            return None
        stored = context[FIELD]
        stored_plan = stored.get('plan') if isinstance(stored, dict) else None
        schema = stored_plan.get('schema') if isinstance(stored_plan, dict) else None
        if not isinstance(schema, str) or schema not in RECEIPTS:
            raise ValueError('會議查詢的紀錄版本無法核對。')

        def validate(receipt, delivered, question):
            plan = lookup(db, row['session_id'], question, row['user_message_id'], schema=schema)
            if plan is None:
                raise ValueError('會議查詢的原問無法核對。')
            answer, expected = evaluate(plan)
            if delivered != answer or not meeting.same_json(receipt, expected):
                raise ValueError('會議查詢與原始來源或交付不符。')
            return receipt

        return local_answer_receipts.checked_row(db, mid, context, MODE, FIELD, validate)


def describe(receipt):
    plan = receipt['plan']
    return '\n'.join((plan['request'], '', f'已檢查 {len(plan["checked"])} 筆保存的會議快照。', receipt['output']))
