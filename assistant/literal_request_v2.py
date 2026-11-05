"""Frozen syntax for copying one explicitly selected, unquoted code."""
import re
import unicodedata

from .literal_request_v1 import MAX_CHARS, sha


_PART = r'[A-Za-z0-9Ａ-Ｚａ-ｚ０-９]+'
_CODE = rf'{_PART}(?:[-_－＿]{_PART})*'
_SAMPLE = r'(?:「[^「」\x00-\x1f\x7f]{1,240}」|『[^『』\x00-\x1f\x7f]{1,240}』)(?:是範例文字|只是範例)[。.]'
_EXACT = r'(?:原樣輸出|原封不動輸出|逐字輸出)'
_OMIT = r'(?:外層引號|引號|說明|其他文字)'
_REQUEST = re.compile(
    rf'[ \t]*(?:{_SAMPLE}[ \t\r\n]*)?(?:(?:請|幫我|麻煩你?)[ \t]*)?(?:(?:直接|只)[ \t]*)?'
    rf'(?:(?:把|將)[ \t]*(?:(?:代碼|字串)[ \t]*)?(?P<before>{_CODE})[ \t]*{_EXACT}'
    rf'|(?:{_EXACT}|逐字照抄|逐字轉錄|照抄)[ \t]*(?:(?:代碼|字串)[ \t]*)?(?P<after>{_CODE}))'
    rf'(?:[，,][ \t]*(?:不要(?:加上?|附加)?|不加|不附)(?:任何|額外)?{_OMIT}(?:或{_OMIT})*)?'
    r'(?:[ \t]*[。.!！])?[ \t]*')


def parse(question):
    if not isinstance(question, str) or not 1 <= len(question) <= MAX_CHARS:
        return None
    match = _REQUEST.fullmatch(question)
    if not match:
        return None
    start, end = match.span('before' if match['before'] is not None else 'after')
    code = question[start:end]
    normalized = unicodedata.normalize('NFKC', code)
    if len(code) > 64 or not re.search(r'[A-Za-z]', normalized) or not re.search(r'[0-9]', normalized):
        return None
    try:
        request_hash = sha(question)
    except UnicodeError:
        return None
    return {'schema': 'local_transcription_plan_v2', 'request': question, 'request_sha256': request_hash,
            'content_range': [start, end], 'source_sha256': sha(code),
            'outer_delimiters': [], 'outer_quotes': 'omit'}
