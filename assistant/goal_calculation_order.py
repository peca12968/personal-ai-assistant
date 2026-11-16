"""Recover the order of explicitly cited options in the question the user answered."""
import re
import unicodedata

from scripts.daily_dialogue import CHINESE_NUMBERS, ordinal_position


_NUMBER = rf'[0-9]{{1,2}}|十[{CHINESE_NUMBERS[1:]}]?|[{CHINESE_NUMBERS}]'
_POSITION = re.compile(rf'第\s*(?P<number>{_NUMBER})\s*(?:個|筆|份|版|項|個選項|個來源)?')
_LAST = re.compile(r'最後(?:(?:那|這|一)?(?:個|筆|份|版|項|個選項|個來源))?')
_MARKER = re.compile(rf'^\s*(?:[-*]\s+)?(?:(?P<number>{_NUMBER})[.．、)]|'
                     rf'[（(](?P<wrapped>{_NUMBER})[）)])\s*')
_LABEL = r'(?:「[^「」\[\]\r\n]{1,80}」|[^\s「」\[\]。！？、，：;；]{1,40})?'
_SEPARATOR = re.compile(rf'\s*(?:還是|或(?:者)?|、\s*(?:還是)?|／)\s*(?:採用|用|改用)?\s*{_LABEL}\s*')
_TAIL = re.compile(r'\s*(?:為準|呢|這一份|這份|哪一份)?\s*')
_ASK = re.compile(r'選(?:擇|用|哪|出|一)|採(?:用|哪)|改(?:用|採)|以.{0,40}為準|要用|確認.{0,35}(?:來源|版本|哪)')
_REORDER = re.compile(r'重列|改列|換列|重排|調換順序|順序改|排序改|更換候選|調整候選|加(?:入|列).{0,12}候選')
_PREFIX = re.compile(r'^(?:我選|改選|選擇|就是|確認選|是)\s*')
_NONOPERATIVE = re.compile(r'如果|假如|假設|試算|暫定|先別|不要|若要')
_QUOTED_LABEL = re.compile(r'「[^「」]*」|『[^『』]*』|“[^“”]*”|"[^"]*"')


def parse(text):
    word = _PREFIX.sub('', unicodedata.normalize('NFKC', text))
    if match := _POSITION.fullmatch(word):
        return ('position', ordinal_position(match))
    if _LAST.fullmatch(word):
        return ('last', None)
    return None


def _record(match, shift):
    return {
        'citation': match[0],
        'question_start': shift + match.start(),
        'question_end': shift + match.end(),
    }


def _unique(rows):
    return 2 <= len(rows) <= 8 and len({row['citation'] for row in rows}) == len(rows)


def _numbered(question, line_pattern):
    numbered = []
    offset = 0
    for index, raw in enumerate(question.splitlines(keepends=True)):
        line = unicodedata.normalize('NFKC', raw[:12])
        marker = _MARKER.match(line)
        if marker:
            matches = list(re.finditer(line_pattern, raw))
            if len(matches) != 1:
                return None
            match = matches[0]
            # Numeric marker characters are one codepoint in either width.
            if match.start() < marker.end() or not re.fullmatch(r'\s*[。？?]?\s*', raw[match.end():]):
                return None
            number = marker['number'] or marker['wrapped']
            numbered.append((index, ordinal_position({'number': number}), offset, _record(match, offset)))
        offset += len(raw)
    if not numbered:
        return []
    positions = [index for index, _, _, _ in numbered]
    orders = [number for _, number, _, _ in numbered]
    header = question[:numbered[0][2]]
    rows = [row for _, _, _, row in numbered]
    if (not _ASK.search(header) or orders != list(range(1, len(rows) + 1))
            or positions != list(range(positions[0], positions[0] + len(rows))) or not _unique(rows)):
        return None
    tail = question[rows[-1]['question_end']:]
    if not re.fullmatch(r'\s*[。？?]?\s*(?:請(?:選擇|選用|選)一(?:份|筆|項)[。？?]?\s*)?', tail):
        return None
    return rows


def _alternatives(question, line_pattern, clauses):
    lists = []
    bits = clauses(question)
    for offset, part, is_question in bits:
        matches = list(re.finditer(line_pattern, part))
        openers = [text for earlier, text, flag in bits if flag and earlier < offset]
        if (len(matches) < 2 or _NONOPERATIVE.search(_QUOTED_LABEL.sub('', part))
                or any(flag and later > offset for later, _, flag in bits)
                or len(openers) > 1
                or any(not _ASK.search(text) or re.search(line_pattern, text) for text in openers)
                or not (_ASK.search(part) or openers) or not (is_question or openers)):
            continue
        if not all(_SEPARATOR.fullmatch(part[left.end():right.start()])
                   for left, right in zip(matches, matches[1:])) or not _TAIL.fullmatch(part[matches[-1].end():]):
            continue
        rows = [_record(match, offset) for match in matches]
        if _unique(rows):
            lists.append(rows)
    if len(lists) != 1:
        return []
    return lists[0]


def options(question, line_pattern, clauses):
    """Return ordered citation spans, excluding citations in background text."""
    numbered = _numbered(question, line_pattern)
    if numbered is None:
        return []
    if numbered:
        before = question[:numbered[0]['question_start']]
        if any(flag and not _ASK.search(text) for _, text, flag in clauses(before)):
            return []
        return numbered
    return _alternatives(question, line_pattern, clauses)


def source_list(question, line_pattern):
    if not _ASK.search(question):
        return False
    cited = len(list(re.finditer(line_pattern, question))) >= 2
    return (cited and ('\n' in question or '還是' in question or '或' in question)
            or bool(re.search(r'哪(?:一)?(?:份|版|個來源|個版本|個文件)', question)))


def source_prompt(question, clauses):
    asked = [part for _, part, flag in clauses(question) if flag and _ASK.search(part)]
    return asked[0] if len(asked) == 1 else question


def reordered(content, clauses):
    for _, text, is_question in clauses(content):
        unquoted = _QUOTED_LABEL.sub('', text)
        if not is_question and _REORDER.search(unquoted) and not _NONOPERATIVE.search(unquoted):
            return True
    return False
