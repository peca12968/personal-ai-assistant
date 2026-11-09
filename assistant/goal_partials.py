"""Keep a provisional report attached to its own saved clarification step."""
import json

from .documents import check_citations


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


def waiting(question, action, observation):
    partial = saved(action, observation)
    if partial is not None and observation['question'] != question:
        raise ValueError('待補期間的成果與目前問題不一致。')
    return partial


def read_waiting(db, gid, question):
    row = db.execute('SELECT action_json,observation_json FROM autonomous_steps WHERE goal_id=? '
                     'ORDER BY sequence DESC LIMIT 1', (gid,)).fetchone()
    if row is not None:
        return waiting(question, json.loads(row[0]), json.loads(row[1]))
    return None


def validate_saved(db):
    goals = db.execute('''SELECT DISTINCT g.id,g.status,g.question FROM autonomous_goals g
        JOIN autonomous_steps s ON s.goal_id=g.id
        WHERE s.action_json LIKE '%"partial_result"%' OR s.observation_json LIKE '%"partial_result"%' ''')
    for gid, status, question in goals:
        excerpts = []
        for action_json, observation_json in db.execute('SELECT action_json,observation_json '
                'FROM autonomous_steps WHERE goal_id=? ORDER BY sequence', (gid,)):
            action, observation = json.loads(action_json), json.loads(observation_json)
            partial = saved(action, observation)
            if partial is not None:
                expected = check_citations(partial, excerpts)
                if observation.get('citation_checks') != expected or any(not row['range_valid'] for row in expected):
                    raise ValueError('待補期間的成果引用與保存來源不一致。')
            excerpts.extend(observation.get('excerpts', []))
        if status == 'needs_input':
            read_waiting(db, gid, question)
