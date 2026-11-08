"""Frozen TZif rules and local wall-time conversions for named meeting zones."""
import base64
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from importlib.resources import files
from io import BytesIO
import re
import struct
from zoneinfo import ZoneInfo

IANA_ZONE = r'[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)+'
MAX_TZIF_BYTES = 32768


def snapshot(keys):
    try:
        import tzdata
    except ImportError:
        raise ValueError('命名時區需要 tzdata，請用 Start.cmd 安裝專案依賴。') from None
    directory = files('tzdata.zoneinfo')
    zones = {}
    for key in sorted(keys):
        try:
            with directory.joinpath(*key.split('/')).open('rb') as source:
                data = source.read(MAX_TZIF_BYTES + 1)
        except (FileNotFoundError, IsADirectoryError):
            raise ValueError(f'找不到時區 {key}，請使用完整的 IANA 名稱。') from None
        if not data.startswith(b'TZif') or len(data) > MAX_TZIF_BYTES:
            raise ValueError(f'無法讀取時區 {key} 的規則。')
        zones[key] = dict(sha256=sha256(data).hexdigest(), tzif=base64.b64encode(data).decode('ascii'))
    return dict(version=tzdata.__version__, zones=zones)


def check_tzif(data):
    def block_end(start, timestamp_width):
        if start + 44 > len(data) or data[start:start + 4] != b'TZif':
            raise ValueError
        utc, standard, leaps, times, types, names = struct.unpack_from('>6I', data, start + 20)
        end = (start + 44 + times * (timestamp_width + 1) + types * 6 + names
               + leaps * (timestamp_width + 4) + standard + utc)
        if not 1 <= types <= 256 or end > len(data):
            raise ValueError
        return end

    first = block_end(0, 4)
    if data[4:5] in (b'2', b'3', b'4'):
        block_end(first, 8)
    elif data[4:5] != b'\x00':
        raise ValueError


def load_rules(saved, keys):
    if (not isinstance(saved, dict) or set(saved) != {'version', 'zones'}
            or not isinstance(saved['version'], str) or not re.fullmatch(r'[0-9]{4}\.[0-9]{1,3}', saved['version'])
            or not isinstance(saved['zones'], dict) or set(saved['zones']) != keys):
        raise ValueError('會議的時區規則來源已變更。')
    zones = {}
    for key, entry in saved['zones'].items():
        if (not isinstance(entry, dict) or set(entry) != {'sha256', 'tzif'} or not isinstance(entry['tzif'], str)
                or len(entry['tzif']) > 4 * ((MAX_TZIF_BYTES + 2) // 3)):
            raise ValueError(f'時區 {key} 的保存規則不完整。')
        try:
            data = base64.b64decode(entry['tzif'], validate=True)
            if len(data) > MAX_TZIF_BYTES or sha256(data).hexdigest() != entry['sha256']:
                raise ValueError
            check_tzif(data)
            zones[key] = ZoneInfo.from_file(BytesIO(data), key=key)
        except (ValueError, OSError, IndexError, AssertionError, OverflowError, struct.error):
            raise ValueError(f'時區 {key} 的保存規則無法核對。') from None
    return zones


def utc_minute(day, minute, zone, label, occurrence=None):
    try:
        local = datetime.combine(date.fromisoformat(day), time()) + timedelta(minutes=minute)
        matches = set()
        for fold in (0, 1):
            utc = local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) == local:
                matches.add(utc)
    except (ValueError, OverflowError):
        raise ValueError(f'{label} 的時間超出可換算的日期。') from None
    if not matches or (len(matches) > 1 and occurrence is None):
        reason = '不存在的當地時間' if not matches else '重複的當地時間'
        hint = '請指定可辨認的時段' if not matches else '請在時刻後標註「（第一次）」或「（第二次）」'
        raise ValueError(f'{label} 的 {local:%Y-%m-%d %H:%M} 是{reason}，{hint}。')
    if occurrence is not None and len(matches) != 2:
        raise ValueError(f'{label} 的 {local:%Y-%m-%d %H:%M} 只出現一次，請移除出現次序。')
    instant = sorted(matches)[1 if occurrence == 2 else 0]
    if instant.second or instant.microsecond:
        raise ValueError(f'{label} 的時區偏移精確到秒，請改用可用分鐘表示的時段。')
    return instant.toordinal() * 1440 + instant.hour * 60 + instant.minute


def utc_intervals(day, intervals, zone, label):
    for slot in intervals:
        if isinstance(slot, dict):
            left = utc_minute(day, slot['start'], zone, label, slot['start_choice'])
            right = utc_minute(day, slot['end'], zone, label, slot['end_choice'])
        else:
            start, end = slot
            left = utc_minute(day, start, zone, label)
            right = utc_minute(day, end, zone, label)
        if left >= right:
            raise ValueError(f'{label} 的空檔經換算後須為正向時段。')
        yield left, right


def local_clock(instant, zone, day):
    ordinal, minute = divmod(instant, 1440)
    utc = datetime.combine(date.fromordinal(ordinal), time(), tzinfo=timezone.utc) + timedelta(minutes=minute)
    local = utc.astimezone(zone)
    offset = local.strftime('%z')
    if len(offset) != 5 or local.second or local.microsecond:
        raise ValueError('基準時區的換算結果無法用整分鐘表示。')
    if local.date() == day:
        clock = f'{local:%H:%M}'
    elif local.date() == day + timedelta(days=1) and local.time() == time():
        clock = '24:00'
    else:
        raise ValueError('基準時區的換算結果超出同一日期。')
    return f'{clock} UTC{offset[:3]}:{offset[3:]}'
