"""Compare exact quantities in already read lines and saved user sources."""
import json
import re
from fractions import Fraction

from . import (
    conversation_history, document_units, exact_math, expression_math, goal_guidance_sources, goal_reply_sources,
    operand_evidence, source_decision, work_plan,
)
from .document_table import _SAME_UNIT_ONLY


GUIDANCE = '''原量缺單位、相同欄位有兩筆數值，或比例基數有不同候選時，已有相關原話就先用check_quantities核對，再決定是否詢問。args例如{"unit":"公斤","candidates":[{"label":"本批配料","citation":"[D1:L1]","quote":"配料 450"}]}。每次一至四筆，每筆逐字引用一個數值和已有的原單位，僅引用本次適用的對象。unit是比較單位，可用支援的重量、長度、容量、時間、件數單位或ratio。引用限已完整查讀的單行、同一目標已保存的補充或更正，及[目標]。
needs_evidence時保留原數及其他已核對值。若短句含多個數字而無法定位，先縮到該欄完整數值。單位待補只問該欄單位。只需確認換算值時，可再次核對，該候選另加unit_evidence，例如{"citation":"[補充#3]","quote":"本批配料的單位是公克","unit":"公克"}。另案單位不能補本批。equivalent代表同單位的數值相同，對同一欄位採一次；different代表換算後仍不同，保留各候選，若影響交付只問採哪筆或哪個基數。只要條件式成果時列分支，不另追問。工具核對文字及精確數值，來源適用範圍仍依已保存的使用者原話。
若確認後還要算式，可在calculate_expression的expression旁直接加quantity_inputs，不必重送check_quantities。每筆寫path、step及從1起算的candidate，例如{"path":"/values/1","step":2,"candidate":1,"unit_evidence":{"citation":"[補充#3]","quote":"本批配料的單位是公克","unit":"公克"}}。只補單位就沿用原數並附這項證據；只確認採用哪個已核對的值就附selection，例如{"citation":"[補充#3]","quote":"本次採完成查驗件數為基數。"}。確認句須明確採用原候選的完整label。算式葉節點填核對後的精確值。若原數已更改，按新資料核對。同一條件已有可用的數量核對也可直接連結，不重讀。各袋分別有明確數值而只需加總時，直接用既有算式，無需比較為同一欄位。'''

GUIDANCE += '''
多筆來源以『欄位「完整名稱」為數值』明列同一欄位時，在check_quantities的args另填field為原文完整名稱。各候選的label用版本名稱，quote包含原句欄位與數值。程式核對同欄位的各筆原話和精確值。正式採用既有版本仍用selection。若最新回覆的完整原話是『正式更正欄位「完整名稱」為數值和單位。』，可直接在算式寫{"path":"/values/1","step":2,"replacement":{"citation":"[補充#3]","quote":"正式更正欄位「完整名稱」為 7 件。"}}，不用填candidate或再次送核對。引用須對應同一欄位及最新正式補充，算式數字用更正後精確值。原候選仍留在核對步驟。明確採用正式數值後不用再問；仍未確認時只問採用哪筆，若要求條件式就保留分支。'''

GUIDANCE += '''
文件使用『版本「複核版」欄位「完整欄位名」為 8 件』及獨立一行『欄位「完整欄位名」｜採用版本｜複核版』時，可在有field的check_quantities另填adoption_ranges，例如[{"document_id":3,"start_line":1,"end_line":1}]。納入本次所有相關採用行，包括互相矛盾的記載。候選label逐字用原版名，quote最好包含版名、欄位和數值。adoption.status為adopted時，算式quantity_inputs直接帶path、step、該adoption.candidate，不另詢問或補selection。版本與採用行各自引用。若adoption為unrecorded、conflicting、incomplete或candidate_unavailable，依回傳保留版本、採用記載或缺行，日期和檔名不決定生效。資料確實不足且需要定稿才問。後續明確使用者採用或正式更正可用既有selection或replacement。'''

SCOPE_GUIDANCE = ('quantity_adoption_scope是使用者在目標首行固定的欄位及完整採用區段。'
                  '把數值原行和missing_ranges中可取得的行用一次read_ranges查讀，不重讀已取得的行。'
                  '對照documents的line_count，若有不存在的行，只讀實際存在的部分，交付時列缺行。'
                  'check_quantities填原欄位及逐字版名，由工具帶入整個宣告，省略adoption_ranges。'
                  '保留另欄採用紀錄及其引用，但不視為本欄採用。未讀齊或互相矛盾時不把已讀的首筆當定稿。'
                  '可依要求交各版的條件結果及缺漏。')

BRANCH_GUIDANCE = (
    '同一具名欄位的採用未定，且使用者要各版的條件式計算時，用calculate_quantity_branches一次獨立代入。'
    'args例如{"step":2,"expression":{"operation":"difference","values":["30","$candidate"]},'
    '"operands":[{"path":"/values/0","citation":"[D1:L1]","quote":"本批原備 30 盒"}]}。'
    'step引用本次條件下已完成且含adoption的check_quantities。'
    '算式只在候選位置放一次字串"$candidate"，其餘每個數字提供逐字運算元來源。'
    '工具依每版已核對數值分開計算，branches各列出version、數值證據、採用引用及獨立算式和結果。'
    '採用衝突只列該欄有採用記載的版本，缺候選或缺單位的分支仍列待查。'
    '依使用者指定的成果欄位交付各版算式及可得結果，保留adoption的缺行或矛盾。'
    '各版互斥，不用sum把各版的結果合成一數，不以分支數字當正式採用。'
    '唯一正式採用仍用calculate_expression與quantity_inputs。')

ALIGNED_BRANCH_GUIDANCE = (
    '若同一採用版本決定多個欄位，且各欄已有分開保存的check_quantities步驟，可沿用這些核對。'
    '各完整欄位的候選label逐字使用原文版名，且核對使用同一完整採用範圍。'
    '再用calculate_quantity_branches，例如{"step":3,"expression":{"operation":"difference",'
    '"values":["$candidate","$candidate"]},"bindings":[{"path":"/values/0","step":2},'
    '{"path":"/values/1","step":3}],"operands":[]}。外層step是決定版本範圍的核對，須出現在bindings。'
    '每個版本欄位各用一個$candidate，bindings用實際步驟和葉路徑。固定數字仍用operands附逐字來源。'
    '以完整版本名配對，不按行序、數字或近似版名。分支缺同版欄位、單位或遇另欄明文採用限制時，保留原值及缺口，不借用其他版。'
    '只有原問或來源支持各欄共用版本時才使用跨欄配對。')

BATCH_GUIDANCE = (
    '同一採用範圍下要分版核算二至四個欄位時，優先一次用check_quantity_fields核對各欄，'
    'args為{"fields":[{"field":"完整欄位名","unit":"件","candidates":[{"label":"原文版名","citation":"[D1:L1]","quote":"逐字原文"}]},'
    '{"field":"另一完整欄位名","unit":"件","candidates":[]}]}。每欄用已讀原行及完整版名；'
    '整欄確實缺數時才留空，某版缺數時保留該欄其他已核對版本。'
    '已宣告範圍時須含宣告欄位，由工具把同一範圍套到全批；未宣告時才在外層填adoption_ranges。'
    '數值原行單獨查讀。缺採用行、另欄採用或缺單位時保存對應缺口。'
    '算式一次引用該批核對，例如{"step":2,"anchor_field":"決定候選的完整欄位名",'
    '"expression":{"operation":"difference","values":["$candidate","$candidate"]},'
    '"bindings":[{"path":"/values/0","field":"完整原備欄位名"},{"path":"/values/1","field":"完整退回欄位名"}],"operands":[]}。'
    'step填實際check_quantity_fields步驟，anchor_field須在bindings內。'
    '批次後直接引用欄位，不重送單欄核對。單欄及唯一正式採用仍使用原有流程。')

BATCH_CORRECTION_GUIDANCE = (
    '若原批次核對後只新增一則正式更正，例如『正式更正版本「甲」欄位「本批退回」為 6 件。』，'
    '直接重用原批次和原算式，args另填"correction":"[補充#4]"或實際更正編號。'
    '程式核對原話、完整版本與欄位和補充前後的條件。只代入指定欄位的該版數值，保留原值、其他原文及採用限制。'
    '缺同版數字也可用符合格式的更正補齊。列明更新值及更正來源，原值有資料時一併保留。'
    '若原批次後有二至四則正式更正，改用"corrections":["[補充#4]","[補充#6]"]，'
    '依實際保存順序列齊原批次後的所有更正，包含前一輪已套用的更正。沿用原批次和算式。'
    'correction_history保留各則原話，同版同欄以最後一筆代入，不同版或欄各自生效。'
    '看到quantity_note_review時，優先依其編號和模式處理。')

BATCH_NOTE_REVIEW_GUIDANCE = (
    'quantity_note_review逐筆區分正式更正、明確維持目前數字的試想，以及需重核的補充。'
    'mode為correction或corrections時，沿用原批次分版算式，以citations提供實際編號。'
    'mode為reviewed_notes時，在原批次分版算式填"reviewed_notes":citations的完整有序清單。'
    '工具保存試想原話但不代入。交付中分開列正式數值、試想與採用限制。'
    'mode為recheck時不要重試這個批次的更正工具。按需重核的補充指出變更和缺口，'
    '沿用可確認的原值或已核對更正，區分新交付要求和試算；只有影響交付且必須補資料時才問。')

_TARGETS = frozenset(document_units.SCALES) | _SAME_UNIT_ONLY | {'片', '袋', 'ratio'}
_UNITS = '|'.join(re.escape(unit) for unit in sorted(_TARGETS - {'ratio'}, key=lambda unit: (-len(unit), unit)))
_BOUNDARY = r'(?=$|[\s,，。；;、！!？?：:（()）\[\]「」『』【】]|\.(?!\S))'
_MEASURED = re.compile(operand_evidence._NUMBER + rf'[ \t]*(?P<unit>{_UNITS}|%)'
                       + rf'(?:(?<=%)(?![\d%/^])|(?<!%){_BOUNDARY})')
_BARE = re.compile(operand_evidence._LITERAL.pattern + _BOUNDARY)
_DOCUMENT = operand_evidence._DOCUMENT
_USER = operand_evidence._USER
_DOCUMENT_ADOPTION = re.compile(r'欄位「(?P<field>[^「」｜\x00\r\n]{1,80})」｜採用版本｜(?P<version>[^「」｜\x00\r\n]{1,80})')
_VERSION_CORRECTION = re.compile(
    r'正式更正版本「(?P<version>[^「」｜\x00\r\n]{1,80})」[ \t]*'
    r'欄位「(?P<field>[^「」｜\x00\r\n]{1,80})」[ \t]*為[ \t]*')
_RETAINED_HYPOTHESIS = re.compile(
    r'(?:若)?(?:試想|假設)(?:把|將)(?P<subject>[^，,。；;！？\r\n]{1,100})改成'
    r'[ \t]*(?P<quantity>[^，,。；;！？\r\n]{1,80})[，,][ \t]*'
    r'(?:這只是假設|這只是試想|僅供試算)[，,][ \t]*'
    r'(?P<retention>維持第一輪正式數字|維持目前正式數字|沿用目前正式值)[。]?')
_SCOPE_PREFIX = '數量採用範圍：'
_SCOPE = re.compile(r'欄位「([^「」｜\x00\r\n]{1,80})」｜(.+)')
_METADATA = {'companion_profile', 'engine_selection', 'goal_conditions_sha256'}
_SENTENCE_BREAKS = '。.!?！？\r\n'
_BRANCH_LEAF = '$candidate'


def _short_text(value, limit):
    return (isinstance(value, str) and 1 <= len(value) <= limit and value == value.strip()
            and not any(char in value for char in '\0\r\n'))


def declared_scope(objective):
    first = objective.partition('\n')[0].removesuffix('\r')
    if not first.startswith(_SCOPE_PREFIX):
        return None
    invalid = '數量採用範圍請在目標首行列出完整欄位與行號，例如欄位「本批退回」｜[D3:L1]。'
    match = _SCOPE.fullmatch(first.removeprefix(_SCOPE_PREFIX))
    if match is None or not _short_text(match[1], 80):
        raise ValueError(invalid)
    return {'field': match[1], 'ranges': source_decision.parse_range_list(match[2], invalid=invalid)}


def _check_scope_sources(scope, sources):
    if not {row['document_id'] for row in scope['ranges']} <= {row['document_id'] for row in sources}:
        raise ValueError('請先在目前對話匯入宣告的數量採用文件。')


def save_scope(db, gid, scope, sources):
    _check_scope_sources(scope, sources)
    db.execute('INSERT INTO autonomous_quantity_scopes(goal_id,scope_json) VALUES(?,?)',
               (gid, json.dumps(scope, ensure_ascii=False)))


def _verified_scope(payload, objective, sources):
    scope = declared_scope(objective)
    saved = json.loads(payload)
    if scope is None or scope != saved:
        raise ValueError('保存的數量採用範圍與目標原文不一致。')
    source_decision._validate_ranges(saved['ranges'])
    _check_scope_sources(scope, sources)
    return scope


def read_scope(db, gid, objective, sources):
    row = db.execute('SELECT scope_json FROM autonomous_quantity_scopes WHERE goal_id=?', (gid,)).fetchone()
    return _verified_scope(row[0], objective, sources) if row else None


def _scoped_ranges(args, declared):
    ranges = declared['ranges']
    if 'adoption_ranges' in args:
        supplied = set(source_decision._positions(args['adoption_ranges']))
        if supplied != set(source_decision._positions(ranges)):
            raise ValueError('數量採用須涵蓋目標首行宣告的全部區段。請省略adoption_ranges以套用宣告。')
    return ranges


def _apply_scope(args, declared):
    if declared is None or args.get('field', declared['field']) != declared['field']:
        return args
    return {**args, 'field': declared['field'], 'adoption_ranges': _scoped_ranges(args, declared)}


def _user_excerpt(evidence, fields):
    return (isinstance(evidence, dict) and evidence.keys() == fields
            and _short_text(evidence['quote'], 180)
            and isinstance(evidence['citation'], str) and _USER.fullmatch(evidence['citation']))


def _unit_evidence(value):
    return (_user_excerpt(value, {'citation', 'quote', 'unit'})
            and isinstance(value['unit'], str) and value['unit'] in _TARGETS - {'ratio'})


def validate(args, *, batch_field=False):
    required = {'unit', 'candidates'}
    if (not isinstance(args, dict) or not required <= args.keys() <= required | {'field', 'adoption_ranges'}
            or not isinstance(args['unit'], str) or args['unit'] not in _TARGETS
            or not isinstance(args['candidates'], list) or not (0 if batch_field else 1) <= len(args['candidates']) <= 4):
        raise ValueError('數量核對須有可用比較單位與一至四筆來源。')
    if 'field' in args and not _short_text(args['field'], 80):
        raise ValueError('同欄位核對須提供原文明列的完整欄位名。')
    if 'adoption_ranges' in args:
        if 'field' not in args:
            raise ValueError('文件採用核對須指定完整欄位名。')
        source_decision._validate_ranges(args['adoption_ranges'])
    seen = set()
    versions = set()
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
        if batch_field or 'adoption_ranges' in args:
            label = row['label']
            if (not _DOCUMENT.fullmatch(row['citation']) or label in versions
                    or any(char in label for char in '「」｜')):
                raise ValueError('文件採用核對須有各自具唯一完整版名的原文數值。')
            versions.add(label)
        if 'unit_evidence' in row:
            evidence = row['unit_evidence']
            if not _unit_evidence(evidence) or evidence['citation'] == row['citation']:
                raise ValueError('單位補正須引用另一則保存的使用者答覆或更正，並指定支援的單位。')


def validate_fields(args):
    if (not isinstance(args, dict) or args.keys() not in ({'fields'}, {'fields', 'adoption_ranges'})
            or not isinstance(args['fields'], list) or not 2 <= len(args['fields']) <= 4):
        raise ValueError('批次數量核對須提供二至四個欄位及一份共用採用範圍。')
    if 'adoption_ranges' in args:
        source_decision._validate_ranges(args['adoption_ranges'])
    names = set()
    for field in args['fields']:
        if not isinstance(field, dict) or field.keys() != {'field', 'unit', 'candidates'}:
            raise ValueError('批次中的每個欄位只接受完整名稱、比較單位及候選原文。')
        validate(field, batch_field=True)
        name = field['field']
        if name in names or any(char in name for char in '「」｜'):
            raise ValueError('批次核對須使用不同的原文完整欄位名。')
        names.add(name)


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


def _adoption_lines(ranges, lines):
    records, missing = [], []
    for did, line in source_decision._positions(ranges):
        citation = f'[D{did}:L{line}]'
        saved = lines.get((did, line))
        if saved is None:
            missing.append(citation)
            continue
        _, sha, text = saved
        match = _DOCUMENT_ADOPTION.fullmatch(text.strip().removesuffix('。'))
        if match and _short_text(match['field'], 80) and _short_text(match['version'], 80):
            records.append({**match.groupdict(), 'citation': citation, 'source_sha256': sha, 'quote': text})
    return records, missing


def _document_adoption(field, adoption, quantities, source_texts):
    for quantity in quantities:
        label = quantity['label']
        text = source_texts[quantity['citation']][0]
        version = re.search(rf'版本「({re.escape(label)})」[ \t]*欄位「\Z', text[:quantity['field_start']])
        if version is None:
            raise ValueError('候選版名須逐字出現在該欄位及數字前的原文。')
        quantity.update(version_start=version.start(1), version_end=version.end(1))
    records, missing = adoption
    result = {'records': records}
    if missing:
        return {**result, 'status': 'incomplete', 'missing': missing}
    versions = {row['version'] for row in records if row['field'] == field}
    if not versions:
        return {**result, 'status': 'unrecorded'}
    if len(versions) != 1:
        return {**result, 'status': 'conflicting'}
    version, = versions
    candidate = next((i for i, row in enumerate(quantities, 1) if row['label'] == version), None)
    if candidate is None:
        return {**result, 'status': 'candidate_unavailable', 'version': version}
    return {**result, 'status': 'adopted', 'version': version, 'candidate': candidate}


def _candidate_ranges(args):
    refs = [match for row in args['candidates'] if (match := _DOCUMENT.fullmatch(row['citation']))]
    return [{'document_id': int(match[1]), 'start_line': int(match[2]), 'end_line': int(match[2])}
            for match in refs]


def _source_texts(lines, objective, replies, guidance, sequence):
    source_texts = {f'[D{did}:L{line}]': (text, sha, position)
                    for (did, line), (position, sha, text) in lines.items()}
    positions = {row['citation']: row['ask_sequence'] for row in replies}
    positions.update((row['citation'], row['steps_seen']) for row in guidance)
    source_texts.update((citation, (text, sha, positions.get(citation, 0)))
                        for citation, (text, sha) in operand_evidence.user_texts(objective, replies, guidance, sequence).items())
    return source_texts


def _compare(args, source_texts, adoption):
    field = args.get('field')
    quantities = [_candidate(row, source_texts, args['unit'], field) for row in args['candidates']]
    values = list(dict.fromkeys(row['converted_exact'] for row in quantities if row['status'] == 'confirmed'))
    if not quantities or any(row['status'] != 'confirmed' for row in quantities):
        status = 'needs_evidence'
    elif len(values) > 1:
        status = 'different'
    else:
        status = 'equivalent' if len(quantities) > 1 else 'confirmed'
    result = {'status': status, 'unit': args['unit'], 'quantities': quantities, 'possible_exact': values}
    if field is not None:
        result['field'] = field
    if adoption is not None:
        result['adoption'] = _document_adoption(field, adoption, quantities, source_texts)
    return result


def observe(args, steps, sources, objective='', replies=(), guidance=(), *, declared=None):
    validate(args)
    effective = _apply_scope(args, declared)
    uses_declared = effective is not args
    if uses_declared:
        validate(effective)
    ranges = effective.get('adoption_ranges', [])
    _, lines = source_decision.gather_lines(_candidate_ranges(effective) + ranges, steps, sources)
    texts = _source_texts(lines, objective, replies, guidance, len(steps) + 1)
    result = _compare(effective, texts, _adoption_lines(ranges, lines) if ranges else None)
    if uses_declared:
        result['adoption']['declared_ranges'] = ranges
    return result


def observe_fields(args, steps, sources, objective='', replies=(), guidance=(), *, declared=None):
    validate_fields(args)
    fields = args['fields']
    ranges = args.get('adoption_ranges')
    if declared is not None:
        if declared['field'] not in {field['field'] for field in fields}:
            raise ValueError('批次核對須包含目標宣告的採用欄位。')
        ranges = _scoped_ranges(args, declared)
    if not ranges:
        raise ValueError('批次核對需指定全批共用的採用範圍。')
    _check_scope_sources({'ranges': ranges}, sources)
    candidates = [item for field in fields for item in _candidate_ranges(field)]
    _, lines = source_decision.gather_lines(candidates + ranges, steps, sources)
    texts = _source_texts(lines, objective, replies, guidance, len(steps) + 1)
    records, missing = _adoption_lines(ranges, lines)
    comparisons = []
    for field in fields:
        result = _compare(field, texts, (records, missing))
        result['adoption'].pop('records')
        result['adoption'].pop('missing', None)
        comparisons.append(result)
    return {'status': 'quantity_fields_checked', 'adoption_ranges': ranges,
            'adoption_records': records, 'missing': missing, 'comparisons': comparisons}


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


def _verified_comparison(number, steps, sources, objective, replies, guidance, declared):
    saved = next((step for step in steps if step['sequence'] == number), None)
    if saved is None or saved['action']['tool'] != 'check_quantities':
        raise ValueError('算式須引用同一目標先前實際完成的數量核對。')
    original = saved['action']['args']
    prior = [step for step in steps if step['sequence'] < number]
    verified = observe(original, prior, sources, objective, replies, guidance, declared=declared)
    if verified != {key: value for key, value in saved['observation'].items() if key not in _METADATA}:
        raise ValueError('原數量核對與保存的來源不一致。')
    return saved, verified


def validate_branches(args):
    fields = {'step', 'expression', 'operands'}
    if (not isinstance(args, dict) or args.keys() not in
            (fields, fields | {'bindings'}, fields | {'bindings', 'anchor_field'},
             fields | {'bindings', 'anchor_field', 'correction'}, fields | {'bindings', 'anchor_field', 'corrections'},
             fields | {'bindings', 'anchor_field', 'reviewed_notes'})
            or type(args['step']) is not int or not 1 <= args['step'] <= 32):
        raise ValueError('分版計算須提供本目標的核對步驟、算式與其餘運算元原文。')
    if 'correction' in args and (not isinstance(args['correction'], str) or not _USER.fullmatch(args['correction'])):
        raise ValueError('批次更正須引用本目標已保存的補充或更正編號。')
    if 'corrections' in args or 'reviewed_notes' in args:
        citations = args.get('corrections', args.get('reviewed_notes'))
        minimum = 1 if 'reviewed_notes' in args else 2
        if (not isinstance(citations, list) or not minimum <= len(citations) <= 4
                or any(not isinstance(citation, str) or not _USER.fullmatch(citation) for citation in citations)
                or len(set(citations)) != len(citations)):
            raise ValueError('請按保存順序提供不同的補充或更正編號，最多四則。')
    expression = args['expression']
    expression_math.validate({'expression': expression}, placeholder=_BRANCH_LEAF)
    placeholders = [path for path, value in operand_evidence._leaves(expression) if value == _BRANCH_LEAF]
    if 'bindings' not in args:
        if len(placeholders) != 1:
            raise ValueError('分版算式須用一次 "$candidate" 表示互斥版本的數量。')
        bindings = {placeholders[0]: args['step']}
    else:
        rows = args['bindings']
        key = 'field' if 'anchor_field' in args else 'step'
        if not isinstance(rows, list) or not 2 <= len(rows) == len(placeholders) <= 4:
            raise ValueError('跨欄分版需有二至四個版本欄位及對應的代入位置。')
        bindings = {}
        for row in rows:
            if (not isinstance(row, dict) or row.keys() != {'path', key}
                    or not isinstance(row['path'], str) or row['path'] not in placeholders or row['path'] in bindings):
                raise ValueError('跨欄綁定須使用實際的不同候選位置。')
            value = row[key]
            valid = _short_text(value, 80) if key == 'field' else type(value) is int and 1 <= value <= 32
            if not valid or value in bindings.values():
                raise ValueError('跨欄綁定須用不同數量核對步驟或不同批次欄位。')
            bindings[row['path']] = value
        if args.get('anchor_field', args['step']) not in bindings.values():
            raise ValueError('跨欄綁定須包含決定候選版本的數量核對步驟或欄位。')
    operand_evidence.validate(args['operands'], expression, None, omitted_paths=bindings)
    return bindings


def _substitute_candidates(node, values, path=''):
    if isinstance(node, str):
        return values.get(path, node)
    return {**node, 'values': [_substitute_candidates(child, values, f'{path}/values/{index}')
                               for index, child in enumerate(node['values'])]}


def _calculate_branch(expression, values, cancel):
    expression = _substitute_candidates(expression, values)
    try:
        calculation = expression_math.calculate({'expression': expression}, cancel)
    except expression_math.CalculationFailure as exc:
        return {'status': 'calculation_failed', 'expression': expression, 'calculation_failure': exc.details()}
    return {'status': 'calculated', 'expression': expression, 'calculation': calculation}


def _branch_operands(args, steps, sources, objective, replies, guidance):
    ranges = []
    for row in args['operands']:
        match = _DOCUMENT.fullmatch(row['citation'])
        if match:
            did, line = map(int, match.groups())
            ranges.append({'document_id': did, 'start_line': line, 'end_line': line})
    _, found = source_decision.gather_lines(ranges, steps, sources)
    excerpts = [{'document_id': did, 'start_line': line, 'end_line': line, 'sha256': sha, 'text': f'L{line}: {text}'}
                for (did, line), (_, sha, text) in found.items()]
    user_sources = operand_evidence.user_texts(objective, replies, guidance, len(steps) + 1)
    return operand_evidence.bind(args['operands'], args['expression'], excerpts, user_sources)


def _branch_comparison(number, steps, sources, scope, objective, replies, guidance, declared, *, anchor):
    saved, verified = _verified_comparison(number, steps, sources, objective, replies, guidance, declared)
    adoption = verified.get('adoption')
    if adoption is None:
        raise ValueError('分版計算須引用有文件採用範圍的數量核對。')
    if anchor and adoption['status'] == 'adopted':
        raise ValueError('決定候選的文件核對已有唯一正式採用版本。')
    if saved['observation'].get('goal_conditions_sha256') != scope:
        raise ValueError('分版計算須先依目前補充或更正重新核對版本。')
    if adoption['status'] == 'incomplete':
        latest = observe(saved['action']['args'], steps, sources, objective, replies, guidance, declared=declared)
        if latest != verified:
            raise ValueError('採用範圍已補讀，請以目前讀到的原文重新核對版本。')
    ranges = adoption.get('declared_ranges', saved['action']['args'].get('adoption_ranges'))
    return verified, set(source_decision._positions(ranges))


def _correction_transition(citations, goal, before, after, replies, guidance):
    if goal is None or before == after:
        raise ValueError('批次更正須核對更正前後不同的工作條件。')
    notes = goal['notes']
    reply_by_citation = {row['citation']: row for row in replies}
    guidance_by_citation = {row['citation']: row for row in guidance}
    blocks, anchors = [], []
    offset, last_reply = 0, 0
    for citation in citations:
        source = guidance_by_citation.get(citation)
        if source is not None:
            block = goal_guidance_sources.note(source['content'], source['steps_seen'], source['recorded_at'])
            anchors.append((offset, source['notes_start']))
        else:
            source = reply_by_citation[citation]
            if source['ask_sequence'] <= last_reply:
                raise ValueError('更正編號須按實際保存順序排列。')
            last_reply = source['ask_sequence']
            block = goal_reply_sources.note(source['question'], source['answer'])
        blocks.append(block)
        offset += len(block)
    for match in re.finditer(re.escape(''.join(blocks)), notes):
        start, end = match.span()
        if (all(start + relative == actual for relative, actual in anchors)
                and work_plan.conditions({**goal, 'notes': notes[:start]}) == before
                and work_plan.conditions({**goal, 'notes': notes[:end]}) == after):
            return
    if len(citations) == 1:
        raise ValueError('批次核對後只有這一則正式更正時，才可沿用其餘數量。')
    raise ValueError('更正編號須依保存順序涵蓋原批次後到本次算式前的所有補充。')


def _available_corrections(number, sequence, replies, guidance):
    available = {row['citation']: (row['answer'], row['sha256'], row['ask_sequence']) for row in replies
                 if number < row['ask_sequence'] < sequence}
    available.update((row['citation'], (row['content'], row['sha256'], row['steps_seen'])) for row in guidance
                     if number <= row['steps_seen'] < sequence)
    return available


def _correction_sources(args, sequence, replies, guidance):
    number = args['step']
    if 'correction' in args:
        citation = args['correction']
        return {citation: _latest_confirmation(citation, number, sequence, replies, guidance)}
    citations = args.get('corrections', args.get('reviewed_notes'))
    if citations is None:
        return {}
    available = _available_corrections(number, sequence, replies, guidance)
    if set(citations) != available.keys():
        raise ValueError('多筆更正須列齊原批次後到本次算式前已保存的補充或更正。')
    return {citation: available[citation] for citation in citations}


def _batch_formula(args):
    return (args['expression'], sorted(args['bindings'], key=lambda row: row['path']),
            sorted(args['operands'], key=lambda row: row['path']))


def _batch_comparisons(verified, declared):
    comparisons = {}
    for row in verified['comparisons']:
        adoption = {**row['adoption'], 'records': verified['adoption_records']}
        if verified['missing']:
            adoption['missing'] = verified['missing']
        if declared is not None:
            adoption['declared_ranges'] = verified['adoption_ranges']
        comparisons[row['field']] = {**row, 'adoption': adoption}
    return comparisons


def _batch_for_branches(args, steps, sources, scope, objective, replies, guidance, declared, goal):
    number = args['step']
    saved = next((step for step in steps if step['sequence'] == number), None)
    if saved is None or saved['action']['tool'] != 'check_quantity_fields':
        raise ValueError('分版算式須引用本目標已完成的批次數量核對。')
    previous_scope = saved['observation'].get('goal_conditions_sha256')
    corrections = _correction_sources(args, len(steps) + 1, replies, guidance)
    if corrections:
        _correction_transition(corrections, goal, previous_scope, scope, replies, guidance)
        earlier = [_batch_formula(step['action']['args']) for step in steps
                   if step['action']['tool'] == 'calculate_quantity_branches'
                   and step['action']['args'].get('step') == number
                   and step['action']['args'].get('anchor_field') == args['anchor_field']
                   and step['observation'].get('goal_conditions_sha256') == previous_scope]
        if earlier and _batch_formula(args) not in earlier:
            raise ValueError('正式更正僅更新數值，須沿用原批次已核對的分版算式與來源。')
        if any(row['citation'] in corrections for row in args['operands']):
            raise ValueError('版本數量更正只能代入指定分支的欄位。')
    elif previous_scope != scope:
        raise ValueError('分版計算須先依目前補充或更正重新核對版本。')
    if declared is not None and args['anchor_field'] != declared['field']:
        raise ValueError('批次分版須由目標宣告的採用欄位決定候選版本。')
    original = saved['action']['args']
    prior = [step for step in steps if step['sequence'] < number]
    verified = observe_fields(original, prior, sources, objective, replies, guidance, declared=declared)
    if verified != {key: value for key, value in saved['observation'].items() if key not in _METADATA}:
        raise ValueError('原批次數量核對與保存的來源不一致。')
    if verified['missing']:
        latest = observe_fields(original, steps, sources, objective, replies, guidance, declared=declared)
        if latest != verified:
            raise ValueError('採用範圍已補讀，請以目前讀到的原文重新核對版本。')
    return _batch_comparisons(verified, declared), corrections


def _version_sources(verified):
    candidates = {row['label']: (index, row) for index, row in enumerate(verified['quantities'], 1)}
    citations = {}
    for record in verified['adoption']['records']:
        if record['field'] == verified['field']:
            citations.setdefault(record['version'], []).append(record['citation'])
    return candidates, citations


def _explicit_adoption(verified):
    return verified['adoption']['status'] in {'adopted', 'conflicting', 'candidate_unavailable'}


def _aligned_input(version, comparison, candidates, citations, restricted, correction):
    inputs = {**comparison, 'adoption_citations': citations.get(version, [])}
    candidate = candidates.get(version)
    if candidate is not None:
        index, quantity = candidate
        inputs.update(candidate=index, quantity=quantity)
    if correction is not None:
        if candidate is not None:
            inputs['original_quantity'] = inputs['quantity']
        inputs['quantity'] = correction
    if restricted and version not in citations:
        return {**inputs, 'status': 'outside_adoption'}
    return {**inputs, 'status': inputs.get('quantity', {}).get('status', 'candidate_unavailable')}


def _aligned_branches(args, versions, inputs_by_path, cancel, corrections=None):
    evidence = []
    for path, number, verified in inputs_by_path:
        info = {'path': path, 'step': number, 'field': verified['field'], 'unit': verified['unit'],
                'adoption_status': verified['adoption']['status']}
        candidates, citations = _version_sources(verified)
        evidence.append((info, candidates, citations, _explicit_adoption(verified)))
    branches = []
    corrections = corrections or {}
    for version in versions:
        expression_math.check_cancel(cancel)
        inputs = [_aligned_input(version, *entry, corrections.get((version, entry[0]['field']))) for entry in evidence]
        branch = {'version': version, 'inputs': inputs, 'status': 'needs_evidence'}
        if all(row['status'] == 'confirmed' for row in inputs):
            values = {row['path']: row['quantity']['converted_exact'] for row in inputs}
            branch.update(_calculate_branch(args['expression'], values, cancel))
        branches.append(branch)
    return {'comparisons': [entry[0] for entry in evidence], 'branches': branches}


def _version_correction(citation, source, versions, fields, comparisons):
    text = source[0]
    match = _VERSION_CORRECTION.match(text)
    if (match is None
            or not _MEASURED.fullmatch(text[match.end():].removesuffix('。').translate(operand_evidence._NUMERIC_CHARS))):
        raise ValueError('分版更正須為一整則正式原話，明列版本、欄位、數值與單位。')
    version, field = match['version'], match['field']
    if version not in versions or field not in fields:
        raise ValueError('正式更正須對應本次既有候選版本與算式中的完整欄位。')
    quantity = _candidate({'citation': citation, 'label': version, 'quote': text},
                          {citation: source}, comparisons[field]['unit'], field)
    if quantity['status'] != 'confirmed':
        raise ValueError('正式更正的單位須能換算至該欄位的比較單位。')
    return {**quantity, 'version_start': match.start('version'), 'version_end': match.end('version')}


def _hypothetical_target(text, versions, fields, history, comparisons):
    match = _RETAINED_HYPOTHESIS.fullmatch(text)
    if match is None:
        return None
    value = _MEASURED.fullmatch(match['quantity'].translate(operand_evidence._NUMERIC_CHARS))
    if value is None:
        return None
    subject = match['subject']
    targets = [(version, field) for version in versions for field in fields if subject in {
        version + field, version + '版' + field, f'版本「{version}」欄位「{field}」',
        version + field.split('本批', 1)[-1], version + '版' + field.split('本批', 1)[-1]}]
    if len(targets) != 1:
        return None
    version, field = targets[0]
    if _factor(value['unit'], comparisons[field]['unit']) is None:
        return None
    if match['retention'] == '維持第一輪正式數字':
        edits = [item for item in history if (item['label'], item['field']) == (version, field)]
        if not history or edits != history[:1]:
            return None
    return version, field


def _reviewed_quantity_notes(corrections, versions, fields, comparisons):
    history, retained, review = [], [], []
    for citation, source in corrections.items():
        text, sha, _ = source
        if _VERSION_CORRECTION.match(text):
            try:
                quantity = _version_correction(citation, source, versions, fields, comparisons)
            except ValueError as exc:
                review.append({'citation': citation, 'status': 'needs_recheck', 'reason': str(exc)})
            else:
                history.append(quantity)
                review.append({'citation': citation, 'status': 'formal_correction',
                               'version': quantity['label'], 'field': quantity['field']})
            continue
        target = _hypothetical_target(text, versions, fields, history, comparisons)
        if target is None:
            review.append({'citation': citation, 'status': 'needs_recheck',
                           'reason': '補充無法核對為單筆正式更正或明確沿用目前數值的試想。'})
            continue
        version, field = target
        note = {'citation': citation, 'status': 'hypothetical_retained', 'version': version, 'field': field}
        retained.append({**note, 'quote': text, 'source_sha256': sha})
        review.append(note)
    return history, retained, review


def _ordered_review_sources(goal, available, replies, guidance):
    blocks = {row['citation']: (None, goal_reply_sources.note(row['question'], row['answer']))
              for row in reversed(replies) if row['citation'] in available}
    blocks.update((row['citation'], (row['notes_start'],
                  goal_guidance_sources.note(row['content'], row['steps_seen'], row['recorded_at'])))
                  for row in guidance if row['citation'] in available)
    notes = goal['notes']
    reverse = []
    while blocks:
        suffixes = [(citation, block) for citation, (start, block) in blocks.items()
                    if notes.endswith(block) and (start is None or start == len(notes) - len(block))]
        if not suffixes:
            return {}
        citation, block = max(suffixes, key=lambda pair: len(pair[1]))
        reverse.append(citation)
        notes = notes[:-len(block)]
        del blocks[citation]
    return {citation: available[citation] for citation in reversed(reverse)}


def review_batch_notes(goal, scope, replies, guidance):
    prior_branch = next((step for step in reversed(goal['steps'])
                         if step['action']['tool'] == 'calculate_quantity_branches'
                         and 'anchor_field' in step['action']['args']), None)
    if prior_branch is None or prior_branch['observation'].get('goal_conditions_sha256') == scope:
        return None
    args = prior_branch['action']['args']
    batch = next((step for step in goal['steps'] if step['sequence'] == args['step']
                  and step['action']['tool'] == 'check_quantity_fields'), None)
    if batch is None:
        return None
    available = _available_corrections(args['step'], len(goal['steps']) + 1, replies, guidance)
    result = {'step': args['step'], 'anchor_field': args['anchor_field'], 'mode': 'recheck'}
    if not 1 <= len(available) <= 4:
        return {**result, 'reason': '原批次後的補充來源不足或超出四則。'}
    ordered = _ordered_review_sources(goal, available, replies, guidance)
    if not ordered:
        return {**result, 'reason': '無法對照原批次後的完整補充順序。'}
    current = {key: value for key, value in args.items()
               if key not in {'correction', 'corrections', 'reviewed_notes'}}
    current['reviewed_notes'] = list(ordered)
    try:
        comparisons, _ = _batch_for_branches(current, goal['steps'], goal['sources'], scope, goal['objective'],
                                             replies, guidance, goal.get('quantity_adoption_scope'), goal)
    except ValueError as exc:
        return {**result, 'reason': str(exc)}
    anchor = comparisons[args['anchor_field']]
    candidates, citations = _version_sources(anchor)
    versions = citations if _explicit_adoption(anchor) else candidates | citations
    _, retained, review = _reviewed_quantity_notes(ordered, versions, [row['field'] for row in args['bindings']],
                                                   comparisons)
    mode = 'reviewed_notes' if retained else 'correction' if len(ordered) == 1 else 'corrections'
    if any(row['status'] == 'needs_recheck' for row in review):
        mode = 'recheck'
    return {**result, 'mode': mode, 'citations': list(ordered), 'notes': review}


def _calculate_batch_branches(args, bindings, steps, sources, scope, objective, replies, guidance, cancel, declared, goal):
    comparisons, corrections = _batch_for_branches(args, steps, sources, scope, objective, replies, guidance, declared, goal)
    if not set(bindings.values()) <= comparisons.keys():
        raise ValueError('分版綁定只能引用批次內已核對的完整欄位名。')
    verified = comparisons[args['anchor_field']]
    adoption = verified['adoption']
    if adoption['status'] == 'adopted':
        raise ValueError('決定候選的文件核對已有唯一正式採用版本。')
    candidates, citations = _version_sources(verified)
    versions = citations if _explicit_adoption(verified) else candidates | citations
    if not versions:
        raise ValueError('決定候選的欄位缺少版本名，無法列出分支。')
    retained = []
    if 'reviewed_notes' in args:
        history, retained, review = _reviewed_quantity_notes(corrections, versions, bindings.values(), comparisons)
        blocker = next((row for row in review if row['status'] == 'needs_recheck'), None)
        if blocker:
            raise ValueError(f"{blocker['citation']} 需要重核。{blocker['reason']}")
        if not retained:
            raise ValueError('沒有需保留的試想時，請使用正式更正的原有格式。')
    else:
        history = [_version_correction(citation, source, versions, bindings.values(), comparisons)
                   for citation, source in corrections.items()]
    effective = {(item['label'], item['field']): item for item in history}
    operands = _branch_operands(args, steps, sources, objective, replies, guidance)
    inputs = [(path, args['step'], comparisons[field]) for path, field in bindings.items()]
    aligned = _aligned_branches(args, versions, inputs, cancel, effective)
    expression_math.check_cancel(cancel)
    result = {'status': 'conditional_calculations', 'step': args['step'], 'anchor_field': args['anchor_field'],
              'adoption': adoption, 'operands': operands, **aligned}
    if {'corrections', 'reviewed_notes'} & args.keys():
        result['correction_history'] = history
    if retained:
        result['retained_notes'] = retained
    return result


def calculate_branches(args, steps, sources, scope, objective='', replies=(), guidance=(), cancel=None, *,
                       declared=None, goal=None):
    bindings = validate_branches(args)
    expression_math.check_cancel(cancel)
    if 'anchor_field' in args:
        return _calculate_batch_branches(args, bindings, steps, sources, scope, objective, replies, guidance, cancel,
                                         declared, goal)
    anchor = args['step']
    verified, ranges = _branch_comparison(anchor, steps, sources, scope, objective, replies, guidance, declared, anchor=True)
    operands = _branch_operands(args, steps, sources, objective, replies, guidance)
    candidates, citations = _version_sources(verified)
    versions = citations if _explicit_adoption(verified) else candidates | citations
    result = {'status': 'conditional_calculations', 'step': anchor, 'adoption': verified['adoption'], 'operands': operands}
    if 'bindings' in args:
        comparisons = {anchor: verified}
        fields = {verified['field']}
        for number in bindings.values():
            if number == anchor:
                continue
            other, other_ranges = _branch_comparison(number, steps, sources, scope, objective, replies, guidance,
                                                     declared, anchor=False)
            if other_ranges != ranges or other['field'] in fields:
                raise ValueError('跨欄分版須核對不同欄位，且每欄使用完全相同的採用範圍。')
            comparisons[number] = other
            fields.add(other['field'])
        inputs = [(path, number, comparisons[number]) for path, number in bindings.items()]
        aligned = _aligned_branches(args, versions, inputs, cancel)
        expression_math.check_cancel(cancel)
        return {**result, **aligned}

    path, = bindings
    branches = []
    for version in versions:
        expression_math.check_cancel(cancel)
        branch = {'version': version, 'adoption_citations': citations.get(version, [])}
        if version not in candidates:
            branches.append({**branch, 'status': 'candidate_unavailable'})
            continue
        index, quantity = candidates[version]
        branch.update(candidate=index, quantity=quantity)
        if quantity['status'] != 'confirmed':
            branches.append({**branch, 'status': quantity['status']})
            continue
        branches.append({**branch, **_calculate_branch(args['expression'], {path: quantity['converted_exact']}, cancel)})
    expression_math.check_cancel(cancel)
    return {**result, 'candidate_path': path, 'field': verified['field'],
            'comparison_unit': verified['unit'], 'branches': branches}


def calculate_expression(args, steps, sources, scope, objective='', replies=(), guidance=(), cancel=None, *,
                         declared=None):
    validate_expression(args)
    expression_math.check_cancel(cancel)
    sequence = len(steps) + 1
    leaves = dict(operand_evidence._leaves(args['expression']))
    bindings = []
    for binding in args['quantity_inputs']:
        number = binding['step']
        saved, verified = _verified_comparison(number, steps, sources, objective, replies, guidance, declared)
        original = saved['action']['args']
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
            adoption = verified.get('adoption')
            if 'unit_evidence' in binding:
                if adoption is not None and adoption.get('candidate') != binding['candidate']:
                    raise ValueError('單位補充無法替代文件版本的正式採用。')
                confirmation = binding['unit_evidence']
                _latest_confirmation(confirmation['citation'], number, sequence, replies, guidance)
                if quantity['status'] != 'unit_unconfirmed':
                    raise ValueError('只可替原核對缺少單位的候選補單位。')
                request = {'unit': original['unit'], 'candidates': [
                    {**original['candidates'][index], 'unit_evidence': confirmation}]}
                if 'field' in original:
                    request['field'] = original['field']
                if adoption is not None:
                    if 'adoption_ranges' in original:
                        request['adoption_ranges'] = original['adoption_ranges']
                    receipt['adoption'] = adoption
                quantity, = observe(request, steps, sources, objective, replies, guidance, declared=declared)['quantities']
            elif 'selection' in binding:
                selection = binding['selection']
                source = _latest_confirmation(selection['citation'], number, sequence, replies, guidance)
                receipt['selection'] = _selection_proof(selection, index, original, source)
            elif adoption and adoption['status'] == 'adopted':
                if adoption['candidate'] != binding['candidate'] or saved['observation'].get('goal_conditions_sha256') != scope:
                    raise ValueError('算式須連結目前條件下文件明列採用的版本。')
                receipt['adoption'] = adoption
            elif adoption and adoption['status'] != 'unrecorded':
                raise ValueError('文件採用缺漏或有衝突，須有正式確認才能代入。')
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


def validate_saved(db, *, replies_present, guidance_present, scope_present=False):
    scopes = {}
    if scope_present:
        for gid, payload, objective, source_json in db.execute(
                'SELECT s.goal_id,s.scope_json,g.objective,g.sources_json FROM autonomous_quantity_scopes s '
                'JOIN autonomous_goals g ON g.id=s.goal_id'):
            scopes[gid] = _verified_scope(payload, objective, json.loads(source_json))
    query = '''SELECT DISTINCT g.id,g.notes,g.sources_json,g.objective FROM autonomous_goals g
               JOIN autonomous_steps s ON s.goal_id=g.id WHERE s.action_json LIKE '%"check_quantities"%'
               OR s.action_json LIKE '%"check_quantity_fields"%' OR s.action_json LIKE '%"quantity_inputs"%'
               OR s.action_json LIKE '%"calculate_quantity_branches"%' '''
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
                expected = observe(action['args'], prior, sources, objective, replies, guidance, declared=scopes.get(gid))
                actual = {key: value for key, value in observation.items() if key not in _METADATA}
                if actual != expected:
                    raise ValueError('保存的數量核對與原話或換算結果不一致。')
            if action['tool'] == 'check_quantity_fields':
                expected = observe_fields(action['args'], prior, sources, objective, replies, guidance,
                                          declared=scopes.get(gid))
                actual = {key: value for key, value in observation.items() if key not in _METADATA}
                if actual != expected:
                    raise ValueError('保存的批次數量核對與原話或採用範圍不一致。')
            if action['tool'] == 'calculate_expression' and 'quantity_inputs' in action['args']:
                scope = observation.get('goal_conditions_sha256')
                if not isinstance(scope, str) or not re.fullmatch(r'[0-9a-f]{64}', scope):
                    raise ValueError('保存的算式缺少有效數量條件。')
                expected = calculate_expression(action['args'], prior, sources, scope, objective, replies, guidance,
                                                declared=scopes.get(gid))
                actual = {key: value for key, value in observation.items() if key not in _METADATA}
                if actual != expected:
                    raise ValueError('保存的算式與數量核對關聯不一致。')
            if action['tool'] == 'calculate_quantity_branches':
                scope = observation.get('goal_conditions_sha256')
                if not isinstance(scope, str) or not re.fullmatch(r'[0-9a-f]{64}', scope):
                    raise ValueError('保存的分版算式缺少有效數量條件。')
                goal = None
                if {'correction', 'corrections', 'reviewed_notes'} & action['args'].keys():
                    history = conversation_history.saved_fingerprint(db, gid)
                    goal = {'id': gid, 'objective': objective, 'notes': notes, 'sources': sources,
                            'conversation_history': {'sha256': history} if history else None}
                expected = calculate_branches(action['args'], prior, sources, scope, objective, replies, guidance,
                                              declared=scopes.get(gid), goal=goal)
                actual = {key: value for key, value in observation.items() if key not in _METADATA}
                if actual != expected:
                    raise ValueError('保存的分版算式與版本及原文數量不一致。')
            prior.append({'sequence': sequence, 'action': action, 'observation': observation})
