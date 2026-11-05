"""Read-only earliest common slot in explicitly listed, same-day availability."""
import re

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


def parse(question):
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
    last = clauses.pop()
    duration = DURATION.fullmatch(last) or NATURAL_DURATION.fullmatch(last)
    if not duration:
        return None
    length_text = duration[2]
    length = LENGTHS[length_text] if length_text in LENGTHS else int(length_text[:-2].strip())
    if length > 1440:
        return None
    available, cancelled = {}, []
    for clause in clauses:
        if not available:
            clause = clause.removeprefix('今天')
        removal = CANCEL.fullmatch(clause)
        if removal:
            cut = cancelled_interval(removal)
            name = removal[1].strip('「」')
            if name not in available or not cut or len(cancelled) >= 24:
                return None
            cancelled.append(dict(name=name, interval=cut, source=clause))
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
    n = len(available)
    if n < 2 or (duration[1] and int(COUNT.get(duration[1], duration[1])) != n):
        return None
    plan = dict(schema='daily_availability_v1', available=available, cancelled=cancelled, duration=length)
    if conversational:
        plan['output'] = 'conversational'
    return plan


FOLLOWUP = re.compile(r'上一場會議[，,]\s*(.{1,512})[，,]\s*(?:請)?(?:重算|重新計算)(?:最早時段)?[。！]?')


def revise(plan, clause):
    """Change the detached prior snapshot, retaining unaffected attendees and constraints."""
    replacement = re.fullmatch(rf'({NATURAL_NAME})的(?:空檔|可用時段)改為\s*(.+)', clause)
    removal = CANCEL.fullmatch(clause)
    duration = re.fullmatch(r'(?:會議)?時長改為\s*([1-9][0-9]{0,3})\s*分鐘', clause)
    if duration:
        length = int(duration[1])
        if length > 1440:
            raise ValueError('請指定同一天內的會議時長（1 至 1440 分鐘）。')
        plan['duration'] = length
        return
    if not (replacement or removal):
        raise ValueError('請指明要更正哪位參與者（' + '、'.join(plan['available'])
                         + '）及其同日空檔；若要換日期，請重新列出當天每人的空檔。')
    name = (replacement or removal)[1].strip('「」')
    if name not in plan['available']:
        raise ValueError('上一場會議的參與者是' + '、'.join(plan['available']) + '。請指定其中一人，或重新提供完整明細。')
    if replacement:
        slots = slot_list(replacement[2])
        if not slots:
            raise ValueError('請列出這位參與者當天完整的新空檔，例如 13:00-14:30。')
        plan['available'][name] = slots
        plan['cancelled'] = [c for c in plan['cancelled'] if c['name'] != name]
    else:
        cut = cancelled_interval(removal)
        if not cut or len(plan['cancelled']) >= 24:
            raise ValueError('請提供有效的同日取消時段；累計超過 24 筆時，請重新列出空檔。')
        plan['cancelled'].append(dict(name=name, interval=cut, source=clause))


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
    # The day is bounded to 1,440 cells; overlap, adjacency and subtraction have
    # the same semantics regardless of the order or duplication of input ranges.
    common = bytearray([1]) * 1440
    effective = {}
    for name, intervals in plan['available'].items():
        mask = bytearray(1440)
        for a, b in intervals:
            mask[a:b] = bytes([1]) * (b-a)
        for cut in plan['cancelled']:
            if cut['name'] == name:
                a, b = cut['interval']
                mask[a:b] = bytes(b-a)
        effective[name] = list(map(display, ranges(mask)))
        common = bytearray(x & y for x, y in zip(common, mask))
    windows = list(ranges(common))
    length = plan['duration']
    first = next(((a, a + length) for a, b in windows if b - a >= length), None)
    answer = display(first) if first else '沒有可行時段'
    receipt = dict(schema='daily_meeting_calculation_v1', status='complete', model_called=False,
                   effective=effective, common=list(map(display, windows)), duration=length,
                   expression=f'共同空檔中最早連續 {length} 分鐘', result=dict(exact=answer, calculations=[]))
    return visible_answer(plan, receipt), receipt


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
