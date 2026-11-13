"""Checked history for a user's explicit withdrawal of one saved question."""
import json
import re

from . import goal_guidance_sources, goal_partials, goal_reply_sources, goal_stages


GUIDANCE = ('withdrawn_questions列出依使用者主動更正撤下的舊提問步驟。'
            '這些提問仍可查看，但沒有使用者答覆，不能當作目前仍待回答或已有答案。'
            '依相連的更正原話與目前工作範圍核對：保留仍適用的已確認資料，'
            '只對仍會影響成果的缺漏提出新問題。資料已足夠就完成成果。')
_DISMISS_CURRENT = re.compile(r'(?:請\s*)?撤下(?:目前|這個|這則)(?:的)?問題')
_DISMISS_PART = re.compile(r'(?:請\s*)?撤下分問\s*「([^「」\r\n]{2,400})」')
_QUESTION_PART = re.compile(r'[^\r\n。；;！!？?]+[？?]')
PART_MARK = 'question_part_withdrawal'


def requests_current_dismissal(content):
    return any(not question and _DISMISS_CURRENT.fullmatch(clause)
               for _, clause, question in goal_guidance_sources.standalone_clauses(content))


def remaining_question(content, question):
    clauses = goal_guidance_sources.standalone_clauses(content)
    targets = [match[1] for _, clause, is_question in clauses
               if not is_question and (match := _DISMISS_PART.fullmatch(clause))]
    if not targets:
        return None
    if len(targets) != 1 or requests_current_dismissal(content):
        raise ValueError('每次請明確指定一個要撤下的分問。')
    parts = [part.strip() for part in _QUESTION_PART.findall(question)]
    if (not 2 <= len(parts) <= 4 or len(set(parts)) != len(parts)
            or re.sub(r'\s+', '', ''.join(parts)) != re.sub(r'\s+', '', question)
            or targets[0] not in parts):
        raise ValueError('請引用目前問題中一個完整分問，包含原問號。')
    return '\n'.join(part for part in parts if part != targets[0])


def _receipt(sequence, action, observation):
    question = goal_reply_sources.checked_question(action, observation)
    goal_partials.waiting(question, action, observation)
    return {'ask_sequence': sequence, 'question': question,
            'question_sha256': goal_stages.digest([sequence, action, observation])}


def options(db, gid, question, step_count, guidance):
    numbers = [source['number'] for source in guidance if source['steps_seen'] == step_count]
    if not numbers or not step_count:
        return None
    row = db.execute('SELECT action_json,observation_json FROM autonomous_steps WHERE goal_id=? AND sequence=?',
                     (gid, step_count)).fetchone()
    if row is None:
        return None
    action, observation = map(json.loads, row)
    if action.get('tool') != 'ask' or observation.get('question') != question:
        return None
    return {**_receipt(step_count, action, observation), 'correction_numbers': numbers}


def record(db, gid, options, source):
    db.execute('INSERT INTO autonomous_question_withdrawals(goal_id,ask_sequence,guidance_number,question_sha256,guidance_sha256) '
               'VALUES(?,?,?,?,?)', (gid, options['ask_sequence'], source['number'],
                                    options['question_sha256'], source['sha256']))


def dismiss(db, gid, options, source):
    record(db, gid, options, source)
    db.execute("UPDATE autonomous_goals SET question='',status='paused',notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
               ('已依所選更正撤下問題，可開始／接續。', gid))


def read(db, gid, guidance, replies, *, waiting_step=None):
    corrections = {source['number']: source for source in guidance}
    answered = {source['ask_sequence'] for source in replies}
    result = []
    marked = {}
    for sequence, raw in db.execute('SELECT sequence, observation_json FROM autonomous_steps '
                                    'WHERE goal_id=? AND observation_json LIKE ?', (gid, f'%"{PART_MARK}"%')):
        observation = json.loads(raw)
        if PART_MARK in observation:
            marked[sequence] = observation[PART_MARK]
    for sequence, number, ask_hash, correction_hash, action, observation, next_action, next_observation in db.execute('''
            SELECT w.ask_sequence,w.guidance_number,w.question_sha256,w.guidance_sha256,s.action_json,s.observation_json,
                   n.action_json,n.observation_json
            FROM autonomous_question_withdrawals w LEFT JOIN autonomous_steps s
            ON s.goal_id=w.goal_id AND s.sequence=w.ask_sequence
            LEFT JOIN autonomous_steps n ON n.goal_id=w.goal_id AND n.sequence=w.ask_sequence+1
            WHERE w.goal_id=? ORDER BY w.ask_sequence''', (gid,)):
        source = corrections.get(number)
        if (sequence in answered or sequence == waiting_step or source is None or source['steps_seen'] != sequence
                or source['sha256'] != correction_hash or action is None or observation is None):
            raise ValueError('撤下紀錄與原提問或更正來源不一致。')
        receipt = _receipt(sequence, json.loads(action), json.loads(observation))
        if receipt['question_sha256'] != ask_hash:
            raise ValueError('撤下紀錄與原提問步驟指紋不一致。')
        if sequence + 1 in marked:
            successor = goal_reply_sources.checked_question(json.loads(next_action), json.loads(next_observation))
            if (marked.pop(sequence + 1) != {'ask_sequence': sequence, 'guidance_number': number}
                    or successor != remaining_question(source['content'], receipt['question'])):
                raise ValueError('保留的分問與原提問或更正不一致。')
        result.append({**receipt, 'guidance_number': number, 'guidance_citation': source['citation'],
                       'guidance_sha256': correction_hash})
    if marked:
        raise ValueError('保留的分問缺少相符的問題撤下紀錄。')
    return result


def validate_saved(db, *, replies_present):
    from .goal_guidance_sources import read as read_guidance
    for gid, status, notes, count in db.execute('''
            SELECT DISTINCT g.id,g.status,g.notes,
            (SELECT COUNT(*) FROM autonomous_steps s WHERE s.goal_id=g.id)
            FROM autonomous_goals g WHERE EXISTS (SELECT 1 FROM autonomous_question_withdrawals w WHERE w.goal_id=g.id)
            OR EXISTS (SELECT 1 FROM autonomous_steps s WHERE s.goal_id=g.id AND s.observation_json LIKE ?)''',
            (f'%"{PART_MARK}"%',)):
        guidance = read_guidance(db, gid, notes, count)
        replies = goal_reply_sources.read(db, gid, notes) if replies_present else []
        read(db, gid, guidance, replies, waiting_step=count if status == 'needs_input' else None)
