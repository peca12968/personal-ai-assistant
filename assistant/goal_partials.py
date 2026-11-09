"""Keep a provisional report attached to its own saved clarification step."""
import json

from .documents import check_citations
from . import goal_reply_sources


MAX_CHARS = 4000


def validate(text):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_CHARS or '\0' in text:
        raise ValueError('待補期間的成果需為1至4000字。')
    try:
        text.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('待補期間的成果含無效文字。') from exc


def saved(action, observation):
    args = action.get('args', {})
    if 'partial_result' not in observation and (not isinstance(args, dict) or 'partial_result' not in args):
        return None
    if not isinstance(args, dict):
        raise ValueError('待補期間的成果缺少保存動作。')
    partial = observation.get('partial_result')
    validate(partial)
    question = args.get('question')
    if (action.get('tool') != 'ask' or set(args) != {'question', 'partial_result'}
            or args['partial_result'] != partial or not isinstance(question, str)
            or not question.strip() or observation.get('question') != question):
        raise ValueError('待補期間的成果與保存問題不一致。')
    return partial


def question_citations(question, excerpts, reply_sources):
    documents = check_citations(question, excerpts)
    if any(not item['range_valid'] for item in documents):
        raise ValueError('問題有未讀取的來源行號，請先讀取或改以文字描述缺漏。')
    replies = goal_reply_sources.citations(question, reply_sources, label='問題')
    return {'documents': documents, 'replies': replies} if documents or replies else None


def waiting(question, action, observation):
    partial = saved(action, observation)
    details = {'partial_result': partial} if partial is not None else {}
    if 'question_citations' in observation:
        details['question_citations'] = observation['question_citations']
    if details and goal_reply_sources.checked_question(action, observation) != question:
        raise ValueError('保存的待補資料與目前問題不一致。')
    return details


def read_waiting(db, gid, question):
    row = db.execute('SELECT action_json,observation_json FROM autonomous_steps WHERE goal_id=? '
                     'ORDER BY sequence DESC LIMIT 1', (gid,)).fetchone()
    if row is not None:
        return waiting(question, json.loads(row[0]), json.loads(row[1]))
    return {}


def validate_saved(db, *, reply_sources_present):
    goals = db.execute('''SELECT DISTINCT g.id,g.status,g.question,g.notes FROM autonomous_goals g
        JOIN autonomous_steps s ON s.goal_id=g.id
        WHERE s.action_json LIKE '%"partial_result"%' OR s.observation_json LIKE '%"partial_result"%'
        OR s.observation_json LIKE '%"question_citations"%' ''')
    for gid, status, current_question, notes in goals:
        replies = goal_reply_sources.read(db, gid, notes) if reply_sources_present else []
        excerpts = []
        for sequence, action_json, observation_json in db.execute('SELECT sequence,action_json,observation_json '
                'FROM autonomous_steps WHERE goal_id=? ORDER BY sequence', (gid,)):
            action, observation = json.loads(action_json), json.loads(observation_json)
            partial = saved(action, observation)
            if partial is not None:
                expected = check_citations(partial, excerpts)
                if observation.get('citation_checks') != expected or any(not row['range_valid'] for row in expected):
                    raise ValueError('待補期間的成果引用與保存來源不一致。')
            if 'question_citations' in observation:
                question = goal_reply_sources.checked_question(action, observation)
                prior = [source for source in replies if source['ask_sequence'] < sequence]
                expected = question_citations(question, excerpts, prior)
                if expected is None or observation['question_citations'] != expected:
                    raise ValueError('問題引用與保存的來源不一致。')
            excerpts.extend(observation.get('excerpts', []))
        if status == 'needs_input':
            read_waiting(db, gid, current_question)
