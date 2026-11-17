"""Read a requested field from the attributable user records of a single goal."""
import re
import unicodedata

from . import goal_guidance_sources, goal_reply_sources


_LABEL = r'[A-Za-z0-9\u3400-\u9fff][A-Za-z0-9\u3400-\u9fff _·\-]{1,79}'
_FIELD = rf'(?:「(?P<quoted>{_LABEL})」|(?P<field>{_LABEL}?))'
_QUESTION_END = r'(?:是什麼|是多少|是哪一個|是哪個|是哪位|是誰|在哪裡|為何)'
_REQUEST = re.compile(
    r'^(?:請問\s*|(?P<report>(?:請\s*)?(?:只\s*)?(?:回報|告訴我|查詢|查一下|查看))\s*|我\s*)?'
    r'(?:先前在原工作|原工作裡|工作裡)?\s*'
    r'(?:最後(?:一次)?|上次|先前|之前)?\s*'
    r'(?:正式保存|已正式保存|已保存|已正式確認|正式確認|已確認|已存檔)的\s*'
    rf'{_FIELD}(?P<end>{_QUESTION_END})?$')
_DRAFT = re.compile(r'^(?:這一則|本次|這次)(?:先)?(?:只)?(?:談|寫|提到|提供)(?:本次)?草稿')
_ASK = re.compile(
    rf'(?:請\s*(?:提供|告知|填寫|確認)\s*(?P<provide>「{_LABEL}」|{_LABEL})|'
    rf'(?:請問\s*)?(?P<ask>「{_LABEL}」|{_LABEL}?){_QUESTION_END})[。？?]?')
_COMPOUND = re.compile(r'或|以及|及其|[、與和及]\s|^(?:欄位|資料|內容)$')
_VALUE = re.compile(r'[A-Za-z0-9\u3400-\u9fff][A-Za-z0-9\u3400-\u9fff ._/\-·‧（）()＋+－]{0,39}')
_UNCERTAIN = re.compile(
    r'如果|假如|假設|若是|若要|若能|暫定|暫填|草稿|樣例|範例|例如|提議|建議|'
    r'尚未|待確認|未確認|未定|待定|待查|待核對|沒確認|不確定|可能|或許|也許|應該|'
    r'只是|僅供|備選|不要|不採用|不使用|尚待|先試|預計|試填')
_NONOPERATIVE = re.compile(
    r'^(?:如果|假如|假設|若(?:是|要|能|把)|暫定|暫填|草稿|範例|例如|引用|原文寫|'
    r'有人(?:提議|建議|說)|提議|建議|我(?:想先討論|只是|建議))'
    r'|(?:只是(?:提議|草稿)(?:[，,]\s*尚未確認)?|僅供參考|先別採用)[。！!\s]*$')
_NOT_VALUE = re.compile(
    r'或|但是|不過|不是|還是|並且|而且|再說|稍後|其他|同時|改成|改為|撤回|取消|提到|聽說|'
    r'^(?:收到|了解|沒問題|不清楚|不方便|稍後|我再|我先)|'
    r'^(?:有|無|沒有|未知|不明|不詳|好的|可以|是的|好|是|否|暫)$|[嗎呢吧]$')
_PLACEHOLDERS = {'tbd', 'pending', 'unknown', 'none', 'null', 'na'}
MAX_SCOPE_CHOICES = 6
_MAX_WORK_FIELDS = 6
_COMPARE = re.compile(r'^(?:(?:並排|同時|一起)\s*(?:核對|查回|查看|查詢)|比較|比對|對照)\s*')
_SAVED_VALUES = (r'(?:最後(?:一次)?|上次|先前)?\s*(?:正式(?:確認|保存)|已(?:正式)?(?:確認|保存))的?'
                 r'(?:欄值|確認值|值|欄位|資料)(?:是什麼|為何)?')
_COMPARE_HEADER = re.compile(
    rf'(?:(?:這|以下)?(?P<count>[兩二三四五六2-6])(?:件|項)(?:工作|任務)(?:各自)?的?\s*)?'
    rf'{_SAVED_VALUES}[。\uFF1A:；;，,]?\s*')
_COMPARE_SUFFIX = re.compile(rf'^{_SAVED_VALUES}[。？！?!；;，,]?\s*')
_WORK_TITLE = r'(?:「[^「」『』“”"\x00-\x1f\x7f]{2,80}」|『[^「」『』“”"\x00-\x1f\x7f]{2,80}』|“[^「」『』“”"\x00-\x1f\x7f]{2,80}”)'
_QUOTED_FIELD = rf'(?:「{_LABEL}」|『{_LABEL}』|“{_LABEL}”)'
_WORK_PAIR = re.compile(rf'(?P<work>{_WORK_TITLE})\s*(?:的|[：:])\s*(?P<field>{_QUOTED_FIELD})')
_PAIR_BETWEEN = re.compile(r'\s*(?:[、，,。；;]|以及|與|和|及)\s*|\s+')
_PAIR_END = re.compile(r'^[。？！?!；;，,]?\s*')
_FORMAT_TAIL = re.compile(r'^(?:(?:請\s*)?(?:各(?:件|列|自)?|每件|兩件|用|列|輸出|回報|只|缺|若|如果)|'
                          r'格式|結果|資料不足|待確認|並列)')
_CANCEL_LOOKUP = re.compile(r'(?:不要|不必|不用|先別|取消)(?:再)?(?:查詢|查回|比較|核對)')
_ASSIGN = '確定寫|確認寫|確定為|確定是|已確認為|正式確認為|改為|改成|改用|定為|為|是|：|:'
_CORRECTION = '正式改為|正式改成|正式改用|正式確認為|已正式確認為|正式定為'
_ASSIGNMENT = re.compile(rf'(?P<label>「{_LABEL}」|{_LABEL}?)(?P<verb>{_CORRECTION}|{_ASSIGN})\s*(?P<value>.+)')
_FORMAL = re.compile(r'^正式(?:更正|確認)\s*[：:]\s*')
_RETRACT = re.compile(rf'撤回(?:先前|之前)?(?:對)?[「]?(?P<label>{_LABEL}?)[」]?(?:的)?(?:正式)?確認')
_BARE = re.compile(r'(?P<explicit>(?:正式)?(?:確定|確認)(?:寫|用|為|是)\s*)?(?P<value>.+)')


def requested_field(text):
    fields = set()
    for _, clause, inquiry in goal_guidance_sources.standalone_clauses(text):
        if _DRAFT.match(clause):
            clause = re.split(r'[，,]', clause, maxsplit=1)[-1].strip()
        match = _REQUEST.fullmatch(clause)
        if match and ((inquiry and match['end']) or match['report']):
            field = (match['quoted'] or match['field']).strip()
            if not _COMPOUND.search(field):
                fields.add(field)
    return next(iter(fields)) if len(fields) == 1 else None


def requested_work_fields(text):
    """Recognize a bounded comparison of explicitly paired work titles and fields."""
    if len(text) > 2000 or not (request := _COMPARE.match(text)):
        return ()
    rest = text[request.end():]
    header = _COMPARE_HEADER.match(rest)
    if header:
        rest = rest[header.end():]
    items = []
    position = 0
    while pair := _WORK_PAIR.match(rest, position):
        name, field = (value[1:-1].strip() for value in pair.groups())
        if not 2 <= len(name) <= 80 or not 2 <= len(field) <= 80 or _COMPOUND.search(field):
            return ()
        items.append((name, field))
        if len(items) > _MAX_WORK_FIELDS:
            return ()
        position = pair.end()
        if not (separator := _PAIR_BETWEEN.match(rest, position)):
            break
        position = separator.end()
    if len(items) < 2 or len({(label_key(name), label_key(field)) for name, field in items}) != len(items):
        return ()
    tail = rest[position:]
    if not header:
        if not (suffix := _COMPARE_SUFFIX.match(tail)):
            return ()
        tail = tail[suffix.end():]
    elif count := header['count']:
        if len(items) != (int(count) if count.isascii() else '二三四五六'.index(count.replace('兩', '二')) + 2):
            return ()
    tail = _PAIR_END.sub('', tail, count=1)
    if tail and (not _FORMAT_TAIL.match(tail) or _WORK_PAIR.search(tail) or _CANCEL_LOOKUP.search(tail)):
        return ()
    return tuple(items)


def label_key(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def _scope(label, field):
    if label == field:
        return ''
    if label.endswith(field):
        prefix = label[:-len(field)].rstrip()
        return _scope_prefix(prefix[:-1]) if prefix.endswith('的') else prefix
    return None


def _ask_label(question):
    if match := _ASK.fullmatch(question.strip()):
        return (match['provide'] or match['ask']).strip('「」')
    return None


def _scope_prefix(prefix):
    return re.sub(r'(?:使用|所用)$', '', prefix.rstrip()).rstrip()


def _ask_groups(questions, field):
    groups = {}
    for sequence, label in questions:
        if label_key(label) == field:
            groups.setdefault('', ('', []))[1].append(sequence)
            continue
        for end in re.finditer('的', label):
            if label_key(label[end.end():]) == field:
                scope = _scope_prefix(label[:end.start()])
                if scope:
                    groups.setdefault(label_key(scope), (scope, []))[1].append(sequence)
                break
    return groups


def _binding(questions, requested, selected_scope):
    field = label_key(requested)
    groups = _ask_groups(questions, field)
    if selected_scope is not None:
        return field, groups, label_key(selected_scope)
    prefix, delimiter, base = field.rpartition('的')
    if delimiter and prefix and base and not any(groups):
        qualified = _ask_groups(questions, base)
        scope = _scope_prefix(prefix)
        if scope in qualified and scope:
            return base, qualified, scope
    return field, groups, next(iter(groups)) if len(groups) == 1 else None


def _literal(text):
    match = re.fullmatch(r'「([^「」]+)」', text)
    value = match[1] if match else text
    if (_VALUE.fullmatch(value) and value.casefold() not in _PLACEHOLDERS
            and not _UNCERTAIN.search(value) and not _NOT_VALUE.search(value)):
        return value
    return None


def _record_value(content, field, scope, prefixes, *, reply, shared_field):
    """Return confirmed/pending when a record applies, or None for a disjoint record."""
    if _NONOPERATIVE.search(content.strip()):
        return None
    clauses = goal_guidance_sources.standalone_clauses(content)
    if len(clauses) != 1 or clauses[0][2]:
        return ('unverified', None) if reply or field in label_key(content) else None
    clause = clauses[0][1]
    formal = _FORMAL.match(clause)
    core = clause[formal.end():] if formal else clause
    direct = _BARE.fullmatch(core) if reply else None
    if direct and direct['explicit']:
        value = _literal(direct['value'])
        return ('confirmed', value) if value is not None else ('unverified', None)
    match = _ASSIGNMENT.fullmatch(core)
    withdrawal = _RETRACT.fullmatch(core)
    if match or withdrawal:
        label = label_key((match or withdrawal)['label'].strip('「」'))
        for prefix in prefixes:
            if label.startswith(prefix) and (suffix := label[len(prefix):].lstrip()).startswith('的'):
                label = suffix[1:].lstrip()
                break
        named_scope = _scope(label, field)
        if named_scope not in ('', scope):
            return None if not reply else ('unverified', None)
        if withdrawal or (shared_field and not reply and not named_scope):
            return 'unverified', None
        if reply or formal or match['verb'] in _CORRECTION.split('|'):
            value = _literal(match['value'])
            return ('confirmed', value) if value is not None else ('unverified', None)
        return 'unverified', None
    if direct:
        raw = direct['value']
        if raw.startswith('「') or re.fullmatch(r'[A-Za-z0-9\u3400-\u9fff_-]{1,12}', raw):
            if (value := _literal(raw)) is not None:
                return 'confirmed', value
        return 'unverified', None
    if field in label_key(clause):
        return 'unverified', None
    return None


def project(db, gid, objective, step_count, requested, *, selected_scope=None):
    """Project one literal and its source fingerprint, without replaying goal notes."""
    result = {'goal_id': gid, 'field': requested, 'status': 'unverified'}
    notes, = db.execute('SELECT notes FROM autonomous_goals WHERE id=?', (gid,)).fetchone()
    questions = []
    try:
        for seq, raw_action, raw_observation in db.execute('''
                SELECT sequence,action_json,observation_json FROM autonomous_steps
                WHERE goal_id=? AND json_extract(action_json,'$.tool')='ask' ORDER BY sequence''', (gid,)):
            question = goal_reply_sources._question(raw_action, raw_observation)
            if label := _ask_label(question):
                questions.append((seq, label))
        replies = goal_reply_sources.read(db, gid, notes, include_positions=True)
        guidance = goal_guidance_sources.read(db, gid, notes, step_count)
    except ValueError:
        return result
    field, groups, scope = _binding(questions, requested, selected_scope)
    if scope not in groups:
        if (selected_scope is None and 2 <= len(groups) <= MAX_SCOPE_CHOICES and all(groups)
                and all(not _COMPOUND.search(name) for name, _ in groups.values())):
            return {**result, 'status': 'needs_scope', 'scope_choices': [name for name, _ in groups.values()]}
        return result
    display, sequences = groups[scope]
    if display and (selected_scope is not None or label_key(requested) != field):
        result['scope'] = display
    title = label_key(objective.partition('\n')[0])
    project, _, _ = title.partition('的')
    prefixes = [title]
    if project != title and re.search(r'[a-z][ -]?\d+(?![a-z0-9])', project):
        prefixes.append(project.strip())
    records = [(row, row['answer'], True) for row in replies if row['ask_sequence'] in sequences]
    records.extend((row, row['content'], False) for row in guidance)
    for row, content, reply in sorted(records, key=lambda item: item[0]['notes_start'], reverse=True):
        if outcome := _record_value(content, field, scope, prefixes, reply=reply, shared_field=len(groups) > 1):
            state, value = outcome
            if state == 'confirmed':
                return {**result, 'status': state, 'value': value, 'citation': row['citation'],
                        'sha256': row['sha256']}
            return result
    return result
