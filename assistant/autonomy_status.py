"""Bounded historical status for chat, without replaying tool data or old sources."""
import json
import re
import unicodedata
from . import delivery_preference, goal_sources


_POLITE = r'(?:(?:請問|請幫我|幫我|麻煩幫我|我想|請)\s*)?'
_RETURN_OPENING = re.compile(r'^(?:對了|話說回來|說回來|順便問一下)[，,：:]\s*')
_LOOKUP = re.compile(
    r'^(?:先\s*)?(?:回到|回去接續|找回|繼續|接續|查一下|查看|查詢|查查)\s*'
    r'(?:(?:先前|之前|上次)\s*)?(?:(?:自主目標|目標)\s*[:：]?\s*)?')
_NAME_QUOTE = re.compile(r'^「([^「」\r\n]+)」|^『([^『』\r\n]+)』|^“([^“”\r\n]+)”')
_NAME_END = re.compile(r'[。；;，,？！?!\r\n]|(?:那一件|那個工作|這件工作|這個目標|目前|現在|接下來|'
                       r'下一步|還缺|還要|還需要|的(?:進度|狀態))')
_STATUS_CUE = re.compile(
    r'(?:目前|現在|還|仍|接下來|後續|下一步)[^。；;？！?!「」『』\r\n]{0,28}'
    r'(?:待答|待補|等[^。；;？！?!「」『』\r\n]{0,12}(?:回答|回覆|補|答案)|'
    r'(?:回答|回覆|補(?:充)?|缺)(?:什麼|哪)|進度|狀態|完成|做什麼|進行到哪)'
    r'|(?:進度|狀態)(?:如何|是什麼|[？?]|$)')
_STATUS_START = re.compile(r'^(?:這|那|的|目前|現在|等|還|仍|接下來|後續|下一步|進度|狀態)')
_TOPIC = re.compile(r'^(?:的)?(?:(?:這|那)(?:個|段))?話題')
_WORK_REFERENCE = r'(?:這|那)(?:一)?(?:件|個|項)(?:自主目標|目標|工作|任務)?'
_NATURAL_CUE = (
    r'[ \t，,：:]*(?:請問\s*)?(?:(?:目前|現在|接下來|後續|下一步)\s*)?'
    r'(?:(?:還|仍)?(?:在等(?:待)?|等(?:著|待)?|需要|要|該|得)?(?:我)?'
    r'(?:回答|回覆|補(?:充)?|確認|提供|填寫|填|缺)(?:什麼|哪)'
    r'|(?:還|仍)?(?:卡在|做到|停在|進行到|等)哪'
    r'|(?:進度|狀態)(?:如何|是什麼|怎麼樣|[？?]|$)'
    r'|(?:還有什麼待補|還有哪個欄位待補|(?:已經|已)?完成了嗎))'
)
_REFERENT = re.compile(rf'^\s*{_WORK_REFERENCE}{_NATURAL_CUE}')
_NATURAL_REFERENCE = re.compile(rf'^(?P<name>[^。；;，,？！?!：:「」『』“”`\r\n]{{2,80}}?)\s*{_WORK_REFERENCE}{_NATURAL_CUE}')
_NOT_A_REFERENCE = re.compile(r'^(?:我\s*)?(?:如果|若|假設|只要|引用|原文|例如|範例|不是|不要|不必|別|先別|暫不|選)')
_WORK_VERB = re.compile(r'^(?:請先|請)?\s*(?:整理|準備|完成|處理|核對|製作|追蹤|建立)\s*')
_GOAL_ID = re.compile(r'[a-f0-9]{32}')
_PROJECT_CODE = re.compile(r'(?<![a-z0-9])[a-z]{1,8}[- ]?[0-9]{1,12}(?![a-z0-9])')
_CODE_SPACING = re.compile(r'(?<=[\u3400-\u9fff]) (?=[a-z0-9])|(?<=[a-z0-9]) (?=[\u3400-\u9fff])')
_LABEL_BOUNDARY = ' ：:。；;，,。—–（(的'
_CHOOSE = re.compile(r'^(?:我指的是|我選的是|我選|就選|那就|選擇|選|就)\s*')
_CHOICE_SUFFIX = re.compile(r'\s*(?:這|那)(?:一)?(?:件|個)(?:工作|目標)?$')
_CHOICE_SEPARATOR = re.compile(r'[，,。；;？！?!\r\n]')
_CHOICE_ORDINAL = re.compile(r'第\s*([一二三123])\s*[件個項筆]')
_CHOICE_NUMBER = re.compile(r'[1-3]')
_QUOTED_TITLE = re.compile(r'「([^「」\r\n]+)」|『([^『』\r\n]+)』|“([^“”\r\n]+)”')
_NUMBERED_TITLE = re.compile(r'^\s*([1-3])[.)、]\s*「([^「」\r\n]+)」', re.M)
_NOT_A_CHOICE = re.compile(r'^(?:我\s*)?(?:不是|不要|不選|別|先別|如果|假設|引用|原文|例如)')


def _request_body(question):
    return re.sub('^' + _POLITE, '', _RETURN_OPENING.sub('', question.strip(), count=1), count=1)


def _natural_name(body):
    if _NOT_A_REFERENCE.match(body):
        return None
    if quote := _NAME_QUOTE.match(body):
        name = next(value for value in quote.groups() if value is not None)
        if not _REFERENT.match(body[quote.end():]):
            return None
    elif match := _NATURAL_REFERENCE.match(body):
        name = match['name']
        if re.search(r'\s(?:或|與|和|以及)\s|、', name):
            return None
    else:
        return None
    name = name.strip()
    if 2 <= len(name) <= 80 and not re.search(r'[\x00-\x1f\x7f]', name):
        return name
    return None


def _status_request(question):
    body = _request_body(question)
    lookup = _LOOKUP.match(body)
    if lookup:
        body = body[lookup.end():]
    if name := _natural_name(body):
        return name, lookup is None
    if quote := _NAME_QUOTE.match(body):
        name = next(value for value in quote.groups() if value is not None)
        tail = body[quote.end():].lstrip(' \t：:，,。；;\n')
        if _TOPIC.match(tail) or (not lookup and not _STATUS_START.match(tail)):
            return None, False
    elif lookup and (end := _NAME_END.search(body)):
        name, tail = body[:end.start()], body[end.start():]
    else:
        return None, False
    name = name.strip()
    if (2 <= len(name) <= 80 and not re.search(r'[\x00-\x1f\x7f「」『』“”]', name)
            and not re.match(r'^\s*(?:[、]|或|和|與|以及)', tail)
            and _STATUS_CUE.search(tail)):
        return name, False
    return None, False


def _requested_name(question):
    return _status_request(question)[0]


def _key(value):
    spaced = ' '.join(unicodedata.normalize('NFKC', value).casefold().split())
    return _CODE_SPACING.sub('', spaced)


def _label_rank(objective, key):
    heading = _key(re.split(r'[\r\n]', objective, maxsplit=1)[0])
    if heading == key:
        return 2
    for title in (heading, _WORK_VERB.sub('', heading, count=1)):
        if title.startswith(f'「{key}」') or title == key:
            return 1
        if title.startswith(key) and title[len(key)] in _LABEL_BOUNDARY:
            return 1
    return 0


def _named_ids(db, sid, name):
    key = _key(name)
    if _GOAL_ID.fullmatch(key):
        return [row['id'] for row in db.execute(
            'SELECT id FROM autonomous_goals WHERE session_id=? AND id=?', (sid, key))]
    best_rank, found = 0, []
    for row in db.execute('SELECT id,substr(objective,1,400) AS heading FROM autonomous_goals '
                          'WHERE session_id=? ORDER BY rowid DESC', (sid,)):
        rank = _label_rank(row['heading'], key)
        if rank > best_rank:
            best_rank, found = rank, []
        if rank and rank == best_rank and len(found) < 4:
            found.append(row['id'])
    return found


def _choice_request(question):
    from .context import context_reset
    if len(question) > 500:
        return None
    clauses = _CHOICE_SEPARATOR.split(_request_body(question), maxsplit=1)
    head = clauses[0].strip()
    tail = clauses[1].strip() if len(clauses) == 2 else ''
    if (not head or _NOT_A_CHOICE.match(head) or context_reset(question)
            or (tail and not _STATUS_CUE.search(tail))):
        return None
    lead = _CHOOSE.match(head)
    if lead:
        head = head[lead.end():]
    suffix = _CHOICE_SUFFIX.search(head)
    if suffix:
        head = head[:suffix.start()].strip()
    if (quote := _NAME_QUOTE.match(head)) and quote.end() == len(head):
        head = next(value for value in quote.groups() if value is not None)
    key = _key(head)
    if _CHOICE_NUMBER.fullmatch(key):
        return head, int(key), True
    if ordinal := _CHOICE_ORDINAL.fullmatch(key):
        return head, '一二三'.index(ordinal[1]) + 1 if ordinal[1] in '一二三' else int(ordinal[1]), True
    if not 2 <= len(head) <= 80 or re.search(r'[「」『』“”`、/：:\x00-\x1f\x7f]', head):
        return None
    return head, None, bool(lead or suffix)


def _latest_completed_answer(db, sid, before=None):
    from .local_answer_receipts import load_context, MAX_JSON_BYTES, MAX_TEXT_BYTES
    rows = db.execute('''WITH tail AS (
        SELECT id,role,content FROM messages WHERE session_id=? AND (? IS NULL OR id<?)
        ORDER BY id DESC LIMIT 1)
        SELECT tail.id,tail.content,u.id AS user_id,u.content AS request,c.payload FROM tail
        JOIN answer_runs r ON r.answer_message_id=tail.id AND r.session_id=? AND r.status='succeeded'
        JOIN messages u ON u.id=r.user_message_id AND u.session_id=r.session_id AND u.role='user'
        JOIN answer_contexts c ON c.message_id=tail.id AND c.payload=r.context_json
        WHERE tail.role='assistant' AND u.id<tail.id AND length(CAST(tail.content AS BLOB))<=?
        AND length(CAST(c.payload AS BLOB))<=? LIMIT 2''',
        (sid, before, before, sid, MAX_TEXT_BYTES, MAX_JSON_BYTES)).fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    try:
        context = load_context(row['payload'])
    except (ValueError, UnicodeError):
        return None
    if not isinstance(context, dict) or context.get('current_request') != row['request']:
        return None
    return row, context


def _choice_origin(db, sid, before):
    source = _latest_completed_answer(db, sid, before)
    if source is None:
        return None
    row, context = source
    summary = context.get('autonomous_goals')
    if not isinstance(summary, dict):
        return None
    reference, goals = summary.get('reference'), summary.get('goals')
    if (not isinstance(reference, dict) or reference.get('status') != 'ambiguous'
            or not isinstance(reference.get('name'), str) or not 2 <= len(reference['name']) <= 80
            or not isinstance(goals, list) or not 2 <= len(goals) <= 3):
        return None
    if 'selection' not in reference and _requested_name(row['request']) != reference['name']:
        return None
    return row, reference['name'], goals


def _visible_choices(answer, name, goals):
    shown = {_key(next(value for value in match.groups() if value is not None))
             for match in _QUOTED_TITLE.finditer(answer)}
    grouped = {}
    for goal in goals:
        if (not isinstance(goal, dict) or not _GOAL_ID.fullmatch(str(goal.get('id')))
                or not isinstance(goal.get('objective'), str)):
            return {}
        parts = goal['objective'].splitlines()
        if not parts or (len(parts) == 1 and goal.get('objective_truncated')):
            continue
        title = _key(parts[0])
        if title in shown and _label_rank(parts[0], _key(name)):
            grouped.setdefault(title, []).append(goal)
    return {title: rows[0] for title, rows in grouped.items() if len(rows) == 1}


def _short_name(title, name):
    prefix = _key(name)
    if title.startswith(prefix):
        # The full title remains valid. Only strip the previously asked name
        # across the same label boundary used for ordinary named status lookup.
        rest = title[len(prefix):]
        if rest and rest[0] in _LABEL_BOUNDARY:
            return rest.lstrip(' ：:—–').removeprefix('的').strip()
    return title


def _choice_matches(db, sid, question, before, *, only_offered=False):
    selection = _choice_request(question)
    if not selection or not (origin := _choice_origin(db, sid, before)):
        return None
    label, ordinal, explicit = selection
    row, name, candidates = origin
    offered = _visible_choices(row['content'], name, candidates)
    if len(offered) < 2:
        return None
    if ordinal:
        numbered = [(int(match[1]), _key(match[2])) for match in _NUMBERED_TITLE.finditer(row['content'])]
        expected = list(range(1, len(offered) + 1))
        matches = ([numbered[ordinal - 1][1]] if ordinal <= len(offered)
                   and [number for number, _ in numbered] == expected
                   and {title for _, title in numbered} == offered.keys() else [])
    else:
        key = _key(label)
        matches = [title for title in offered if key in (title, _short_name(title, name))]
    if not matches and (only_offered or not explicit):
        return None
    # Resolve from the prior list, then verify the selected ID and heading in
    # the current session. Newly created goals cannot replace a prior option.
    actual = {}
    for title, goal in offered.items():
        current = db.execute('SELECT objective,created_at FROM autonomous_goals WHERE session_id=? AND id=?',
                             (sid, goal['id'])).fetchone()
        if (current and _key(current['objective'].partition('\n')[0]) == title
                and current['created_at'] == goal.get('created_at')):
            actual[title] = goal['id']
    proof = {'answer_id': row['id'], 'label': label}
    if len(matches) == 1:
        if gid := actual.get(matches[0]):
            return [gid], {'name': name, 'status': 'matched', 'selection': {**proof, 'goal_id': gid}}
        return [], {'name': name, 'status': 'not_found', 'selection': proof}
    return list(actual.values()), {'name': name, 'status': 'ambiguous' if actual else 'not_found', 'selection': proof}


def _chat_goal_link(db, sid, answer_id):
    if type(answer_id) is not int or answer_id <= 0:
        return None
    source = _latest_completed_answer(db, sid)
    if source is None or source[0]['id'] != answer_id:
        return None
    row, context = source
    summary = context.get('autonomous_goals')
    if not isinstance(summary, dict):
        return None
    reference, goals = summary.get('reference'), summary.get('goals')
    if (not isinstance(reference, dict) or reference.get('status') != 'matched'
            or not isinstance(goals, list) or len(goals) != 1 or not isinstance(goals[0], dict)):
        return None
    saved = goals[0]
    gid = saved.get('id')
    if not isinstance(gid, str) or not _GOAL_ID.fullmatch(gid):
        return None
    current = db.execute('SELECT objective FROM autonomous_goals WHERE session_id=? AND id=?',
                         (sid, gid)).fetchone()
    if current is None or current['objective'][:400] != saved.get('objective'):
        return None
    if 'selection' in reference:
        resolved = _choice_matches(db, sid, row['request'], row['user_id'])
        if resolved is None or resolved != ([gid], reference):
            return None
    else:
        name = _requested_name(row['request'])
        if (not name or name != reference.get('name')
                or (_key(name) != gid and not _label_rank(current['objective'], _key(name)))):
            return None
    return {'goal_id': gid, 'user_message_id': row['user_id'], 'request': row['request'],
            'reply_basis': saved.get('reply_basis')}


def chat_goal_link(store, sid, answer_id):
    """Resolve a desktop link to the saved, single goal on the current answer."""
    with store.connect() as db:
        db.execute('BEGIN')
        return _chat_goal_link(db, sid, answer_id)


def chat_goal_target(store, sid, answer_id):
    link = chat_goal_link(store, sid, answer_id)
    return link['goal_id'] if link else None


def _reply_basis(db, sid, gid, sources):
    from .goal_conversation import digest
    row = db.execute('''SELECT g.question,g.notes,g.sources_json,
        s.sequence,s.action_json,s.observation_json FROM autonomous_goals g
        JOIN autonomous_steps s ON s.goal_id=g.id
        WHERE g.session_id=? AND g.id=? AND g.status='needs_input' AND g.question<>''
        ORDER BY s.sequence DESC LIMIT 1''', (sid, gid)).fetchone()
    if row is None or json.loads(row['action_json']).get('tool') != 'ask':
        return None
    return digest([gid, list(row), sources])


def chat_reply_draft(store, sid, answer_id, user_message_id, offset, content):
    """Bind the user's selected verbatim text to the exact reviewed goal ask."""
    from .autonomy import signature
    from .goal_guidance import _snapshot
    if (type(offset) is not int or offset < 0 or not isinstance(content, str)
            or not 1 <= len(content) <= 4000 or not content.strip() or '\0' in content):
        raise ValueError('請選取 1 到 4000 字的答案文字。')
    with store.connect() as db:
        db.execute('BEGIN')
        link = _chat_goal_link(db, sid, answer_id)
        if (not link or type(user_message_id) is not int or link['user_message_id'] != user_message_id
                or link['request'][offset:offset + len(content)] != content):
            raise ValueError('請在這次回答所對應的使用者訊息中選取答案。')
        gid = link['goal_id']
        sources = signature(db.execute('SELECT id,name,sha256 FROM documents WHERE session_id=? ORDER BY id', (sid,)))
        if not link['reply_basis'] or _reply_basis(db, sid, gid, sources) != link['reply_basis']:
            raise ValueError('待答問題或工作條件已變更，請重新查看原工作。')
        current = _snapshot(db, sid, gid)
        return {'goal_id': gid, 'text': content, 'user_message_id': user_message_id,
                'fingerprint': current['fingerprint']}


def goal_context(store, sid, question='', *, before=None):
    with store.connect() as db:
        # Read goal selection, status, and imported-source state together.
        db.execute('BEGIN')
        return _goal_context(db, sid, question, before)


def verify_choice(db, sid, question, context, before):
    state = context.get('autonomous_goals', {})
    if state.get('reference', {}).get('selection') and _goal_context(db, sid, question, before) != state:
        raise ValueError('候選工作或目前狀態已變更，請重新查詢後再選擇。')


def _goal_context(db, sid, question, before):
    # The engine also imports this module while autonomy is being initialized.
    from .autonomy import signature
    from .goal_review import next_action
    name, natural = _status_request(question)
    matches = None
    reference = None
    choice = _choice_matches(db, sid, question, before, only_offered=natural) if natural or not name else None
    if choice:
        matches, reference = choice
    elif name:
        found = _named_ids(db, sid, name)
        key = _key(name)
        if found or not natural or _GOAL_ID.fullmatch(key) or _PROJECT_CODE.search(key):
            matches = found
            reference = {'name': name, 'status': {0: 'not_found', 1: 'matched'}.get(len(matches), 'ambiguous')}
    if matches == []:
        return {'goals': [], 'limited': False, 'reference': reference}
    # Keep this projection independent of cross-session reviews and full reports.
    restriction = ' AND g.id IN (' + ','.join('?' for _ in matches) + ')' if matches else ''
    include_question = reference is None or reference['status'] == 'matched'
    rows = [dict(row) for row in db.execute('''SELECT g.id,g.objective,g.status,g.updated_at,g.created_at,
            CASE WHEN ? THEN g.question ELSE '' END AS question,
            g.sources_json,length(CAST(g.notes AS BLOB))>0 AS user_notes_present,
            (SELECT parent_goal_id FROM autonomous_handoffs h WHERE h.goal_id=g.id) AS parent_goal_id,
            EXISTS(SELECT 1 FROM autonomous_delivery d WHERE d.goal_id=g.id) AS file_delivery,
            (SELECT COUNT(*) FROM autonomous_artifacts a WHERE a.goal_id=g.id) AS saved_artifact_count,
            (SELECT COUNT(*) FROM autonomous_steps s WHERE s.goal_id=g.id) AS step_count
            FROM autonomous_goals g WHERE g.session_id=?''' + restriction + ' ORDER BY g.rowid DESC LIMIT 4',
        (include_question, sid, *(matches or ())))]
    for row in rows:
        if row['file_delivery'] and (change := delivery_preference.latest(db, row['id'])):
            row['file_delivery'] = change['file_delivery']
    sources = signature(db.execute(
        'SELECT id,name,sha256 FROM documents WHERE session_id=? ORDER BY id', (sid,)).fetchall())
    scoped = {row['id'] for row in rows if goal_sources.saved_supplement(db, row['id'])}
    goals = []
    for row in rows[:3]:
        sources_match = goal_sources.matching_scope(json.loads(row['sources_json']), sources, scoped=row['id'] in scoped)
        action, explanation = next_action(row['status'], sources_match, row['step_count'])
        # A saved question is model output, not proof it remains unanswered.
        # Explicit replies clear it; newer guidance/current chat may already answer it.
        question = row['question'] if row['status'] == 'needs_input' else ''
        goals.append({'id': row['id'], 'objective': row['objective'][:400],
                      'objective_truncated': len(row['objective']) > 400,
                      'recorded_status': row['status'], 'step_count': row['step_count'],
                      'recorded_question': question[:800], 'question_truncated': len(question) > 800,
                      'user_notes_present': bool(row['user_notes_present']),
                      'sources_match': sources_match, 'next_action': action,
                      'next_action_explanation': explanation,
                      'parent_goal_id': row['parent_goal_id'],
                      'file_delivery': bool(row['file_delivery']),
                      'saved_artifact_count': row['saved_artifact_count'],
                      'updated_at': row['updated_at']})
        if reference and reference['status'] == 'ambiguous':
            goals[-1]['created_at'] = row['created_at']
    result = {'goals': goals, 'limited': len(rows) > 3}
    if reference:
        result['reference'] = reference
        if reference['status'] == 'matched' and goals[0]['recorded_question']:
            if basis := _reply_basis(db, sid, goals[0]['id'], sources):
                goals[0]['reply_basis'] = basis
    return result
