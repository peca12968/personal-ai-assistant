"""Tie a CSV weight total to cited factors and an actual successful calculation."""

import re
from fractions import Fraction

from . import exact_math, goal_artifact_fields as fields, goal_calculation, goal_stages


_COUNT = re.compile(rf'(?:(?:件數|袋數|份數|箱數|數量)\s*[:：為]?\s*)?'
                    rf'(?P<number>{fields._NUMBER})\s*(?P<unit>件|份|袋|箱)')


def _recipe(line, entity):
    label, *parts = (part.strip() for part in re.split(r'[｜|\t]', line))
    if label != entity:
        return None
    counts, weights = [], []
    for part in parts:
        if match := _COUNT.fullmatch(part):
            counts.append((Fraction(match['number']), match['unit'], part))
        if (weight := fields._WEIGHT_SOURCE.fullmatch(part)) and fields._scope(weight['scope']).startswith('每'):
            weights.append((Fraction(weight['number']) * fields._MASS_UNITS[weight['unit']],
                            fields._scope(weight['scope']), weight['unit'], part))
    if len(counts) != 1 or len(weights) != 1 or '每' + counts[0][1] != weights[0][1]:
        return None
    return counts[0], weights[0]


def _products(node, path=''):
    if not isinstance(node, dict):
        return
    values = node['values']
    if node['operation'] == 'product' and len(values) == 2 and all(isinstance(value, str) for value in values):
        yield path, values
    for index, child in enumerate(values):
        yield from _products(child, f'{path}/values/{index}')


def _successful_products(step):
    action, observation = step['action'], step['observation']
    args = action['args']
    if action['tool'] == 'calculate' and args.get('operation') == 'product' and len(args['values']) == 2:
        yield '', args['values'], observation.get('exact'), {}
    elif action['tool'] == 'calculate_expression' and observation.get('status') == 'expression_calculated':
        results = {item['path']: item for item in observation['calculations']}
        operands = {item['path']: item for item in observation.get(goal_calculation.PROOF, {}).get('operands', ())}
        if 'operands' in args and not operands:
            return
        for path, values in _products(args['expression']):
            result = results.get(path, {})
            if result.get('operation') == 'product' and result.get('operand_count') == 2:
                yield path, values, result.get('exact'), operands


def _bind_operands(path, values, operands, pairs):
    for factors in (pairs, pairs[::-1]):
        if any(value != factor[0] for value, factor in zip(values, factors, strict=True)):
            continue
        if not operands:
            return True
        bound = [operands.get(f'{path}/values/{index}') for index in range(2)]
        if not all(operand and
                   (operand['citation'], operand['source_sha256']) == (factor[1]['citation'], factor[1]['source_sha256'])
                   and operand['quote'] in factor[2]
                   for operand, factor in zip(bound, factors, strict=True)):
            continue
        left, right = bound
        if (left['citation'] != right['citation'] or left['quote_end'] <= right['quote_start']
                or right['quote_end'] <= left['quote_start']):
            return True
    return False


def _calculation(steps, count, weight, count_source, weight_source, scales, *, after):
    for step in steps:
        if step['sequence'] <= after:
            continue
        for path, original, exact, operands in _successful_products(step):
            if exact is None:
                continue
            values = [exact_math.parse(value) for value in original]
            result = exact_math.parse(exact)
            for unit in scales:
                scale = fields._MASS_UNITS[unit]
                factors = ((count[0], count_source, count[2]), (weight[0] / scale, weight_source, weight[3]))
                if result * scale == count[0] * weight[0] and _bind_operands(path, values, operands, factors):
                    return {'step': step['sequence'], 'step_sha256': goal_stages.digest(step),
                            'path': path, 'exact': exact, 'unit': unit}
    return None


def _source_factor(row, step, prior, key, original, source):
    earlier = prior.get(key)
    if earlier:
        value = Fraction(earlier['value'])
        if value != original[0]:
            proof = fields._previous_value(row, value, earlier, step)
            if not proof:
                return None
            labels = original[1:-1]
            if key[1][0] == 'mass':
                literal = fields._VALUE.fullmatch(earlier['new_text'])
                labels = (original[1], (literal['unit'] if literal else None) or original[2])
            return (value, *labels, earlier['quote']), proof, [earlier['citation']]
    return original, source, []


def _row_revision(goal, step, row, column, cell, entity, old_total, current, prior):
    original_steps = goal['steps'][:step['sequence'] - 1]
    candidates = [(recipe, source) for source in fields._cited_lines(row, step, original_steps, goal['sources'])
                  if (recipe := _recipe(source['line'], entity))]
    if len(candidates) != 1:
        return None
    (count, weight), source = candidates[0]
    count_key, weight_key = (entity, ('count', '')), (entity, ('mass', weight[1]))
    # The count correction grammar refers to pieces; bags, boxes and portions
    # still use their own original count and can each have a weight correction.
    if count[1] != '件':
        count_key = None
    count_basis = _source_factor(row, step, prior, count_key, count, source)
    weight_basis = _source_factor(row, step, prior, weight_key, weight, source)
    if not count_basis or not weight_basis:
        return None
    count, count_source, count_notes = count_basis
    weight, weight_source, weight_notes = weight_basis
    if old_total != count[0] * weight[0]:
        return None
    changes = [(key, correction) for key, value in ((count_key, count[0]), (weight_key, weight[0]))
               if (correction := current.get(key)) and Fraction(correction['value']) != value]
    if not changes:
        return None
    literal = fields._VALUE.fullmatch(cell.strip())
    scales = [unit for unit in (literal['unit'] if literal else None, fields._field(column)[1], weight[2])
              if unit in fields._MASS_UNITS]
    after = max(source['read_step'], *(prior[key]['steps_seen'] for key in (count_key, weight_key)
                                     if prior.get(key) and prior[key]['citation'] in count_notes + weight_notes), 0)
    calculation = _calculation(original_steps, count, weight, count_source, weight_source,
                               dict.fromkeys(scales), after=after)
    if calculation is None:
        return None
    updated = {key: Fraction(correction['value']) for key, correction in changes}
    return {
        'entity': entity, 'field': '總重量', 'csv_column': column, 'previous_value': str(old_total),
        'value': str(updated.get(count_key, count[0]) * updated.get(weight_key, weight[0])),
        'value_unit': '公克', 'previous_sources': [source, *(proof for proof in (count_source, weight_source) if proof != source)],
        'calculation': calculation, 'input_revisions': [correction for _, correction in changes],
        'required_corrections': sorted({*(correction['citation'] for _, correction in changes), *count_notes, *weight_notes}
                                       - {prior[key]['citation'] for key in updated if key in prior}),
    }


def provenance(goal, writes):
    notes = goal.get('guidance_sources', ())
    latest = fields._latest(notes)
    if not latest:
        return {}
    total_rows = {(entity, ('mass', '總')): True for entity, scope in latest
                  if scope == ('count', '') or scope[0] == 'mass' and scope[1].startswith('每')}
    result = {}
    for key, step in writes.items():
        action = step['action']['args']
        if not total_rows or not action['filename'].lower().endswith('.csv'):
            continue
        sequence = step['sequence']
        current = {key: correction for key, correction in latest.items() if correction['steps_seen'] >= sequence}
        if not current:
            continue
        prior = fields._latest(note for note in notes if note['steps_seen'] < sequence)
        for key_scope, column, row, value, _, cell in fields._csv_cells(action['content'], total_rows):
            if key_scope in latest or value is None:
                continue
            if proof := _row_revision(goal, step, row, column, cell, key_scope[0], value, current, prior):
                result.setdefault(key, []).append(proof)
    return result
