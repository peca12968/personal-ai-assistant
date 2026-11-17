"""Recheck one waiting comparison field against its original work."""
import json
import re

from . import goal_chat_fields, goal_comparison_handoff, goal_conversation, goal_guidance_sources, goal_reply_sources
from .acceptance import strict_json


GUIDANCE = ('comparison_resolved_questions 記錄已用原工作正式資料解除的舊待答問題。'
            '舊提問與暫表只供回顧，這裡沒有新工作的使用者答覆。'
            '依本輪 verified_work_fields 逐列核對。已正式確認的欄位直接沿用最新值和來源，'
            '有其他缺欄時只問真正缺的資料。')
_REQUEST = re.compile(r'^請\s*(?:提供|告知|確認|填寫)\s*')
_WH = re.compile(r'(?:(?:目前|現在|最後)(?:已)?(?:正式)?(?:確認|保存)的值)?'
                 r'(?:是什麼|是多少|是哪個|是哪一個|是哪位|是誰|在哪裡|在哪一個|是幾點|為何)$')
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


def target(question, originals):
    clauses = goal_guidance_sources.standalone_clauses(question)
    if not clauses or any(inquiry or not _SAME_FIELD_SOURCE.fullmatch(text)
                          for _, text, inquiry in clauses[1:]):
        return None
    _, text, inquiry = clauses[0]
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
    index = target(question, originals)
    return None if index is None else {'ask_sequence': last['sequence'], 'row': index}


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
        if evidence['rows'][expected['row']]['status'] != 'confirmed' or cancel.is_set():
            return False
        receipt = {'row': expected['row'], 'ask_sha256': goal_conversation.digest(step), 'evidence': evidence}
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
        if (not isinstance(entry, dict) or entry.keys() != {'row', 'ask_sha256', 'evidence'}
                or digest != goal_conversation.digest(entry) or step is None or waiting == sequence
                or entry['ask_sha256'] != goal_conversation.digest(step)):
            raise ValueError('比較接續的保存範圍或原提問指紋不符。')
        question = goal_reply_sources.checked_question(step['action'], step['observation'])
        match = waiting_target({'status': 'needs_input', 'question': question, 'steps': [step]}, originals)
        index = entry['row']
        if (type(index) is not int or match != {'ask_sequence': sequence, 'row': index}):
            raise ValueError('比較接續未對應原提問的指定欄位。')
        if validate_evidence:
            evidence = entry['evidence']
            if (not isinstance(evidence, dict) or evidence.keys() != {'rows', 'cutoffs'}
                    or goal_comparison_handoff.evidence(
                        db, goal['session_id'], originals, cutoffs=evidence['cutoffs']) != evidence
                    or evidence['rows'][index]['status'] != 'confirmed'):
                raise ValueError('比較接續的保存來源與已確認欄位不符。')
        selected = originals[index]
        rows.append({'ask_sequence': sequence, 'question': question,
                     'work': selected['work'], 'field': selected['field'], 'scope': selected.get('scope', '')})
    return rows


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
        for item in saved:
            for table, present in (('autonomous_reply_sources', replies_present),
                                   ('autonomous_question_withdrawals', withdrawals_present)):
                if present and db.execute(f'SELECT 1 FROM {table} WHERE goal_id=? AND ask_sequence=?',
                                          (gid, item['ask_sequence'])).fetchone():
                    raise ValueError('比較接續與同一提問的其他解除紀錄重疊。')
