"""Preserve superseded drafts and bind their replacement names to reviewed replies."""

import hashlib
import json
import re

from . import artifact_names, delivery_preference, goal_artifact_fields, goal_guidance_sources, goal_reply_sources
from . import goal_artifact_totals, goal_artifact_weight_groups, goal_sources, goal_stages


MARK = 'artifact_revision_question'
DELIVERY_MARK = 'artifact_field_revision_check'
GUIDANCE = (
    'superseded_artifacts 是使用者已正式更正後留下的舊檔。依現行更正更新受影響欄位，'
    '可沿用其他仍適用的來源及結果。把新版存為目前指定的檔名，再交付新版。'
    '舊檔保留供回顧，禁止再次交付或用另一個檔名交付相同舊內容。'
    '若有 derived_revisions，依 input_revisions 的更正及原查讀核對該品項合計，保留其他品項。'
    'derived_revisions 的舊值與新值以 value_unit 表示，檔案沿用使用者指定的單位；該列須引 required_corrections。'
    '若沒有要求逐項核對算式，已定位來源的單列合計可直接用 calculate 的 product 重算。'
    '不必為這筆乘法另送 operands。'
    'aggregate_revisions 的 members 是合計的全部已核對品項。沿用未修改的小計，只重算改動品項及其加總。'
    '可用 calculate_expression 在 sum 中結合仍可用的小計和更正後的 product。'
    '修正版延續同一列名稱、全部 member_citations 和所需更正引用。'
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


def _record(step, source):
    receipt = step['observation']['artifact']
    return {'name': receipt['name'], 'sha256': receipt['sha256'], 'write_step': step['sequence'],
            'write_sha256': goal_stages.digest(step), 'citation': source['citation'], 'source_sha256': source['sha256']}


def superseded(goal, *, fields=True, derived=True, aggregated=True):
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
            result[key] = _record(written[key], note)
    if fields:
        original_writes = _first_writes(goal['steps'])
        guidance = {note['citation']: note for note in goal.get('guidance_sources', ())}
        for key, revisions in goal_artifact_fields.provenance(goal, original_writes).items():
            latest = max(revisions, key=lambda row: guidance[row['citation']]['notes_start'])
            record = result.setdefault(key, _record(original_writes[key], guidance[latest['citation']]))
            record['field_revisions'] = revisions
        if derived:
            for key, revisions in goal_artifact_totals.provenance(goal, original_writes).items():
                latest = max((item for revision in revisions for item in revision['input_revisions']),
                             key=lambda row: guidance[row['citation']]['notes_start'])
                record = result.setdefault(key, _record(original_writes[key], guidance[latest['citation']]))
                record['derived_revisions'] = revisions
        if aggregated:
            for key, revisions in goal_artifact_weight_groups.provenance(goal, original_writes).items():
                latest = max((item for revision in revisions for item in revision['input_revisions']),
                             key=lambda row: guidance[row['citation']]['notes_start'])
                record = result.setdefault(key, _record(original_writes[key], guidance[latest['citation']]))
                record['aggregate_revisions'] = revisions
                groups = {(row['entity'], row['csv_column']) for row in revisions}
                if 'derived_revisions' in record:
                    record['derived_revisions'] = [row for row in record['derived_revisions']
                                                   if (row['entity'], row['csv_column']) not in groups]
                    if not record['derived_revisions']:
                        del record['derived_revisions']
    return result


def question(goal, expected, stale, inventory):
    if (expected is None or expected['filename'].casefold() not in stale
            or inventory.keys() != _first_writes(goal['steps']).keys()):
        return None
    draft = stale[expected['filename'].casefold()]
    version = (4 if 'aggregate_revisions' in draft else 3 if 'derived_revisions' in draft else
               2 if 'field_revisions' in draft else 1)
    return {'version': version, 'requirement': expected, 'draft': draft,
            'suggested': artifact_names._suggest(draft['name'], inventory), 'notes_end': len(goal['notes'])}


def question_step(proof, scope):
    text = artifact_names.question_text(proof)
    return ({'tool': 'ask', 'args': {'question': text, MARK: True}, 'note': '確認正式更正後的定稿檔名'},
            {'question': text, MARK: proof, 'goal_conditions_sha256': scope})


def check_action(stale, inventory, action, steps):
    if not stale or action['tool'] not in ('write_artifact', 'deliver'):
        return
    stale_hashes = {row['sha256'] for row in stale.values()}
    if action['tool'] == 'write_artifact':
        name = action['args']['filename'].casefold()
        content = action['args']['content']
        blocked = name in stale or hashlib.sha256(content.encode('utf-8')).hexdigest() in stale_hashes
        candidates = [(name, content)]
    else:
        names = {name.casefold() for name in action['args']['filenames']}
        blocked = any(name in stale or inventory.get(name, {}).get('sha256') in stale_hashes for name in names)
        written = _first_writes(steps)
        candidates = [(name, written[name]['action']['args']['content']) for name in names & written.keys()]
    if blocked:
        raise ValueError('此檔案內容在正式更正之前保存，請保留舊檔並用現行資料另存後交付。')
    revisions = [row for file in stale.values() for row in _revisions(file) if 'aggregation' not in row]
    groups = [row for file in stale.values() for row in file.get('aggregate_revisions', ())]
    csv_contents = [content for name, content in candidates if name.endswith('.csv')]
    if revisions and any(goal_artifact_fields.has_wrong_current_value(content, revisions)
                         for content in csv_contents):
        raise ValueError('CSV 仍含未更新的已更正欄值，請依更正來源重新核對。')
    if groups and any(goal_artifact_weight_groups.has_wrong_current_value(content, groups) for content in csv_contents):
        raise ValueError('CSV 的多品項合計或其來源與已核對的現行資料不符。')


def _revisions(file):
    return [*file.get('field_revisions', ()), *file.get('derived_revisions', ()), *file.get('aggregate_revisions', ())]


def delivery_check(goal, stale):
    reviewed = [stale[key] for key in sorted(stale) if _revisions(stale[key])]
    version = (3 if any('aggregate_revisions' in row for row in reviewed) else
               2 if any('derived_revisions' in row for row in reviewed) else 1)
    return {'version': version, 'notes_end': len(goal['notes']), 'superseded': reviewed} if reviewed else None


def _inventory(db, gid):
    return {name.casefold(): {'name': name, 'sha256': digest, 'content': content}
            for name, digest, content in db.execute(
                'SELECT name,sha256,content FROM autonomous_artifacts WHERE goal_id=?', (gid,))}


def _verify_file(inventory, steps, source):
    record = inventory.get(source['name'].casefold())
    written = steps[source['write_step'] - 1]['action']['args']
    if record != {'name': source['name'], 'sha256': source['sha256'], 'content': written['content']}:
        raise ValueError('更正後的交付核對所引用的原檔與寫入紀錄不符。')


def validate_saved(db, *, replies_present, guidance_present):
    selected = ('SELECT DISTINCT goal_id FROM autonomous_steps WHERE observation_json LIKE ? '
                'OR action_json LIKE ? OR observation_json LIKE ?')
    for (gid,) in db.execute(selected, (f'%"{MARK}"%', f'%"{MARK}"%', f'%"{DELIVERY_MARK}"%')):
        sid, objective, notes, raw = db.execute('SELECT session_id,objective,notes,sources_json '
                                               'FROM autonomous_goals WHERE id=?', (gid,)).fetchone()
        sources = json.loads(raw)
        inventory = _inventory(db, gid)
        steps = [{'sequence': seq, 'action': json.loads(action), 'observation': json.loads(observation)}
                 for seq, action, observation in db.execute('SELECT sequence,action_json,observation_json '
                                                           'FROM autonomous_steps WHERE goal_id=? ORDER BY sequence', (gid,))]
        guidance = goal_guidance_sources.read(db, gid, notes, len(steps)) if guidance_present else []
        replies = goal_reply_sources.read(db, gid, notes, include_positions=True) if replies_present else []
        documents = {row[0]: dict(zip(('id', 'name', 'sha256', 'content'), row)) for row in db.execute(
            'SELECT id,name,sha256,content FROM documents WHERE session_id=?', (sid,))}
        history = goal_sources.verify_history(steps, sources, documents) if goal_sources.has_supplement(steps) else {}
        goal = {'objective': objective, 'notes': notes, 'steps': steps, 'sources': sources,
                'guidance_sources': guidance, 'reply_sources': replies}
        for index, step in enumerate(steps):
            is_question = MARK in step['observation'] or MARK in step['action']['args']
            key = MARK if is_question else DELIVERY_MARK
            if not is_question and key not in step['observation']:
                continue
            proof = step['observation'].get(key)
            end = proof.get('notes_end') if isinstance(proof, dict) else None
            if (type(end) is not int or not 0 <= end <= len(notes)
                    or not delivery_preference.enabled_before(db, gid, end)):
                raise ValueError('改版檔名問題或交付核對缺少有效來源。')
            context = _context_at({**goal, 'sources': history.get(step['sequence'], sources)}, end, steps[:index])
            version = proof.get('version')
            allowed = (1, 2, 3, 4) if is_question else (1, 2, 3)
            if type(version) is not int or version not in allowed:
                raise ValueError('更正後的交付核對版本不符。')
            stale = superseded(context, fields=not is_question or version >= 2,
                               derived=version >= (3 if is_question else 2),
                               aggregated=version == (4 if is_question else 3))
            if not is_question:
                checked_delivery = delivery_check(context, stale)
                if step['action']['tool'] != 'deliver' or checked_delivery is None or checked_delivery != proof:
                    raise ValueError('更正後的交付核對與保存來源不符。')
                check_action(stale, inventory, step['action'], context['steps'])
                for draft in checked_delivery['superseded']:
                    _verify_file(inventory, steps, draft)
                    goal_artifact_fields.verify_documents(_revisions(draft), documents)
                delivered = step['observation'].get('artifacts', ())
                names = [name.casefold() for name in step['action']['args']['filenames']]
                if (names != [item['name'].casefold() for item in delivered]
                        or any(inventory.get(item['name'].casefold(), {}).get('sha256') != item['sha256']
                               for item in delivered)):
                    raise ValueError('更正後的交付核對與交付檔案不符。')
                continue
            checked = question(context, requirement(context), stale, _first_writes(context['steps']))
            scope = step['observation'].get('goal_conditions_sha256', '')
            if (checked is None or checked != proof or not isinstance(scope, str) or not re.fullmatch('[0-9a-f]{64}', scope)
                    or (step['action'], step['observation']) != question_step(checked, scope)):
                raise ValueError('改版檔名問題與原檔或更正來源不符。')
            _verify_file(inventory, steps, checked['draft'])
            goal_artifact_fields.verify_documents(_revisions(checked['draft']), documents)
            if reply := next((item for item in replies if item['ask_sequence'] == step['sequence']), None):
                choose(steps[:index + 1], checked, reply['answer'])
