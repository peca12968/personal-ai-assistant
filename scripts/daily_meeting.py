"""Read-only earliest common slot in explicitly listed, same-day availability."""
import re

CLOCK = r'(?:(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00)'
SPAN = re.compile(rf'({CLOCK})\s*[-–—至到]\s*({CLOCK})')
# Explicit aliases keep dates, qualifications and partial instructions out of the ledger.
NAME = r'(?:「[\u4e00-\u9fffA-Za-z0-9]{1,16}」|小[\u4e00-\u9fff]|[甲乙丙丁戊己庚辛]|[A-Za-z]{1,20})'
AVAILABILITY = re.compile(rf'({NAME})(原本)?(可開會的時段為|可開會[：:]|可用時段[為：:]|有空[：:]|為)\s*(.+)')
CANCEL = re.compile(rf'({NAME})(?:剛)?取消\s*(.+)')
DURATION = re.compile(r'(?:(兩|二|三|四|五|六|七|八|[2-8])人)?需要連續\s*([1-9][0-9]{0,3})\s*分鐘')
OUTPUT = re.compile(r'(?:請)?只(?:輸出|回答|回覆)最早可行的?(?:開始及結束時間|時段|時間區間)[，,]\s*格式\s*HH:MM-HH:MM')
COUNT = dict(zip('兩二三四五六七八', (2, 2, 3, 4, 5, 6, 7, 8)))


def minutes(clock):
    return int(clock[:2]) * 60 + int(clock[3:])


def span(text):
    match = SPAN.fullmatch(text)
    if match and minutes(match[1]) < minutes(match[2]):
        return [minutes(match[1]), minutes(match[2])]
    return None


def parse(question):
    if len(question) > 2048:
        return None
    lines = [s.strip() for s in re.split(r'[。\n]+', question.removesuffix('。'))]
    if len(lines) < 3 or not OUTPUT.fullmatch(lines[-1]):
        return None
    duration = DURATION.fullmatch(lines[-2])
    if not duration or int(duration[2]) > 1440:
        return None
    available, cancelled = {}, []
    for line in lines[:-2]:
        for clause in re.split(r'[，,；;]', line):
            clause = clause.strip()
            if not available:
                clause = clause.removeprefix('今天')
            removal = CANCEL.fullmatch(clause)
            if removal:
                cut = [0, 720] if removal[2] == '整個上午' else span(removal[2])
                name = removal[1].strip('「」')
                if name not in available or not cut or len(cancelled) >= 24:
                    return None
                cancelled.append(dict(name=name, interval=cut, source=clause))
                continue
            entry = AVAILABILITY.fullmatch(clause)
            if (not entry or len(available) >= 8
                    or (entry[3] == '為' and not available)):
                return None
            name = entry[1].strip('「」')
            if name in available:
                return None
            slots = [span(s.strip()) for s in re.split(r'[和與、]', entry[4])]
            if len(slots) > 12 or not all(slots):
                return None
            available[name] = slots
    n = len(available)
    if n < 2 or (duration[1] and int(COUNT.get(duration[1], duration[1])) != n):
        return None
    return dict(schema='daily_availability_v1', available=available, cancelled=cancelled, duration=int(duration[2]))


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
    return answer, receipt
