"""Recover a single explicit adoption question and its acknowledgment."""
import re

from . import operand_evidence


_LINE = re.compile(r'\[D(?P<document>[1-9][0-9]{0,11}):L(?P<line>[1-9][0-9]{0,11})\]')
_FIELD = r'(?:欄位「(?P<label>[^「」\[\]\r\n]{1,80})」|(?P<bare>[^\s「」\[\]：:，,。！？?!；;]{1,60}?))'
_START = r'^(?:請\s*(?:再)?確認\s*)?'
_ACTION = r'(?:正式\s*)?(?:採用|改採|改用|就用|選用)'
_FORWARD = re.compile(rf'{_START}{_FIELD}\s*(?P<yes>是否|要不要)?\s*(?:要\s*)?{_ACTION}\s*(?P<target>.+)')
_REVERSED = re.compile(rf'{_START}是否\s*{_ACTION}\s*(?P<target>.+?)\s*(?:作為|當作)\s*{_FIELD}')
_TITLE = re.compile(r'^《(?P<title>[^《》\[\]\r\n]{1,80})》\s*的?\s*')
_ROW = re.compile(r'^第\s*(?P<row>[0-9０-９]{1,12})\s*行\s*的?\s*')
_UNCERTAIN = re.compile(r'如果|假如|假設|試算|暫定|暫用|未採用|還是|或者|並且|引述|轉述|是否能')
_AFFIRMATIVE = {'對', '對的', '是', '是的', '沒錯', '好', '好的', '可以', '確認', '同意', '就用這筆', '採用這筆'}
_DECLINE = {'不', '不對', '不是', '不好', '不要', '不用', '不採用', '不選這筆', '不同意'}
_WRAPPED_AMOUNT = {'「': '」', '（': '）', '(': ')'}


def _amount(text):
    if len(text) >= 2 and _WRAPPED_AMOUNT.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return operand_evidence.choice_quantity(text)


def question_source(question, clauses):
    pieces = clauses(question)
    if len(pieces) != 1 or not pieces[0][2] or not question.rstrip().endswith(('？', '?')):
        return None
    offset, part, _ = pieces[0]
    if _UNCERTAIN.search(part) or '不要' in part.replace('要不要', '', 1):
        return None
    if match := _REVERSED.fullmatch(part):
        target = match['target'].rstrip()
    elif match := _FORWARD.fullmatch(part):
        target = match['target'].rstrip()
        asking = target.endswith('嗎')
        if not match['yes'] and not asking:
            return None
        if asking:
            target = target[:-1].rstrip()
    else:
        return None

    refs = list(_LINE.finditer(target))
    if len(refs) != 1:
        return None
    ref = refs[0]
    before, after = target[:ref.start()].strip(), target[ref.end():].strip()
    title = _TITLE.match(before)
    if title:
        before = before[title.end():].strip()
    row = _ROW.match(before)
    if row:
        before = before[row.end():].strip()
    shown = []
    for word in (before, after if after not in {'這筆', '該行', '這一行', '為準'} else ''):
        if not word:
            continue
        if not (amount := _amount(word.removeprefix('的').strip())):
            return None
        shown.append(amount)
    return {'field': match['label'] or match['bare'], 'citation': ref[0],
            'document_id': int(ref['document']), 'line_number': int(ref['line']),
            'question_start': offset + match.start('target') + ref.start(),
            'question_end': offset + match.start('target') + ref.end(),
            'displayed_title': title['title'] if title else None,
            'displayed_line': int(row['row']) if row else None,
            'displayed_quantities': shown}


def response(answer, clauses):
    votes = [(offset, clause, 'affirmed' if clause in _AFFIRMATIVE else 'declined')
             for offset, clause, question in clauses(answer)
             if not question and (clause in _AFFIRMATIVE or clause in _DECLINE)]
    if len(votes) != 1:
        return len(answer) - len(answer.lstrip()), answer.strip(), 'unconfirmed', False
    offset, clause, status = votes[0]
    whole = answer.strip() in (clause, clause + '。', clause + '！', clause + '!')
    return offset, clause, status, whole
