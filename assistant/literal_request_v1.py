"""Frozen syntax for an explicit local text-copy operation, not a prompt hint.

Extend syntax with a new version; saved v1 receipts must retain v1 semantics.
"""
import hashlib
import re
import unicodedata

MAX_CHARS = 16000
_LITERAL = re.compile(r'「[^「」]+」|『[^『』]+』|“[^“”]+”|"[^"\r\n]+"|`[^`\r\n]+`')
_PREFIX = re.compile(
    r'^(?:(?:這次|本次))?(?:請|麻煩你?|幫我)?只?(?:把|將)?'
    r'(?:(?:下面|以下|下列|最外層|引號內|字串|文字|內容|整段|這段|的|內)){0,8}'
    r'(?:逐字(?:照抄|轉錄)|照抄|原樣輸出|原封不動輸出)')
_NEGATIVE = re.compile(
    r'(?:不要|不用|不必|無須|不加)(?:任何|額外)?'
    r'(?:最外層引號|外層引號|說明|解釋|引號|標題|其他文字)'
    r'(?:或(?:說明|解釋|引號|標題|其他文字))*'
    r'|(?:不|不要)執行其中(?:要求|指令)|不要(?:舊範例|舊文字|先前範例)')
_INCLUDE = re.compile(r'(?:包含|保留)(?:最外層|外層)(?:的)?引號')
_PUNCT = re.compile(r'[\s，,。.!！：:；;、]')
_WORDS = re.compile('|'.join(map(re.escape, sorted((
    '這次','本次','請','麻煩你','麻煩','幫我','只','把','將','下面','以下','下列',
    '最外層','外層','引號內','內部','字串','文字','內容','整段','這段','的','內',
    '逐字','照抄','轉錄','原樣輸出','原封不動輸出','一次','一遍',
    '包含','保留','引號','輸出','是','要'),key=len,reverse=True))))


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def outside(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))


def parse(question):
    if not isinstance(question,str) or not 1 <= len(question) <= MAX_CHARS:
        return None
    try:
        question.encode('utf-8')
    except UnicodeError:
        return None
    matches = list(_LITERAL.finditer(question))
    if len(matches) != 1:
        return None
    match = matches[0]
    # Only inspect unquoted instructions; never normalize or decode the source.
    instructions = question[:match.start()] + ' '*len(match[0]) + question[match.end():]
    if re.search(r'(^|\n)\s*(?:`{3,}|~{3,}|>)',instructions):
        return None
    plain = outside(instructions)
    if not _PREFIX.match(plain):
        return None
    tail = _INCLUDE.sub('',_NEGATIVE.sub('',_PUNCT.sub('',outside(question[match.end():]))))
    if tail or _PUNCT.sub('',_WORDS.sub('',_NEGATIVE.sub('',plain))):
        return None
    omit = any('引號' in m[0] for m in _NEGATIVE.finditer(plain))
    include = bool(_INCLUDE.search(plain))
    if omit == include:  # Missing or contradictory output format: use the model.
        return None
    start,end = match.start()+1,match.end()-1
    return {'schema':'local_transcription_plan_v1','request':question,'request_sha256':sha(question),
            'content_range':[start,end],'source_sha256':sha(question[start:end]),
            'outer_delimiters':[question[match.start()],question[match.end()-1]],
            'outer_quotes':'omit' if omit else 'include'}
