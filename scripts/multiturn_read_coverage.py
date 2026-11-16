"""Grade whether saved goal steps exposed the original lines required by a fixture."""
import hashlib

from assistant.documents import check_citations, matches_numbered_excerpt


def _files(case):
    return [*case.get('documents', ()), *([case['csv']] if 'csv' in case else [])]


def _lines(file):
    return file['content'].removeprefix('\ufeff').splitlines()


def validate(case, ranges):
    if not isinstance(ranges, list) or not ranges:
        raise ValueError('原文查讀規約至少需要一段文件行。')
    files = _files(case)
    seen = set()
    for row in ranges:
        if (not isinstance(row, dict) or row.keys() != {'doc', 'start_line', 'end_line'}
                or any(type(value) is not int for value in row.values())):
            raise ValueError('原文查讀規約須列明文件索引與開始、結束行。')
        doc, low, high = row['doc'], row['start_line'], row['end_line']
        key = (doc, low, high)
        if key in seen or not 0 <= doc < len(files) or not 1 <= low <= high <= len(_lines(files[doc])):
            raise ValueError('原文查讀範圍重複或超出合成文件。')
        seen.add(key)


def _observed_excerpts(steps, originals, *, artifact=None):
    excerpts = []
    for step in steps:
        action = step['action']
        observation = step.get('observation', {})
        if observation.get('error') or action['tool'] == 'invalid':
            continue
        if artifact and action['tool'] == 'write_artifact' and observation.get('artifact', {}).get('name') == artifact:
            break
        for excerpt in observation.get('excerpts', ()):
            source = originals.get(excerpt.get('document_id'))
            if (source and (excerpt.get('name'), excerpt.get('sha256')) == source[:2]
                    and matches_numbered_excerpt(excerpt, source[2])):
                excerpts.append(excerpt)
    return excerpts


def measure(case, state, ranges, *, artifact=None):
    files, sources = _files(case), state.get('sources', ())
    originals, citations = {}, {}
    for doc in {row['doc'] for row in ranges}:
        file = files[doc]
        if doc >= len(sources):
            continue
        source = sources[doc]
        digest = hashlib.sha256(file['content'].encode('utf-8')).hexdigest()
        did = source.get('document_id')
        if type(did) is int and (source.get('name'), source.get('sha256')) == (file['name'], digest):
            citations[doc] = did
            originals[did] = (file['name'], digest, _lines(file))
    excerpts = _observed_excerpts(state.get('steps', ()), originals, artifact=artifact)
    results = []
    for row in ranges:
        did = citations.get(row['doc'])
        if did is None:
            results.append(False)
            continue
        text = f"[D{did}:L{row['start_line']}-L{row['end_line']}]"
        result, = check_citations(text, excerpts, allow_joined_lines=True)
        results.append(result['range_valid'])
    return results
