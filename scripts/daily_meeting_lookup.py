"""Find completed, source-verified meeting versions in the current conversation."""
from datetime import date
import re

from assistant import local_answer_receipts
from scripts import daily_meeting as meeting

FIELD = 'meeting_lookup'
CHOICE_FIELD = 'meeting_lookup_choice'
MODE = 'local_calculation'
PLAN_V1 = 'daily_meeting_lookup_plan_v1'
PLAN_V2 = 'daily_meeting_lookup_plan_v2'
PLAN_V3 = 'daily_meeting_lookup_plan_v3'
PLAN_V4 = 'daily_meeting_lookup_plan_v4'
PLAN_V5 = 'daily_meeting_lookup_plan_v5'
CURRENT_PLAN = PLAN_V5
RECEIPTS = {PLAN_V1: 'daily_meeting_lookup_receipt_v1', PLAN_V2: 'daily_meeting_lookup_receipt_v2',
            PLAN_V3: 'daily_meeting_lookup_receipt_v3', PLAN_V4: 'daily_meeting_lookup_receipt_v4',
            PLAN_V5: 'daily_meeting_lookup_receipt_v5'}
MAX_SCAN = 100
MAX_RESULTS = 10
REQUEST = re.compile(r'(?:請)?(?:查詢?|找)會議紀錄[，,：:]\s*(.{1,240}?)[。！!？?]?')
DATE_FILTER = re.compile(rf'(?:基準)?日期(?:是|為)?\s*({meeting.DATE})')
RANGE_FILTER = re.compile(r'(?:基準)?日期(?:範圍)?(?:是|為|從)?\s*(.+)')
DATE_RANGE = re.compile(rf'({meeting.DATE})\s*[至到～~]\s*({meeting.DATE})')
ATTENDEE_FILTER = re.compile(r'(?:參與者|人員)(?:是|為)?\s*(.+)')
PERSON = re.compile(r'「([\u4e00-\u9fffA-Za-z0-9]{1,16})」|([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]{0,19})')
TOPIC_FILTER = re.compile(rf'(?:會議)?主題(?:(?P<contains>包含|含有|含|有)|是|為)?\s*{meeting.TOPIC_TEXT}')
CHINESE_NUMBERS = '零一二三四五六七八九'
CHOICE = re.compile(r'(?:請)?(?:我選|選擇|選|用|查看|看)?\s*(?P<scope>會議(?:清單|紀錄)(?:中的?|的))?\s*'
                    rf'第\s*(?P<number>[0-9]{{1,2}}|十[{CHINESE_NUMBERS[1:]}]?|[{CHINESE_NUMBERS}])\s*'
                    r'(?:個|筆|項)(?:[。！!？?])?')


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


def parse(question, *, schema=CURRENT_PLAN):
    request = REQUEST.fullmatch(question) if isinstance(question, str) else None
    if not request or not isinstance(schema, str) or schema not in RECEIPTS:
        return None
    day, attendees, topic, ranges = None, [], None, []
    topic_mode = None
    supports_ranges = schema in (PLAN_V3, PLAN_V4, PLAN_V5)
    supports_contains = schema in (PLAN_V4, PLAN_V5)
    separator = r'[，,；;]' if schema == PLAN_V1 else r'[，,；;](?![^「]*」)'
    for clause in re.split(rf'\s*{separator}\s*', request[1]):
        if day is None and not ranges and (match := DATE_FILTER.fullmatch(clause)):
            try:
                day = date.fromisoformat(match[1]).isoformat()
            except ValueError:
                raise ValueError('請輸入有效的完整日期，例如 2028-02-29。') from None
        elif supports_ranges and day is None and not ranges and (match := RANGE_FILTER.fullmatch(clause)):
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
            contains, text = match.groups()
            if (contains and not supports_contains) or not meeting.valid_topic(text):
                return None
            topic = text
            topic_mode = 'contains' if contains else 'exact'
        else:
            return None
    criteria = dict(date=day, attendees=attendees)
    if schema != PLAN_V1:
        criteria['topic'] = topic
    if supports_ranges:
        criteria['date_ranges'] = ranges
    if supports_contains:
        criteria['topic_mode'] = topic_mode
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


def matches_topic(criteria, title):
    wanted = criteria.get('topic')
    if wanted is None:
        return True
    if criteria.get('topic_mode') == 'contains':
        return title is not None and wanted in title
    return wanted == title


def lookup(db, sid, question, before, cancel=None, *, schema=CURRENT_PLAN):
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
                and matches_topic(criteria, item.get('topic'))
                and set(criteria['attendees']).issubset(item['attendees'])):
            matches.append(dict(answer_id=mid, **item))
    return dict(schema=schema, request=question, criteria=criteria, checked=checked,
                matches=matches, limited=len(candidates) > MAX_SCAN)


def capture(store, sid, question, before=None, cancel=None, *, schema=CURRENT_PLAN):
    with store.connect() as db:
        db.execute('BEGIN')
        return lookup(db, sid, question, before, cancel, schema=schema)


def evaluate(plan):
    matches = plan['matches']
    criteria = plan['criteria']
    lines = ([f'找到 {len(matches)} 筆符合的會議回答。'] if matches
             else ['這段對話沒有符合條件的可核對會議回答。'])
    if criteria.get('topic_mode') == 'contains':
        lines.append(f'依保存的完整主題查找含有「{criteria["topic"]}」的版本。')
    if criteria.get('date_ranges'):
        lines.append('日期欄列出符合範圍的原候選日期，含起訖日。時段保留原回答。')
    elif criteria['date'] is not None:
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
        if plan['schema'] == PLAN_V5:
            lines.append('要查看某一筆，可依清單順序回「第一個」等選項。')
        lines.append('要更正其中一版，可用「沿用會議回答 #編號」指定來源。')
    answer = '\n'.join(lines)
    return answer, dict(schema=RECEIPTS[plan['schema']], plan=plan, output=answer, model_called=False)


def read_saved(db, mid, field, cancel=None):
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
    if not isinstance(context, dict) or context.get('response_mode') != MODE or field not in context:
        return None

    def validate(receipt, delivered, question):
        if field == CHOICE_FIELD:
            plan = choose(db, row['session_id'], question, row['user_message_id'], cancel)
            render = evaluate_choice
        else:
            stored = receipt.get('plan') if isinstance(receipt, dict) else None
            schema = stored.get('schema') if isinstance(stored, dict) else None
            if not isinstance(schema, str) or schema not in RECEIPTS:
                raise ValueError('會議查詢的紀錄版本無法核對。')
            plan = lookup(db, row['session_id'], question, row['user_message_id'], cancel, schema=schema)
            render = evaluate
        if plan is None:
            raise ValueError('會議查詢的原問無法核對。')
        answer, expected = render(plan)
        if delivered != answer or not meeting.same_json(receipt, expected):
            raise ValueError('會議查詢與原始來源或交付不符。')
        return receipt

    return local_answer_receipts.checked_row(db, mid, context, MODE, field, validate)


def read(store, mid, *, field=FIELD):
    with store.connect() as db:
        db.execute('BEGIN')
        return read_saved(db, mid, field)


def choose(db, sid, question, before, cancel=None):
    match = CHOICE.fullmatch(question) if isinstance(question, str) else None
    if not match:
        return None
    lookup_id = meeting.previous_id(db, sid, before)
    receipt = read_saved(db, lookup_id, FIELD, cancel)
    if receipt is None:
        if match['scope']:
            raise ValueError('請緊接在會議查詢結果後選擇，或指定會議回答編號。')
        return None
    number = match['number']
    if number.isascii():
        position = int(number)
    elif number.startswith('十'):
        position = 10 + CHINESE_NUMBERS.index(number[-1]) if len(number) > 1 else 10
    else:
        position = CHINESE_NUMBERS.index(number)
    source_plan = receipt['plan']
    shown = source_plan['matches'][:MAX_RESULTS]
    if not 1 <= position <= len(shown):
        if not shown:
            raise ValueError('上一則會議查詢沒有符合的版本，請調整查詢條件。')
        raise ValueError(f'上一則會議查詢列出 {len(shown)} 筆，請選清單中的順序。')
    selected = shown[position - 1]
    source = next(item for item in source_plan['checked'] if item['answer_id'] == selected['answer_id'])
    return dict(schema='daily_meeting_lookup_choice_plan_v1', request=question, lookup_answer_id=lookup_id,
                position=position, selected=selected, meeting_source=source)


def capture_choice(store, sid, question, before=None, cancel=None):
    with store.connect() as db:
        db.execute('BEGIN')
        return choose(db, sid, question, before, cancel)


def evaluate_choice(plan):
    selected = plan['selected']
    mid = selected['answer_id']
    topic = f'「{selected["topic"]}」' if 'topic' in selected else ''
    answer = '\n'.join((f'第 {plan["position"]} 筆是會議回答 #{mid}{topic}。',
                        f'原排程結果為 {selected["result"]}。',
                        f'要從這版重算，可以輸入「沿用會議回答 #{mid}，時長改為 30 分鐘，重算。」'))
    return answer, dict(schema='daily_meeting_lookup_choice_receipt_v1', plan=plan, output=answer, model_called=False)


def describe(receipt):
    plan = receipt['plan']
    if 'lookup_answer_id' in plan:
        return '\n'.join((plan['request'], '', f'依會議查詢回答 #{plan["lookup_answer_id"]} 的顯示順序。',
                          receipt['output']))
    return '\n'.join((plan['request'], '', f'已檢查 {len(plan["checked"])} 筆保存的會議快照。', receipt['output']))
