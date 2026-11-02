"""Bounded civil-date calculations; no holiday service or calendar mutations."""
import re
from datetime import date, datetime, timedelta, timezone

MAX_DAYS = 3660
WEEKDAYS = ('星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日')
GUIDANCE = '''date_math是唯讀日期工具，精確args依operation選擇：
{"operation":"today"}：讀取電腦本地日期、星期、含UTC偏移的時間及觀察時間。這是本機時鐘，不是使用者所在地或已校準的網路時間；過往today紀錄是歷史，接續跨日工作若需要現在日期需重新讀取。使用者明確指定的日期／時區優先；其他時區不能拿本地日期冒充。
{"operation":"weekday","date":"2026-12-24"}：取得日期與星期。
{"operation":"add","date":"2026-12-24","days":5,"calendar":"weekdays","excluded_dates":["2026-12-25"]}：從起日之後起算，正數向後、負數向前，0保留原日期。每次最多正負3660個計數日。
{"operation":"between","start":"2026-12-24","end":"2027-01-04","calendar":"calendar","excluded_dates":[]}：沿起日走向迄日，不含起日、包含迄日，回傳帶方向的日數；相同日期為0，相隔最多3660日曆天。反向的工作日計數因端點不同不保證正好互為負數。
calendar只能是calendar（日曆天，每天都算且excluded_dates必須空）或weekdays（只算週一至週五，另排除excluded_dates明列日期）。排除日期最多366個、不重複，全部日期須為有效YYYY-MM-DD。
工具不內建任何地區國定假日、補班、公司行事曆或農曆，也不建立行事曆事件、鬧鐘或提醒。一般工作日可明示採週一至週五；使用者要求地區／公司實際工作日而假日資料不明，需先取得規則或詢問，不能猜清單或悄悄忽略。若要求包含起日或不同起算規則，需依要求轉換並說明，不能把工具固定規則冒充使用者規則。成果區分工具計算、排程建議與已執行事項；日期工具成功不是整個排程已被確認。'''


def parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('日期須為YYYY-MM-DD。')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError('日期不存在或超出0001-01-01至9999-12-31。') from None


def validate(args):
    fields = {'today': {'operation'}, 'weekday': {'operation', 'date'},
              'add': {'operation', 'date', 'days', 'calendar', 'excluded_dates'},
              'between': {'operation', 'start', 'end', 'calendar', 'excluded_dates'}}
    if not isinstance(args, dict) or not isinstance(args.get('operation'), str) or args['operation'] not in fields or set(args) != fields[args['operation']]:
        raise ValueError('日期工具運算或參數不符。')
    op = args['operation']
    for key in ('date', 'start', 'end'):
        if key in args:
            parse_date(args[key])
    if op == 'add' and (type(args['days']) is not int or abs(args['days']) > MAX_DAYS):
        raise ValueError('日期加減須為正負3660內的整數。')
    if op == 'between' and abs((parse_date(args['end']) - parse_date(args['start'])).days) > MAX_DAYS:
        raise ValueError('日期間隔最多3660日曆天。')
    if op in ('add', 'between'):
        if args['calendar'] not in ('calendar', 'weekdays'):
            raise ValueError('日期計數僅支援calendar或weekdays。')
        excluded = args['excluded_dates']
        if not isinstance(excluded, list) or len(excluded) > 366:
            raise ValueError('排除日期須為最多366項的清單。')
        for item in excluded:
            parse_date(item)
        if len(set(excluded)) != len(excluded):
            raise ValueError('排除日期不能重複。')
        if args['calendar'] == 'calendar' and excluded:
            raise ValueError('日曆天不能另外排除日期，請明確選擇計數規則。')


def describe(value):
    return {'date': value.isoformat(), 'weekday_iso': value.isoweekday(),
            'weekday': WEEKDAYS[value.weekday()]}


def calculate(args):
    validate(args)
    op = args['operation']
    if op == 'today':
        observed = datetime.now(timezone.utc)
        local = observed.astimezone()
        return {'operation': op, **describe(local.date()), 'local_datetime': local.isoformat(),
                'observed_at_utc': observed.isoformat(), 'timezone_source': 'computer_local',
                'utc_offset_seconds': int(local.utcoffset().total_seconds())}
    if op == 'weekday':
        return {'operation': op, **describe(parse_date(args['date']))}
    start = parse_date(args['date'] if op == 'add' else args['start'])
    end = parse_date(args['end']) if op == 'between' else None
    amount = args['days'] if op == 'add' else (end - start).days
    direction = 1 if amount >= 0 else -1
    excluded = {parse_date(item) for item in args['excluded_dates']}
    current, counted, weekends, exclusions = start, 0, 0, 0
    # At most 3660 counted weekdays plus 366 exclusions; no unbounded input loop.
    try:
        while (abs(counted) < abs(amount) if op == 'add' else current != end):
            current += timedelta(days=direction)
            if args['calendar'] == 'weekdays' and current.weekday() >= 5:
                weekends += 1
            elif args['calendar'] == 'weekdays' and current in excluded:
                exclusions += 1
            else:
                counted += direction
    except OverflowError:
        raise ValueError('計算結果超出0001-01-01至9999-12-31。') from None
    return {'operation': op, 'start': describe(start), 'end': describe(current),
            'counted_days': counted, 'calendar_days': (current - start).days,
            'calendar': args['calendar'], 'excluded_dates': sorted(args['excluded_dates']),
            'start_included': False, 'end_included': current != start,
            'skipped_weekend_days': weekends, 'skipped_excluded_weekdays': exclusions,
            'holiday_source': 'explicit_excluded_dates_only'}
