"""Resolve explicit source ranges against this conversation's imported copies."""
import re
import unicodedata
from itertools import zip_longest

MAX_RANGES = 8
MAX_LINES = 200
MATCHES_PER_PAGE = 6
LOOKUP_CHAR_BUDGET = 8000
BATCH_FIELDS = ('document_id', 'start_line', 'end_line')
STATUS_TEXT = {
    'provided': '指定範圍已完整提供，仍須核對內容。',
    'invalid_range': '行號須從 1 開始，結束行不可早於起始行。',
    'document_not_attached': '此來源編號不在目前對話的匯入文件中。',
    'range_too_large': '每個指定範圍最多 200 行，請縮小範圍。',
    'line_out_of_bounds': '指定行號超出匯入副本的行數。',
    'excerpt_limit': '本次片段名額已用完，請減少同時指定的範圍。',
    'character_limit': '指定範圍超過本次剩餘字數，請縮小範圍；過長單行可另存較小文字檔。',
}
NUMBER = r'([0-9]{1,12})'
RANGES = re.compile(
    r'\[D' + NUMBER + r':L' + NUMBER + r'(?:-L' + NUMBER + r')?\]'
    r'|(?<![A-Za-z0-9_])D' + NUMBER + r'\s*(?:的\s*)?第\s*' + NUMBER +
    r'\s*(?:[-–到至]\s*(?:第\s*)?' + NUMBER + r'\s*)?行'
    r'|(?<![A-Za-z0-9_])D' + NUMBER + r'\s+lines?\s+' + NUMBER +
    r'(?:\s*(?:-|–|to)\s*' + NUMBER + r')?(?![A-Za-z0-9_]|\.[0-9]|\s*(?:[-–]|to\b))', re.IGNORECASE)


def requested_ranges(question):
    """Recognize bounded, explicit IDs only; never infer a file from its name."""
    selected, seen = [], set()
    for match in RANGES.finditer(unicodedata.normalize('NFKC', question)):
        groups = match.groups()
        values = next(groups[i:i+3] for i in (0, 3, 6) if groups[i] is not None)
        did, start = int(values[0]), int(values[1])
        end = int(values[2]) if values[2] is not None else start
        key = (did, start, end)
        if key in seen:
            continue
        if len(selected) == MAX_RANGES:
            return selected, True
        seen.add(key)
        selected.append({'document_id': did, 'start_line': start, 'end_line': end})
    return selected, False


def select_ranges(question, documents, max_excerpts, max_chars):
    requests, limited = requested_ranges(question)
    by_id = {doc['id']: doc for doc in documents}
    selected, used = [], 0
    for request in requests:
        did, start, end = (request[key] for key in ('document_id', 'start_line', 'end_line'))
        document = by_id.get(did)
        if start < 1 or end < start:
            request['status'] = 'invalid_range'
        elif document is None:
            request['status'] = 'document_not_attached'
        elif end - start + 1 > MAX_LINES:
            request['status'] = 'range_too_large'
        else:
            lines = document['content'].splitlines()
            if end > len(lines):
                request['status'] = 'line_out_of_bounds'
            elif any(row['document_id'] == did and row['start_line'] <= start and
                     row['end_line'] >= end for row in selected):
                request['status'] = 'provided'
            elif len(selected) >= max_excerpts:
                request['status'] = 'excerpt_limit'
            else:
                text = '\n'.join(f'L{n}: {lines[n - 1]}' for n in range(start, end + 1))
                if used + len(text) > max_chars:
                    request['status'] = 'character_limit'
                else:
                    selected.append({'document_id': did, 'name': document['name'],
                        'sha256': document['sha256'], 'start_line': start, 'end_line': end,
                        'text': text, 'partial_line': False, 'matched_query': False,
                        'requested_range': True})
                    request['status'] = 'provided'
                    used += len(text)
        request['explanation'] = STATUS_TEXT[request['status']]
    return selected, requests, limited


def range_status_text(documents):
    lines = [f"D{row['document_id']} L{row['start_line']}-L{row['end_line']}：" +
             STATUS_TEXT.get(row.get('status'), '狀態無法確認。')
             for row in documents.get('requested_ranges', [])]
    if documents.get('requested_ranges_limited'):
        lines.append('最多處理八個不同指定範圍，其餘尚未處理。')
    return '\n'.join(lines)


def validate_batch(args):
    if not isinstance(args, dict) or set(args) != {'ranges'}:
        raise ValueError('批次查讀須提供ranges。')
    ranges = args['ranges']
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= MAX_RANGES:
        raise ValueError('每次批次查讀須指定1至8個不同範圍。')
    seen = set()
    for item in ranges:
        if (not isinstance(item, dict) or set(item) != set(BATCH_FIELDS)
                or any(type(value) is not int or not 1 <= value < 10**12 for value in item.values())):
            raise ValueError('每個範圍須指定正整數的文件編號、開始行與結束行。')
        key = tuple(item[field] for field in BATCH_FIELDS)
        if key in seen:
            raise ValueError('同一批查讀的範圍不可重複。')
        seen.add(key)


def read_batch(documents, args):
    """Use the normal explicit-range reader with a shared output budget."""
    validate_batch(args)
    query = ' '.join(f"[D{row['document_id']}:L{row['start_line']}-L{row['end_line']}]"
                     for row in args['ranges'])
    excerpts, requests, _ = select_ranges(query, documents, MAX_RANGES, 10000)
    return {'excerpts': excerpts, 'requested_ranges': requests}


LOOKUP_GUIDANCE = '''find_document_text在本目標指定的一份匯入副本找精確字面。args為{"document_id":2,"query":"核准號","start_line":1}。
同一文件需查多個欄位或別名時，先合併成一次查找，args改用{"document_id":2,"queries":[{"query":"核准號","start_line":1},{"query":"放行核准","start_line":1}]}。最多六個不同字面，保留各自命中、原文和續頁位置。共用同一行只回傳一次；共用回傳預算使部分命中可能只有位置。只替仍需更多證據的字面續頁，不必重查已核對欄位。
在較長的指定文件找完整欄名、批號或確切片語時優先用它，避免把舊版或另案命中帶入。逐行比對原字面，區分大小寫，每頁最多六個命中行；total_matching_lines是這份副本的全部字面命中行數。依next_start_line接續，或用較窄的字面定位現行行。完整回傳的原行才可引用；full_line_provided為false的長行用read_line從命中字元附近讀取，已取得目標欄位時，補查首頁摘要或鄰近上下文只讀所需短區段，不重讀整份。若仍缺證據，再按需要擴大查讀。
零命中只表示這份文字副本沒有該字面。可能有同義寫法時改查具體別名或讀相關區段。命中也不代表生效，須核對上下文、批次及版本。使用者要求依文件找資料時，先核對適用來源，只問仍缺且會改變交付的資訊；不重複無效搜尋。'''


def validate_lookup(args):
    if not isinstance(args, dict) or set(args) not in (
            {'document_id', 'query', 'start_line'}, {'document_id', 'queries'}):
        raise ValueError('字面查找須指定一份文件，以及單筆或一批查找項目。')
    if type(args['document_id']) is not int or not 1 <= args['document_id'] < 10**12:
        raise ValueError('來源編號須為正整數。')
    if 'queries' not in args:
        requests = [{'query': args['query'], 'start_line': args['start_line']}]
    else:
        requests = args['queries']
        if not isinstance(requests, list) or not 1 <= len(requests) <= 6:
            raise ValueError('一批須包含 1 至 6 個不同字面。')
    seen = set()
    for request in requests:
        if not isinstance(request, dict) or set(request) != {'query', 'start_line'}:
            raise ValueError('每個查找項目須指定查找文字和起始行。')
        start, query = request['start_line'], request['query']
        if type(start) is not int or not 1 <= start < 10**12:
            raise ValueError('起始行須為正整數。')
        _validate_lookup_query(query)
        if query in seen:
            raise ValueError('同一批查找文字不可重複。')
        seen.add(query)
    return requests


def _validate_lookup_query(query):
    if (not isinstance(query, str) or not 1 <= len(query) <= 120 or not query.strip()
            or query.splitlines() != [query] or '\x00' in query):
        raise ValueError('查找文字須為 1 至 120 字的單行字面。')
    try:
        query.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('查找文字含無效字元。') from exc


def _lookup_page(lines, request, cancel):
    start, query = request['start_line'], request['query']
    total, matches, next_start = 0, [], None
    for number, line in enumerate(lines, 1):
        if cancel is not None and cancel.is_set():
            from .engine import EngineError
            raise EngineError('文件字面查找已停止，未新增結果。', 'cancelled')
        position = line.find(query)
        if position < 0:
            continue
        total += 1
        if number < start:
            continue
        if len(matches) < MATCHES_PER_PAGE:
            matches.append({'line_number': number, 'match_start_char': position + 1,
                            'match_end_char': position + len(query), 'line_chars': len(line)})
        elif next_start is None:
            next_start = number
    return {**request, 'total_matching_lines': total, 'matches': matches, 'next_start_line': next_start}


def _lookup_excerpts(document, lines, pages):
    groups = zip_longest(*(page['matches'] for page in pages))
    positions = dict.fromkeys(hit['line_number'] for group in groups for hit in group if hit is not None)
    selected = []
    remaining = LOOKUP_CHAR_BUDGET
    for number in positions:
        size = len(f'L{number}: ') + len(lines[number - 1])
        if size <= remaining:
            selected.append(number)
            remaining -= size
            if len(selected) == MAX_RANGES:
                break
    ranges = ' '.join(f"[D{document['id']}:L{number}]" for number in sorted(selected))
    return select_ranges(ranges, [document], MAX_RANGES, LOOKUP_CHAR_BUDGET)[0]


def lookup(documents, args, cancel=None):
    requests = validate_lookup(args)
    document = next((row for row in documents if row['id'] == args['document_id']), None)
    if document is None:
        raise ValueError('來源不在目前目標的已匯入文件中。')
    lines = document['content'].splitlines()
    if any(request['start_line'] > len(lines) for request in requests):
        raise ValueError('起始行超出匯入副本的範圍。')
    pages = [_lookup_page(lines, request, cancel) for request in requests]
    excerpts = _lookup_excerpts(document, lines, pages)
    provided = {row['start_line'] for row in excerpts}
    for page in pages:
        for hit in page['matches']:
            hit['full_line_provided'] = hit['line_number'] in provided
    source = {'document_id': document['id'], 'sha256': document['sha256']}
    if 'queries' in args:
        return {**source, 'searched_line_count': len(lines), 'queries': pages, 'excerpts': excerpts}
    page = pages[0]
    return {**source, 'start_line': page['start_line'], 'searched_line_count': len(lines),
            'total_matching_lines': page['total_matching_lines'], 'matches': page['matches'],
            'next_start_line': page['next_start_line'], 'excerpts': excerpts}
