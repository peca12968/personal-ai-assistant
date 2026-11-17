"""Recheck waiting comparison fields against their original works."""
import json
import re

from . import goal_chat_fields, goal_comparison_handoff, goal_conversation, goal_guidance_sources, goal_reply_sources
from .acceptance import strict_json


GUIDANCE = ('comparison_resolved_questions 逐欄記錄曾從原工作核對到正式值的舊待答項目。'
            '同一舊提問可能含多個欄位，這裡只記錄當時成功核對的欄位。'
            '舊提問與暫表只供回顧，這裡沒有新工作的使用者答覆。'
            '依本輪 verified_work_fields 逐列核對。沿用已正式確認的最新值和來源，'
            '更新暫表，只保留仍缺的獨立分問。若所需資料已齊，繼續交付。')
_REQUEST = re.compile(r'^請\s*(?:提供|告知|確認|填寫)\s*')
_WH = re.compile(r'(?:(?:目前|現在|最後)(?:已)?(?:正式)?(?:確認|保存)的值)?'
                 r'(?:是什麼|是多少|是哪個|是哪一個|是哪位|是誰|是哪裡|在哪裡|在哪一個|是幾點|為何)$')
_SAME_FIELD_SOURCE = re.compile(
    r'(?:請\s*)?(?:一併|同時|並|也)?(?:提供|附上|列出|註明)\s*'
    r'(?:該值|這個值|該欄值|這筆確認)的(?:原工作(?:內)?的?)?'
    r'來源(?:種類(?:和|及|與|、)(?:原工作內的)?編號|編號|種類)')
_QUOTES = {'「': '」', '『': '』', '“': '”'}


def _literal(text):
    text = text.strip()
    if text and text[0] in _QUOTES and text.endswith(_QUOTES[text[0]]):
        return text[1:-1].strip()
    return text


def _labels(entry):
    field = entry['field']
    scope = goal_chat_fields.label_key(entry.get('scope', ''))
    label = goal_chat_fields.label_key(field)
    if not scope:
        return {label}
    prefixes = [scope + word for word in ('的', '使用的', '所用的')]
    for prefix in prefixes:
        if label.startswith(prefix):
            label = label[len(prefix):]
            break
    return {prefix + label for prefix in prefixes}


def _target(text, inquiry, originals):
    opener = _REQUEST.match(text)
    if opener:
        text = text[opener.end():]
    if inquiry and (tail := _WH.search(text)):
        text = text[:tail.start()].removeprefix('請問').strip()
    elif not opener:
        return None
    matches = []
    for index, entry in enumerate(originals):
        name = entry['work']
        for title in (name, f'「{name}」', f'『{name}』', f'“{name}”'):
            head, sep, field = text.partition(title)
            if sep and not head and (part := field.lstrip()).startswith('的'):
                if entry.get('goal_id') and goal_chat_fields.label_key(_literal(part[1:])) in _labels(entry):
                    matches.append(index)
                break
    return matches[0] if len(matches) == 1 else None


def _targets(question, originals):
    rows = []
    for _, text, inquiry in goal_guidance_sources.standalone_clauses(question):
        if rows and not inquiry and _SAME_FIELD_SOURCE.fullmatch(text):
            continue
        index = _target(text, inquiry, originals)
        if index is None or index in rows:
            return []
        rows.append(index)
    return rows


def waiting_target(goal, originals):
    steps = goal['steps']
    if goal['status'] != 'needs_input' or not steps:
        return None
    last = steps[-1]
    if last['action'].get('tool') != 'ask' or last['observation'].get('question') != goal['question']:
        return None
    args = last['action'].get('args', {})
    if not isinstance(args, dict) or args.keys() - {'question', 'partial_result'}:
        return None
    try:
        question = goal_reply_sources.checked_question(last['action'], last['observation'])
    except ValueError:
        return None
    rows = _targets(question, originals)
    if rows:
        return {'ask_sequence': last['sequence'], **({'row': rows[0]} if len(rows) == 1 else {'rows': rows})}
    return None


def _confirmed(indices, evidence):
    return [index for index in indices if evidence['rows'][index]['status'] == 'confirmed']


def recheck(store, sid, gid, goal, originals, cancel):
    """Caller holds the new goal's lease. Save no reply and run no model here."""
    expected = waiting_target(goal, originals)
    if expected is None or cancel.is_set():
        return False
    with goal_comparison_handoff.locked(store, originals), store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        record = db.execute('''SELECT status,question FROM autonomous_goals
            WHERE id=? AND session_id=?''', (gid, sid)).fetchone()
        saved = db.execute('''SELECT sequence,action_json,observation_json FROM autonomous_steps
            WHERE goal_id=? ORDER BY sequence DESC LIMIT 1''', (gid,)).fetchone()
        if record is None or saved is None:
            return False
        step = {'sequence': saved[0], 'action': strict_json(saved[1]), 'observation': strict_json(saved[2])}
        checked = waiting_target({'status': record[0], 'question': record[1], 'steps': [step]}, originals)
        if checked != expected or step != goal['steps'][-1]:
            return False
        evidence = goal_comparison_handoff.evidence(db, sid, originals)
        compound = 'rows' in expected
        indices = expected['rows'] if compound else [expected['row']]
        confirmed = _confirmed(indices, evidence)
        if not confirmed or cancel.is_set():
            return False
        resolution = {'version': 2, 'rows': confirmed} if compound else {'row': confirmed[0]}
        receipt = {**resolution, 'ask_sha256': goal_conversation.digest(step), 'evidence': evidence}
        db.execute('''INSERT INTO autonomous_comparison_resumes
            (goal_id,ask_sequence,receipt_json,receipt_sha256) VALUES(?,?,?,?)''',
            (gid, expected['ask_sequence'], json.dumps(receipt, ensure_ascii=False), goal_conversation.digest(receipt)))
        db.execute("UPDATE autonomous_goals SET status='paused',question='',notice=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   ('已從原工作核對待答欄位，可接續。', gid))
    return True


def recorded(db, goal, originals, *, validate_evidence=False):
    """Read authoritative mechanical resumptions without replaying old values to the model."""
    gid = goal['id']
    steps = {step['sequence']: step for step in goal['steps']}
    waiting = goal['status'] == 'needs_input' and max(steps, default=0)
    rows = []
    for sequence, raw, digest in db.execute('''SELECT ask_sequence,receipt_json,receipt_sha256
            FROM autonomous_comparison_resumes WHERE goal_id=? ORDER BY ask_sequence''', (gid,)):
        entry = strict_json(raw)
        step = steps.get(sequence)
        if (not isinstance(entry, dict) or digest != goal_conversation.digest(entry)
                or step is None or waiting == sequence or entry.get('ask_sha256') != goal_conversation.digest(step)):
            raise ValueError('比較接續的保存範圍或原提問指紋不符。')
        question = goal_reply_sources.checked_question(step['action'], step['observation'])
        match = waiting_target({'status': 'needs_input', 'question': question, 'steps': [step]}, originals)
        indices = _recorded_indices(entry, match)
        if validate_evidence:
            evidence = entry['evidence']
            if (not isinstance(evidence, dict) or evidence.keys() != {'rows', 'cutoffs'}
                    or goal_comparison_handoff.evidence(
                        db, goal['session_id'], originals, cutoffs=evidence['cutoffs']) != evidence
                    or _confirmed(match['rows'] if 'rows' in match else indices, evidence) != indices):
                raise ValueError('比較接續的保存來源與已確認欄位不符。')
        for index in indices:
            selected = originals[index]
            rows.append({'ask_sequence': sequence, 'question': question,
                         'work': selected['work'], 'field': selected['field'], 'scope': selected.get('scope', '')})
    return rows


def _recorded_indices(entry, match):
    if match and 'row' in match and entry.keys() == {'row', 'ask_sha256', 'evidence'}:
        if type(entry['row']) is int and entry['row'] == match['row']:
            return [entry['row']]
    if match and 'rows' in match and entry.keys() == {'version', 'rows', 'ask_sha256', 'evidence'}:
        rows = entry['rows']
        if (type(entry['version']) is int and entry['version'] == 2 and isinstance(rows, list)
                and rows and all(type(index) is int for index in rows)
                and [index for index in match['rows'] if index in rows] == rows):
            return rows
    raise ValueError('比較接續未對應原提問的指定欄位。')


def validate_saved(db, *, replies_present, withdrawals_present):
    for gid, sid, objective, status in db.execute('''SELECT DISTINCT g.id,g.session_id,g.objective,g.status
            FROM autonomous_goals g JOIN autonomous_comparison_resumes r ON r.goal_id=g.id'''):
        conversation = goal_conversation.read(db, gid)
        if not conversation or goal_comparison_handoff.BASIS not in conversation:
            raise ValueError('比較接續缺少原比較快照。')
        originals = goal_comparison_handoff.selected(db, sid, objective, conversation[goal_comparison_handoff.BASIS])
        steps = [{'sequence': row[0], 'action': strict_json(row[1]), 'observation': strict_json(row[2])}
                 for row in db.execute('SELECT sequence,action_json,observation_json FROM autonomous_steps WHERE goal_id=?',
                                       (gid,))]
        saved = recorded(db, {'id': gid, 'session_id': sid, 'status': status, 'steps': steps},
                         originals, validate_evidence=True)
        for sequence in {item['ask_sequence'] for item in saved}:
            for table, present in (('autonomous_reply_sources', replies_present),
                                   ('autonomous_question_withdrawals', withdrawals_present)):
                if present and db.execute(f'SELECT 1 FROM {table} WHERE goal_id=? AND ask_sequence=?',
                                          (gid, sequence)).fetchone():
                    raise ValueError('比較接續與同一提問的其他解除紀錄重疊。')
