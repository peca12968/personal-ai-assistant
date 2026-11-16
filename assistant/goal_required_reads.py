"""Complete a user's explicitly named original-text reads before a decision."""
import re
from itertools import chain

from . import document_ranges, goal_sources
from .documents import matches_numbered_excerpt
from .goal_guidance_sources import standalone_clauses

GUIDANCE = ('required_source_reads來自使用者明確要求完整查讀的原文。readable是目前仍可取得的缺頁，'
            '先按next_read補讀，沿用同版已讀內容。若提早詢問或交付，系統會先補一個可查讀區段。'
            'unavailable是匯入副本實際缺少的範圍；只請提供缺的原文並保留可確認資料。'
            '完整查讀後若仍有歧義，再問會改變成果的問題。')

_VERB = r'(?:查讀|讀取|閱讀|讀)'
_FULL = rf'(?:完整{_VERB}|{_VERB}完)'
_FROM_START = rf'從(?:第一字|首字|頭)(?:開始)?(?:查讀|讀)到(?:最後一字|末字|行尾|尾)'
_THEN = r'(?:\s*(?:之?後\s*)?再(?:提問|定稿|交付|寫檔|回答))?'
_CURRENT = r'(?:請\s*)?(?:(?:本次|這次|本批)\s*)?'
_BEFORE = re.compile(rf'^{_CURRENT}(?:先\s*)?{_FULL}\s*(?P<target>.+?){_THEN}$')
_AFTER = re.compile(rf'^{_CURRENT}(?:先\s*)?(?:把|將)?\s*(?P<target>.+?)\s*(?:{_FROM_START}|{_FULL}){_THEN}$')
_WAIVE = re.compile(rf'^{_CURRENT}(?:不用|不必|無須)(?:再)?\s*{_FULL}\s*(?P<target>.+)$')
_LONG = re.compile(r'^第\s*([0-9]{1,12})\s*行(?:是|為)長行$')
_CONDITIONAL = re.compile(r'^(?:若|如果|假設|只要|有需要|引述|轉述|範例|例句)')
_COMMA = re.compile(r'')
_JOIN = re.compile(r'\s*(?:、|以及|與|和|及)\s*')
_DECISION_TOOLS = {'ask', 'finish', 'write_artifact', 'deliver'}


def _ranges(document_id, start, end):
    return {'document_id': document_id, 'start_line': start, 'end_line': end}


def _named_pattern(sources, *, include_missing=False):
    names = {}
    for row in sources:
        names.setdefault(row['name'], []).append(row['document_id'])
    if not names:
        return names, None
    labels = '|'.join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    missing = r'(?P<missing>(?:的\s*)?(?:匯入副本\s*)?(?:缺少|尚缺|缺漏)\s*)?' if include_missing else ''
    prefix = r'(?:D(?P<prefix>[1-9][0-9]{0,11})(?:\s+|(?=[「\x60])))?' if include_missing else ''
    shown = r'(?:[（(]\s*D(?P<shown>[1-9][0-9]{0,11})\s*[）)]\s*)?' if include_missing else ''
    pattern = re.compile(
        rf'(?<![A-Za-z0-9_.\\/]){prefix}(?:\x60|「)?(?P<name>{labels})(?:\x60|」)?\s*'
        + shown + r'(?:的\s*)?' + missing +
        r'第\s*(?P<first>[0-9]{1,12})\s*(?:(?:至|到|[-–])\s*(?:第\s*)?(?P<last>[0-9]{1,12})\s*)?行')
    return names, pattern


def _named_range(match, names):
    ids = names[match['name']]
    first, last = int(match['first']), int(match['last'] or match['first'])
    for key in ('prefix', 'shown'):
        if (shown := match.groupdict().get(key)) is not None:
            ids = [did for did in ids if did == int(shown)]
    return (ids[0], first, last) if len(ids) == 1 and 1 <= first <= last else None


def _explicit_targets(text, names, pattern):
    result = []
    start = 0
    while True:
        explicit = document_ranges.RANGES.match(text, start)
        named = pattern.match(text, start) if pattern else None
        if named and (explicit is None or named.end() > explicit.end()):
            match = named
            seen = _named_range(match, names)
            if seen is None:
                return []
        elif explicit:
            match = explicit
            ranges, _ = document_ranges.requested_ranges(match[0])
            row = ranges[0]
            seen = row['document_id'], row['start_line'], row['end_line']
            if seen[0] < 1 or not 1 <= seen[1] <= seen[2]:
                return []
        else:
            return []
        result.append(seen)
        if match.end() == len(text):
            return result
        connector = _JOIN.match(text, match.end())
        if connector is None:
            return []
        start = connector.end()


def _directives(text, names, pattern):
    clauses = standalone_clauses(text, comma_before=_COMMA)
    blocked = False
    previous_end = 0
    for index, (start, raw, question) in enumerate(clauses):
        if not any(mark in text[previous_end:start] for mark in ('，', ',')):
            blocked = False
        previous_end = start + len(raw)
        clause = raw.strip()
        blocked = blocked or bool(_CONDITIONAL.match(clause))
        if question or blocked:
            continue
        waiver = _WAIVE.fullmatch(clause)
        match = waiver or _BEFORE.fullmatch(clause) or _AFTER.fullmatch(clause)
        if match is None:
            continue
        target = match['target'].strip()
        if target == '該行' and index >= 2 and pattern:
            _, long_note, is_question = clauses[index - 1]
            _, anchor, anchor_question = clauses[index - 2]
            candidates = list(pattern.finditer(anchor))
            note = _LONG.fullmatch(long_note)
            if (not is_question and not anchor_question and not _CONDITIONAL.match(anchor)
                    and len(candidates) == 1 and note):
                named_range = _named_range(candidates[0], names)
                if named_range and named_range[1:] == (int(note[1]), int(note[1])):
                    yield bool(waiver), named_range
        else:
            for item in _explicit_targets(target, names, pattern):
                yield bool(waiver), item


def requirements(goal):
    return list(dict.fromkeys((did, low, high) for did, low, high, _ in bound_requirements(goal)))


def bound_requirements(goal):
    """Track which supplement supplies each range, including shared source lines."""
    supplements = [step for step in goal.get('steps', ()) if goal_sources.supplement_step(step)]
    initial = supplements[0]['action']['args']['original_sources'] if supplements else goal['sources']
    names, pattern = _named_pattern(initial)
    updates = [(row['notes_start'], row.get('steps_seen', 0), row['content']) for row in goal.get('guidance_sources', ())]
    updates.extend((row['notes_start'], row['ask_sequence'], row['answer']) for row in goal.get('reply_sources', ()))
    active = []
    bindings = {}
    remaining = iter(supplements)
    supplement = next(remaining, None)
    for _, sequence, source in [(-1, 0, goal['objective']), *sorted(updates), (0, float('inf'), '')]:
        while supplement is not None and supplement['sequence'] <= sequence:
            args = supplement['action']['args']
            if supplement['action']['tool'] == goal_sources.REQUIRED_SUPPLEMENT:
                previous = args.get('replaces')
                if previous is not None and (type(previous) is not int or previous not in bindings):
                    raise ValueError('保存的原文補件缺少被取代的對應。')
                missing = args['missing'] if previous is None else bindings[previous]
                active = _mapped_ranges(active, missing, args['replacement'], previous, supplement['sequence'])
                bindings[supplement['sequence']] = args['replacement']
            names, pattern = _named_pattern(goal_sources.merge_sources(args['original_sources'], args['added_sources']))
            supplement = next(remaining, None)
        for waive, (did, low, high) in _directives(source, names, pattern):
            if waive:
                active = [(doc, start, end, binding) for doc, first, last, binding in active
                          for start, end in (_missing([(low, high)], first, last) if doc == did else [(first, last)])]
            elif (did, low, high, None) not in active:
                active.append((did, low, high, None))
    return active


def _mapped_ranges(active, missing, replacement, previous, current):
    did, low, high = (missing[key] for key in document_ranges.BATCH_FIELDS)
    shift = replacement['start_line'] - low
    result = []
    for doc, start, end, binding in active:
        first, last = max(low, start), min(high, end)
        if binding != previous or doc != did or first > last:
            result.append((doc, start, end, binding))
            continue
        result.extend((doc, left, right, binding) for left, right in _missing([(low, high)], start, end))
        result.append((replacement['document_id'], first + shift, last + shift, current))
    return result


def _missing(covered, low, high):
    cursor = low
    for first, last in sorted(covered):
        if last < cursor:
            continue
        if first > high:
            break
        if first > cursor:
            yield cursor, first - 1
        cursor = last + 1
        if cursor > high:
            return
    if cursor <= high:
        yield cursor, high


def _coverage(excerpts, documents, originals, wanted):
    complete, pages = {}, {}
    for row in excerpts:
        did = row.get('document_id')
        if (did not in documents
                or row.get('sha256') != documents[did]['sha256']
                or row.get('name') != documents[did]['name']
                or not matches_numbered_excerpt(row, originals[did])
                or not any(start <= row['end_line'] and row['start_line'] <= end for start, end in wanted[did])):
            continue
        if row['partial_line']:
            pages.setdefault((did, row['start_line']), []).append((row['start_char'], row['end_char']))
        else:
            complete.setdefault(did, []).append((row['start_line'], row['end_line']))
    continuations = {}
    for (did, number), intervals in pages.items():
        gap = next(_missing(intervals, 1, len(originals[did][number - 1])), None)
        if gap:
            continuations[did, number] = gap[0]
        else:
            complete.setdefault(did, []).append((number, number))
    return complete, continuations


def _read_action(unread, continuations, originals):
    ranges, used = [], 0
    for did, first, last in unread:
        low, high = first, first - 1
        for number in range(first, min(last, first + document_ranges.MAX_LINES - 1) + 1):
            chars = len(originals[did][number - 1]) + len(f'L{number}: ')
            if (did, number) in continuations or chars > document_ranges.READ_CHAR_BUDGET:
                if not ranges and high < low:
                    return {'tool': 'read_line', 'args': {'document_id': did, 'line_number': number,
                            'start_char': continuations.get((did, number), 1)}}
                break
            if used + chars + 1 > document_ranges.READ_CHAR_BUDGET and (ranges or high >= low):
                break
            used += chars + 1
            high = number
        if high >= low:
            ranges.append(_ranges(did, low, high))
        if high < last or len(ranges) >= document_ranges.MAX_RANGES:
            break
    return {'tool': 'read_ranges', 'args': {'ranges': ranges}} if ranges else None


def progress(goal, documents):
    required = requirements(goal)
    if not required:
        return None
    wanted = {}
    for did, first, last in required:
        wanted.setdefault(did, []).append((first, last))
    snapshots = {row['id']: row for row in documents if row['id'] in wanted}
    originals = {did: row['content'].splitlines() for did, row in snapshots.items()}
    seen = (row for step in goal['steps'] if not step['observation'].get('error')
            for row in step['observation'].get('excerpts', ()))
    excerpts = chain(seen, goal.get('recheck_evidence', {}).get('excerpts', ()))
    covered, continuations = _coverage(excerpts, snapshots, originals, wanted)
    readable, unavailable = [], []
    for did, positions in wanted.items():
        count = len(originals.get(did, ()))
        first, last = min(low for low, _ in positions), max(high for _, high in positions)
        outside_requested = _missing(positions, first, last)
        for low, high in _missing([*covered.get(did, ()), *outside_requested], first, last):
            if low <= min(count, high):
                readable.append((did, low, min(count, high)))
            if max(low, count + 1) <= high:
                unavailable.append(_ranges(did, max(low, count + 1), high))
    if not readable and not unavailable:
        return None
    return {'readable': [_ranges(*row) for row in readable], 'unavailable': unavailable,
            'next_read': _read_action(readable, continuations, originals)}


def recovery(action, current):
    if not current or action['tool'] not in _DECISION_TOOLS:
        return None
    if read := current['next_read']:
        return {**read, 'note': '先補齊使用者明確要求查讀的原文。'}
    if action['tool'] != 'ask':
        raise ValueError('使用者指定的完整原文仍有缺行；請只詢問缺少的原文，或等待使用者明確取消該查讀要求。')
    return None
