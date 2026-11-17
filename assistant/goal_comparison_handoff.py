"""Carry the original source of a saved chat comparison into an explicit new goal."""
import hashlib
import re
from contextlib import ExitStack, contextmanager

from . import artifacts, autonomy_status, goal_chat_fields, goal_conversation, goal_guidance_sources, goal_comparison_prose
from .acceptance import strict_json
from .answer_runs import LeaseBusy, lease


BASIS = 'comparison_handoff'
REFERENCE = 'comparison_reference'
REFERENCE_GUIDANCE = (
    'comparison_reference 是本目標選定的比較原問與實際回答。以這一份比較為目標對象，'
    '同對話的其他比較只供回顧。有 verified_work_fields 時依原工作現行來源核對正式欄值。'
    '一般比較依使用者原問及選定的文字或表格，保留當時的假設及未確認條件。'
)
RECEIPT = 'comparison_source_check'
GUIDANCE = (
    'verified_work_fields 是從這個目標明確帶入的同對話比較中定位原工作後重查的欄列。'
    'confirmed 使用現行 value 和各列自己的 citation，先前聊天文字及步驟中的舊值只是歷史。'
    '同樣的來源編號在不同工作各有自己的來源。範圍已選定時沿用該範圍。'
    '有缺值時保留已確認的列，只問真正需要的欄位。只要這些列已足夠，就直接交付，不重問。'
    '原工作是唯讀。若使用者要求來源種類和數字，從該列 citation 分拆，無範圍依要求使用空字串。'
    '只有按本輪有效來源核對過的文件可交付。若先前已保存不同內容，保留舊文件並以新檔名重做，'
    '固定檔名若須變更，按現有程序請使用者決定。'
)
_RECENT = r'(?:剛才|剛剛|最後(?:一個|一次|一張)?|最近(?:一個|一次|一張)?|上一(?:個|次|張)|前一次)'
_TABLE = r'(?:的)?(?:正式)?比較(?P<form>結果|表)?'
_HANDOFF = (r'^(?:請\s*)?(?:把|將|依據|依照|依|以|用|沿用|使用|採用|帶入)\s*'
            r'(?:(?:這|此|本|同一)(?:個)?對話(?:中|內|裡)?(?:的)?\s*)?')
_END = r'(?=$|[，,：:\s（(]|的|為準|作為|整理|帶入|交付|製作|產出|匯出)'
_INTENT = re.compile(rf'{_HANDOFF}{_RECENT}(?:(?P<works>[兩二三四五六2-6])件工作(?:的)?)?{_TABLE}{_END}')
_PINNED = re.compile(rf'{_HANDOFF}回答\s*[#＃]\s*(?P<id>[1-9][0-9]{{0,18}})\s*的?{_TABLE}{_END}')
_TOPIC_CHANGE = re.compile(r'^(?:另一個(?:主題|話題)|換(?:一下|個)?(?:主題|話題)|換題)[，,。：:]\s*')
_TABLE_SEPARATOR = re.compile(r':?-{3,}:?')
_FENCE = re.compile(r'^\s*(?:`{3,}|~{3,})')
_NARROW = re.compile(r'(?:只|僅)(?:取|用|帶入|沿用|保留|採用)(?:第|這|那|有些|部分|其中|[一二三四五六兩]列)')
_CANCEL = re.compile(r'^(?:請\s*)?(?:不要|不必|不用|取消|別|先別)\s*(?:沿用|採用|帶入|使用|核對)[^。]{0,80}比較')
_TEMPLATE = re.compile(r'^(?:範例|示例|例如|格式|(?:問題|提問|問句)(?:請)?(?:用|寫成|格式))')
_SOURCE = re.compile(r'\[(補充|更正)#([1-9][0-9]?)\]')
_HEX = re.compile(r'[0-9a-f]{64}')
_GOAL_ID = re.compile(r'[0-9a-f]{32}')
_JSON_KEYS = ('工作', '欄位', '範圍', '確認值', '來源種類', '來源編號')
_REQUIRED_OUTPUT = {'finish', 'write_artifact', 'deliver'}


def _intent(objective):
    clauses = [clause for _, clause, inquiry in goal_guidance_sources.standalone_clauses(objective) if not inquiry]
    return any(_INTENT.match(clause) for clause in clauses) and not any(_CANCEL.match(clause) for clause in clauses)


def _pinned_answer(objective):
    clauses = [clause for _, clause, inquiry in goal_guidance_sources.standalone_clauses(objective) if not inquiry]
    if any(_CANCEL.match(clause) for clause in clauses):
        return None
    numbers = {int(match['id']) for clause in clauses if (match := _PINNED.match(clause))}
    if len(numbers) > 1 or any(number >= 2**63 - 1 for number in numbers):
        raise ValueError('請指定一個有效的比較回答編號。')
    return next(iter(numbers), None)


def _completed(db, sid, answer_id):
    previous = autonomy_status._latest_completed_answer(db, sid, answer_id + 1)
    return previous if previous and previous[0]['id'] == answer_id else None


def _work_fields(db, sid, row, context):
    summary = context.get('autonomous_goals')
    fields = summary.get('saved_work_fields') if isinstance(summary, dict) else None
    if not isinstance(fields, list) or not all(isinstance(item, dict) for item in fields):
        return None
    return fields if autonomy_status._comparison_origin(db, sid, row, summary, fields) else None


def _origin(db, sid, answer_id):
    if completed := _completed(db, sid, answer_id):
        row, context = completed
        if fields := _work_fields(db, sid, row, context):
            return row, fields
    return None


def _comparison_subjects(text):
    body = autonomy_status._request_body(_TOPIC_CHANGE.sub('', text.strip(), count=1))
    match = goal_chat_fields._COMPARE.match(body)
    if match and not goal_chat_fields._CANCEL_LOOKUP.search(body):
        return body[match.end():]
    return None


def _visible_table(text):
    contiguous = []
    fenced = False
    for line in [*text.splitlines(), '']:
        line = line.strip()
        if _FENCE.match(line):
            fenced = not fenced
        if not fenced and line.startswith('|') and line.endswith('|'):
            contiguous.append([cell.strip() for cell in line[1:-1].split('|')])
            continue
        if len(contiguous) >= 4:
            header, divider, *rows = contiguous
            if (len(header) >= 2 and all(header) and len(divider) == len(header)
                    and all(_TABLE_SEPARATOR.fullmatch(cell) for cell in divider)
                    and all(len(row) == len(header) and row[0] and any(row[1:]) for row in rows)
                    and len({goal_chat_fields.label_key(row[0]) for row in rows}) >= 2):
                return True
        contiguous.clear()
    return False


def _refers_to_table(objective, pattern):
    return any(match['form'] == '表' for _, clause, inquiry in goal_guidance_sources.standalone_clauses(objective)
               if not inquiry and (match := pattern.match(clause)))


def _ordinary_comparison(row, *, allow_prose):
    subjects = _comparison_subjects(row['request'])
    return subjects is not None and (_visible_table(row['content'])
                                     or (allow_prose and goal_comparison_prose.matches(subjects, row['content'])))


def _reference(row, version):
    if max(len(row['request']), len(row['content'])) > goal_conversation.PER_MESSAGE:
        return None
    return {'version': version, 'answer_id': row['id'], 'user_id': row['user_id'],
            'request': row['request'], 'answer': row['content']}


def _indices(objective, fields, *, version):
    clauses = [objective] if version == 1 else [
        clause for _, clause, inquiry in goal_guidance_sources.standalone_clauses(objective)
        if not inquiry and not _TEMPLATE.match(clause)]
    wanted = [(match['work'][1:-1].strip(), match['field'][1:-1].strip())
              for clause in clauses for match in goal_chat_fields._WORK_PAIR.finditer(clause)]
    if not wanted:
        return list(range(len(fields))) if not _NARROW.search(objective) else None
    chosen = []
    for work, field in wanted:
        matches = []
        for index, item in enumerate(fields):
            name = goal_chat_fields.label_key(item['work'])
            label = goal_chat_fields.label_key(item['field'])
            scope = goal_chat_fields.label_key(item.get('scope', ''))
            keys = {label}
            if scope:
                keys.update(scope + connector + label for connector in ('的', '使用的', '所用的'))
            if name == goal_chat_fields.label_key(work) and goal_chat_fields.label_key(field) in keys:
                matches.append(index)
        if len(matches) != 1 or matches[0] in chosen:
            return None
        chosen.append(matches[0])
    return chosen


def _basis(row, fields, indices, version):
    return {'version': version, 'answer_id': row['id'], 'indices': indices,
            'sha256': goal_conversation.digest([row['request'], row['content'], fields])}


def _capture_pinned(db, sid, objective, answer_id, reference_version):
    if not (completed := _completed(db, sid, answer_id)):
        raise ValueError('在本對話找不到指定的已完成回答，請核對比較回答編號。')
    row, context = completed
    if not (reference := _reference(row, reference_version)):
        raise ValueError('指定比較超過可帶入的單則對話長度。')
    if fields := _work_fields(db, sid, row, context):
        if not (indices := _indices(objective, fields, version=3)):
            raise ValueError('指定比較沒有完整對應本次要求的工作與欄位。')
        return {REFERENCE: reference, BASIS: _basis(row, fields, indices, version=3)}
    table_only = _refers_to_table(objective, _PINNED)
    if _ordinary_comparison(row, allow_prose=reference_version >= 2 and not table_only):
        return {REFERENCE: reference}
    raise ValueError('指定回答沒有可核對的比較內容，請核對回答編號及比較形式。')


def capture(db, sid, objective, messages, *, reference_version=2):
    if answer_id := _pinned_answer(objective):
        return _capture_pinned(db, sid, objective, answer_id, reference_version)
    if not _intent(objective):
        return {}
    work_comparison = any(match['works'] for _, clause, inquiry in goal_guidance_sources.standalone_clauses(objective)
                          if not inquiry and (match := _INTENT.match(clause)))
    allow_prose = reference_version >= 2 and not _refers_to_table(objective, _INTENT)
    for item in reversed(messages):
        if item['role'] != 'assistant' or not (completed := _completed(db, sid, item['message_id'])):
            continue
        row, context = completed
        if fields := _work_fields(db, sid, row, context):
            indices = _indices(objective, fields, version=2)
            if indices and not item['content_truncated']:
                return {BASIS: _basis(row, fields, indices, version=2)}
            return {}
        if not work_comparison and _ordinary_comparison(row, allow_prose=allow_prose):
            reference = _reference(row, reference_version)
            return {REFERENCE: reference} if reference else {}
    return {}


def validate_reference(db, sid, objective, reference, messages, basis):
    if (not isinstance(reference, dict) or reference.keys() != {'version', 'answer_id', 'user_id', 'request', 'answer'}
            or type(reference['version']) is not int or reference['version'] not in (1, 2)
            or type(reference['answer_id']) is not int or not 1 <= reference['answer_id'] < 2**63 - 1
            or type(reference['user_id']) is not int or not 1 <= reference['user_id'] < reference['answer_id']):
        raise ValueError('比較回答快照格式不符。')
    answer_id = reference['answer_id']
    version = reference['version']
    if pinned := _pinned_answer(objective):
        matched = pinned == answer_id and _capture_pinned(db, sid, objective, pinned, version) == {
            REFERENCE: reference, **({BASIS: basis} if basis is not None else {})
        }
    else:
        matched = basis is None and capture(db, sid, objective, messages, reference_version=version) == {REFERENCE: reference}
    if not matched:
        raise ValueError('比較回答快照與本次目標選定的來源不符。')


def selected(db, sid, objective, basis):
    if (not isinstance(basis, dict) or set(basis) - {'version'} != {'answer_id', 'indices', 'sha256'}
            or ('version' in basis and (type(basis['version']) is not int or basis['version'] not in (2, 3)))
            or type(basis['answer_id']) is not int or not 1 <= basis['answer_id'] < 2**63 - 1
            or not isinstance(basis['indices'], list) or not 1 <= len(basis['indices']) <= 6
            or any(type(index) is not int or not 0 <= index < 6 for index in basis['indices'])
            or not isinstance(basis['sha256'], str) or not _HEX.fullmatch(basis['sha256'])
            or not (_pinned_answer(objective) == basis['answer_id'] if basis.get('version') == 3 else _intent(objective))):
        raise ValueError('比較交接的保存範圍不符。')
    original = _origin(db, sid, basis['answer_id'])
    if not original:
        raise ValueError('比較交接缺少本對話保存的原比較。')
    row, fields = original
    if (goal_conversation.digest([row['request'], row['content'], fields]) != basis['sha256']
            or _indices(objective, fields, version=basis.get('version', 1)) != basis['indices']):
        raise ValueError('比較交接的原始欄位或保存指紋不符。')
    return [fields[index] for index in basis['indices']]


def _note_hash(notes):
    return hashlib.sha256(notes.encode('utf-8')).hexdigest()


def evidence(db, sid, original, *, cutoffs=None):
    if cutoffs is not None and (not isinstance(cutoffs, list) or len(cutoffs) != len(original)):
        raise ValueError('比較來源的保存數量不符。')
    rows, used = [], []
    for index, entry in enumerate(original):
        name, field = entry['work'], entry['field']
        owner = entry.get('goal_id')
        initial = {'work': name, 'field': field, 'scope': entry.get('scope', ''), 'status': 'unverified'}
        goal = db.execute('''SELECT g.objective,g.notes,
            (SELECT COUNT(*) FROM autonomous_steps s WHERE s.goal_id=g.id)
            FROM autonomous_goals g WHERE g.id=? AND g.session_id=?''', (owner, sid)).fetchone() if owner else None
        if goal is None:
            if cutoffs is not None and cutoffs[index] is not None:
                raise ValueError('比較來源的原工作不存在。')
            rows.append(initial)
            used.append(None)
            continue
        objective, notes, step_count = goal
        if cutoffs is None:
            checkpoint = [step_count, len(notes), _note_hash(notes)]
        else:
            checkpoint = cutoffs[index]
            if (not isinstance(checkpoint, list) or len(checkpoint) != 3
                    or type(checkpoint[0]) is not int or not 0 <= checkpoint[0] <= step_count
                    or type(checkpoint[1]) is not int or not 0 <= checkpoint[1] <= len(notes)
                    or checkpoint[2] != _note_hash(notes[:checkpoint[1]])):
                raise ValueError('比較來源的歷史範圍或指紋不符。')
        selection = entry.get('scope_selection') or {}
        projected = goal_chat_fields.project(db, owner, objective, step_count, field,
                                             selected_scope=selection.get('scope'),
                                             through=checkpoint[:2])
        if initial['scope'] and initial['scope'] != projected.get('scope'):
            projected = {'goal_id': owner, 'status': 'unverified'}
        rows.append({**initial, **projected})
        used.append(checkpoint)
    return {'rows': rows, 'cutoffs': used}


@contextmanager
def locked(store, originals):
    with ExitStack() as locks:
        ids = sorted({item['goal_id'] for item in originals if item.get('goal_id')})
        try:
            for gid in ids:
                locks.enter_context(lease(store, gid))
        except LeaseBusy:
            raise ValueError('原工作正在執行，稍後才能核對比較來源。') from None
        yield


def check_action(objective, action, current, steps):
    tool, args = action['tool'], action['args']
    if tool == 'deliver':
        writes = {step['action']['args']['filename'].casefold(): step
                  for step in steps if step['action']['tool'] == 'write_artifact'}
        expected = {}
        for name in args['filenames']:
            entry = writes.get(name.casefold())
            if not entry or entry['observation'].get(RECEIPT, {}).get('rows') != current['rows']:
                raise ValueError('比較來源已更新或檔案尚未核對。請依目前值重新建立可交付版本。')
            expected[name.casefold()] = entry['action']['args']['content']
        return expected
    if (tool == 'write_artifact' and args['filename'].casefold().endswith('.json')
            and re.search(r'JSON\s*陣列', objective, re.I)
            and all(f'「{key}」' in objective for key in _JSON_KEYS)):
        expected = []
        for row in current['rows']:
            if not (source := _SOURCE.fullmatch(row.get('citation', ''))):
                raise ValueError('仍有比較欄位未正式確認，只追問缺少的來源或範圍。')
            expected.append(dict(zip(_JSON_KEYS, (row['work'], row['field'], row['scope'],
                                                  row['value'], source[1], int(source[2])))))
        output = strict_json(args['content'])
        if (output != expected or any(type(row['來源編號']) is not int for row in output)):
            raise ValueError('比較 JSON 欄位或來源與原工作現行欄值不一致，請按已核對列重建。')
    return None


def validate_steps(db, gid, sid, objective, originals):
    prior = []
    for raw_action, raw_observation in db.execute('''SELECT action_json,observation_json FROM autonomous_steps
            WHERE goal_id=? ORDER BY sequence''', (gid,)):
        action, observation = strict_json(raw_action), strict_json(raw_observation)
        checked = observation.get(RECEIPT)
        if action['tool'] in _REQUIRED_OUTPUT:
            if (not isinstance(checked, dict) or checked.keys() != {'rows', 'cutoffs'}
                    or evidence(db, sid, originals, cutoffs=checked['cutoffs']) != checked):
                raise ValueError('比較來源的歷史欄列與保存紀錄不符。')
            expected = check_action(objective, action, checked, prior)
            if action['tool'] == 'write_artifact':
                args = action['args']
                data = artifacts.payload(args['filename'], args['content'])
                if observation.get('artifact', {}).get('sha256') != hashlib.sha256(data).hexdigest():
                    raise ValueError('比較文件檢查與保存位元組不一致。')
                artifact_content = db.execute('SELECT content FROM autonomous_artifacts WHERE goal_id=? AND file_key=?',
                                              (gid, args['filename'].casefold())).fetchone()
                if not artifact_content or artifact_content[0] != args['content']:
                    raise ValueError('比較文件與寫入步驟不一致。')
            elif expected is not None:
                actual = {entry['name'].casefold(): entry['sha256'] for entry in observation.get('artifacts', [])}
                if actual != {key: _note_hash(content) for key, content in expected.items()}:
                    raise ValueError('比較交付與先前驗證的文件不一致。')
        elif checked is not None:
            raise ValueError('非交付動作含比較來源紀錄。')
        prior.append({'action': action, 'observation': observation})
