"""Verify a complete field lookup across saved, source-bound pages."""
from bisect import bisect_left

from . import document_ranges
from .goal_stages import digest


_HIT_FIELDS = {'line_number', 'match_start_char', 'match_end_char', 'line_chars', 'full_line_provided'}
_PAGE_FIELDS = {'query', 'start_line', 'total_matching_lines', 'matches', 'next_start_line'}
_BAD_PAGE = '欄位查找結果的續頁、命中範圍或來源不一致。'


def _candidate_pages(steps, document, query, count):
    did, sha = document['document_id'], document['sha256']
    for step in steps:
        action = step['action']
        if action['tool'] != 'find_document_text':
            continue
        args, observed = action['args'], step['observation']
        requests = document_ranges.validate_lookup(args)
        group = requests if 'sources' in args else [{'document_id': args['document_id'], 'queries': requests}]
        requested = {item['start_line'] for request in group if request['document_id'] == did
                     for item in request['queries'] if item['query'] == query}
        if not requested:
            continue
        sources = observed.get('sources', [observed])
        if not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources):
            raise ValueError(_BAD_PAGE)
        for source in sources:
            if (source.get('document_id'), source.get('sha256'), source.get('searched_line_count')) != (did, sha, count):
                continue
            if 'queries' in source:
                queries = source['queries']
                if not isinstance(queries, list) or any(not isinstance(page, dict) for page in queries):
                    raise ValueError(_BAD_PAGE)
                pages = [page for page in queries if page.get('query') == query]
                if len(pages) != 1:
                    raise ValueError(_BAD_PAGE)
            else:
                pages = [{key: source[key] for key in _PAGE_FIELDS - {'query'} if key in source} | {'query': query}]
            for page in pages:
                if page.get('start_line') not in requested:
                    raise ValueError(_BAD_PAGE)
                yield step['sequence'], page


def _validate_page(page, count, query):
    start = page.get('start_line')
    following, total, hits = page.get('next_start_line'), page.get('total_matching_lines'), page.get('matches')
    if (set(page) != _PAGE_FIELDS or type(start) is not int or not 1 <= start <= count
            or type(total) is not int or not 0 <= total <= count or not isinstance(hits, list)
            or not len(hits) <= min(total, document_ranges.MATCHES_PER_PAGE)
            or following is not None and (type(following) is not int
                                         or len(hits) != document_ranges.MATCHES_PER_PAGE
                                         or not start < following <= count)):
        raise ValueError(_BAD_PAGE)
    previous = start - 1
    for hit in hits:
        if (not isinstance(hit, dict) or set(hit) != _HIT_FIELDS or type(hit['full_line_provided']) is not bool
                or any(type(hit[key]) is not int for key in _HIT_FIELDS - {'full_line_provided'})):
            raise ValueError(_BAD_PAGE)
        number, left, right, width = (hit[key] for key in ('line_number', 'match_start_char', 'match_end_char', 'line_chars'))
        if (not previous < number <= count or following is not None and number >= following
                or not 1 <= left <= right <= width or right - left + 1 != len(query)):
            raise ValueError(_BAD_PAGE)
        previous = number


def _line_match(line, query):
    index = line.find(query)
    return index + 1, index + len(query), len(line)


def _hit_match(hit):
    return hit['match_start_char'], hit['match_end_char'], hit['line_chars']


def _signature(page):
    return (page['next_start_line'], page['total_matching_lines'],
            [(hit['line_number'], *_hit_match(hit)) for hit in page['matches']])


def _verify_against_copy(page, query, originals, numbers):
    start = bisect_left(numbers, page['start_line'])
    end = start + document_ranges.MATCHES_PER_PAGE
    expected = numbers[start:end]
    following = numbers[end] if end < len(numbers) else None
    if (page['total_matching_lines'] != len(numbers) or page['next_start_line'] != following
            or [hit['line_number'] for hit in page['matches']] != expected
            or any(_hit_match(hit) != _line_match(originals[number - 1], query)
                   for number, hit in zip(expected, page['matches'], strict=True))):
        raise ValueError(_BAD_PAGE)


def coverage(steps, document, query, count, *, originals, seen, literal):
    """Follow the saved cursor until exhausted. Missing pages or full lines stay pending."""
    pages = {}
    for sequence, page in _candidate_pages(steps, document, query, count):
        _validate_page(page, count, query)
        start = page['start_line']
        if start in pages and _signature(pages[start][1]) != _signature(page):
            raise ValueError(_BAD_PAGE)
        pages.setdefault(start, (sequence, page))

    if 1 not in pages:
        return {'next_page': 1}

    hits, evidence = [], []
    total, cursor = pages[1][1]['total_matching_lines'], 1
    original_numbers = ([index for index, line in enumerate(originals, 1) if query in line]
                        if originals is not None else None)
    while cursor is not None:
        if cursor not in pages:
            return {'next_page': cursor}
        sequence, page = pages[cursor]
        matched = page['matches']
        if (page['total_matching_lines'] != total or cursor != 1 and
                (not matched or matched[0]['line_number'] != cursor)):
            raise ValueError(_BAD_PAGE)
        if original_numbers is not None:
            _verify_against_copy(page, query, originals, original_numbers)
        hits.extend(matched)
        evidence.append({'step_sequence': sequence, 'start_line': cursor, 'page_sha256': digest(page)})
        cursor = page['next_start_line']
        if len(hits) > total or (cursor is None) != (len(hits) == total):
            raise ValueError(_BAD_PAGE)

    numbers = [hit['line_number'] for hit in hits]
    missing = [number for number in numbers if number not in seen]
    if missing:
        return {'missing_lines': missing}
    did = document['document_id']
    for number, hit in zip(numbers, hits, strict=True):
        text, sha = literal(f'[D{did}:L{number}]')
        if query not in text or sha != document['sha256'] or _line_match(text, query) != _hit_match(hit):
            raise ValueError(_BAD_PAGE)
    return {'numbers': numbers, 'lookup_evidence': {
        'document_id': did, 'source_sha256': document['sha256'], 'query': query,
        'searched_line_count': count, 'total_matching_lines': total, 'pages': evidence,
    }}
