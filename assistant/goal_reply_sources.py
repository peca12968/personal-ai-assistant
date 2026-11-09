"""Saved answers are attributable to an exact question within one goal."""
import hashlib
import json
import re

from .acceptance import strict_json


REPORT_FIELDS = {'ask': 'partial_result', 'finish': 'report', 'deliver': 'report', 'write_artifact': 'content'}
CITATIONS = re.compile(r'\[補充#[^\]\r\n]{0,32}\]')


def note(question, answer):
    return '\n問題：' + question + '\n使用者補充：' + answer


def _question(action_json, observation_json):
    if not isinstance(action_json, str) or not isinstance(observation_json, str):
        raise ValueError('回覆來源缺少所對應的步驟。')
    return checked_question(strict_json(action_json), strict_json(observation_json))


def checked_question(action, observation):
    if not isinstance(action, dict) or action.get('tool') != 'ask' or not isinstance(action.get('args'), dict):
        raise ValueError('回覆來源必須對應保存的提問。')
    question = action['args'].get('question')
    if (not isinstance(question, str) or not question.strip() or len(question) > 800 or '\0' in question
            or not isinstance(observation, dict) or observation.get('question') != question):
        raise ValueError('回覆來源與保存的問題不一致。')
    return question


def record(db, gid, question, answer, notes_start):
    step = db.execute('SELECT sequence,action_json,observation_json FROM autonomous_steps '
                      'WHERE goal_id=? ORDER BY sequence DESC LIMIT 1', (gid,)).fetchone()
    if step is None:
        return  # Legacy waiting state without a saved step can still accept a reply.
    if _question(step[1], step[2]) != question:
        raise ValueError('目前問題與最新提問步驟不一致。')
    db.execute('INSERT INTO autonomous_reply_sources(goal_id,ask_sequence,notes_start,answer) VALUES(?,?,?,?)',
               (gid, step[0], notes_start, answer))


def read(db, gid, notes):
    sources = []
    previous_end = 0
    for sequence, start, answer, action, observation in db.execute('''
            SELECT r.ask_sequence,r.notes_start,r.answer,s.action_json,s.observation_json
            FROM autonomous_reply_sources r LEFT JOIN autonomous_steps s
            ON s.goal_id=r.goal_id AND s.sequence=r.ask_sequence
            WHERE r.goal_id=? ORDER BY r.ask_sequence''', (gid,)):
        if (type(sequence) is not int or not 1 <= sequence <= 32 or type(start) is not int
                or not previous_end <= start <= len(notes)
                or not isinstance(answer, str) or not answer.strip() or len(answer) > 4000 or '\0' in answer):
            raise ValueError('保存的回覆來源範圍不符。')
        question = _question(action, observation)
        part = note(question, answer)
        previous_end = start + len(part)
        if notes[start:previous_end] != part:
            raise ValueError('保存的回覆來源與使用者補充原文不一致。')
        evidence = json.dumps([gid, sequence, start, question, answer], ensure_ascii=False, separators=(',', ':'))
        sources.append({'citation': f'[補充#{sequence}]', 'ask_sequence': sequence,
                        'question': question, 'answer': answer,
                        'sha256': hashlib.sha256(evidence.encode('utf-8')).hexdigest()})
    return sources


def citations(report, sources, *, label='成果'):
    by_token = {item['citation']: item for item in sources}
    checks = []
    for match in CITATIONS.finditer(report):
        token = match.group()
        source = by_token.get(token)
        if source is None:
            raise ValueError(f'{label}引用了本目標尚未保存的回覆，請修正補充編號。')
        checks.append({'citation': token, 'ask_sequence': source['ask_sequence'], 'sha256': source['sha256']})
    return checks


def validate_saved(db, *, table_present):
    candidates = "SELECT goal_id FROM autonomous_steps WHERE observation_json LIKE '%\"reply_citations\"%'"
    if table_present:
        candidates += ' UNION SELECT goal_id FROM autonomous_reply_sources'
    for (gid,) in db.execute(f'SELECT DISTINCT goal_id FROM ({candidates})'):
        row = db.execute('SELECT notes FROM autonomous_goals WHERE id=?', (gid,)).fetchone()
        if row is None:
            raise ValueError('回覆來源缺少所屬目標。')
        sources = read(db, gid, row[0]) if table_present else []
        for sequence, raw_action, raw_observation in db.execute(
                "SELECT sequence,action_json,observation_json FROM autonomous_steps WHERE goal_id=? "
                "AND observation_json LIKE '%\"reply_citations\"%'", (gid,)):
            action, observation = strict_json(raw_action), strict_json(raw_observation)
            if not isinstance(observation, dict) or not isinstance(action, dict):
                raise ValueError('回覆引用步驟格式不符。')
            if 'reply_citations' not in observation:
                continue
            field = REPORT_FIELDS.get(action.get('tool'))
            args = action.get('args')
            report = args.get(field) if isinstance(args, dict) else None
            if not isinstance(report, str):
                raise ValueError('回覆引用缺少所對應的成果。')
            prior = [source for source in sources if source['ask_sequence'] < sequence]
            expected = citations(report, prior)
            if not expected or observation['reply_citations'] != expected:
                raise ValueError('成果中的回覆引用與保存的來源不一致。')
