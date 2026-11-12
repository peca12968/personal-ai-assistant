"""Keep a required final filename tied to the user's actual instruction."""
import re

from . import artifacts, delivery_preference, goal_guidance_sources, goal_reply_sources, goal_stages, work_plan


_NAME = r'(?P<name>`[^`\r\n]+`|「[^」\r\n]+」|[\w][\w .-]{0,63}?)'
_CONDITIONAL = r'(?:(?:若|如果)(?:目前)?(?:需要|要求)?(?:交檔|出檔|交付文件)[，,]?\s*)?'
_INSTRUCTION = re.compile(
    rf'{_CONDITIONAL}(?:最後\s*)?(?:(?:定稿|成果|交付)檔名\s*(?:改為|改成|為|是|：|:)|'
    rf'請\s*(?:交付|交|另存為)|定稿(?:請)?(?:使用|用))\s*{_NAME}')
_USE_TO_DELIVER = re.compile(rf'{_CONDITIONAL}(?:請)?(?:使用|用)\s*{_NAME}\s*交付(?:完整)?(?:結果|成果)')
_ANSWER = re.compile(rf'(?:(?:定稿)?(?:請)?(?:改用|使用|用|選擇|選)(?:\s+|(?=[`「])))?{_NAME}')
_TRAILING = re.compile(r'\s*(?:不要|不用|不必)(?:在|附|把|加|另)')
_END = re.compile(r'[。！!]$')
ASK_MARK = 'artifact_filename_question'
REPLY_VERSION = 6
GUIDANCE = 'artifact_filename_requirement 是使用者已指定的定稿檔名。交付時檔名必須一致。請使用最新欄位及可沿用的核對證據。'


def _parsed(match):
    if match is None:
        return None
    raw = match['name']
    name = raw[1:-1] if raw.startswith(('`', '「')) else raw
    try:
        return artifacts.filename(name)
    except ValueError:
        return None


def declaration(text, *, only=False):
    clauses = goal_guidance_sources.standalone_clauses(text, comma_before=_TRAILING)
    if only and len(clauses) != 1:
        return None
    names = []
    for start, clause, question in clauses:
        if not question and (name := _parsed(_INSTRUCTION.fullmatch(clause) or _USE_TO_DELIVER.fullmatch(clause))):
            names.append({'filename': name, 'start': start, 'end': start + len(clause), 'quote': clause})
    if names and len({row['filename'] for row in names}) == 1:
        return names[-1]
    return None


def requirement(goal):
    expected = declaration(goal['objective'])
    if expected:
        expected = {**expected, 'citation': '[目標]'}
    replies = {row['citation']: row for row in goal.get('reply_sources', ())}
    candidates = []
    for row in goal.get('guidance_sources', ()):
        if match := declaration(row['content'], only=True):
            candidates.append((row['notes_start'], {**match, 'citation': row['citation']}))
    for step in goal['steps']:
        proof = step['observation'].get('scope_reply', {})
        if (choice := proof.get('filename_choice')) and (source := replies.get(proof['citation'])):
            candidates.append((source['notes_start'], {**choice['selection'], 'citation': source['citation']}))
    return max(candidates, key=lambda row: row[0])[1] if candidates else expected


def writes(steps):
    return {receipt['name'].casefold(): {'name': receipt['name'], 'sha256': receipt['sha256'],
                                        'sequence': step['sequence'], 'step_sha256': goal_stages.digest(step),
                                        'scope': step['observation'].get('goal_conditions_sha256')}
            for step in steps if step['action']['tool'] == 'write_artifact'
            and (receipt := step['observation'].get('artifact'))}


def _changed_after(steps, sequence):
    for step in steps[sequence:]:
        scope = step['observation'].get('scope_reply') or step['observation'].get('scope_change') or {}
        if (scope.get('changes') or any(part['state'] == 'confirmed' for part in scope.get('parts', ()))
                or any(row['value'] != row['previous']['value'] for row in scope.get('revisions', ()))):
            return True
    return False


def _suggest(name, used):
    stem, extension = name.rsplit('.', 1)
    for number in range(1, artifacts.MAX_FILES + 2):
        suffix = f'-final-{number}.{extension}' if number > 1 else f'-final.{extension}'
        candidate = stem[:64 - len(suffix)].rstrip() + suffix
        if candidate.casefold() not in used:
            return artifacts.filename(candidate)
    raise ValueError('無法提出可用的定稿檔名。')


def question(goal, scope, expected):
    plan = work_plan.latest(goal['steps'])
    if (not expected or not goal['file_delivery'] or not plan or plan['goal_conditions_sha256'] != scope
            or any(row['state'] not in {'done', 'superseded'} for row in plan['items'])):
        return None
    proof = plan.get('scope_reply') or plan.get('scope_change') or {}
    if proof.get('language', {}).get('pending_revisions'):
        return None
    known = writes(goal['steps'])
    draft = known.get(expected['filename'].casefold())
    if not draft or draft['scope'] == scope or not _changed_after(goal['steps'], draft['sequence']):
        return None
    actual = {row['name'].casefold(): row['sha256'] for row in goal['artifacts']}
    if actual != {key: row['sha256'] for key, row in known.items()}:
        return None
    return {'requirement': expected, 'draft': draft, 'suggested': _suggest(expected['filename'], known),
            'notes_end': len(goal['notes']), 'plan_step': plan['sequence'],
            'plan_sha256': goal_stages.digest(goal['steps'][plan['sequence'] - 1])}


def question_text(proof):
    return f'「{proof["draft"]["name"]}」已有舊草稿。請確認定稿檔名，例如「{proof["suggested"]}」。'


def selection(steps, source, prior_scope):
    ask = steps[-1]
    proof = ask['observation'].get(ASK_MARK)
    if (not proof or ask['action']['tool'] != 'ask' or ask['action']['args'].get(ASK_MARK) is not True
            or ask['sequence'] != source['ask_sequence'] or ask['observation'].get('goal_conditions_sha256') != prior_scope
            or goal_reply_sources.checked_question(ask['action'], ask['observation']) != source['question']):
        raise ValueError('定稿檔名答覆與當前問題或條件不一致。')
    literal = _END.sub('', source['answer']).rstrip()
    name = _parsed(_ANSWER.fullmatch(literal))
    if not name or name.casefold() in writes(steps) or name.rsplit('.', 1)[1].casefold() != proof['draft']['name'].rsplit('.', 1)[1].casefold():
        raise ValueError('請提供同格式且尚未使用的定稿檔名。')
    return {'selection': {'filename': name, 'start': 0, 'end': len(literal), 'quote': literal},
            'question_step': ask['sequence'], 'question_sha256': goal_stages.digest(ask)}


def check_action(expected, action):
    name = expected['filename']
    if action['tool'] == 'deliver' and action['args']['filenames'] != [name]:
        raise ValueError(f'使用者指定定稿檔名為「{name}」。請依目前指定檔名交付。')
    if action['tool'] == 'write_artifact' and expected['citation'].startswith('[補充#') and action['args']['filename'] != name:
        raise ValueError(f'請用已確認的定稿檔名「{name}」保存文件。')


def verify_question(db, goal, prior, step, replies, guidance):
    proof = step['observation'].get(ASK_MARK)
    if not isinstance(proof, dict) or type(proof.get('notes_end')) is not int or not 0 <= proof['notes_end'] <= len(goal['notes']):
        raise ValueError('定稿檔名問題缺少有效來源。')
    end = proof['notes_end']
    known = writes(prior)
    actual = {name.casefold(): sha for name, sha in db.execute(
        'SELECT name,sha256 FROM autonomous_artifacts WHERE goal_id=?', (goal['id'],))}
    if any(actual.get(key) != row['sha256'] for key, row in known.items()):
        raise ValueError('定稿檔名問題的原草稿已改變。')
    context = {**goal, 'notes': goal['notes'][:end], 'steps': prior, 'artifacts': list(known.values()),
               'file_delivery': delivery_preference.enabled_before(db, goal['id'], end),
               'reply_sources': [row for row in replies if row['notes_start'] < end],
               'guidance_sources': [row for row in guidance if row['notes_start'] < end]}
    scope = work_plan.conditions(context)
    expected = question(context, scope, requirement(context))
    if (expected is None or expected != proof or step['observation'].get('goal_conditions_sha256') != scope
            or step['action']['tool'] != 'ask'
            or step['action']['args'] != {'question': question_text(expected), ASK_MARK: True}
            or step['observation'].get('question') != question_text(expected)):
        raise ValueError('定稿檔名問題與原指示或舊草稿不一致。')
