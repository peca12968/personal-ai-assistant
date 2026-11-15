"""Bounded historical status for chat, without replaying tool data or old sources."""
import json
import re
import unicodedata
from . import delivery_preference, goal_sources


_POLITE = r'(?:(?:請問|請幫我|幫我|麻煩幫我|我想|請)\s*)?'
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
_WORK_VERB = re.compile(r'^(?:請先|請)?\s*(?:整理|準備|完成|處理|核對|製作|追蹤|建立)\s*')
_GOAL_ID = re.compile(r'[a-f0-9]{32}')
_CODE_SPACING = re.compile(r'(?<=[\u3400-\u9fff]) (?=[a-z0-9])|(?<=[a-z0-9]) (?=[\u3400-\u9fff])')
_LABEL_BOUNDARY = ' ：:。；;，,。—–（(的'


def _requested_name(question):
    body = re.sub('^' + _POLITE, '', question.strip(), count=1)
    lookup = _LOOKUP.match(body)
    if lookup:
        body = body[lookup.end():]
    if quote := _NAME_QUOTE.match(body):
        name = next(value for value in quote.groups() if value is not None)
        tail = body[quote.end():].lstrip(' \t：:，,。；;\n')
        if _TOPIC.match(tail) or (not lookup and not _STATUS_START.match(tail)):
            return None
    elif lookup and (end := _NAME_END.search(body)):
        name, tail = body[:end.start()], body[end.start():]
    else:
        return None
    name = name.strip()
    if (2 <= len(name) <= 80 and not re.search(r'[\x00-\x1f\x7f「」『』“”]', name)
            and not re.match(r'^\s*(?:[、]|或|和|與|以及)', tail)
            and _STATUS_CUE.search(tail)):
        return name
    return None


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


def goal_context(store, sid, question=''):
    # The engine also imports this module while autonomy is being initialized.
    from .autonomy import signature
    from .goal_review import next_action
    name = _requested_name(question)
    reference = None
    with store.connect() as db:
        # Goals and current imported-source versions must describe one observation.
        # Do not use the cross-session review query: it reads full reports/receipts.
        db.execute('BEGIN')
        parameters = (sid,)
        restriction = ''
        if name:
            matches = _named_ids(db, sid, name)
            if not matches:
                return {'goals': [], 'limited': False, 'reference': {'name': name, 'status': 'not_found'}}
            reference = {'name': name, 'status': 'matched' if len(matches) == 1 else 'ambiguous'}
            restriction = ' AND g.id IN (' + ','.join('?' for _ in matches) + ')'
            parameters = (sid, *matches)
        include_question = reference is None or reference['status'] == 'matched'
        rows = [dict(row) for row in db.execute('''SELECT g.id,g.objective,g.status,g.updated_at,g.created_at,
            CASE WHEN ? THEN g.question ELSE '' END AS question,
            g.sources_json,length(CAST(g.notes AS BLOB))>0 AS user_notes_present,
            (SELECT parent_goal_id FROM autonomous_handoffs h WHERE h.goal_id=g.id) AS parent_goal_id,
            EXISTS(SELECT 1 FROM autonomous_delivery d WHERE d.goal_id=g.id) AS file_delivery,
            (SELECT COUNT(*) FROM autonomous_artifacts a WHERE a.goal_id=g.id) AS saved_artifact_count,
            (SELECT COUNT(*) FROM autonomous_steps s WHERE s.goal_id=g.id) AS step_count
            FROM autonomous_goals g WHERE g.session_id=?''' + restriction + ' ORDER BY g.rowid DESC LIMIT 4',
            (include_question, *parameters))]
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
    return result
