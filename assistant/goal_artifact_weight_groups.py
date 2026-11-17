"""Verify weight sums from the members cited by the saved CSV row."""

import re
from collections import Counter
from fractions import Fraction

from . import exact_math, goal_artifact_fields as fields, goal_artifact_totals as totals, goal_stages
from .tasks import parse_csv


_DOCUMENT_REF = re.compile(r'\[D[0-9]+:[^\]\r\n]{1,50}\]')
_TOTAL = ('mass', '總')


def _rows(content):
    headers, rows = parse_csv(content.removeprefix('\ufeff'))
    identity = [index for index, label in enumerate(headers) if label in fields._IDENTITY - {'區域'}]
    if not identity and '區域' in headers:
        identity = [headers.index('區域')]
    weights = [index for index, label in enumerate(headers) if fields._field(label)[0] == _TOTAL]
    if len(identity) != 1 or len(weights) != 1:
        return
    weight = weights[0]
    column = headers[weight]
    for row in rows:
        literal = fields._VALUE.fullmatch(row[weight].strip())
        unit = (literal['unit'] if literal else None) or fields._field(column)[1]
        yield row[identity[0]].strip(), column, row, fields._value(row[weight], 'mass', unit), unit


def _source_tokens(row):
    return {token for cell in row for token in _DOCUMENT_REF.findall(cell)}


def _member(source, row, write, prior, current):
    entity = re.split(r'[｜|\t]', source['line'], maxsplit=1)[0].strip()
    recipe = totals._recipe(source['line'], entity)
    if not entity or not recipe or (entity, _TOTAL) in (prior.keys() | current.keys()):
        return None
    count, weight = recipe
    count_key = (entity, ('count', '')) if count[1] == '件' else None
    weight_key = (entity, ('mass', weight[1]))
    count_basis = totals._source_factor(row, write, prior, count_key, count, source)
    weight_basis = totals._source_factor(row, write, prior, weight_key, weight, source)
    if not count_basis or not weight_basis:
        return None
    count, count_source, count_notes = count_basis
    weight, weight_source, weight_notes = weight_basis
    changes = {key: correction for key, original in ((count_key, count[0]), (weight_key, weight[0]))
               if (correction := current.get(key)) and Fraction(correction['value']) != original}
    confirmed = set(count_notes + weight_notes)
    after = max([source['read_step'], *(note['steps_seen'] for note in prior.values()
                                       if note['citation'] in confirmed)])
    references = set(count_notes if count_key not in changes else ()) | set(weight_notes if weight_key not in changes else ())
    references.update(correction['citation'] for correction in changes.values())
    adjusted_count = Fraction(changes[count_key]['value']) if count_key in changes else count[0]
    adjusted_weight = Fraction(changes[weight_key]['value']) if weight_key in changes else weight[0]
    return {'entity': entity, 'source': source, 'count': count, 'weight': weight, 'count_source': count_source,
            'weight_source': weight_source, 'after': after, 'input_revisions': list(changes.values()),
            'before': count[0] * weight[0], 'after_value': adjusted_count * adjusted_weight, 'references': references}


def _sum_nodes(node, path=''):
    if not isinstance(node, dict):
        return
    if node['operation'] == 'sum' and len(node['values']) >= 2:
        yield path, node['values']
    for index, child in enumerate(node['values']):
        yield from _sum_nodes(child, f'{path}/values/{index}')


def _sum_terms(values, path):
    products, literals = [], []
    for index, child in enumerate(values):
        child_path = f'{path}/values/{index}'
        if isinstance(child, str):
            literals.append(exact_math.parse(child))
        elif child['operation'] == 'product':
            products.append(child_path)
        elif child['operation'] == 'sum':
            terms = _sum_terms(child['values'], child_path)
            if terms is None:
                return None
            products.extend(terms[0])
            literals.extend(terms[1])
        else:
            return None
    return products, literals


def _calculations(steps):
    products, sums = [], []
    for step in steps:
        action, observation = step['action'], step['observation']
        args = action['args']
        if action['tool'] == 'calculate' and args['operation'] == 'sum' and len(args['values']) >= 2:
            if 'exact' in observation:
                sums.append((step, '', args['values'], observation['exact']))
        if action['tool'] == 'calculate_expression' and observation.get('status') == 'expression_calculated':
            recorded = {item['path']: item for item in observation['calculations']}
            for path, values in _sum_nodes(args['expression']):
                result = recorded.get(path, {})
                if result.get('operation') == 'sum' and result.get('operand_count') == len(values):
                    sums.append((step, path, values, result['exact']))
        for path, values, exact, operands in totals._successful_products(step):
            if exact is not None:
                products.append((step, path, [exact_math.parse(value) for value in values],
                                 exact_math.parse(exact), operands))
    return products, sums


def _matches(member, product, scale):
    step, path, values, result, operands = product
    count, weight = member['count'], member['weight']
    return (step['sequence'] > member['after'] and result * scale == member['before']
            and totals._bind_operands(path, values, operands,
                ((count[0], member['count_source'], count[2]),
                 (weight[0] / scale, member['weight_source'], weight[3]))))


def _match_products(members, products, scale, required):
    choices = [[index for index, product in enumerate(products) if _matches(member, product, scale)]
               for member in members]
    mandatory = {}

    def place(product, visited):
        for index, permitted in enumerate(choices):
            if product not in permitted or index in visited:
                continue
            visited.add(index)
            if index not in mandatory or place(mandatory[index], visited):
                mandatory[index] = product
                return True
        return False

    if not all(place(product, set()) for product in required):
        return None
    owners = {product: index for index, product in mandatory.items()}

    def assign(index, seen):
        for product in choices[index]:
            if product in seen:
                continue
            seen.add(product)
            if product not in owners or assign(owners[product], seen):
                owners[product] = index
                return True
        return False

    if not all(index in mandatory or assign(index, set()) for index in range(len(members))):
        return None
    assigned = {member: products[product] for product, member in owners.items()}
    return [assigned[index] for index in range(len(members))]


def _receipt(step, path, exact):
    return {'step': step['sequence'], 'step_sha256': goal_stages.digest(step), 'path': path, 'exact': str(exact)}


def _arithmetic(steps, members, mass_unit, old):
    products, sums = _calculations(steps)
    units = dict.fromkeys([mass_unit, *(member['weight'][2] for member in members)])
    after = max(member['after'] for member in members)
    for step, path, values, exact in sums:
        number = step['sequence']
        if number <= after:
            continue
        terms = _sum_terms(values, path)
        if terms is None:
            continue
        nested_paths, literals = terms
        if len(nested_paths) + len(literals) != len(members) or (literals and 'operands' in step['action']['args']):
            continue
        nested = [product for product in products if product[0]['sequence'] == number and product[1] in nested_paths]
        if len(nested) != len(nested_paths):
            continue
        candidates = nested + ([product for product in products if product[0]['sequence'] < number] if literals else [])
        for unit in units:
            scale = fields._MASS_UNITS[unit]
            if exact_math.parse(exact) * scale != old or Counter([*literals, *(product[3] for product in nested)]) != Counter(
                    member['before'] / scale for member in members):
                continue
            if assigned := _match_products(members, candidates, scale, range(len(nested))):
                return {'unit': unit, 'sum': _receipt(step, path, exact),
                        'products': [_receipt(source, branch, result) for source, branch, _, result, _ in assigned]}
    return None


def _group_revision(goal, write, entity, column, row, old, unit, current, prior):
    requested = _source_tokens(row)
    if not entity or old is None or len(requested) < 2 or unit not in fields._MASS_UNITS:
        return None
    steps = goal['steps'][:write['sequence'] - 1]
    sources = fields._cited_lines(row, write, steps, goal['sources'])
    if requested != {source['citation'] for source in sources}:
        return None
    members = [_member(source, row, write, prior, current) for source in sources]
    if not all(members) or len({member['entity'] for member in members}) != len(members):
        return None
    changes = [item for member in members for item in member['input_revisions']]
    if not changes or sum(member['before'] for member in members) != old:
        return None
    if not (calculation := _arithmetic(steps, members, unit, old)):
        return None
    previous_sources = []
    for member in members:
        for key in ('source', 'count_source', 'weight_source'):
            if member[key] not in previous_sources:
                previous_sources.append(member[key])
    required = set().union(*(member['references'] for member in members))
    return {
        'entity': entity, 'field': '總重量', 'csv_column': column, 'previous_value': str(old),
        'value': str(sum(member['after_value'] for member in members)), 'value_unit': '公克',
        'previous_sources': previous_sources, 'member_citations': sorted(requested),
        'required_corrections': sorted(required), 'input_revisions': changes, 'aggregation': calculation,
        'members': [{'entity': member['entity'], 'citation': member['source']['citation'],
                     'previous_value': str(member['before']), 'value': str(member['after_value'])}
                    for member in members],
    }


def provenance(goal, writes):
    notes = goal.get('guidance_sources', ())
    latest = fields._latest(notes)
    if not any(scope == ('count', '') or scope[0] == 'mass' and scope[1].startswith('每')
               for _, scope in latest):
        return {}
    result = {}
    for key, write in writes.items():
        args = write['action']['args']
        if not args['filename'].lower().endswith('.csv'):
            continue
        sequence = write['sequence']
        current = {key: note for key, note in latest.items() if note['steps_seen'] >= sequence}
        if not current:
            continue
        prior = fields._latest(note for note in notes if note['steps_seen'] < sequence)
        for entity, column, row, value, unit in _rows(args['content']):
            if (entity, _TOTAL) not in latest and (
                    proof := _group_revision(goal, write, entity, column, row, value, unit, current, prior)):
                result.setdefault(key, []).append(proof)
    return result


def has_wrong_current_value(content, revisions):
    for entity, _, row, value, _ in _rows(content):
        offered = _source_tokens(row)
        relevant = [record for record in revisions if record['entity'] == entity
                    and (not offered or offered <= set(record['member_citations'])
                         or set(record['member_citations']) <= offered)]
        if relevant and not any(
                offered == set(record['member_citations']) and str(value) == record['value']
                and all(any(token in cell for cell in row) for token in record['required_corrections'])
                for record in relevant):
            return True
    return False
