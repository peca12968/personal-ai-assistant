"""The frozen document scope and explicit additions to a stopped goal."""
import re

from . import document_ranges


_RANGE = r'\[D[1-9][0-9]{0,11}:L[1-9][0-9]{0,11}(?:-L[1-9][0-9]{0,11})?\]'
_SUPPLEMENT = re.compile(rf'補件[ \t]+{_RANGE}(?:[ \t]+{_RANGE}){{0,7}}')


def supplement_ranges(content):
    if not _SUPPLEMENT.fullmatch(content):
        raise ValueError('請用「補件 [D5:L1-L2]」指定一至八個完整規則範圍。')
    ranges, _ = document_ranges.requested_ranges(content)
    document_ranges.validate_batch({'ranges': ranges})
    if len(ranges) != len(re.findall(_RANGE, content)):
        raise ValueError('每個補件範圍只列一次。')
    if any(row['end_line'] < row['start_line'] for row in ranges):
        raise ValueError('補件結束行不得早於起始行。')
    count = sum(row['end_line'] - row['start_line'] + 1 for row in ranges)
    if count > 8:
        raise ValueError('補件可選一至八條不同的完整規則行。')
    lines = {(row['document_id'], line) for row in ranges
             for line in range(row['start_line'], row['end_line'] + 1)}
    if count != len(lines):
        raise ValueError('補件可選一至八條不同的完整規則行。')
    return ranges


def _supplement_step(step):
    return step['action']['tool'] == 'check_decision' and 'report_supplement' in step['action']['args']


def has_supplement(steps):
    return any(_supplement_step(step) for step in steps)


def saved_supplement(db, goal_id):
    return db.execute("""SELECT 1 FROM autonomous_steps WHERE goal_id=?
        AND json_extract(action_json, '$.tool')='check_decision'
        AND json_extract(action_json, '$.args.report_supplement') IS NOT NULL LIMIT 1""", (goal_id,)).fetchone() is not None


def matching_scope(stored, current, *, scoped=False):
    return current_scope(stored, current, scoped=scoped) == stored


def current_scope(stored, current, *, scoped):
    if not scoped:
        return current
    ids = {source['document_id'] for source in stored}
    return [source for source in current if source['document_id'] in ids]


def valid_source_list(sources):
    return (isinstance(sources, list) and len(sources) <= 8 and all(
        isinstance(row, dict) and row.keys() == {'document_id', 'name', 'sha256'}
        and type(row['document_id']) is int and 1 <= row['document_id'] < 10**12
        and isinstance(row['name'], str) and bool(row['name'])
        and isinstance(row['sha256'], str) and re.fullmatch('[a-f0-9]{64}', row['sha256'])
        for row in sources) and [row['document_id'] for row in sources] ==
        sorted({row['document_id'] for row in sources}))


def merge_sources(previous, added):
    combined = sorted([*previous, *added], key=lambda row: row['document_id'])
    if not valid_source_list(combined):
        raise ValueError('補件須增加新文件，並保留原文件及版本。')
    return combined


def verify_excerpts(excerpts, sources, ranges):
    if not isinstance(excerpts, list) or len(excerpts) != len(ranges):
        raise ValueError('補件查讀與指定範圍不一致。')
    by_id = {row['document_id']: row for row in sources}
    for excerpt, request in zip(excerpts, ranges, strict=True):
        source = by_id.get(request['document_id'], {})
        if (not isinstance(excerpt, dict)
                or excerpt.keys() != {'document_id', 'start_line', 'end_line', 'text', 'name', 'sha256',
                                       'partial_line', 'matched_query', 'requested_range'}
                or any(excerpt[key] != value for key, value in request.items())
                or excerpt['sha256'] != source.get('sha256') or excerpt['name'] != source.get('name')
                or excerpt['partial_line'] is not False or excerpt['matched_query'] is not False
                or excerpt['requested_range'] is not True or not isinstance(excerpt['text'], str)
                or not 1 <= len(excerpt['text']) <= 10000):
            raise ValueError('補件須引用已納入範圍的完整原文。')


def verify_history(steps, final_scope, documents):
    """Replay source additions, including comparisons to still-present original bytes."""
    first = next((step for step in steps if _supplement_step(step)), None)
    if first is None:
        return {}
    current = first['action']['args']['original_sources']
    by_sequence = {}
    for step in steps:
        args = step['action']['args']
        if _supplement_step(step):
            if args['original_sources'] != current:
                raise ValueError('保存的補件前後文件範圍不一致。')
            current = merge_sources(current, args['added_sources'])
            for excerpt in step['observation']['excerpts']:
                imported = documents.get(excerpt['document_id'])
                if imported is None or (imported['sha256'], imported['name']) != (excerpt['sha256'], excerpt['name']):
                    continue
                request = {name: excerpt[name] for name in document_ranges.BATCH_FIELDS}
                observed = document_ranges.read_batch([imported], {'ranges': [request]})
                if observed['excerpts'] != [excerpt]:
                    raise ValueError('補件原行與保存的匯入副本不一致。')
        by_sequence[step['sequence']] = current
    if current != final_scope:
        raise ValueError('目前目標文件範圍缺少相符的補件紀錄。')
    return by_sequence
