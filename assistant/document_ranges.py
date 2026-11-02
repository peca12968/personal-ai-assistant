"""Resolve explicit source ranges against this conversation's imported copies."""
import re
import unicodedata

MAX_RANGES = 8
MAX_LINES = 200
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
