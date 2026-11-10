"""Compare exact quantities in already read lines and saved user sources."""
import json
import re
from fractions import Fraction

from . import document_units, exact_math, expression_math, goal_guidance_sources, goal_reply_sources, operand_evidence, source_decision
from .document_table import _SAME_UNIT_ONLY


GUIDANCE = '''原量缺單位、相同欄位有兩筆數值，或比例基數有不同候選時，已有相關原話就先用check_quantities核對，再決定是否詢問。args例如{"unit":"公斤","candidates":[{"label":"本批配料","citation":"[D1:L1]","quote":"配料 450"}]}。每次一至四筆，每筆逐字引用一個數值和已有的原單位，僅引用本次適用的對象。unit是比較單位，可用支援的重量、長度、容量、時間、件數單位或ratio。引用限已完整查讀的單行、同一目標已保存的補充或更正，及[目標]。
needs_evidence時保留原數及其他已核對值。若短句含多個數字而無法定位，先縮到該欄完整數值。單位待補只問該欄單位。只需確認換算值時，可再次核對，該候選另加unit_evidence，例如{"citation":"[補充#3]","quote":"本批配料的單位是公克","unit":"公克"}。另案單位不能補本批。equivalent代表同單位的數值相同，對同一欄位採一次；different代表換算後仍不同，保留各候選，若影響交付只問採哪筆或哪個基數。只要條件式成果時列分支，不另追問。工具核對文字及精確數值，來源適用範圍仍依已保存的使用者原話。
若確認後還要算式，可在calculate_expression的expression旁直接加quantity_inputs，不必重送check_quantities。每筆寫path、step及從1起算的candidate，例如{"path":"/values/1","step":2,"candidate":1,"unit_evidence":{"citation":"[補充#3]","quote":"本批配料的單位是公克","unit":"公克"}}。只補單位就沿用原數並附這項證據；只確認採用哪個已核對的值就附selection，例如{"citation":"[補充#3]","quote":"本次採完成查驗件數為基數。"}。確認句須明確採用原候選的完整label。算式葉節點填核對後的精確值。若原數已更改，按新資料核對。同一條件已有可用的數量核對也可直接連結，不重讀。各袋分別有明確數值而只需加總時，直接用既有算式，無需比較為同一欄位。'''

GUIDANCE += '''
多筆來源以『欄位「完整名稱」為數值』明列同一欄位時，在check_quantities的args另填field為原文完整名稱。各候選的label用版本名稱，quote包含原句欄位與數值。程式核對同欄位的各筆原話和精確值。正式採用既有版本仍用selection。若最新回覆的完整原話是『正式更正欄位「完整名稱」為數值和單位。』，可直接在算式寫{"path":"/values/1","step":2,"replacement":{"citation":"[補充#3]","quote":"正式更正欄位「完整名稱」為 7 件。"}}，不用填candidate或再次送核對。引用須對應同一欄位及最新正式補充，算式數字用更正後精確值。原候選仍留在核對步驟。明確採用正式數值後不用再問；仍未確認時只問採用哪筆，若要求條件式就保留分支。'''

_TARGETS = frozenset(document_units.SCALES) | _SAME_UNIT_ONLY | {'片', '袋', 'ratio'}
_UNITS = '|'.join(re.escape(unit) for unit in sorted(_TARGETS - {'ratio'}, key=lambda unit: (-len(unit), unit)))
_BOUNDARY = r'(?=$|[\s,，。；;、！!？?：:（()）\[\]「」『』【】]|\.(?!\S))'
_MEASURED = re.compile(operand_evidence._NUMBER + rf'[ \t]*(?P<unit>{_UNITS}|%)'
                       + rf'(?:(?<=%)(?![\d%/^])|(?<!%){_BOUNDARY})')
_BARE = re.compile(operand_evidence._LITERAL.pattern + _BOUNDARY)
_DOCUMENT = operand_evidence._DOCUMENT
_USER = operand_evidence._USER
_METADATA = {'companion_profile', 'engine_selection', 'goal_conditions_sha256'}
_SENTENCE_BREAKS = '。.!?！？\r\n'


def _short_text(value, limit):
    return (isinstance(value, str) and 1 <= len(value) <= limit and value == value.strip()
            and not any(char in value for char in '\0\r\n'))


def _user_excerpt(evidence, fields):
    return (isinstance(evidence, dict) and evidence.keys() == fields
            and _short_text(evidence['quote'], 180)
            and isinstance(evidence['citation'], str) and _USER.fullmatch(evidence['citation']))


def _unit_evidence(value):
    return (_user_excerpt(value, {'citation', 'quote', 'unit'})
            and isinstance(value['unit'], str) and value['unit'] in _TARGETS - {'ratio'})


def validate(args):
    if (not isinstance(args, dict) or args.keys() not in ({'unit', 'candidates'}, {'unit', 'candidates', 'field'})
            or not isinstance(args['unit'], str) or args['unit'] not in _TARGETS
            or not isinstance(args['candidates'], list) or not 1 <= len(args['candidates']) <= 4):
        raise ValueError('數量核對須有可用比較單位與一至四筆來源。')
    if 'field' in args and not _short_text(args['field'], 80):
        raise ValueError('同欄位核對須提供原文明列的完整欄位名。')
    seen = set()
    for row in args['candidates']:
        if (not isinstance(row, dict) or row.keys() not in ({'label', 'citation', 'quote'},
                                                           {'label', 'citation', 'quote', 'unit_evidence'})
                or not _short_text(row['label'], 80) or not _short_text(row['quote'], 180)
                or not isinstance(row['citation'], str)
                or not (_DOCUMENT.fullmatch(row['citation']) or _USER.fullmatch(row['citation'])
                        or row['citation'] == '[目標]')):
            raise ValueError('候選數量須有欄位名、逐字短句與本目標來源。')
        key = row['citation'], row['quote']
        if key in seen:
            raise ValueError('候選數量不可重複引用同一則短句。')
        seen.add(key)
        if 'unit_evidence' in row:
            evidence = row['unit_evidence']
            if not _unit_evidence(evidence) or evidence['citation'] == row['citation']:
                raise ValueError('單位補正須引用另一則保存的使用者答覆或更正，並指定支援的單位。')


def _tokens(text):
    normalized = text.translate(operand_evidence._NUMERIC_CHARS)
    matches = {match.span('number'): (match.group('number'), None, match.end())
               for match in _BARE.finditer(normalized)}
    matches.update((match.span('number'), (match.group('number'), match.group('unit'), match.end()))
                   for match in _MEASURED.finditer(normalized))
    return matches


def _literal_quote(quote, text, sha):
    start = text.find(quote)
    if start < 0 or text.find(quote, start + 1) >= 0:
        raise ValueError('數量短句在已保存的原話中無法唯一定位。')
    return {'source_sha256': sha, 'quote_start': start, 'quote_end': start + len(quote)}


def _factor(source, target):
    if source == target:
        return Fraction(1)
    if (source, target) == ('%', 'ratio'):
        return Fraction(1, 100)
    scales = document_units.SCALES
    if source in scales and target in scales and scales[source][0] == scales[target][0]:
        return scales[source][1] / scales[target][1]
    return None


def _confirmed_sentence(quote, source):
    text, sha, _ = source
    proof = _literal_quote(quote, text, sha)
    start, end = proof['quote_start'], proof['quote_end']
    sentence = quote.rstrip(_SENTENCE_BREAKS)
    prefix, suffix = text[:start].rstrip(), text[end:].lstrip()
    if ((prefix and prefix[-1] not in _SENTENCE_BREAKS)
            or (suffix and quote[-1] not in _SENTENCE_BREAKS and suffix[0] not in _SENTENCE_BREAKS)
            or any(char in sentence for char in _SENTENCE_BREAKS + '，,；;')
            or sentence.startswith('若')
            or any(word in sentence for word in ('如果', '假設', '可能', '待確認', '尚未', '不是', '不確定'))):
        raise ValueError('補充須引用完整且明確確認的原句。')
    return sentence, proof


def _unit_confirmation(supplied, source):
    sentence, proof = _confirmed_sentence(supplied['quote'], source)
    unit = re.escape(supplied['unit'])
    if not re.fullmatch(rf'(?:.*單位(?:確定|確認)?(?:為|是|用|採) *)?{unit}', sentence):
        raise ValueError('單位補正須引用完整且明確確認單位的原句。')
    return {**supplied, **proof}


def _candidate(row, source_texts, target, field=None):
    citation, quote = row['citation'], row['quote']
    if citation not in source_texts:
        raise ValueError('數量核對須引用已完整查讀或當時已保存的本目標來源。')
    text, sha, position = source_texts[citation]
    result = {key: row[key] for key in ('label', 'citation', 'quote')}
    result.update(_literal_quote(quote, text, sha))
    tokens = _tokens(quote)
    if field is not None:
        tokens = {span: value for span, value in tokens.items()
                  if (match := operand_evidence._FIELD.search(quote[:span[0]])) and match['name'] == field}
        if len(tokens) != 1:
            raise ValueError('候選短句須有唯一明列欄位及緊接的原數值。')
    if len(tokens) != 1:
        return {**result, 'status': 'quantity_unconfirmed'}
    (low, high), (raw, unit, token_end) = next(iter(tokens.items()))
    start = result['quote_start']
    original = _tokens(text).get((start + low, start + high))
    if original != (raw, unit, start + token_end):
        raise ValueError('數量短句截斷了完整原數量或單位。')
    number = exact_math.parse(raw)
    result.update(number=quote[low:high], source_exact=str(number), source_unit=unit,
                  number_start=start + low, number_end=start + high)
    if field is not None:
        field_start, field_end = operand_evidence._field_position(quote, low, field)
        result.update(field=field, field_start=start + field_start, field_end=start + field_end)
    if 'unit_evidence' in row:
        supplied = row['unit_evidence']
        unit_source = source_texts.get(supplied['citation'])
        if unit is not None or unit_source is None or unit_source[2] < position:
            raise ValueError('單位補正須接續缺單位的原數量及當時已保存的使用者來源。')
        unit = supplied['unit']
        result['unit_evidence'] = _unit_confirmation(supplied, unit_source)
    if unit is None:
        return {**result, 'status': 'unit_unconfirmed'}
    factor = _factor(unit, target)
    if factor is None:
        return {**result, 'status': 'incompatible_unit'}
    converted = exact_math.bounded(number * factor)
    return {**result, 'status': 'confirmed', 'effective_unit': unit, 'factor_exact': str(factor),
            'converted_exact': str(converted)}


def observe(args, steps, sources, objective='', replies=(), guidance=()):
    validate(args)
    refs = [match for row in args['candidates'] if (match := _DOCUMENT.fullmatch(row['citation']))]
    ranges = [{'document_id': int(match[1]), 'start_line': int(match[2]), 'end_line': int(match[2])}
              for match in refs]
    _, lines = source_decision.gather_lines(ranges, steps, sources)
    source_texts = {f'[D{did}:L{line}]': (text, sha, position)
                    for (did, line), (position, sha, text) in lines.items()}
    sequence = len(steps) + 1
    positions = {row['citation']: row['ask_sequence'] for row in replies}
    positions.update((row['citation'], row['steps_seen']) for row in guidance)
    source_texts.update((citation, (text, sha, positions.get(citation, 0)))
                        for citation, (text, sha) in operand_evidence.user_texts(objective, replies, guidance, sequence).items())
    field = args.get('field')
    quantities = [_candidate(row, source_texts, args['unit'], field) for row in args['candidates']]
    values = list(dict.fromkeys(row['converted_exact'] for row in quantities if row['status'] == 'confirmed'))
    if any(row['status'] != 'confirmed' for row in quantities):
        status = 'needs_evidence'
    elif len(values) > 1:
        status = 'different'
    else:
        status = 'equivalent' if len(quantities) > 1 else 'confirmed'
    result = {'status': status, 'unit': args['unit'], 'quantities': quantities, 'possible_exact': values}
    if field is not None:
        result['field'] = field
    return result


def validate_expression(args):
    if not isinstance(args, dict) or args.keys() != {'expression', 'quantity_inputs'}:
        raise ValueError('使用核對值的算式須提供 expression 及 quantity_inputs。')
    expression_math.validate({'expression': args['expression']})
    leaves = dict(operand_evidence._leaves(args['expression']))
    bindings = args['quantity_inputs']
    if not isinstance(bindings, list) or not 1 <= len(bindings) <= len(leaves):
        raise ValueError('算式需連結一至多個不同的數字葉節點。')
    seen = set()
    common = {'path', 'step'}
    required = common | {'candidate'}
    for binding in bindings:
        if (not isinstance(binding, dict)
                or binding.keys() not in (required, required | {'unit_evidence'}, required | {'selection'},
                                           common | {'replacement'})
                or not isinstance(binding['path'], str) or binding['path'] not in leaves or binding['path'] in seen
                or type(binding['step']) is not int or not 1 <= binding['step'] <= 32
                or ('candidate' in binding and (type(binding['candidate']) is not int
                                                 or not 1 <= binding['candidate'] <= 4))):
            raise ValueError('核對值須提供唯一算式路徑、先前核對步驟及候選序號或正式更正。')
        if ('unit_evidence' in binding and not _unit_evidence(binding['unit_evidence'])
                or 'selection' in binding and not _user_excerpt(binding['selection'], {'citation', 'quote'})
                or 'replacement' in binding and not _user_excerpt(binding['replacement'], {'citation', 'quote'})):
            raise ValueError('單位、選用確認或正式更正須引用已保存的完整補充。')
        seen.add(binding['path'])


def _latest_confirmation(citation, step, sequence, replies, guidance):
    positions = {row['citation']: row['ask_sequence'] for row in replies if step < row['ask_sequence'] < sequence}
    positions.update((row['citation'], row['steps_seen']) for row in guidance if step <= row['steps_seen'] < sequence)
    latest = max(positions.values(), default=-1)
    if [key for key, value in positions.items() if value == latest] != [citation]:
        raise ValueError('沿用先前核對須引用核對後最新且可辨認的正式補充。')
    text, sha = operand_evidence.user_texts('', replies, guidance, sequence)[citation]
    return text, sha, latest


def _selection_proof(selection, candidate, original, source):
    sentence, proof = _confirmed_sentence(selection['quote'], source)
    matching = [i for i, row in enumerate(original['candidates']) if re.fullmatch(
        rf'(?:本次|正式)?(?:採用|採|選用|以) *{re.escape(row["label"])}(?:為準|為基數|作為基數)?', sentence)]
    if matching != [candidate]:
        raise ValueError('選用補充須明確且唯一確認原候選的完整名稱。')
    return {**selection, **proof}


def _replacement_quantity(replacement, field, target, source):
    text, _, _ = source
    quote = replacement['quote']
    head = re.match(rf'正式更正欄位「{re.escape(field)}」[ \t]*為[ \t]*', quote) if field else None
    if (head is None or text.strip() != quote
            or not _MEASURED.fullmatch(quote[head.end():].removesuffix('。').translate(operand_evidence._NUMERIC_CHARS))):
        raise ValueError('正式更正須完整明列同一欄位、單一數值與單位。')
    return _candidate({**replacement, 'label': field}, {replacement['citation']: source}, target, field)


def calculate_expression(args, steps, sources, scope, objective='', replies=(), guidance=(), cancel=None):
    validate_expression(args)
    expression_math.check_cancel(cancel)
    sequence = len(steps) + 1
    leaves = dict(operand_evidence._leaves(args['expression']))
    by_step = {step['sequence']: step for step in steps}
    bindings = []
    for binding in args['quantity_inputs']:
        number = binding['step']
        saved = by_step.get(number)
        if saved is None or saved['action']['tool'] != 'check_quantities':
            raise ValueError('算式須引用同一目標先前實際完成的數量核對。')
        original = saved['action']['args']
        prior = [step for step in steps if step['sequence'] < number]
        verified = observe(original, prior, sources, objective, replies, guidance)
        if verified != {key: value for key, value in saved['observation'].items() if key not in _METADATA}:
            raise ValueError('原數量核對與保存的來源不一致。')
        if 'replacement' in binding:
            replacement = binding['replacement']
            source = _latest_confirmation(replacement['citation'], number, sequence, replies, guidance)
            quantity = _replacement_quantity(replacement, verified.get('field'), original['unit'], source)
            receipt = {'replacement': replacement}
        else:
            index = binding['candidate'] - 1
            if index >= len(verified['quantities']):
                raise ValueError('候選序號不在原數量核對。')
            quantity = verified['quantities'][index]
            receipt = {'candidate': binding['candidate']}
            if 'unit_evidence' in binding:
                confirmation = binding['unit_evidence']
                _latest_confirmation(confirmation['citation'], number, sequence, replies, guidance)
                if quantity['status'] != 'unit_unconfirmed':
                    raise ValueError('只可替原核對缺少單位的候選補單位。')
                request = {'unit': original['unit'], 'candidates': [
                    {**original['candidates'][index], 'unit_evidence': confirmation}]}
                if 'field' in original:
                    request['field'] = original['field']
                quantity, = observe(request, steps, sources, objective, replies, guidance)['quantities']
            elif 'selection' in binding:
                selection = binding['selection']
                source = _latest_confirmation(selection['citation'], number, sequence, replies, guidance)
                receipt['selection'] = _selection_proof(selection, index, original, source)
            elif verified['status'] == 'different' or saved['observation'].get('goal_conditions_sha256') != scope:
                raise ValueError('候選數量尚有差異或屬先前條件，須有目前正式補充才能沿用。')
            elif 'field' in verified and verified['status'] == 'needs_evidence' and len(verified['quantities']) > 1:
                raise ValueError('同欄位的候選尚未核對完整，須有正式採用或更正才能代入。')
        if (quantity['status'] != 'confirmed'
                or exact_math.parse(quantity['converted_exact']) != exact_math.parse(leaves[binding['path']])):
            raise ValueError(f'算式位置 {binding["path"] or "根"} 與已確認的精確數量不符。')
        bindings.append({'path': binding['path'], 'step': number, **receipt,
                         'unit': original['unit'], 'quantity': quantity})
    result = expression_math.calculate({'expression': args['expression']}, cancel)
    return {**result, 'quantity_inputs': bindings}


def validate_saved(db, *, replies_present, guidance_present):
    query = '''SELECT DISTINCT g.id,g.notes,g.sources_json,g.objective FROM autonomous_goals g
               JOIN autonomous_steps s ON s.goal_id=g.id WHERE s.action_json LIKE '%"check_quantities"%'
               OR s.action_json LIKE '%"quantity_inputs"%' '''
    for gid, notes, source_json, objective in db.execute(query):
        sources = json.loads(source_json)
        rows = db.execute('SELECT sequence,action_json,observation_json FROM autonomous_steps WHERE goal_id=? '
                          'ORDER BY sequence', (gid,)).fetchall()
        replies = goal_reply_sources.read(db, gid, notes) if replies_present else []
        guidance = goal_guidance_sources.read(db, gid, notes, len(rows)) if guidance_present else []
        prior = []
        for sequence, action_json, observation_json in rows:
            action, observation = json.loads(action_json), json.loads(observation_json)
            if action['tool'] == 'check_quantities':
                expected = observe(action['args'], prior, sources, objective, replies, guidance)
                actual = {key: value for key, value in observation.items() if key not in _METADATA}
                if actual != expected:
                    raise ValueError('保存的數量核對與原話或換算結果不一致。')
            if action['tool'] == 'calculate_expression' and 'quantity_inputs' in action['args']:
                scope = observation.get('goal_conditions_sha256')
                if not isinstance(scope, str) or not re.fullmatch(r'[0-9a-f]{64}', scope):
                    raise ValueError('保存的算式缺少有效數量條件。')
                expected = calculate_expression(action['args'], prior, sources, scope, objective, replies, guidance)
                actual = {key: value for key, value in observation.items() if key not in _METADATA}
                if actual != expected:
                    raise ValueError('保存的算式與數量核對關聯不一致。')
            prior.append({'sequence': sequence, 'action': action, 'observation': observation})
