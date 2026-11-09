"""Bounded status vocabulary for an explicitly scoped object observation."""
import re

NAME = r'[㐀-鿿A-Za-z0-9][㐀-鿿A-Za-z0-9_ -]{0,23}'
NAME_CHECK = re.compile(NAME)
REPORT = re.compile(rf'確認\s*(?:「(?P<quoted>{NAME})」的?|(?P<bare>{NAME})的)(?P<items>.+)')
REFERENCE_SUBJECTS = ('它', '該物件', '同一物件')
REFERENCE = re.compile(rf'(?:確認\s*)?(?:{"|".join(REFERENCE_SUBJECTS)})的')
PENDING = re.compile(r'(.+?)(?:尚未核對|還沒核對)')
AMBIGUOUS = re.compile(r'的|可能|似乎|看起來|大概|疑似|或許|據說|若|但是|然而|未$|不$|已$|仍$|還$|沒$|沒有$|並非$')
FAMILIES = (
    ('complete', ('完整', '完好'), ('不完整',)),
    ('register', ('已入冊',), ('未入冊', '沒有入冊')),
    ('record', ('已登錄',), ('未登錄', '沒有登錄')),
    ('closed', ('已關閉', '確定關閉', '關著'), ('已開啟', '還開著', '仍開著', '開著')),
    ('clasped', ('已扣緊', '扣緊'), ('未扣緊', '尚未扣緊', '還沒扣緊', '沒有扣緊', '仍鬆開', '已鬆開')),
    ('secured', ('已鎖緊', '鎖緊'), ('未鎖緊', '尚未鎖緊', '還沒鎖緊', '沒有鎖緊', '仍未鎖緊')),
    ('dark', ('已熄滅', '熄滅', '熄了'), ('還亮著', '仍亮著', '亮著', '已亮')),
    ('stamped', ('已蓋章', '已有章', '有章'), ('未蓋章', '沒有章')),
)
STATUS = {word: (family, truth) for family, yes, no in FAMILIES
          for truth, words in ((True, yes), (False, no)) for word in words}
SUFFIXES = tuple(sorted(STATUS, key=len, reverse=True))
CANONICAL = {family: yes[0] for family, yes, _ in FAMILIES}


def field_name(value):
    return value == value.strip() and NAME_CHECK.fullmatch(value) and not AMBIGUOUS.search(value)


def property_status(value):
    suffix = next((word for word in SUFFIXES if value.endswith(word)), None)
    if suffix is None:
        return None
    field = value[:-len(suffix)]
    if not field_name(field) or (suffix == '完好' and not field.endswith('封口')):
        return None
    family, truth = STATUS[suffix]
    return field, family, truth


def observation(body):
    match = REPORT.fullmatch(body)
    if match is None:
        return None
    subject = match['quoted'] or match['bare']
    if subject != subject.strip() or (match['bare'] and '的' in subject):
        return None
    items = observation_items(match['items'])
    return (subject, items) if items else None


def reference_items(body):
    match = REFERENCE.match(body)
    return observation_items(body[match.end():]) if match else None


def observation_items(text):
    phrases = [part.strip() for part in re.split('[、，,]', text)]
    if not 1 <= len(phrases) <= 6:
        return None
    result = []
    for phrase in phrases:
        pending = PENDING.fullmatch(phrase)
        status = property_status(pending[1] if pending else phrase)
        if status:
            field, family, value = status
            result.append((field, family, None if pending else value))
        elif pending and field_name(pending[1]):
            result.append((pending[1], None, None))
        else:
            return None
    return result
