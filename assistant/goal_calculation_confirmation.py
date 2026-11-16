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
_ACTOR = r'(?P<actor>[^\s「」\[\]：:，,。！？?!；;]{1,40}?)'
_PERMISSION = r'(?P<verb>同意|批准|核准)'
_APPROVAL_QUESTION = re.compile(
    rf'{_START}{_ACTOR}\s*是否\s*(?:已經?\s*)?{_PERMISSION}\s*{_ACTION}'
    rf'\s*(?P<target>.+?)\s*(?:作為|當作)\s*(?:正式\s*)?{_FIELD}')
_CONDITION = re.compile(
    rf'(?:但(?:是)?\s*)?(?:(?:尚須|必須|仍須|需要|還要|要|得|先)\s*)?等(?:到)?\s*{_ACTOR}\s*{_PERMISSION}'
    r'\s*(?:後)?\s*(?:(?:才(?:能|會)?|再)(?:採用|使用|用)|才行)?')
_APPROVAL_REPLY = re.compile(rf'{_ACTOR}\s*(?P<state>已經?|尚未|還沒|沒有)\s*{_PERMISSION}'
                             r'\s*(?:(?:採用|使用|用)(?:這筆|該行|這一行)?)?')
_APPROVAL_ACK = {'對', '對的', '是', '是的', '沒錯'}
_UNCLEAR_ACTOR = re.compile(r'不|未|尚|或|和|與|都|全部|可能|如果|要等|還要|還沒|沒有|已經|才能|同意|批准|核准')


def _amount(text):
    if len(text) >= 2 and _WRAPPED_AMOUNT.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return operand_evidence.choice_quantity(text)


def _question_clause(question, clauses):
    pieces = clauses(question)
    if len(pieces) != 1 or not pieces[0][2] or not question.rstrip().endswith(('？', '?')):
        return None
    offset, part, _ = pieces[0]
    if _UNCERTAIN.search(part) or '不要' in part.replace('要不要', '', 1):
        return None
    return offset, part


def question_source(question, clauses):
    if not (piece := _question_clause(question, clauses)):
        return None
    offset, part = piece
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

    return _proposal(match, target, offset)


def _proposal(match, target, offset):
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


def approval_question(question, clauses):
    if not (piece := _question_clause(question, clauses)):
        return None
    offset, part = piece
    match = _APPROVAL_QUESTION.fullmatch(part)
    if not match or _UNCLEAR_ACTOR.search(match['actor']):
        return None
    proposal = _proposal(match, match['target'], offset)
    if proposal is not None:
        proposal['approval'] = {'actor': match['actor'], 'verb': match['verb'],
                                'question_start': offset + match.start('actor')}
    return proposal


def waiting_condition(answer, clauses):
    pieces = clauses(answer)
    if len(pieces) != 2 or any(question for _, _, question in pieces) or pieces[0][1] not in _AFFIRMATIVE:
        return None
    offset, text, _ = pieces[1]
    match = _CONDITION.fullmatch(text)
    if not match or _UNCLEAR_ACTOR.search(match['actor']):
        return None
    return {'actor': match['actor'], 'verb': match['verb'], 'quote': text, 'quote_start': offset,
            'choice_quote': pieces[0][1], 'choice_start': pieces[0][0]}


def approval_response(answer, clauses, approval):
    offset, quote, status, whole = response(answer, clauses)
    if status == 'affirmed' and quote not in _APPROVAL_ACK:
        status, whole = 'unconfirmed', False
    if status != 'unconfirmed':
        return offset, quote, status, whole
    parts = clauses(answer)
    reports = []
    for start, part, question in parts:
        if question or not (match := _APPROVAL_REPLY.fullmatch(part)):
            continue
        if (match['actor'], match['verb']) == (approval['actor'], approval['verb']):
            code = 'affirmed' if match['state'] in {'已', '已經'} else 'declined'
            reports.append((start, part, code))
    if len(reports) == 1:
        start, part, code = reports[0]
        return start, part, code, answer.strip() in (part, part + '。', part + '！', part + '!')
    return offset, quote, status, whole
