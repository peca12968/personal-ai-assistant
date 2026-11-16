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


# V5 options retain their displayed text when an original citation is omitted.
_NAMED_HEAD = re.compile(
    r'(?:(?:請(?:先)?)?(?:要)?(?:採用|改採|改用|選用|選擇|要用)|以)'
    r'\s*(?:哪(?:一)?(?:份|筆|版|個來源|個版本|個文件)(?:文件|來源|版本|紀錄|資料)?'
    r'(?:為準)?\s*)?[：:]?\s*'
)
_SIMPLE_HEAD = re.compile(r'(?:用|採)\s*')
_NAMED_COLON = re.compile(r'[:：]\s*')
_NAMED_SEPARATOR = re.compile(r'還是|或者|或|、|／')
_NAMED_QUOTED = re.compile(r'「(?P<name>[^「」\[\]\r\n]{1,80})」')
_NAMED_PLAIN = re.compile(r'[^「」『』“”"\'\[\]\r\n。！？?!，,、；;：:]{1,80}')
_NAMED_SUFFIX = re.compile(r'\s*(?:為準|呢|這一份|這份|哪一份)$')
_NAMED_TRAILING = re.compile(
    r'\s*[。？?]?\s*(?:請(?:選擇|選用|選)一(?:份|筆|項)[。？?]?\s*)?'
)


def _trim_span(fragment, offset):
    stripped = fragment.strip()
    return stripped, offset + len(fragment) - len(fragment.lstrip())


def _named_option(fragment, offset, line_pattern):
    fragment, offset = _trim_span(fragment, offset)
    fragment = fragment.rstrip('。？?').rstrip()
    if not fragment:
        return None
    matches = list(re.finditer(line_pattern, fragment))
    if len(matches) > 1 or matches and matches[0].end() != len(fragment):
        return None
    citation = matches[0][0] if matches else None
    label = fragment[:matches[0].start()].strip() if matches else fragment
    if quoted := _NAMED_QUOTED.fullmatch(label):
        name = quoted['name'].strip()
        if not name:
            return None
    elif label and _NAMED_PLAIN.fullmatch(label):
        name = label
    elif not label and citation:
        name = None
    else:
        return None
    return {'name': name, 'displayed_citation': citation,
            'question_start': offset, 'question_end': offset + len(fragment)}


def _trial_question(question, offset, part):
    prefix = re.split(r'[。！？!?；;\r\n]', question[:offset])[-1]
    unquoted = _QUOTED_LABEL.sub('', prefix + part)
    return bool(_NONOPERATIVE.search(unquoted) or unquoted.lstrip().startswith(('若', '倘若', '要是')))


def _named_numbered(question, line_pattern, clauses):
    offset, numbered = 0, []
    for index, raw in enumerate(question.splitlines(keepends=True)):
        marker = _MARKER.match(unicodedata.normalize('NFKC', raw[:12]))
        if marker:
            option = _named_option(raw[marker.end():], offset + marker.end(), line_pattern)
            if option is None:
                return None
            number = ordinal_position({'number': marker['number'] or marker['wrapped']})
            numbered.append((index, number, offset, option))
        offset += len(raw)
    if not numbered:
        return []
    positions = [index for index, _, _, _ in numbered]
    order = [number for _, number, _, _ in numbered]
    header = question[:numbered[0][2]]
    rows = [option for _, _, _, option in numbered]
    if (not 2 <= len(rows) <= 8 or order != list(range(1, len(rows) + 1))
            or positions != list(range(positions[0], positions[0] + len(rows))) or not _ASK.search(header)
            or any(flag and not _ASK.search(text) or _ASK.search(text) and _trial_question(header, offset, text)
                   for offset, text, flag in clauses(header))
            or not _NAMED_TRAILING.fullmatch(question[rows[-1]['question_end']:])):
        return None
    return rows


def _inline_spans(part, line_pattern):
    token = re.compile(rf'{_QUOTED_LABEL.pattern}|{line_pattern}|{_NAMED_SEPARATOR.pattern}')
    start = 0
    result = []
    for match in token.finditer(part):
        if _NAMED_SEPARATOR.fullmatch(match[0]):
            if not part[start:match.start()].strip():
                return []
            result.append((start, match.start()))
            start = match.end()
    if not result:
        return []
    result.append((start, len(part)))
    return result


def _option_start(part, first_end, opener):
    intro = part[:first_end]
    # Ignore colons inside a citation or a quoted document name.
    hidden = re.compile(rf'{_QUOTED_LABEL.pattern}|\[D\d+:L\d+\]')
    masked = hidden.sub(lambda match: ' ' * len(match[0]), intro)
    if _ASK.search(masked) and (colons := list(_NAMED_COLON.finditer(masked))):
        return min(colons[-1].end(), len(intro))
    header_text = masked.rstrip()
    header = _NAMED_HEAD.search(header_text) or _SIMPLE_HEAD.search(header_text)
    return header.end() if header else 0 if opener else None


def _named_alternatives(question, line_pattern, clauses):
    bits, found = clauses(question), []
    for offset, part, is_question in bits:
        spans = _inline_spans(part, line_pattern)
        if not 2 <= len(spans) <= 8 or _trial_question(question, offset, part):
            continue
        openers = [(earlier, text) for earlier, text, flag in bits if flag and earlier < offset]
        if (len(openers) > 1 or any(not _ASK.search(text) or re.search(line_pattern, text)
                or _trial_question(question, earlier, text) for earlier, text in openers)
                or any(flag and later > offset for later, _, flag in bits) or not (is_question or openers)):
            continue
        start = _option_start(part, spans[0][1], bool(openers))
        if start is None:
            continue
        spans[0] = (start, spans[0][1])
        left, right = spans[-1]
        if tail := _NAMED_SUFFIX.search(part[left:right]):
            spans[-1] = (left, left + tail.start())
        options = [_named_option(part[left:right], offset + left, line_pattern) for left, right in spans]
        if all(option is not None for option in options):
            found.append(options)
    return found[0] if len(found) == 1 else []


def named_options(question, line_pattern, clauses):
    """Return the complete displayed list, including options without citations."""
    numbered = _named_numbered(question, line_pattern, clauses)
    return (numbered or _named_alternatives(question, line_pattern, clauses)) if numbered is not None else []
