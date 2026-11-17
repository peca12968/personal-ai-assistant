"""Preserve superseded drafts and bind their replacement names to reviewed replies."""

import hashlib
import json
import re

from . import artifact_names, delivery_preference, goal_guidance_sources, goal_reply_sources, goal_stages


MARK = 'artifact_revision_question'
GUIDANCE = (
    'superseded_artifacts 是使用者已正式更正後留下的舊檔。依現行更正更新受影響欄位，'
    '可沿用其他仍適用的來源及結果。把新版存為目前指定的檔名，再交付新版。'
    '舊檔保留供回顧，禁止再次交付或用另一個檔名交付相同舊內容。'
)
_CORRECTION = re.compile(r'^(?:請\s*)?正式(?:更正|撤回|改用|改為)\s*\S')
_CURRENT_ONLY = re.compile(
    r'^(?:請\s*)?(?:(?:保留|留存)(?:已寫(?:入)?檔案|原檔|舊檔|舊草稿)[，,]\s*)?'
    r'(?:只|僅)(?:交付|提交)(?:符合(?:正式)?更正的(?:版本|成果|檔案)|更正後的(?:版本|成果|檔案)|更新後的(?:版本|成果|檔案)|新版)$'
)
_NAMED = re.compile(
    r'^(?:請\s*)?(?:已保存的)?(?:舊檔|草稿|文件|檔案)?\s*「(?P<name>[^」\r\n]+)」'
    r'(?:是舊版|已過時|已撤回)?[，,]?\s*(?:不得|不要|不可|不再)(?:作為定稿)?交付$'
)
_YES = re.compile(r'^(?:可以|好|確認)[，,。]\s*')


def _clauses(text):
    return [clause for _, clause, inquiry in goal_guidance_sources.standalone_clauses(text)
            if not inquiry and not clause.endswith(('嗎', '呢'))]


def choose(steps, proof, answer, *, reserved=()):
    removed = _YES.match(answer)
    selection = artifact_names.reply_filename(answer[removed.end():] if removed else answer,
                                               start=removed.end() if removed else 0)
    if selection is None:
        selection = artifact_names.declaration(answer, only=True)
    if selection is None or selection['filename'].casefold() in reserved:
        raise ValueError('請提供同格式且尚未使用的定稿檔名。')
    ask = steps[-1]
    return artifact_names.confirm_filename(steps, ask, proof, selection)['selection']


def _current_only(clauses):
    return any(_CORRECTION.match(clause) for clause in clauses) and any(_CURRENT_ONLY.fullmatch(clause) for clause in clauses)


def requirement(goal):
    expected = artifact_names.requirement(goal)
    notes = goal.get('guidance_sources', ())
    replies = goal.get('reply_sources', ())
    known = {source['citation']: source['notes_start'] for source in [*notes, *replies]}
    position = known.get(expected['citation'], -1) if expected else -1
    for source in notes:
        if source['notes_start'] <= position:
            continue
        clauses = goal_guidance_sources.standalone_clauses(source['content'])
        if not _current_only([clause for _, clause, inquiry in clauses if not inquiry]):
            continue
        selections = [name for start, clause, inquiry in clauses
                      if not inquiry and (name := artifact_names.replacement_declaration(clause, start))]
        if len(selections) == 1 and _first_writes(goal['steps'][:source['steps_seen']]):
            expected = {**selections[0], 'citation': source['citation']}
            position = source['notes_start']
    for reply in replies:
        prior = goal['steps'][:reply['ask_sequence']]
        proof = prior[-1]['observation'].get(MARK) if prior else None
        if proof and reply['notes_start'] > position:
            selection = choose(prior, proof, reply['answer'])
            expected = {**selection, 'citation': reply['citation']}
            position = reply['notes_start']
    return expected


def _first_writes(steps):
    first = {}
    for step in steps:
        if step['action']['tool'] == 'write_artifact' and (receipt := step['observation'].get('artifact')):
            first.setdefault(receipt['name'].casefold(), step)
    return first


def _context_at(goal, end, steps):
    return {**goal, 'steps': steps, 'notes': goal['notes'][:end],
            **{kind: [source for source in goal.get(kind, ()) if source['notes_start'] < end]
               for kind in ('guidance_sources', 'reply_sources')}}


def superseded(goal):
    result = {}
    for note in goal.get('guidance_sources', ()):
        prior = goal['steps'][:note['steps_seen']]
        written = _first_writes(prior)
        if not written:
            continue
        clauses = _clauses(note['content'])
        names = {match['name'].casefold() for clause in clauses if (match := _NAMED.fullmatch(clause))}
        if not names and _current_only(clauses):
            expected = requirement(_context_at(goal, note['notes_start'], prior))
            key = expected['filename'].casefold() if expected else None
            names = {key} if key in written else set(written) if len(written) == 1 and expected is None else set()
        for key in sorted(names & written.keys()):
            record = written[key]['observation']['artifact']
            result[key] = {'name': record['name'], 'sha256': record['sha256'], 'write_step': written[key]['sequence'],
                           'write_sha256': goal_stages.digest(written[key]),
                           'citation': note['citation'], 'source_sha256': note['sha256']}
    return result


def question(goal, expected, stale, inventory):
    if (expected is None or expected['filename'].casefold() not in stale
            or inventory.keys() != _first_writes(goal['steps']).keys()):
        return None
    draft = stale[expected['filename'].casefold()]
    return {'version': 1, 'requirement': expected, 'draft': draft,
            'suggested': artifact_names._suggest(draft['name'], inventory), 'notes_end': len(goal['notes'])}


def question_step(proof, scope):
    text = artifact_names.question_text(proof)
    return ({'tool': 'ask', 'args': {'question': text, MARK: True}, 'note': '確認正式更正後的定稿檔名'},
            {'question': text, MARK: proof, 'goal_conditions_sha256': scope})


def check_action(stale, inventory, action):
    if not stale or action['tool'] not in ('write_artifact', 'deliver'):
        return
    stale_hashes = {row['sha256'] for row in stale.values()}
    if action['tool'] == 'write_artifact':
        name = action['args']['filename'].casefold()
        blocked = name in stale or hashlib.sha256(action['args']['content'].encode('utf-8')).hexdigest() in stale_hashes
    else:
        blocked = any(name.casefold() in stale or inventory.get(name.casefold(), {}).get('sha256') in stale_hashes
                      for name in action['args']['filenames'])
    if blocked:
        raise ValueError('此檔案內容在正式更正之前保存，請保留舊檔並用現行資料另存後交付。')


def validate_saved(db, *, replies_present, guidance_present):
    selected = 'SELECT DISTINCT goal_id FROM autonomous_steps WHERE observation_json LIKE ? OR action_json LIKE ?'
    for (gid,) in db.execute(selected, (f'%"{MARK}"%', f'%"{MARK}"%')):
        objective, notes = db.execute('SELECT objective,notes FROM autonomous_goals WHERE id=?', (gid,)).fetchone()
        steps = [{'sequence': seq, 'action': json.loads(action), 'observation': json.loads(observation)}
                 for seq, action, observation in db.execute('SELECT sequence,action_json,observation_json '
                                                           'FROM autonomous_steps WHERE goal_id=? ORDER BY sequence', (gid,))]
        guidance = goal_guidance_sources.read(db, gid, notes, len(steps)) if guidance_present else []
        replies = goal_reply_sources.read(db, gid, notes, include_positions=True) if replies_present else []
        goal = {'objective': objective, 'notes': notes, 'steps': steps,
                'guidance_sources': guidance, 'reply_sources': replies}
        for index, step in enumerate(steps):
            proof = step['observation'].get(MARK)
            if proof is None and MARK not in step['action']['args']:
                continue
            end = proof.get('notes_end') if isinstance(proof, dict) else None
            if (type(end) is not int or not 0 <= end <= len(notes)
                    or not delivery_preference.enabled_before(db, gid, end)):
                raise ValueError('改版檔名問題缺少有效來源。')
            context = _context_at(goal, end, steps[:index])
            checked = question(context, requirement(context), superseded(context), _first_writes(context['steps']))
            scope = step['observation'].get('goal_conditions_sha256', '')
            if (checked is None or checked != proof or not isinstance(scope, str) or not re.fullmatch('[0-9a-f]{64}', scope)
                    or (step['action'], step['observation']) != question_step(checked, scope)):
                raise ValueError('改版檔名問題與原檔或更正來源不符。')
            draft = checked['draft']
            written = steps[draft['write_step'] - 1]['action']['args']
            stored = db.execute('SELECT name,sha256,content FROM autonomous_artifacts WHERE goal_id=? AND file_key=?',
                                (gid, draft['name'].casefold())).fetchone()
            if stored is None or tuple(stored) != (draft['name'], draft['sha256'], written['content']):
                raise ValueError('改版檔名問題引用的原檔與寫入紀錄不符。')
            if reply := next((item for item in replies if item['ask_sequence'] == step['sequence']), None):
                choose(steps[:index + 1], checked, reply['answer'])
