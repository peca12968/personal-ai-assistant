"""Attribute a corrected value to the exact cited cell in a saved CSV."""

import re
from fractions import Fraction

from . import document_units, goal_guidance_sources, source_decision
from .tasks import parse_csv


_IDENTITY = {'設備', '品項', '項目', '場次', '方案', '物品', '名稱', '區域'}
_MASS_UNITS = {unit: scale for unit, (dimension, scale) in document_units.SCALES.items() if dimension == 'mass'}
_MASS = '|'.join(re.escape(unit) for unit in sorted(_MASS_UNITS, key=lambda item: (-len(item), item)))
_WEIGHT = re.compile(rf'(?P<scope>每件|單件|每份|每袋|每箱|總|合計)?(?P<unit>重量|{_MASS})')
_WEIGHT_SOURCE = re.compile(
    rf'(?P<scope>每件|單件|每份|每袋|每箱|總|合計)?(?:重量)?\s*[:：為]?\s*'
    rf'(?P<number>[0-9]{{1,20}}(?:\.[0-9]{{1,12}})?)\s*(?P<unit>{_MASS})'
)
_NUMBER = r'(?:[0-9]{1,20}(?:\.[0-9]{1,12})?)'
_VALUE = re.compile(rf'(?P<number>{_NUMBER})\s*(?P<unit>{_MASS}|件|元)?')
_OTHER_SOURCE = re.compile(rf'(?P<label>件數|單價)?\s*[:：為]?\s*(?P<number>{_NUMBER})\s*(?P<unit>件|元)')
_ASSIGNMENT = re.compile(
    r'^(?:(?:請\s*)?正式更正\s*)?「(?P<entity>[^「」\r\n]{1,100})」\s*的\s*'
    r'「(?P<field>[^「」\r\n]{1,60})」\s*(?:(?P<verb>正式改(?:為|成)|正式更正為)|(?:為|是|[:：]))\s*'
    r'(?P<value>[^，,]+?)(?:[，,]\s*(?:件數|其餘資料|其他欄值)(?:不變|維持原值))?$'
)
_CITATION = re.compile(r'\[D([1-9][0-9]{0,10}):L([1-9][0-9]{0,10})\]')
_BASE = {('mass', '每件'): '每件重量', ('mass', '每份'): '每份重量',
         ('mass', '每袋'): '每袋重量', ('mass', '每箱'): '每箱重量',
         ('mass', '總'): '總重量', ('mass', ''): '重量',
         ('count', ''): '件數', ('price', ''): '單價'}


def _scope(scope):
    return {'單件': '每件', '合計': '總'}.get(scope or '', scope or '')


def _field(label):
    if match := _WEIGHT.fullmatch(label):
        return ('mass', _scope(match['scope'])), match['unit'] if match['unit'] != '重量' else None
    return {'件數': (('count', ''), '件'), '單價': (('price', ''), '元')}.get(label, (None, None))


def _value(text, kind, default_unit):
    if not (literal := _VALUE.fullmatch(text.strip())):
        return None
    unit = literal['unit'] or default_unit
    if kind == 'mass':
        return Fraction(literal['number']) * _MASS_UNITS[unit] if unit in _MASS_UNITS else None
    if (kind, unit) in (('count', '件'), ('price', '元')):
        return Fraction(literal['number'])
    return None


def _assignments(note):
    clauses = goal_guidance_sources.standalone_clauses(note['content'])
    for _, clause, inquiry in clauses:
        match = _ASSIGNMENT.fullmatch(clause) if not inquiry else None
        if not match or not (re.match(r'^(?:請\s*)?正式更正', clause) or match['verb']):
            continue
        named, default_unit = _field(match['field'].strip())
        if named is None or (value := _value(match['value'], named[0], default_unit)) is None:
            continue
        yield (match['entity'], named), {
            'entity': match['entity'], 'field': _BASE[named], 'value': str(value),
            'new_text': match['value'].strip(), 'quote': clause, 'citation': note['citation'],
            'source_sha256': note['sha256'], 'steps_seen': note['steps_seen'],
        }


def _latest(notes):
    results = {}
    for note in notes:
        current = {}
        for key, correction in _assignments(note):
            current.setdefault(key, []).append(correction)
        for key, choices in current.items():
            # Conflicting values in the same user correction need interpretation.
            if len({item['value'] for item in choices}) == 1:
                results[key] = choices[-1]
    return results


def _source_value(line, entity, field):
    title, *parts = [part.strip() for part in re.split(r'[｜|\t]', line)]
    if title != entity:
        return None
    values = []
    for part in parts:
        if field[0] == 'mass' and (weight := _WEIGHT_SOURCE.fullmatch(part)):
            if _scope(weight['scope']) == field[1]:
                values.append(Fraction(weight['number']) * _MASS_UNITS[weight['unit']])
        elif field[0] != 'mass' and (match := _OTHER_SOURCE.fullmatch(part)):
            if field[0] == 'count' and match['unit'] == '件' and match['label'] in (None, '件數'):
                values.append(Fraction(match['number']))
            elif field[0] == 'price' and match['unit'] == '元' and match['label'] == '單價':
                values.append(Fraction(match['number']))
    return values[0] if len(values) == 1 else None


def _csv_cells(content, edits):
    headers, rows = parse_csv(content.removeprefix('\ufeff'))
    ids = [index for index, column in enumerate(headers) if column in _IDENTITY]
    if not ids:
        return
    fields = [_field(header) for header in headers]
    for row in rows:
        for key, correction in edits.items():
            entity, scope = key
            columns = [index for index, (field, _) in enumerate(fields) if field == scope]
            if len(columns) == 1 and correction and sum(row[index].strip() == entity for index in ids) == 1:
                index = columns[0]
                yield key, headers[index], row, _value(row[index], scope[0], fields[index][1]), correction


def _document_proof(row, write_step, steps, sources, key, value):
    matches = {tuple(map(int, found.groups())) for cell in row for found in _CITATION.finditer(cell)}
    validated = {row['citation'] for row in write_step['observation'].get('citation_checks', ())
                 if row.get('range_valid') is True}
    wanted = [{'document_id': did, 'start_line': line, 'end_line': line}
              for did, line in sorted(matches) if f'[D{did}:L{line}]' in validated]
    if not wanted:
        return []
    _, found = source_decision.gather_lines(wanted, steps, sources)
    applicable = []
    for (doc, line), (step, digest, text) in found.items():
        if (original := _source_value(text, *key)) is not None:
            applicable.append((original, {'citation': f'[D{doc}:L{line}]', 'read_step': step,
                                          'source_sha256': digest, 'line': text}))
    if not applicable or len({original for original, _ in applicable}) != 1:
        return []
    return [proof for original, proof in applicable if original == value]


def _previous_value(row, value, correction, step):
    if not correction or correction['value'] != str(value):
        return None
    if not any(correction['citation'] in cell for cell in row) or not any(
            source['citation'] == correction['citation'] and source['sha256'] == correction['source_sha256']
            for source in step['observation'].get('guidance_citations', ())):
        return None
    return {key: correction[key] for key in ('citation', 'source_sha256', 'quote')}


def provenance(goal, writes):
    notes = goal.get('guidance_sources', ())
    latest = _latest(notes)
    if not latest:
        return {}
    result = {}
    for key, step in writes.items():
        action = step['action']['args']
        if not action['filename'].lower().endswith('.csv'):
            continue
        sequence = step['sequence']
        current = {key: correction for key, correction in latest.items()
                   if correction and correction['steps_seen'] >= sequence}
        if not current:
            continue
        prior = _latest(note for note in notes if note['steps_seen'] < sequence)
        for scope, column, row, value, correction in _csv_cells(action['content'], current):
            if value is None or str(value) == correction['value']:
                continue
            proof = _document_proof(row, step, goal['steps'][:sequence - 1], goal['sources'], scope, value)
            if not proof and (earlier := _previous_value(row, value, prior.get(scope), step)):
                proof = [earlier]
            if proof:
                result.setdefault(key, []).append({**correction, 'csv_column': column,
                                                    'previous_value': str(value), 'previous_sources': proof})
    return result


def has_wrong_current_value(content, records):
    corrections = {(row['entity'], _field(row['field'])[0]): row for row in records}
    return any(str(value) != correction['value']
               for _, _, _, value, correction in _csv_cells(content, corrections))


def verify_documents(revisions, documents):
    for revision in revisions:
        for source in revision['previous_sources']:
            match = _CITATION.fullmatch(source['citation'])
            if not match:
                continue
            document_id, line = map(int, match.groups())
            document = documents.get(document_id)
            if document and source['source_sha256'] == document['sha256']:
                lines = document['content'].splitlines()
                if not 1 <= line <= len(lines) or lines[line - 1] != source['line']:
                    raise ValueError('更正檔案的核對原行與已保存的文件不符。')
