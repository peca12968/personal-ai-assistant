"""Compare exact quantities in already read lines and saved user sources."""
import json
import re
from fractions import Fraction

from . import document_units, exact_math, goal_guidance_sources, goal_reply_sources, operand_evidence, source_decision
from .document_table import _SAME_UNIT_ONLY


GUIDANCE = '''原量缺單位、相同欄位有兩筆數值，或比例基數有不同候選時，已有相關原話就先用check_quantities核對，再決定是否詢問。args例如{"unit":"公斤","candidates":[{"label":"本批配料","citation":"[D1:L1]","quote":"配料 450"}]}。每次一至四筆，每筆逐字引用一個數值和已有的原單位，僅引用本次適用的對象。unit是比較單位，可用支援的重量、長度、容量、時間、件數單位或ratio。引用限已完整查讀的單行、同一目標已保存的補充或更正，及[目標]。
needs_evidence時保留原數及其他已核對值。若短句含多個數字而無法定位，先縮到該欄完整數值。單位待補只問該欄單位。收到使用者只確認單位時，仍引用原數值，該候選另加unit_evidence，例如{"citation":"[補充#3]","quote":"本批配料的單位是公克","unit":"公克"}，核對後用確定的換算值繼續。另案單位不能補本批。equivalent代表同單位的數值相同，對同一欄位採一次；different代表換算後仍不同，保留各候選，若影響交付只問採哪筆或哪個基數。只要條件式成果時列分支，不另追問。工具核對文字及精確數值，來源是否同一欄位、哪份生效及哪個基數適用仍依已保存的使用者原話。沒有缺漏或比較需求時可直接用既有算式。'''

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


def validate(args):
    if (not isinstance(args, dict) or args.keys() != {'unit', 'candidates'}
            or not isinstance(args['unit'], str) or args['unit'] not in _TARGETS
            or not isinstance(args['candidates'], list) or not 1 <= len(args['candidates']) <= 4):
        raise ValueError('數量核對須有可用比較單位與一至四筆來源。')
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
            if (not isinstance(evidence, dict) or evidence.keys() != {'citation', 'quote', 'unit'}
                    or not _short_text(evidence['quote'], 180)
                    or not isinstance(evidence['citation'], str) or not _USER.fullmatch(evidence['citation'])
                    or evidence['citation'] == row['citation']
                    or not isinstance(evidence['unit'], str) or evidence['unit'] not in _TARGETS - {'ratio'}):
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


def _unit_confirmation(supplied, source):
    quote, unit = supplied['quote'], supplied['unit']
    text, sha, _ = source
    proof = _literal_quote(quote, text, sha)
    start, end = proof['quote_start'], proof['quote_end']
    sentence = quote.rstrip(_SENTENCE_BREAKS)
    if ((start and text[:start].rstrip() and text[:start].rstrip()[-1] not in _SENTENCE_BREAKS)
            or (end < len(text) and quote[-1] not in _SENTENCE_BREAKS
                and text[end:].lstrip() and text[end:].lstrip()[0] not in _SENTENCE_BREAKS)
            or any(char in sentence for char in _SENTENCE_BREAKS + '，,；;')
            or sentence.startswith('若')
            or any(word in sentence for word in ('如果', '假設', '可能', '待確認', '尚未', '不是', '不確定'))
            or not re.fullmatch(rf'(?:.*單位(?:確定|確認)?(?:為|是|用|採) *)?{re.escape(unit)}', sentence)):
        raise ValueError('單位補正須引用完整且明確確認單位的原句。')
    return {**supplied, **proof}


def _candidate(row, source_texts, target):
    citation, quote = row['citation'], row['quote']
    if citation not in source_texts:
        raise ValueError('數量核對須引用已完整查讀或當時已保存的本目標來源。')
    text, sha, position = source_texts[citation]
    result = {key: row[key] for key in ('label', 'citation', 'quote')}
    result.update(_literal_quote(quote, text, sha))
    tokens = _tokens(quote)
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
    quantities = [_candidate(row, source_texts, args['unit']) for row in args['candidates']]
    values = list(dict.fromkeys(row['converted_exact'] for row in quantities if row['status'] == 'confirmed'))
    if any(row['status'] != 'confirmed' for row in quantities):
        status = 'needs_evidence'
    elif len(values) > 1:
        status = 'different'
    else:
        status = 'equivalent' if len(quantities) > 1 else 'confirmed'
    return {'status': status, 'unit': args['unit'], 'quantities': quantities, 'possible_exact': values}


def validate_saved(db, *, replies_present, guidance_present):
    query = '''SELECT DISTINCT g.id,g.notes,g.sources_json,g.objective FROM autonomous_goals g
               JOIN autonomous_steps s ON s.goal_id=g.id WHERE s.action_json LIKE '%"check_quantities"%' '''
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
            prior.append({'sequence': sequence, 'action': action, 'observation': observation})
