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
_WAIT_APPROVAL = re.compile(
    rf'(?:請\s*)?等(?:待|到)?\s*{_ACTOR}\s*(?:已經?\s*)?{_PERMISSION}\s*{_ACTION}'
    rf'\s*(?P<target>.+?)\s*(?:作為|當作)\s*(?:正式\s*)?{_FIELD}(?:之)?後')
_WAIT_STATUS = re.compile(
    rf'(?:目前|截至目前)?\s*{_ACTOR}\s*(?:尚未|還沒|沒有)\s*{_PERMISSION}'
    r'\s*(?P<citation>\[補充#[1-9][0-9]{0,3}\])?')
_WAIT_OTHER_APPROVAL = re.compile(
    rf'已收到\s*{_ACTOR}\s*{_PERMISSION}\s*的(?:回覆|補答)'
    r'\s*(?P<citation>\[補充#[1-9][0-9]{0,3}\])?')
_WAIT_INSTRUCTIONS = {'請回覆確認', '請再回覆確認', '再回覆確認', '請回覆', '請再回覆'}
_NOT_YET = {'還沒', '還沒有', '尚未', '仍未', '還在等'}
_UNCLEAR_ACTOR = re.compile(r'不|未|尚|或|和|與|都|全部|可能|如果|要等|還要|還沒|沒有|已經|才能|同意|批准|核准')
_GROUP_ACTOR = r'[\u3400-\u9fffA-Za-z0-9·・]{1,40}?'
_GROUP_SEPARATOR = r'\s*(?:、|和|與|以及|及|跟)\s*'
_GROUP_CONDITION = re.compile(
    rf'(?:但(?:是)?\s*)?(?:(?:尚須|必須|仍須|需要|還要|要|得|先)\s*)?等(?:到)?\s*'
    rf'(?P<actors>{_GROUP_ACTOR}(?:{_GROUP_SEPARATOR}{_GROUP_ACTOR}){{1,3}})\s*(?:都|皆)\s*{_PERMISSION}'
    r'\s*(?:後)?\s*(?:(?:才(?:能|會)?|再)(?:採用|使用|用)|才行)?')
_GROUP_QUESTION = re.compile(
    rf'{_START}{_ACTOR}\s*是否\s*(?:也\s*)?(?:已經?\s*)?(?:(?:另行|另外)\s*)?'
    rf'{_PERMISSION}\s*{_ACTION}\s*(?P<target>.+?)\s*(?:作為|當作)\s*(?:正式\s*)?{_FIELD}')
_REPLY_AFTER_APPROVAL = re.compile(rf'請(?:在|於)?\s*取得\s*{_PERMISSION}\s*後\s*回覆')
_LIMITED_SCOPE_BODY = (
    rf'(?:仍\s*)?(?:只|僅)\s*{_PERMISSION}\s*'
    r'(?:更換|變更|調整|修改|更新|換|改)\s*(?:欄位)?'
    r'(?:「(?P<label>[^「」\[\]\r\n]{1,80})」|(?P<bare>[\u3400-\u9fffA-Za-z0-9_-]{1,60}))')
_LIMITED_SCOPE = re.compile(rf'{_ACTOR}\s*(?:(?:目前|現在|這次|本次)\s*)?{_LIMITED_SCOPE_BODY}')
_LIMITED_RECAP = re.compile(rf'{_ACTOR}\s*(?:(?:目前|現在|這次|本次|先前|當時)\s*)?{_LIMITED_SCOPE_BODY}')
_SUPPLEMENT_AT_END = re.compile(r'\s*(?P<citation>\[補充#[1-9][0-9]{0,3}\])$')
_GROUP_APPROVAL_REPLY = re.compile(
    rf'{_ACTOR}\s*(?P<state>已經?|尚未|還沒|沒有)\s*(?:(?:另行|另外)\s*)?{_PERMISSION}'
    r'\s*(?:(?:採用|使用|用)(?:這筆|該行|這一行)?)?')
_CORRECTED_QUESTION = re.compile(
    r'(?:上一問|前一問)(?:的)?(?:顯示(?:的)?[^\s\[\]「」]{1,30}與(?:引用)?原文不(?:一致|符)|'
    r'(?:補答)?引用(?:錯誤|有誤))|更正(?:上一問|前一問)(?:的)?(?:引用|顯示值)(?:後)?')
_SCOPED_REPORT = re.compile(
    rf'{_ACTOR}\s*(?P<state>已經?|尚未|還沒|沒有)\s*(?:(?:另行|另外)\s*)?{_PERMISSION}\s*{_ACTION}'
    rf'\s*(?P<reference>{_LINE.pattern})\s*(?:作為|當作)\s*(?:正式\s*)?{_FIELD}')
_CLARIFY_VOTE = re.compile(
    r'請(?:再)?確認(?:你|您)說的「(?P<vote>[^「」]{1,10})」\s*'
    r'(?P<citation>\[補充#[1-9][0-9]{0,3}\])\s*是指(?P<claim>.+)嗎')


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


def _proposal(match, target, offset, *, allow_text_value=False):
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
    shown, shown_text = [], None
    for index, word in enumerate((before, after if after not in {'這筆', '該行', '這一行', '為準'} else '')):
        if not word:
            continue
        value = word.removeprefix('的').strip()
        if amount := _amount(value):
            shown.append(amount)
        elif allow_text_value and index == 0 and 1 <= len(value) <= 100:
            shown_text = value[1:-1] if value.startswith('「') and value.endswith('」') else value
            if not shown_text:
                return None
        else:
            return None
    result = {'field': match['label'] or match['bare'], 'citation': ref[0],
            'document_id': int(ref['document']), 'line_number': int(ref['line']),
            'question_start': offset + match.start('target') + ref.start(),
            'question_end': offset + match.start('target') + ref.end(),
            'displayed_title': title['title'] if title else None,
            'displayed_line': int(row['row']) if row else None,
            'displayed_quantities': shown}
    if shown_text is not None:
        result['displayed_value'] = shown_text
    return result


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


def _limited_scope(text, *, recap=False):
    pattern = _LIMITED_RECAP if recap else _LIMITED_SCOPE
    match = pattern.fullmatch(text)
    if not match or not _valid_group_actor(match['actor']):
        return None
    return {'actor': match['actor'], 'verb': match['verb'], 'other_field': match['label'] or match['bare']}


def group_approval_question(question, clauses, *, corrected=False):
    parts = clauses(question)
    correction = None
    if corrected:
        candidates = [index for index, (_, text, is_question) in enumerate(parts[:2])
                      if not is_question and _CORRECTED_QUESTION.fullmatch(text)]
        if len(candidates) == 1:
            start, text, _ = parts.pop(candidates[0])
            correction = {'text': text, 'question_start': start}
    instruction = None
    if parts and not parts[-1][2]:
        instruction = _REPLY_AFTER_APPROVAL.fullmatch(parts[-1][1])
        if instruction:
            parts = parts[:-1]
    if (not 1 <= len(parts) <= 2 or not parts[-1][2]
            or not (instruction or question.rstrip().endswith(('？', '?')))):
        return None
    offset, part, _ = parts[-1]
    if _UNCERTAIN.search(part) or '不要' in part:
        return None
    recap = None
    if len(parts) == 2:
        start, text, is_question = parts[0]
        citation = _SUPPLEMENT_AT_END.search(text)
        if is_question or not citation or not (scope := _limited_scope(text[:citation.start()], recap=corrected)):
            return None
        recap = {**scope, 'citation': citation['citation'], 'text': text, 'question_start': start}
    match = _GROUP_QUESTION.fullmatch(part)
    if (not match or not _valid_group_actor(match['actor'])
            or instruction and instruction['verb'] != match['verb']):
        return None
    proposal = _proposal(match, match['target'], offset, allow_text_value=True)
    if proposal is not None:
        approval = {'actor': match['actor'], 'verb': match['verb'],
                    'question_start': offset + match.start('actor')}
        if recap:
            approval['reported_limited_scope'] = recap
        if instruction:
            approval['wait_notice'] = True
        if correction:
            approval['correction'] = correction
        proposal['approval'] = approval
    return proposal


def _scoped_report(text, offset):
    match = _SCOPED_REPORT.fullmatch(text)
    if not match or not _valid_group_actor(match['actor']):
        return None
    return {'actor': match['actor'], 'verb': match['verb'],
            'status': 'affirmed' if match['state'] in {'已', '已經'} else 'declined',
            'quote': text, 'quote_start': offset,
            'scope': {'field': match['label'] or match['bare'], 'citation': match['reference'],
                      'document_id': int(match['document']), 'line_number': int(match['line']),
                      'quote_start': offset + match.start('reference'),
                      'quote_end': offset + match.end('reference')}}


def clarification_question(question, clauses):
    parts = clauses(question)
    if not parts or not parts[0][2]:
        return None
    start, text, _ = parts[0]
    match = _CLARIFY_VOTE.fullmatch(text)
    if not match or match['vote'] not in _APPROVAL_ACK:
        return None
    claim = _scoped_report(match['claim'], start + match.start('claim'))
    if not claim or claim['status'] != 'affirmed':
        return None
    tail = parts[1:]
    if tail:
        if len(tail) != 2 or any(question for _, _, question in tail) or tail[0][1] != f"若已{claim['verb']}":
            return None
        if tail[1][1] != f"請回覆「{match['claim']}」":
            return None
    scope = claim['scope']
    return {'field': scope['field'], 'citation': scope['citation'], 'document_id': scope['document_id'],
            'line_number': scope['line_number'], 'question_start': scope['quote_start'], 'question_end': scope['quote_end'],
            'displayed_title': None, 'displayed_line': None, 'displayed_quantities': [],
            'approval': {'actor': claim['actor'], 'verb': claim['verb'], 'question_start': claim['quote_start'],
                         'wait_notice': True, 'clarifies_reply': {'citation': match['citation'], 'quote': match['vote'],
                                                                   'question_start': start + match.start('vote')}}}


def approval_wait(question, clauses, *, other_approvals=False):
    pieces = clauses(question)
    if any(is_question for _, _, is_question in pieces) or not 1 <= len(pieces) <= 3:
        return None
    parts = list(pieces)
    evidence, evidence_key = None, 'reported_pending'
    if len(parts) > 1:
        status = _WAIT_STATUS.fullmatch(parts[0][1])
        if status is None and other_approvals:
            status = _WAIT_OTHER_APPROVAL.fullmatch(parts[0][1])
            evidence_key = 'reported_other_approval'
        if status:
            evidence = {'actor': status['actor'], 'verb': status['verb'], 'citation': status['citation'],
                        'text': parts[0][1], 'question_start': parts[0][0]}
            parts.pop(0)
    if len(parts) == 1:
        offset, part, _ = parts[0]
        suffix = next((text for text in sorted(_WAIT_INSTRUCTIONS, key=len, reverse=True) if part.endswith(text)), None)
        if suffix is None:
            return None
        part = part[:-len(suffix)].rstrip()
    elif len(parts) == 2 and parts[1][1] in _WAIT_INSTRUCTIONS:
        offset, part, _ = parts[0]
    else:
        return None

    match = _WAIT_APPROVAL.fullmatch(part)
    if not match or _UNCERTAIN.search(part) or _UNCLEAR_ACTOR.search(match['actor']):
        return None
    proposal = _proposal(match, match['target'], offset)
    if proposal is not None:
        proposal['approval'] = {'actor': match['actor'], 'verb': match['verb'],
                                'question_start': offset + match.start('actor'), 'wait_notice': True}
        if evidence is not None:
            proposal['approval'][evidence_key] = evidence
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


def waiting_group(answer, clauses):
    pieces = clauses(answer)
    if len(pieces) != 2 or any(question for _, _, question in pieces) or pieces[0][1] not in _AFFIRMATIVE:
        return None
    offset, text, _ = pieces[1]
    if not (match := _GROUP_CONDITION.fullmatch(text)):
        return None
    actors = re.split(_GROUP_SEPARATOR, match['actors'])
    if (not 2 <= len(actors) <= 4 or len(set(actors)) != len(actors)
            or any(not re.fullmatch(_GROUP_ACTOR, actor) or _UNCLEAR_ACTOR.search(actor) for actor in actors)):
        return None
    return {'actors': actors, 'verb': match['verb'], 'quote': text, 'quote_start': offset,
            'choice_quote': pieces[0][1], 'choice_start': pieces[0][0]}


def _valid_group_actor(actor):
    return re.fullmatch(_GROUP_ACTOR, actor) and not _UNCLEAR_ACTOR.search(actor)


def group_response(answer, clauses, approval, *, limited_scope=False, corrected=False):
    if corrected:
        scoped = [report for offset, text, question in clauses(answer)
                  if not question and (report := _scoped_report(text, offset))]
        if len(scoped) > 1:
            return None
        if scoped:
            report, = scoped
            quote = report['quote']
            return report, answer.strip() in (quote, quote + '。', quote + '！', quote + '!')
        if approval.get('wait_notice'):
            offset, quote, status, whole = response(answer, clauses)
            if whole and status == 'affirmed' and quote in _APPROVAL_ACK:
                return {'actor': approval['actor'], 'verb': approval['verb'], 'status': 'unclear',
                        'quote': quote, 'quote_start': offset}, True
    if limited_scope:
        scopes = [(offset, text, scope) for offset, text, question in clauses(answer)
                  if not question and (scope := _limited_scope(text))]
        if len(scopes) == 1:
            offset, text, scope = scopes[0]
            whole = answer.strip() in (text, text + '。', text + '！', text + '!')
            return {**scope, 'status': 'limited', 'quote': text, 'quote_start': offset}, whole
        if scopes:
            return None
    pattern = _GROUP_APPROVAL_REPLY if limited_scope else _APPROVAL_REPLY
    reports = [(offset, text, match) for offset, text, question in clauses(answer)
               if not question and (match := pattern.fullmatch(text))]
    if len(reports) > 1:
        return None
    if reports:
        offset, text, match = reports[0]
        actor, verb = match['actor'], match['verb']
        if not re.fullmatch(_GROUP_ACTOR, actor) or _UNCLEAR_ACTOR.search(actor):
            return None
        status = 'affirmed' if match['state'] in {'已', '已經'} else 'declined'
        whole = answer.strip() in (text, text + '。', text + '！', text + '!')
    else:
        offset, text, status, whole = approval_response(answer, clauses, approval, repeated=True)
        actor, verb = approval['actor'], approval['verb']
    if status == 'unconfirmed':
        return None
    return {'actor': actor, 'verb': verb, 'status': status, 'quote': text, 'quote_start': offset}, whole


def approval_response(answer, clauses, approval, *, repeated=False):
    raw = answer.strip()
    if repeated and raw.removesuffix('。') in _NOT_YET:
        return len(answer) - len(answer.lstrip()), raw.removesuffix('。'), 'declined', True
    offset, quote, status, whole = response(answer, clauses)
    if status == 'affirmed' and (approval.get('wait_notice') or quote not in _APPROVAL_ACK):
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


def other_approval_report(answer, clauses, expected_actor):
    reports = []
    for offset, quote, question in clauses(answer):
        if not question and (match := _APPROVAL_REPLY.fullmatch(quote)):
            reports.append((offset, quote, match))
    if len(reports) != 1:
        return None
    offset, quote, match = reports[0]
    actor = match['actor']
    if actor == expected_actor or _UNCLEAR_ACTOR.search(actor) or match['state'] not in {'已', '已經'}:
        return None
    return {'actor': actor, 'verb': match['verb'], 'quote': quote, 'quote_start': offset}
